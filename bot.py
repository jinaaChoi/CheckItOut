import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta
import pytz
import os
from dotenv import load_dotenv
import config
import settings as cfg

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# =====================================================
# 봇 초기화
# =====================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
TZ = pytz.timezone(config.TIMEZONE)


# =====================================================
# 유틸 함수
# =====================================================

def get_challenge_date(now: datetime = None):
    if now is None:
        now = datetime.now(TZ)
    if now.hour < cfg.get("day_start_hour"):
        return (now - timedelta(days=1)).date()
    return now.date()


def is_challenge_day(date=None) -> bool:
    if date is None:
        date = get_challenge_date()
    return date.weekday() in cfg.get("challenge_days")


def get_day_range(date):
    start_hour = cfg.get("day_start_hour")
    start = TZ.localize(datetime(date.year, date.month, date.day, start_hour, 0, 0))
    end = start + timedelta(days=1)
    return start, end


def get_participant_channels(guild: discord.Guild) -> list:
    prefix = cfg.get("channel_prefix")
    return [ch for ch in guild.text_channels if ch.name.startswith(prefix)]


def get_member_name_from_channel(channel: discord.TextChannel) -> str:
    prefix = cfg.get("channel_prefix")
    return channel.name[len(prefix):]


async def get_attendance_channel(guild: discord.Guild):
    return discord.utils.get(guild.text_channels, name=cfg.get("attendance_channel"))


async def get_weekly_channel(guild: discord.Guild):
    """주간 정산 채널 반환. 미설정 시 출석 채널 사용."""
    weekly = cfg.get("weekly_channel")
    if weekly:
        return discord.utils.get(guild.text_channels, name=weekly)
    return await get_attendance_channel(guild)


# =====================================================
# 휴식 신청 / 선업로드 파싱
# =====================================================

import re
from datetime import date as date_type

def parse_rest_dates(text: str) -> list:
    """
    #휴식 채널 메시지에서 날짜 파싱.
    형식: [2025-06-25] 또는 [2025-06-25 ~ 2025-06-28]
    반환: [date, date, ...] (해당 날짜들)
    """
    dates = []
    # 기간 형식: [YYYY-MM-DD ~ YYYY-MM-DD]
    range_match = re.search(r'\[(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})\]', text)
    if range_match:
        try:
            start = datetime.strptime(range_match.group(1), "%Y-%m-%d").date()
            end   = datetime.strptime(range_match.group(2), "%Y-%m-%d").date()
            d = start
            while d <= end:
                dates.append(d)
                d += timedelta(days=1)
        except ValueError:
            pass
        return dates

    # 단일 날짜 형식: [YYYY-MM-DD]
    single_match = re.search(r'\[(\d{4}-\d{2}-\d{2})\]', text)
    if single_match:
        try:
            dates.append(datetime.strptime(single_match.group(1), "%Y-%m-%d").date())
        except ValueError:
            pass
    return dates


async def get_rest_exempt_dates(guild: discord.Guild, channel_name: str) -> dict:
    """
    #휴식 채널 메시지를 읽어서 { 참여자이름: [면제날짜, ...] } 반환.
    메시지 작성자를 channel_members로 역매핑해서 이름 찾음.
    """
    rest_ch = discord.utils.get(guild.text_channels, name=channel_name)
    if rest_ch is None:
        return {}

    channel_members: dict = cfg.get("channel_members")
    # user_id → 참여자이름 역매핑
    id_to_name = {}
    prefix = cfg.get("channel_prefix")
    for ch_name, uid in channel_members.items():
        member_name = ch_name[len(prefix):]
        id_to_name[uid] = member_name

    exempt = {}
    try:
        cutoff = datetime.now(TZ) - timedelta(days=14)
        async for msg in rest_ch.history(after=cutoff, limit=500):
            dates = parse_rest_dates(msg.content)
            if not dates:
                continue
            name = id_to_name.get(msg.author.id)
            if name is None:
                name = msg.author.display_name
            if name not in exempt:
                exempt[name] = []
            exempt[name].extend(dates)
            # ☑️ 리액션 추가
            already = any(str(r.emoji) == "☑️" and r.me for r in msg.reactions)
            if not already:
                try:
                    await msg.add_reaction("☑️")
                except (discord.Forbidden, discord.HTTPException):
                    pass
    except discord.Forbidden:
        pass
    return exempt


async def scan_channel(channel: discord.TextChannel, week_dates: list) -> dict:
    """
    채널 히스토리를 단 한 번 읽어서 아래를 모두 처리.
    - 날짜별 일반 이미지 수 (daily_counts)
    - 선업로드 면제 날짜 (preupload_exempt)
    - 정상/선업로드 이모지 리액션

    반환:
    {
        "daily_counts": { date: int, ... },
        "preupload_exempt": [ date, ... ],
        "status_reactions": { date: "정상" | "선업로드", ... }  # 리액션용 캐시
    }
    """
    if not week_dates:
        return {"daily_counts": {}, "preupload_exempt": [], "messages_by_date": {}}

    challenge_days = cfg.get("challenge_days")
    cutoff = datetime.now(TZ) - timedelta(days=14)

    # 주간 날짜 범위 (가장 이른 날 start ~ 가장 늦은 날 end)
    _, week_end = get_day_range(week_dates[-1])

    daily_counts: dict = {d: 0 for d in week_dates}
    preupload_exempt: list = []
    # 리액션 추가를 위해 메시지 캐싱: { date: [(msg, is_preupload), ...] }
    messages_by_date: dict = {d: [] for d in week_dates}

    try:
        async for msg in channel.history(after=cutoff, before=week_end, limit=1000):
            has_image = any(
                a.content_type and a.content_type.startswith("image/")
                for a in msg.attachments
            )
            if not has_image:
                continue

            msg_time = msg.created_at.astimezone(TZ)
            msg_date = get_challenge_date(msg_time)
            is_preupload = "미리" in (msg.content or "")

            if is_preupload:
                # 선업로드 면제 날짜 파싱
                parsed = parse_rest_dates(msg.content)
                if parsed:
                    preupload_exempt.extend(parsed)
                else:
                    future = msg_date + timedelta(days=1)
                    for _ in range(7):
                        if future.weekday() in challenge_days:
                            preupload_exempt.append(future)
                            break
                        future += timedelta(days=1)
                # ✨ 리액션
                already = any(str(r.emoji) == "✨" and r.me for r in msg.reactions)
                if not already:
                    try:
                        await msg.add_reaction("✨")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            else:
                # 일반 이미지 → 날짜별 카운트 + 캐싱
                if msg_date in daily_counts:
                    daily_counts[msg_date] += 1
                    messages_by_date[msg_date].append(msg)

    except discord.Forbidden:
        pass

    return {
        "daily_counts": daily_counts,
        "preupload_exempt": preupload_exempt,
        "messages_by_date": messages_by_date,
    }


async def add_reactions_from_scan(messages_by_date: dict, status: dict):
    """
    scan_channel 결과를 바탕으로 정상/지각 리액션 추가.
    messages_by_date: { date: [msg, ...] }
    status: { date: "정상" | "지각" | ... }
    """
    emoji_map = {"정상": "✅", "지각": "⏰"}
    for date, msgs in messages_by_date.items():
        emoji = emoji_map.get(status.get(date))
        if not emoji:
            continue
        for msg in msgs:
            already = any(str(r.emoji) == emoji and r.me for r in msg.reactions)
            if not already:
                try:
                    await msg.add_reaction(emoji)
                except (discord.Forbidden, discord.HTTPException):
                    pass


# =====================================================
# 출석 확인
# =====================================================

async def check_attendance(guild: discord.Guild, date=None) -> dict:
    """
    각 참여자 채널 조회 → { "이름": "정상" | "선업로드" | "휴식" | "미참여" }
    """
    if date is None:
        date = get_challenge_date()
    start, end = get_day_range(date)
    channels = get_participant_channels(guild)
    challenge_days = cfg.get("challenge_days")

    # 휴식 면제 날짜 수집
    rest_exempt = await get_rest_exempt_dates(guild, cfg.get("rest_channel"))

    result = {}
    for ch in channels:
        name = get_member_name_from_channel(ch)

        # 휴식 면제 체크
        if date in rest_exempt.get(name, []):
            result[name] = "휴식"
            continue

        uploaded = False
        is_preupload = False
        # 선업로드 면제 체크 (14일치)
        cutoff = datetime.now(TZ) - timedelta(days=14)
        try:
            async for msg in ch.history(after=cutoff, limit=500):
                has_image = any(
                    a.content_type and a.content_type.startswith("image/")
                    for a in msg.attachments
                )
                if not has_image:
                    continue
                if "미리" in (msg.content or ""):
                    parsed = parse_rest_dates(msg.content)
                    exempt_dates = parsed if parsed else []
                    if not parsed:
                        # 날짜 없이 미리 → 다음 챌린지일 1일
                        msg_time = msg.created_at.astimezone(TZ)
                        msg_date = get_challenge_date(msg_time)
                        future = msg_date + timedelta(days=1)
                        for _ in range(7):
                            if future.weekday() in challenge_days:
                                exempt_dates = [future]
                                break
                            future += timedelta(days=1)
                    if date in exempt_dates:
                        is_preupload = True
                        break

            if is_preupload:
                result[name] = "선업로드"
                continue

            # 당일 이미지 업로드 체크
            async for msg in ch.history(after=start, before=end, limit=200):
                if any(a.content_type and a.content_type.startswith("image/") for a in msg.attachments):
                    uploaded = True
                    break
        except discord.Forbidden:
            name = f"{name}(접근불가)"

        result[name] = "정상" if uploaded else "미참여"
    return result


async def get_absent_members_with_mention(guild: discord.Guild, date=None) -> tuple:
    """미참여자 이름 목록과 멘션 목록 반환. 휴식/선업로드는 제외."""
    if date is None:
        date = get_challenge_date()
    attendance = await check_attendance(guild, date)
    channel_members: dict = cfg.get("channel_members")
    channels = get_participant_channels(guild)
    absent_names = []
    mentions = []
    for ch in channels:
        name = get_member_name_from_channel(ch)
        if attendance.get(name) != "미참여":
            continue
        absent_names.append(name)
        user_id = channel_members.get(ch.name)
        if user_id:
            mentions.append(f"<@{user_id}>")
        else:
            mentions.append(f"**{name}** _(미등록 — `/참여자등록`으로 멘션 연결 가능)_")
    return absent_names, mentions




# =====================================================
# Embed 빌더
# =====================================================

def build_report(date, attendance: dict, is_rest_day: bool) -> discord.Embed:
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    day_str = f"{date.month}/{date.day}({weekday_names[date.weekday()]})"

    if is_rest_day:
        embed = discord.Embed(
            title=f"{config.REST_EMOJI} {day_str} — 오늘은 휴식일이에요!",
            description="오늘은 챌린지 쉬는 날입니다. 푹 쉬세요 😊",
            color=0x95a5a6,
        )
        embed.set_footer(text=f"{cfg.get('challenge_topic')} 챌린지 봇")
        return embed

    present   = [name for name, s in attendance.items() if s == "정상"]
    rest      = [name for name, s in attendance.items() if s == "휴식"]
    preupload = [name for name, s in attendance.items() if s == "선업로드"]
    absent    = [name for name, s in attendance.items() if s == "미참여"]
    total     = len(attendance)
    p_count   = len(present) + len(rest) + len(preupload)
    color     = 0x2ecc71 if not absent else (0xe67e22 if present or rest or preupload else 0xe74c3c)
    topic     = cfg.get("challenge_topic")

    embed = discord.Embed(title=f"🎨 {day_str} {topic} 챌린지 출석 현황", color=color)
    embed.add_field(
        name=f"{config.PRESENT_EMOJI} 참여 완료 ({len(present)}명)",
        value="\n".join(f"• {n}" for n in present) if present else "없음",
        inline=False,
    )
    if rest:
        embed.add_field(
            name=f"💤 휴식 ({len(rest)}명)",
            value="\n".join(f"• {n}" for n in rest),
            inline=False,
        )
    if preupload:
        embed.add_field(
            name=f"✨ 선업로드 ({len(preupload)}명)",
            value="\n".join(f"• {n}" for n in preupload),
            inline=False,
        )
    embed.add_field(
        name=f"{config.ABSENT_EMOJI} 미참여 ({len(absent)}명)",
        value="\n".join(f"• {n}" for n in absent) if absent else "없음",
        inline=False,
    )
    embed.set_footer(text=f"전체 {total}명 중 {p_count}명 참여 • {topic} 챌린지 봇")
    embed.timestamp = datetime.now(TZ)
    return embed


async def calc_weekly_result(guild: discord.Guild, ref_date=None):
    """
    이번 주 챌린지 날짜별 참여자 판정 + 리액션 추가.

    판정 기준:
    - 휴식 면제일                → 💤 휴식
    - 선업로드 면제일             → ✨ 선업로드
    - 당일 1장 이상              → ✅ 정상
    - 당일 0장 + 다음날 2장 이상 → ⏰ 전날 지각 + ✅ 당일 정상
    - 당일 0장 + 다음날 1장      → ❌ 전날 결석 + ✅ 당일 정상
    - 끝까지 0장                 → ❌ 결석
    """
    today = ref_date if ref_date else get_challenge_date()
    challenge_days = cfg.get("challenge_days")
    rest_channel_name = cfg.get("rest_channel")

    weekday = today.weekday()
    monday = today - timedelta(days=weekday)
    week_dates = [
        monday + timedelta(days=i)
        for i in range(7)
        if (monday + timedelta(days=i)).weekday() in challenge_days
        and (monday + timedelta(days=i)) <= today
    ]

    # 휴식 면제 날짜 수집
    rest_exempt = await get_rest_exempt_dates(guild, rest_channel_name)

    channels = get_participant_channels(guild)
    results = {}

    for ch in channels:
        name = get_member_name_from_channel(ch)

        # 채널 히스토리 단 한 번 읽기
        scan = await scan_channel(ch, week_dates)
        daily_counts    = scan["daily_counts"]
        preupload_exempt = scan["preupload_exempt"]
        messages_by_date = scan["messages_by_date"]

        # 판정
        status = {}
        skip_next = False
        member_rest_dates = rest_exempt.get(name, [])

        for i, d in enumerate(week_dates):
            if d in member_rest_dates:
                status[d] = "휴식"
                continue
            if d in preupload_exempt:
                status[d] = "선업로드"
                continue
            if skip_next:
                skip_next = False
                if d not in status:
                    status[d] = "정상" if daily_counts[d] >= 1 else "결석"
                continue

            count = daily_counts[d]
            if count >= 1:
                status[d] = "정상"
            else:
                if i + 1 < len(week_dates):
                    next_d = week_dates[i + 1]
                    if next_d in member_rest_dates or next_d in preupload_exempt:
                        status[d] = "결석"
                    else:
                        next_count = daily_counts[next_d]
                        if next_count >= 2:
                            status[d] = "지각"
                            status[next_d] = "정상"
                            skip_next = True
                        else:
                            status[d] = "결석"
                else:
                    status[d] = "결석"

        results[name] = status

        # 리액션 추가 (캐싱된 메시지 활용, 추가 API 호출 없음)
        await add_reactions_from_scan(messages_by_date, status)

    return results, week_dates


def build_weekly_report(results: dict, week_dates: list) -> discord.Embed:
    if not week_dates:
        return discord.Embed(title="📊 주간 정산", description="이번 주 챌린지 날짜가 없어요.", color=0x95a5a6)

    fine_late   = cfg.get("fine_late")
    fine_absent = cfg.get("fine_absent")
    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    STATUS_EMOJI  = {"정상": "✅", "지각": "⏰", "결석": "❌", "휴식": "💤", "선업로드": "✨"}

    start_str = f"{week_dates[0].month}/{week_dates[0].day}"
    end_str   = f"{week_dates[-1].month}/{week_dates[-1].day}"

    topic = cfg.get("challenge_topic")
    embed = discord.Embed(
        title=f"📊 주간 {topic} 챌린지 정산 ({start_str}~{end_str})",
        color=0x9b59b6,
    )

    fine_lines = []
    all_perfect = True

    for name, status in results.items():
        day_row = " ".join(
            f"{weekday_names[d.weekday()]}{STATUS_EMOJI.get(status.get(d, '결석'), '❌')}"
            for d in week_dates
        )
        late_count   = sum(1 for v in status.values() if v == "지각")
        absent_count = sum(1 for v in status.values() if v == "결석")
        rest_count   = sum(1 for v in status.values() if v in ("휴식", "선업로드"))
        total_fine   = late_count * fine_late + absent_count * fine_absent

        summary = []
        if late_count:
            summary.append(f"지각 {late_count}회")
        if absent_count:
            summary.append(f"결석 {absent_count}회")
        if rest_count:
            summary.append(f"휴식/선업로드 {rest_count}회")

        if not summary:
            summary_str = "개근 🎉"
        else:
            summary_str = ", ".join(summary)
            all_perfect = False

        embed.add_field(
            name=f"**{name}**  {day_row}",
            value=summary_str + (f"  |  💸 **{total_fine:,}원**" if total_fine else ""),
            inline=False,
        )
        if total_fine:
            fine_lines.append(f"• {name}: {total_fine:,}원 (지각 {late_count}×{fine_late:,} + 결석 {absent_count}×{fine_absent:,})")

    if all_perfect:
        embed.add_field(name="🎉 이번 주 전원 개근!", value="수고했어요!", inline=False)
    elif fine_lines:
        embed.add_field(name="💸 벌금 대상", value="\n".join(fine_lines), inline=False)

    embed.set_footer(text=f"지각 {fine_late:,}원 / 결석 {fine_absent:,}원 • {topic} 챌린지 봇")
    embed.timestamp = datetime.now(TZ)
    return embed


async def post_attendance_report(guild: discord.Guild, date=None, interaction: discord.Interaction = None):
    ch = await get_attendance_channel(guild)
    if ch is None:
        msg = f"❗ `#{cfg.get('attendance_channel')}` 채널을 찾을 수 없어요."
        if interaction:
            await interaction.followup.send(msg, ephemeral=True)
        return

    if date is None:
        date = get_challenge_date()

    rest_day   = not is_challenge_day(date)
    attendance = {} if rest_day else await check_attendance(guild, date)
    embed      = build_report(date, attendance, rest_day)

    if interaction:
        await interaction.followup.send(embed=embed)
    else:
        await ch.send(embed=embed)


# =====================================================
# 자동 태스크
# =====================================================

@tasks.loop(minutes=1)
async def auto_report_task():
    """매일 AUTO_REPORT_HOUR:AUTO_REPORT_MINUTE에 출석 결과 자동 발표."""
    now = datetime.now(TZ)
    if now.hour == cfg.get("auto_report_hour") and now.minute == cfg.get("auto_report_minute"):
        for guild in bot.guilds:
            await post_attendance_report(guild)


@tasks.loop(minutes=1)
async def midnight_reminder_task():
    """매일 자정(00:00)에 미참여자 멘션 알림. 휴식일이면 스킵."""
    now = datetime.now(TZ)
    if now.hour != 0 or now.minute != 0:
        return

    date = get_challenge_date(now)
    if not is_challenge_day(date):
        return

    for guild in bot.guilds:
        ch = await get_attendance_channel(guild)
        if ch is None:
            continue

        absent_names, mentions = await get_absent_members_with_mention(guild, date)
        if not absent_names:
            await ch.send("🎉 자정 기준 오늘 참여자 전원이 업로드를 완료했어요!")
            continue

        weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
        day_str = f"{date.month}/{date.day}({weekday_names[date.weekday()]})"
        mention_str = " ".join(mentions)

        # 멘션은 일반 텍스트로 먼저 전송해야 알림이 울림
        await ch.send(mention_str)

        embed = discord.Embed(
            title=f"⏰ {day_str} 자정 미참여 알림",
            description=(
                f"아직 오늘 {cfg.get('challenge_topic')}을(를) 업로드하지 않았어요!\n"
                f"하루 기준 시각({cfg.get('day_start_hour')}시)까지 업로드하면 참여 인정됩니다 🖊️"
            ),
            color=0xe74c3c,
        )
        embed.set_footer(text=f"{cfg.get('challenge_topic')} 챌린지 봇 • 자정 알림")
        embed.timestamp = datetime.now(TZ)
        await ch.send(embed=embed)


@tasks.loop(minutes=1)
async def weekly_settlement_task():
    """매주 마지막 챌린지 요일 AUTO_REPORT 시각에 주간 정산 자동 발표."""
    now = datetime.now(TZ)
    if now.hour != cfg.get("auto_report_hour") or now.minute != cfg.get("auto_report_minute"):
        return

    today = get_challenge_date(now)
    challenge_days = cfg.get("challenge_days")
    if not challenge_days:
        return
    if today.weekday() != max(challenge_days):
        return

    for guild in bot.guilds:
        ch = await get_weekly_channel(guild)
        if ch is None:
            continue
        results, week_dates = await calc_weekly_result(guild)
        embed = build_weekly_report(results, week_dates)
        await ch.send(embed=embed)


# =====================================================
# 슬래시 커맨드 — 조회
# =====================================================

@bot.tree.command(name="출석확인", description="오늘(또는 특정 날짜)의 챌린지 출석 현황을 확인합니다.")
@app_commands.describe(날짜="조회할 날짜 (YYYY-MM-DD 형식, 생략 시 오늘)")
async def slash_check(interaction: discord.Interaction, 날짜: str = None):
    await interaction.response.defer()
    date = None
    if 날짜:
        try:
            date = datetime.strptime(날짜, "%Y-%m-%d").date()
        except ValueError:
            await interaction.followup.send("❗ 날짜 형식이 올바르지 않아요. `YYYY-MM-DD` 형식으로 입력해주세요.", ephemeral=True)
            return
    await post_attendance_report(interaction.guild, date=date, interaction=interaction)


@bot.tree.command(name="채널목록", description="현재 감지된 참여자 채널 목록을 보여줍니다.")
async def slash_channels(interaction: discord.Interaction):
    channels = get_participant_channels(interaction.guild)
    if not channels:
        await interaction.response.send_message(
            f"❗ `{cfg.get('channel_prefix')}` 로 시작하는 채널이 없어요.", ephemeral=True
        )
        return
    names = "\n".join(f"• #{ch.name}  →  **{get_member_name_from_channel(ch)}**" for ch in channels)
    embed = discord.Embed(title="📋 참여자 채널 목록", description=names, color=0x3498db)
    embed.set_footer(text=f"총 {len(channels)}개 채널 감지 중")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="주간정산", description="이번 주(또는 특정 날짜가 속한 주)의 챌린지 정산 결과를 조회합니다.")
@app_commands.describe(날짜="조회할 주의 날짜 (YYYY-MM-DD 형식, 생략 시 이번 주)")
async def slash_weekly(interaction: discord.Interaction, 날짜: str = None):
    await interaction.response.defer()
    ref_date = None
    if 날짜:
        try:
            ref_date = datetime.strptime(날짜, "%Y-%m-%d").date()
        except ValueError:
            await interaction.followup.send("❗ 날짜 형식이 올바르지 않아요. `YYYY-MM-DD` 형식으로 입력해주세요.", ephemeral=True)
            return
    results, week_dates = await calc_weekly_result(interaction.guild, ref_date)
    embed = build_weekly_report(results, week_dates)
    ch = await get_weekly_channel(interaction.guild)
    if ch and ch.id != interaction.channel.id:
        await ch.send(embed=embed)
        await interaction.followup.send(f"✅ {ch.mention} 에 정산 결과를 올렸어요!", ephemeral=True)
    else:
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="설정확인", description="현재 봇 설정을 보여줍니다.")
async def slash_config_view(interaction: discord.Interaction):
    day_names = ["월", "화", "수", "목", "금", "토", "일"]
    challenge_days_str = ", ".join(day_names[d] for d in cfg.get("challenge_days"))
    weekly_ch = cfg.get("weekly_channel")

    embed = discord.Embed(title="⚙️ 현재 봇 설정", color=0x9b59b6)
    embed.add_field(name="출석 채널",      value=f"#{cfg.get('attendance_channel')}", inline=True)
    embed.add_field(name="주간 정산 채널", value=f"#{weekly_ch}" if weekly_ch else f"#{cfg.get('attendance_channel')} (동일)", inline=True)
    embed.add_field(name="휴식 신청 채널", value=f"#{cfg.get('rest_channel')}", inline=True)
    embed.add_field(name="하루 기준 시각", value=f"{cfg.get('day_start_hour')}시", inline=True)
    embed.add_field(name="자동 발표 시각", value=f"{cfg.get('auto_report_hour'):02d}:{cfg.get('auto_report_minute'):02d}", inline=True)
    embed.add_field(name="참여일",         value=challenge_days_str, inline=True)
    embed.add_field(name="챌린지 주제",    value=cfg.get("challenge_topic"), inline=True)
    embed.add_field(name="채널 접두사",    value=f"`{cfg.get('channel_prefix')}`", inline=True)
    embed.add_field(name="지각 벌금",      value=f"{cfg.get('fine_late'):,}원", inline=True)
    embed.add_field(name="결석 벌금",      value=f"{cfg.get('fine_absent'):,}원", inline=True)
    embed.add_field(name="타임존",         value=config.TIMEZONE, inline=True)
    embed.set_footer(text="/설정변경출석채널 | /설정변경정산채널 | /설정변경휴식채널 | /설정변경발표시각 | /설정변경주제 | /설정변경기준시각 | /설정변경참여일 | /설정변경접두사 | /설정변경지각비 | /설정변경결석비")
    await interaction.response.send_message(embed=embed)


# =====================================================
# 슬래시 커맨드 — 설정 변경
# =====================================================

@bot.tree.command(name="설정변경출석채널", description="출석 결과를 발표할 채널을 변경합니다.")
@app_commands.describe(채널="출석 결과를 보낼 채널")
async def slash_set_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    cfg.set_and_save("attendance_channel", 채널.name)
    await interaction.response.send_message(f"✅ 출석 채널이 {채널.mention} 으로 변경됐어요!")


@bot.tree.command(name="설정변경정산채널", description="주간 정산 결과를 발표할 채널을 변경합니다.")
@app_commands.describe(채널="주간 정산을 올릴 채널")
async def slash_set_weekly_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    cfg.set_and_save("weekly_channel", 채널.name)
    await interaction.response.send_message(f"✅ 주간 정산 채널이 {채널.mention} 으로 변경됐어요!")


@bot.tree.command(name="설정변경휴식채널", description="개인사정 휴식 신청 채널을 변경합니다. (기본: 휴식)")
@app_commands.describe(채널="휴식 신청 메시지를 올릴 채널")
async def slash_set_rest_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    cfg.set_and_save("rest_channel", 채널.name)
    await interaction.response.send_message(
        f"✅ 휴식 신청 채널이 {채널.mention} 으로 변경됐어요!\n"
        f"이 채널에 `[2025-06-25]` 또는 `[2025-06-25 ~ 2025-06-28]` 형식으로 작성하면 해당 날짜 벌금이 면제돼요."
    )


@bot.tree.command(name="설정변경기준시각", description="하루 시작 기준 시각을 변경합니다. (이 시각 이전 업로드는 전날로 처리)")
@app_commands.describe(시각="기준 시각 (0~23 사이 숫자, 기본: 6)")
async def slash_set_hour(interaction: discord.Interaction, 시각: int):
    if not 0 <= 시각 <= 23:
        await interaction.response.send_message("❗ 0~23 사이 숫자를 입력해주세요.", ephemeral=True)
        return
    cfg.set_and_save("day_start_hour", 시각)
    await interaction.response.send_message(f"✅ 하루 기준 시각이 **{시각}시**로 변경됐어요!")


@bot.tree.command(name="설정변경참여일", description="챌린지 참여일을 변경합니다.")
@app_commands.describe(참여일="요일 번호를 쉼표로 입력 (0=월 1=화 2=수 3=목 4=금 5=토 6=일), 예: 0,1,2,3,4")
async def slash_set_days(interaction: discord.Interaction, 참여일: str):
    day_names = ["월", "화", "수", "목", "금", "토", "일"]
    try:
        days = [int(d.strip()) for d in 참여일.split(",")]
        if not all(0 <= d <= 6 for d in days):
            raise ValueError
        days = sorted(set(days))
    except ValueError:
        await interaction.response.send_message("❗ 올바른 형식으로 입력해주세요.\n예: `0,1,2,3,4` (월~금)", ephemeral=True)
        return
    cfg.set_and_save("challenge_days", days)
    days_str = ", ".join(day_names[d] for d in days)
    await interaction.response.send_message(f"✅ 참여일이 **{days_str}** 으로 변경됐어요!")


@bot.tree.command(name="설정변경주제", description="챌린지 주제 이름을 변경합니다. (출석/정산 메시지에 표시됩니다)")
@app_commands.describe(주제="챌린지 주제 이름 (예: 크로키, 스터디, 코딩)")
async def slash_set_topic(interaction: discord.Interaction, 주제: str):
    if not 주제:
        await interaction.response.send_message("❗ 주제 이름을 입력해주세요.", ephemeral=True)
        return
    old_topic = cfg.get("challenge_topic")
    cfg.set_and_save("challenge_topic", 주제)
    embed = discord.Embed(title="✅ 챌린지 주제 변경 완료", color=0x2ecc71)
    embed.add_field(name="이전 주제", value=old_topic, inline=True)
    embed.add_field(name="새 주제",   value=주제, inline=True)
    embed.add_field(name="적용 범위", value="출석 현황, 주간 정산, 자정 알림 메시지에 반영됩니다.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="설정변경접두사", description="참여자 채널 접두사를 변경합니다.")
@app_commands.describe(접두사="새 채널 접두사 (예: 크로키-, 스터디-, coding-)")
async def slash_set_prefix(interaction: discord.Interaction, 접두사: str):
    if not 접두사:
        await interaction.response.send_message("❗ 접두사를 입력해주세요.", ephemeral=True)
        return
    old_prefix = cfg.get("channel_prefix")
    cfg.set_and_save("channel_prefix", 접두사)
    embed = discord.Embed(title="✅ 채널 접두사 변경 완료", color=0x2ecc71)
    embed.add_field(name="이전 접두사", value=f"`{old_prefix}`", inline=True)
    embed.add_field(name="새 접두사",   value=f"`{접두사}`", inline=True)
    embed.add_field(name="⚠️ 주의", value="기존 `/참여자등록` 연결이 있다면 `/참여자등록`을 다시 해주세요.", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="설정변경지각비", description="지각 벌금을 변경합니다. (기본: 1,000원)")
@app_commands.describe(금액="지각 벌금 금액 (원 단위 숫자, 예: 1000)")
async def slash_set_fine_late(interaction: discord.Interaction, 금액: int):
    if 금액 < 0:
        await interaction.response.send_message("❗ 0 이상의 숫자를 입력해주세요.", ephemeral=True)
        return
    cfg.set_and_save("fine_late", 금액)
    await interaction.response.send_message(f"✅ 지각 벌금이 **{금액:,}원**으로 변경됐어요!")


@bot.tree.command(name="설정변경결석비", description="결석 벌금을 변경합니다. (기본: 2,000원)")
@app_commands.describe(금액="결석 벌금 금액 (원 단위 숫자, 예: 2000)")
async def slash_set_fine_absent(interaction: discord.Interaction, 금액: int):
    if 금액 < 0:
        await interaction.response.send_message("❗ 0 이상의 숫자를 입력해주세요.", ephemeral=True)
        return
    cfg.set_and_save("fine_absent", 금액)
    await interaction.response.send_message(f"✅ 결석 벌금이 **{금액:,}원**으로 변경됐어요!")


@bot.tree.command(name="설정변경발표시각", description="매일 자동 출석 발표 시각을 변경합니다. (주간 정산도 같은 시각에 발표)")
@app_commands.describe(시="발표 시각 (0~23)", 분="발표 분 (0~59)")
async def slash_set_report_time(interaction: discord.Interaction, 시: int, 분: int):
    if not (0 <= 시 <= 23):
        await interaction.response.send_message("❗ 시는 0~23 사이 숫자를 입력해주세요.", ephemeral=True)
        return
    if not (0 <= 분 <= 59):
        await interaction.response.send_message("❗ 분은 0~59 사이 숫자를 입력해주세요.", ephemeral=True)
        return
    cfg.set_and_save("auto_report_hour", 시)
    cfg.set_and_save("auto_report_minute", 분)
    await interaction.response.send_message(
        f"✅ 자동 발표 시각이 **{시:02d}:{분:02d}** 으로 변경됐어요!\n"
        f"매일 출석 현황 발표와 주간 정산 발표가 이 시각에 실행돼요."
    )


# =====================================================
# 슬래시 커맨드 — 참여자 등록
# =====================================================

@bot.tree.command(name="참여자등록", description="채널과 멤버를 연결해서 자정 알림 멘션을 설정합니다.")
@app_commands.describe(채널="참여자의 업로드 채널 (예: #크로키-진아)", 멤버="해당 채널의 참여자")
async def slash_register(interaction: discord.Interaction, 채널: discord.TextChannel, 멤버: discord.Member):
    prefix = cfg.get("channel_prefix")
    if not 채널.name.startswith(prefix):
        await interaction.response.send_message(f"❗ `{prefix}` 로 시작하는 채널만 등록할 수 있어요.", ephemeral=True)
        return
    channel_members: dict = cfg.get("channel_members")
    channel_members[채널.name] = 멤버.id
    cfg.set_and_save("channel_members", channel_members)
    await interaction.response.send_message(f"✅ **#{채널.name}** → {멤버.mention} 연결 완료!\n이제 자정 알림에서 정확히 멘션돼요.")


@bot.tree.command(name="참여자해제", description="채널-멤버 연결을 해제합니다.")
@app_commands.describe(채널="연결을 해제할 채널")
async def slash_unregister(interaction: discord.Interaction, 채널: discord.TextChannel):
    channel_members: dict = cfg.get("channel_members")
    if 채널.name not in channel_members:
        await interaction.response.send_message(f"❗ **#{채널.name}** 은 등록된 채널이 아니에요.", ephemeral=True)
        return
    del channel_members[채널.name]
    cfg.set_and_save("channel_members", channel_members)
    await interaction.response.send_message(f"✅ **#{채널.name}** 연결이 해제됐어요.")


@bot.tree.command(name="참여자목록", description="등록된 채널-멤버 연결 목록을 보여줍니다.")
async def slash_member_list(interaction: discord.Interaction):
    channel_members: dict = cfg.get("channel_members")
    participant_channels = get_participant_channels(interaction.guild)
    if not participant_channels:
        await interaction.response.send_message(f"❗ `{cfg.get('channel_prefix')}` 로 시작하는 채널이 없어요.", ephemeral=True)
        return
    lines = []
    for ch in participant_channels:
        user_id = channel_members.get(ch.name)
        lines.append(f"✅ #{ch.name}  →  <@{user_id}>" if user_id else f"❌ #{ch.name}  →  _미등록_")
    embed = discord.Embed(title="👥 참여자 채널-멤버 연결 목록", description="\n".join(lines), color=0x3498db)
    registered = sum(1 for ch in participant_channels if ch.name in channel_members)
    embed.set_footer(text=f"{len(participant_channels)}개 채널 중 {registered}개 등록됨 • /참여자등록 으로 연결하세요")
    await interaction.response.send_message(embed=embed)


# =====================================================
# 봇 이벤트
# =====================================================

@bot.event
async def on_ready():
    print(f"✅ 봇 로그인 완료: {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"✅ 슬래시 커맨드 {len(synced)}개 동기화 완료")
    except Exception as e:
        print(f"❌ 슬래시 커맨드 동기화 실패: {e}")

    auto_report_task.start()
    midnight_reminder_task.start()
    weekly_settlement_task.start()
    h = cfg.get('auto_report_hour')
    m = cfg.get('auto_report_minute')
    print(f"✅ 자동 출석 발표: 매일 {h:02d}:{m:02d}")
    print(f"✅ 자정 미참여 알림: 매일 00:00")
    print(f"✅ 주간 정산 자동 발표: 매주 마지막 챌린지 요일 {h:02d}:{m:02d}")


@bot.event
async def on_message(message: discord.Message):
    """
    메시지 실시간 감지 → 즉시 리액션 추가.

    - 개인 채널 + 이미지 첨부 + '미리' 키워드 → ✨ (선업로드)
    - 개인 채널 + 이미지 첨부 (일반)           → ✅ (정상 출석)
    - 휴식 채널 + 날짜 형식 포함               → ☑️ (휴식 신청 확인)
    """
    if message.author.bot:
        return
    if not message.guild:
        return

    prefix = cfg.get("channel_prefix")
    rest_ch_name = cfg.get("rest_channel")

    # ── 개인 채널 감지 ──
    if message.channel.name.startswith(prefix):
        has_image = any(
            a.content_type and a.content_type.startswith("image/")
            for a in message.attachments
        )
        if has_image:
            # 선업로드 키워드 있으면 ✨, 없으면 ✅
            if "미리" in (message.content or ""):
                try:
                    await message.add_reaction("✨")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            else:
                try:
                    await message.add_reaction("✅")
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # ── 휴식 채널 감지 ──
    elif message.channel.name == rest_ch_name:
        if parse_rest_dates(message.content):
            try:
                await message.add_reaction("☑️")
            except (discord.Forbidden, discord.HTTPException):
                pass

    await bot.process_commands(message)


# =====================================================
# 실행
# =====================================================

if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN 환경변수가 없습니다. .env 파일을 확인하세요.")
    else:
        bot.run(TOKEN)