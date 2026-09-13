import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta
import pytz
import os
from dotenv import load_dotenv
import config
import settings as cfg
import store

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
    return [
        ch for ch in guild.text_channels
        if ch.name.startswith(prefix) and ch.permissions_for(guild.me).view_channel
    ]


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


async def get_fine_channel(guild: discord.Guild):
    """벌금 현황 채널 반환. 미설정이거나 채널이 없으면 주간 정산 채널 사용."""
    fine_ch_name = cfg.get("fine_channel")
    if fine_ch_name:
        ch = discord.utils.get(guild.text_channels, name=fine_ch_name)
        if ch is not None:
            return ch
    return await get_weekly_channel(guild)


def get_participant_name(member: discord.Member):
    """discord.Member → 참여자 이름 (channel_members 역매핑). 미등록이면 None."""
    prefix = cfg.get("channel_prefix")
    channel_members: dict = cfg.get("channel_members")
    for ch_name, uid in channel_members.items():
        if uid == member.id:
            return ch_name[len(prefix):]
    return None


def get_week_dates(ref_date) -> list:
    """ref_date가 속한 주의 챌린지 날짜 목록 (ref_date 이하만)."""
    challenge_days = cfg.get("challenge_days")
    monday = ref_date - timedelta(days=ref_date.weekday())
    return [
        monday + timedelta(days=i)
        for i in range(7)
        if (monday + timedelta(days=i)).weekday() in challenge_days
        and (monday + timedelta(days=i)) <= ref_date
    ]


def format_week_label(week_key) -> str:
    """'YYYY-MM-DD'(월요일) 또는 date → 'M/D 주' 라벨."""
    try:
        d = week_key if isinstance(week_key, date_type) else datetime.strptime(week_key, "%Y-%m-%d").date()
        return f"{d.month}/{d.day} 주"
    except (ValueError, TypeError):
        return str(week_key)


# =====================================================
# 휴식 신청 / 선업로드 파싱
# =====================================================

import re
from datetime import date as date_type

MAX_REST_RANGE_DAYS = 31  # 휴식 기간 상한 (연도 오타 방어용)


def parse_rest_dates_checked(text: str) -> tuple:
    """
    #휴식 채널 메시지에서 날짜 파싱 + 유효성 검사.
    형식: [2025-06-25] 또는 [2025-06-25 ~ 2025-06-28]

    반환: (dates, error)
      - error가 None이 아니면 dates는 빈 리스트 (신청 무효)
      - 날짜 형식 자체가 없으면 ([], None) — 휴식 신청이 아닌 일반 메시지
    """
    # 기간 형식: [YYYY-MM-DD ~ YYYY-MM-DD]
    range_match = re.search(r'\[(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})\]', text)
    if range_match:
        try:
            start = datetime.strptime(range_match.group(1), "%Y-%m-%d").date()
            end   = datetime.strptime(range_match.group(2), "%Y-%m-%d").date()
        except ValueError:
            return [], "날짜가 올바르지 않아요. (없는 날짜인지 확인해주세요)"

        if end < start:
            return [], "끝 날짜가 시작 날짜보다 빨라요."

        span = (end - start).days + 1
        if span > MAX_REST_RANGE_DAYS:
            return [], (
                f"휴식 기간이 **{span:,}일**로 잡혔어요. 연도를 잘못 쓰신 건 아닐까요?\n"
                f"(한 번에 신청 가능한 최대 기간은 {MAX_REST_RANGE_DAYS}일이에요)"
            )

        dates = []
        d = start
        while d <= end:
            dates.append(d)
            d += timedelta(days=1)
        return dates, None

    # 단일 날짜 형식: [YYYY-MM-DD]
    single_match = re.search(r'\[(\d{4}-\d{2}-\d{2})\]', text)
    if single_match:
        try:
            return [datetime.strptime(single_match.group(1), "%Y-%m-%d").date()], None
        except ValueError:
            return [], "날짜가 올바르지 않아요. (없는 날짜인지 확인해주세요)"

    return [], None


def parse_rest_dates(text: str) -> list:
    """#휴식 채널 메시지에서 유효한 휴식 날짜만 반환. (무효한 신청은 빈 리스트)"""
    return parse_rest_dates_checked(text)[0]


async def get_rest_exempt_dates(guild: discord.Guild, channel_name: str, ref_date=None) -> dict:
    """
    #휴식 채널 메시지를 읽어서 { 참여자이름: [면제날짜, ...] } 반환.
    메시지 작성자를 channel_members로 역매핑해서 이름 찾음.
    ref_date: 조회 기준 날짜 — 이 날짜 기준 한 달 전부터 읽어요 (과거 주 조회 지원).
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
        if ref_date is None:
            ref_date = get_challenge_date()
        base = TZ.localize(datetime(ref_date.year, ref_date.month, ref_date.day))
        cutoff = min(base, datetime.now(TZ)) - timedelta(days=30)
        async for msg in rest_ch.history(after=cutoff, limit=1000):
            dates = parse_rest_dates(msg.content)
            if not dates:
                continue
            name = id_to_name.get(msg.author.id)
            if name is None:
                name = msg.author.display_name
            if name not in exempt:
                exempt[name] = []
            exempt[name].extend(dates)
            # ☑️ 리액션은 on_message에서 실시간으로 처리하므로 여기선 생략
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

    # 스캔 범위: 첫 챌린지일 ~ 마지막 챌린지일 다음날까지
    # (중간 비챌린지일 + 마지막 다음날 지각 감지 포함)
    last_challenge_date = week_dates[-1]
    next_day = last_challenge_date + timedelta(days=1)
    _, scan_end = get_day_range(next_day)

    # 조회 대상 날짜 기준으로 최근 한 달치를 읽어요 (과거 주 조회 지원)
    cutoff = scan_end - timedelta(days=30)

    # 카운트 대상: 챌린지일 + 각 챌린지일 바로 다음 캘린더 날짜(비챌린지일인 경우)
    scan_dates = set(week_dates)
    for d in week_dates:
        next_cal = d + timedelta(days=1)
        if next_cal.weekday() not in challenge_days:
            scan_dates.add(next_cal)

    daily_counts: dict = {d: 0 for d in scan_dates}
    preupload_exempt: list = []
    messages_by_date: dict = {d: [] for d in week_dates}  # 리액션은 챌린지일만

    try:
        async for msg in channel.history(after=cutoff, before=scan_end, limit=2000):
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
                # ✨ 리액션은 on_message에서 실시간으로 처리하므로 여기선 생략
            else:
                # 일반 이미지 → 날짜별 카운트 + 캐싱
                if msg_date in daily_counts:
                    daily_counts[msg_date] += 1
                    if msg_date in messages_by_date:  # 챌린지일만 리액션 캐싱
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
    scan_channel을 활용해서 채널당 히스토리 1번만 읽음.
    """
    if date is None:
        date = get_challenge_date()
    channels = get_participant_channels(guild)

    # 휴식 면제 날짜 수집
    rest_exempt = await get_rest_exempt_dates(guild, cfg.get("rest_channel"), date)

    result = {}
    for ch in channels:
        name = get_member_name_from_channel(ch)

        # scan_channel로 한 번에 처리
        scan = await scan_channel(ch, [date])
        daily_counts     = scan["daily_counts"]
        preupload_exempt = scan["preupload_exempt"]
        count            = daily_counts.get(date, 0)

        if date in rest_exempt.get(name, []):
            # 휴식 신청일이라도 실제로 올렸으면 참여로 인정 (주간 판정과 동일 규칙)
            result[name] = "정상" if count >= 1 else "휴식"
        elif date in preupload_exempt:
            result[name] = "선업로드"
        elif count >= 1:
            result[name] = "정상"
        else:
            result[name] = "미참여"

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
# 일일 스냅샷 기록
# =====================================================

async def record_daily_snapshot(guild: discord.Guild, date):
    """
    해당 날짜의 참여자별 업로드 기록을 attendance_log.json에 저장.
    이미 기록된 날짜는 절대 다시 쓰지 않아요 (기록 동결 —
    나중에 이미지를 지웠다 다시 올려도 과거 판정이 바뀌지 않도록).
    """
    if store.has_snapshot(date):
        return

    rest_exempt = await get_rest_exempt_dates(guild, cfg.get("rest_channel"), date)
    snapshot = {}
    for ch in get_participant_channels(guild):
        name = get_member_name_from_channel(ch)
        scan = await scan_channel(ch, [date])
        snapshot[name] = {
            "count": scan["daily_counts"].get(date, 0),
            "preupload": date in scan["preupload_exempt"],
            "rest": date in rest_exempt.get(name, []),
        }
    store.save_snapshot(date, snapshot)


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


def judge_member_week(week_dates: list, judge_counts: dict, member_rest_dates: list,
                      preupload_exempt: list, overrides: dict = None) -> dict:
    """
    한 참여자의 주간 판정 로직. { date: 상태 } 반환.

    judge_counts: { date: 장수 } — 스냅샷 우선 값 (없으면 라이브 스캔 값)
    overrides:    { date: 상태 } — /출석수정 수동 보정 (다른 모든 판정보다 우선)

    판정 기준:
    - 수동 보정                        → 해당 상태 그대로
    - 휴식 면제일                      → 💤 휴식
    - 선업로드 면제일                   → ✨ 선업로드
    - 당일 1장 이상                    → ✅ 정상
    - 당일 0장 + 다음날 1장            → ⏰ 전날 지각
    - 당일 0장 + 다음날 2장 이상       → ⏰ 전날 지각 + ✅ 당일 정상
    - 당일 0장 + 다음날이 휴식/선업로드 면제일인데 1장 이상 → ⏰ 전날 지각
      (휴식일엔 올릴 의무가 없으니 1장이면 충분해요)
    - 끝까지 0장                       → ❌ 결석
    (다음날은 휴식일 포함, 금요일이면 토요일 업로드도 확인.
     지각 인정은 딱 하루 전까지만 — 몰아서 올려도 여러 날이 살아나진 않아요.)
    """
    challenge_days = cfg.get("challenge_days")
    if overrides is None:
        overrides = {}

    status = {}
    skip_next = False

    for i, d in enumerate(week_dates):
        if d in overrides:
            # 수동 보정 최우선
            status[d] = overrides[d]
            skip_next = False
            continue
        if d in member_rest_dates:
            # 휴식 신청일이라도 실제로 올렸으면 참여로 인정해요.
            # (벌금은 어차피 둘 다 0원이고, 기록이 실제 참여를 반영하는 게 맞아서)
            status[d] = "정상" if judge_counts.get(d, 0) >= 1 else "휴식"
            continue
        if d in preupload_exempt:
            status[d] = "선업로드"
            continue
        if skip_next:
            skip_next = False
            if d not in status:
                status[d] = "정상" if judge_counts.get(d, 0) >= 1 else "결석"
            continue

        count = judge_counts.get(d, 0)
        if count >= 1:
            status[d] = "정상"
        else:
            if i + 1 < len(week_dates):
                next_challenge_d = week_dates[i + 1]
                next_calendar_d = d + timedelta(days=1)

                if next_challenge_d in member_rest_dates or next_challenge_d in preupload_exempt:
                    # 다음 챌린지일이 휴식/선업로드 면제일이어도
                    # 그날 1장 이상 올렸으면 전날 지각으로 인정해요
                    next_count = judge_counts.get(next_challenge_d, 0)
                    status[d] = "지각" if next_count >= 1 else "결석"
                elif next_calendar_d != next_challenge_d and next_calendar_d.weekday() not in challenge_days:
                    # 바로 다음 캘린더 날짜가 챌린지일이 아닌 경우 (예: 화→수 비챌린지)
                    # 그 날 1장 이상 올리면 지각, 없으면 다음 챌린지일 장수로 판단
                    next_cal_count = judge_counts.get(next_calendar_d, 0)
                    if next_cal_count >= 1:
                        status[d] = "지각"
                    else:
                        next_count = judge_counts.get(next_challenge_d, 0)
                        if next_count >= 2:
                            status[d] = "지각"
                            status[next_challenge_d] = "정상"
                            skip_next = True
                        else:
                            status[d] = "결석"
                else:
                    # 바로 다음 캘린더 날짜가 다음 챌린지일인 경우 (예: 월→화)
                    next_count = judge_counts.get(next_challenge_d, 0)
                    if next_count >= 2:
                        status[d] = "지각"
                        status[next_challenge_d] = "정상"
                        skip_next = True
                    else:
                        status[d] = "결석"
            else:
                # 마지막 챌린지일: 바로 다음 캘린더 날짜가 챌린지일이 아니면 지각 인정
                next_calendar_day = d + timedelta(days=1)
                if next_calendar_day.weekday() not in challenge_days:
                    next_count = judge_counts.get(next_calendar_day, 0)
                    status[d] = "지각" if next_count >= 1 else "결석"
                else:
                    status[d] = "결석"

    return status


def build_judge_counts(daily_counts: dict, name: str) -> dict:
    """
    판정에 사용할 날짜별 장수 계산.
    스냅샷(attendance_log.json)이 있는 날짜는 스냅샷 값을 우선 사용 —
    과거 이미지를 지웠다 다시 올려도 판정이 바뀌지 않아요.
    """
    judge_counts = {}
    for d, live_count in daily_counts.items():
        snap = store.get_member_snapshot(d, name)
        judge_counts[d] = snap["count"] if snap is not None else live_count
    return judge_counts


async def calc_weekly_result(guild: discord.Guild, ref_date=None):
    """
    이번 주(또는 ref_date가 속한 주) 챌린지 날짜별 참여자 판정 + 리액션 추가.
    판정 장수는 일일 스냅샷 우선, 휴식/선업로드 면제는 항상 라이브로 읽어요.
    """
    today = ref_date if ref_date else get_challenge_date()
    rest_channel_name = cfg.get("rest_channel")

    week_dates = get_week_dates(today)

    # 휴식 면제 날짜 수집 (조회 주 기준)
    rest_exempt = await get_rest_exempt_dates(
        guild, rest_channel_name, week_dates[0] if week_dates else today
    )

    channels = get_participant_channels(guild)
    results = {}

    for ch in channels:
        name = get_member_name_from_channel(ch)

        # 채널 히스토리 단 한 번 읽기
        scan = await scan_channel(ch, week_dates)
        daily_counts     = scan["daily_counts"]
        preupload_exempt = scan["preupload_exempt"]
        messages_by_date = scan["messages_by_date"]

        # 판정 (스냅샷 우선 장수 + 수동 보정 반영)
        judge_counts = build_judge_counts(daily_counts, name)
        member_rest_dates = rest_exempt.get(name, [])
        overrides = {}
        for d in week_dates:
            ov = store.get_override(d, name)
            if ov is not None:
                overrides[d] = ov
        status = judge_member_week(week_dates, judge_counts, member_rest_dates, preupload_exempt, overrides)

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


# =====================================================
# 벌금 원장
# =====================================================

def _make_fine_record(late: int, absent: int, amount: int) -> dict:
    """벌금 레코드 기본 형태 생성."""
    return {
        "late": late,
        "absent": absent,
        "amount": amount,
        "paid": False,
        "paid_at": None,
        "edited": False,
        "edit_reason": None,
    }


def count_fine_from_status(status: dict) -> tuple:
    """주간 상태 dict → (지각 수, 결석 수, 벌금액)."""
    late   = sum(1 for v in status.values() if v == "지각")
    absent = sum(1 for v in status.values() if v == "결석")
    amount = late * cfg.get("fine_late") + absent * cfg.get("fine_absent")
    return late, absent, amount


def settle_week_fines(week_monday_date, results: dict):
    """
    주간 판정 결과로 해당 주 벌금 원장 기록.
    이미 납부(paid) 또는 수동 수정(edited)된 레코드는 절대 건드리지 않아요 —
    낸 돈이 다시 미납으로 살아나면 안 되니까요.
    """
    week = store.get_week_fines(week_monday_date)
    for name, status in results.items():
        late, absent, amount = count_fine_from_status(status)
        existing = week.get(name)
        if existing and (existing.get("paid") or existing.get("edited")):
            continue  # 납부/수정 완료 기록은 동결
        if amount == 0 and existing is None:
            continue  # 벌금 없는 참여자는 기록 생략
        week[name] = _make_fine_record(late, absent, amount)
    store.set_week_fines(week_monday_date, week)


def update_member_ledger(week_monday_date, name: str, status: dict) -> str:
    """
    출석 보정 후 해당 주 벌금 레코드 재계산.
    반환: "unsettled"(아직 정산 전) | "frozen"(납부/수정 동결) | "updated"(반영 완료)
    """
    if not store.has_week_fines(week_monday_date):
        return "unsettled"
    rec = store.get_member_fine(week_monday_date, name)
    if rec and (rec.get("paid") or rec.get("edited")):
        return "frozen"
    late, absent, amount = count_fine_from_status(status)
    if amount == 0 and rec is None:
        return "updated"  # 원래도 없고 지금도 0원 — 기록할 것 없음
    store.set_member_fine(week_monday_date, name, _make_fine_record(late, absent, amount))
    return "updated"


def get_outstanding_fines() -> dict:
    """전체 원장에서 미납 벌금 집계 → { 이름: {"amount": 총액, "weeks": 미납 주 수} }."""
    outstanding = {}
    for week_key, members in store.get_all_fines().items():
        for name, rec in members.items():
            if rec.get("paid"):
                continue
            amount = rec.get("amount", 0)
            if amount <= 0:
                continue
            o = outstanding.setdefault(name, {"amount": 0, "weeks": 0})
            o["amount"] += amount
            o["weeks"] += 1
    return outstanding


def build_fine_summary_embed(guild: discord.Guild) -> discord.Embed:
    """미납 벌금 현황 임베드."""
    outstanding = get_outstanding_fines()
    total_members = len(get_participant_channels(guild))

    if not outstanding:
        embed = discord.Embed(
            title="💸 미납 벌금 현황",
            description="🎉 미납 벌금이 없어요! 전원 정산 완료",
            color=0x2ecc71,
        )
        embed.set_footer(text=f"총 {total_members}명 전원 정산 완료 🎉")
    else:
        lines = [
            f"• {name} — {o['amount']:,}원 ({o['weeks']}주치)"
            for name, o in sorted(outstanding.items(), key=lambda x: (-x[1]["amount"], x[0]))
        ]
        total = sum(o["amount"] for o in outstanding.values())
        lines.append(f"**합계 {total:,}원**")
        embed = discord.Embed(
            title="💸 미납 벌금 현황",
            description="\n".join(lines),
            color=0xe67e22,
        )
        embed.set_footer(text=f"총 {total_members}명 중 {len(outstanding)}명 미납 · /벌금납부 로 정리")

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

def get_last_finished_date(now: datetime = None):
    """
    하루가 완전히 끝난 가장 최근 챌린지 날짜.

    챌린지 날짜 D의 하루는 (D+1일 기준시각)에 끝나요. 그래서 '지금 진행 중인 날짜'의
    바로 전날이 마지막으로 끝난 날이에요. 발표와 기록 동결은 모두 이 날짜를 기준으로 해야
    마감 직전 업로드가 누락되지 않아요.
    """
    if now is None:
        now = datetime.now(TZ)
    return get_challenge_date(now) - timedelta(days=1)


@tasks.loop(minutes=1)
async def auto_report_task():
    """매일 AUTO_REPORT_HOUR:AUTO_REPORT_MINUTE에 '완전히 끝난 하루'의 출석 결과 발표."""
    now = datetime.now(TZ)
    if now.hour == cfg.get("auto_report_hour") and now.minute == cfg.get("auto_report_minute"):
        for guild in bot.guilds:
            await post_attendance_report(guild, date=get_last_finished_date(now))


@tasks.loop(minutes=1)
async def snapshot_task():
    """
    하루가 완전히 끝난 뒤에 그날의 출석 기록을 동결.

    발표 시각(예: 05:58)에 찍으면, 하루 기준 시각(06:00)까지 남은 몇 분 사이의
    업로드가 0장으로 동결돼버려요. 실제로 05:59 업로드가 결석 처리된 사고가 있었어요.
    그래서 기준 시각이 지난 뒤에 찍고, 봇이 꺼져 있었으면 다음 기회에 자동으로 보충돼요.
    """
    now = datetime.now(TZ)
    if now.hour < cfg.get("day_start_hour"):
        return  # 어제 하루가 아직 안 끝났어요

    date = get_last_finished_date(now)
    if store.has_snapshot(date):
        return
    for guild in bot.guilds:
        await record_daily_snapshot(guild, date)


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
    """
    주간 정산 자동 발표.
    마지막 챌린지 요일 '다음날'(챌린지 날짜 기준)의 발표 시각에 실행돼요 —
    예: 월~금 챌린지면 챌린지 날짜 토요일, 즉 일요일 새벽 발표.
    토요일 업로드가 금요일 지각으로 인정된 뒤에 정산되도록 하기 위해서예요.

    봇 재시작 등으로 정확한 시각을 놓쳐도, meta.json의 마지막 정산 주를 확인해서
    아직 정산 안 된 주가 있으면 뒤늦게라도 실행해요. (같은 주 중복 정산은 없음)
    """
    now = datetime.now(TZ)
    challenge_days = cfg.get("challenge_days")
    if not challenge_days:
        return

    settle_weekday = (max(challenge_days) + 1) % 7  # 정산 기준 요일 (챌린지 날짜 기준)
    today = get_challenge_date(now)

    # 가장 최근의 정산 기준일 (챌린지 날짜) 계산
    days_since = (today.weekday() - settle_weekday) % 7
    target = today - timedelta(days=days_since)

    # 정산 실행 시각 = 대상 챌린지 날짜의 '다음 캘린더 날짜' 발표 시각.
    # 챌린지 날짜 토요일의 하루는 일요일 오전 6시에 끝나므로, 일요일 발표 시각에 정산해야
    # 토요일 업로드(금요일 지각분)까지 반영돼요.
    # ※ 단순히 요일+시각만 비교하면 토요일 오전 6시에 바로 실행돼버려서
    #    정작 토요일 업로드를 하나도 못 보는 문제가 있었어요.
    settle_day = target + timedelta(days=1)
    settle_at = TZ.localize(datetime(
        settle_day.year, settle_day.month, settle_day.day,
        cfg.get("auto_report_hour"), cfg.get("auto_report_minute"),
    ))
    # 발표 시각을 하루 기준 시각보다 이르게 설정해도(예: 05:00) 대상 하루가
    # 끝나기 전에 정산되지 않도록, 하루가 실제로 닫히는 시점 이후로 보정해요.
    day_close = get_day_range(target)[1]
    if settle_at < day_close:
        settle_at = day_close
    if now < settle_at:
        return

    week_monday_date = target - timedelta(days=target.weekday())
    last = store.get_last_settled_week()
    if last is not None and last >= week_monday_date:
        return  # 이미 정산된 주

    for guild in bot.guilds:
        ch = await get_weekly_channel(guild)
        if ch is None:
            continue

        # 스냅샷이 누락된 날짜 보충 기록 (이미 기록된 날은 그대로 둠)
        d = week_monday_date
        while d <= target:
            await record_daily_snapshot(guild, d)
            d += timedelta(days=1)

        results, week_dates = await calc_weekly_result(guild, target)
        embed = build_weekly_report(results, week_dates)
        await ch.send(embed=embed)

        # 벌금 원장 기록 + 미납 현황 발표 (벌금 채널)
        settle_week_fines(week_monday_date, results)
        fine_ch = await get_fine_channel(guild)
        if fine_ch is not None:
            await fine_ch.send(embed=build_fine_summary_embed(guild))

    store.set_last_settled_week(week_monday_date)


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


@bot.tree.command(name="미참여알림", description="현재 미참여자에게 수동으로 알림을 보냅니다.")
async def slash_absent_reminder(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    date = get_challenge_date()
    if not is_challenge_day(date):
        await interaction.followup.send("❗ 오늘은 챌린지 휴식일이에요.", ephemeral=True)
        return

    ch = await get_attendance_channel(interaction.guild)
    if ch is None:
        await interaction.followup.send(f"❗ `#{cfg.get('attendance_channel')}` 채널을 찾을 수 없어요.", ephemeral=True)
        return

    absent_names, mentions = await get_absent_members_with_mention(interaction.guild, date)
    if not absent_names:
        await interaction.followup.send("🎉 현재 미참여자가 없어요! 전원 업로드 완료!", ephemeral=True)
        return

    weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
    day_str = f"{date.month}/{date.day}({weekday_names[date.weekday()]})"
    mention_str = " ".join(mentions)

    await ch.send(mention_str)

    embed = discord.Embed(
        title=f"⏰ {day_str} 미참여 알림",
        description=(
            f"아직 오늘 {cfg.get('challenge_topic')}을(를) 업로드하지 않았어요!\n"
            f"하루 기준 시각({cfg.get('day_start_hour')}시)까지 업로드하면 참여 인정됩니다 🖊️"
        ),
        color=0xe74c3c,
    )
    embed.set_footer(text=f"{cfg.get('challenge_topic')} 챌린지 봇 • 수동 알림")
    embed.timestamp = datetime.now(TZ)
    await ch.send(embed=embed)
    await interaction.followup.send(f"✅ {ch.mention} 에 미참여 알림을 보냈어요! ({len(absent_names)}명)", ephemeral=True)


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
    embed.add_field(name="벌금 채널",      value=f"#{cfg.get('fine_channel')}" if cfg.get("fine_channel") else "정산 채널과 동일", inline=True)
    embed.add_field(name="휴식 신청 채널", value=f"#{cfg.get('rest_channel')}", inline=True)
    embed.add_field(name="하루 기준 시각", value=f"{cfg.get('day_start_hour')}시", inline=True)
    embed.add_field(name="자동 발표 시각", value=f"{cfg.get('auto_report_hour'):02d}:{cfg.get('auto_report_minute'):02d}", inline=True)
    embed.add_field(name="참여일",         value=challenge_days_str, inline=True)
    embed.add_field(name="챌린지 주제",    value=cfg.get("challenge_topic"), inline=True)
    embed.add_field(name="채널 접두사",    value=f"`{cfg.get('channel_prefix')}`", inline=True)
    embed.add_field(name="지각 벌금",      value=f"{cfg.get('fine_late'):,}원", inline=True)
    embed.add_field(name="결석 벌금",      value=f"{cfg.get('fine_absent'):,}원", inline=True)
    embed.add_field(name="타임존",         value=config.TIMEZONE, inline=True)
    embed.set_footer(text=(
        "/설정변경출석채널 | /설정변경정산채널 | /설정변경벌금채널 | /설정변경휴식채널 | /설정변경발표시각 | /설정변경주제 | /설정변경기준시각 | /설정변경참여일 | /설정변경접두사 | /설정변경지각비 | /설정변경결석비\n"
        "💸 /벌금현황 /벌금납부 /벌금내역 /벌금수정 /벌금수정취소 /벌금취소 /벌금정산 | ✏️ /출석수정 /출석수정취소"
    ))
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


@bot.tree.command(name="설정변경벌금채널", description="벌금 현황을 발표할 채널을 변경합니다.")
@app_commands.describe(채널="벌금 현황을 올릴 채널")
async def slash_set_fine_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    cfg.set_and_save("fine_channel", 채널.name)
    await interaction.response.send_message(f"✅ 벌금 채널이 {채널.mention} 으로 변경됐어요!")


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

    msg = (
        f"✅ 자동 발표 시각이 **{시:02d}:{분:02d}** 으로 변경됐어요!\n"
        f"매일 출석 현황 발표와 주간 정산 발표가 이 시각에 실행돼요."
    )
    start_hour = cfg.get("day_start_hour")
    if 시 < start_hour:
        # 하루 기준 시각 전에 발표하면 '한 번 더 지난 날'이 발표돼요 (아직 안 끝난 하루는 발표 못 함)
        msg += (
            f"\n\n⚠️ 하루 기준 시각({start_hour}시)보다 이른 시각이에요.\n"
            f"발표는 **완전히 끝난 하루**만 대상으로 하기 때문에, 이 시각에는 하루 더 이전 날짜가 발표돼요.\n"
            f"방금 끝난 하루를 발표하려면 **{start_hour:02d}:00 이후**로 설정해주세요."
        )
    await interaction.response.send_message(msg)


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


@bot.tree.command(name="참여자일괄등록", description="접두사로 시작하는 채널을 스캔해서 닉네임이 일치하는 멤버를 자동으로 연결합니다.")
async def slash_register_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    channels = get_participant_channels(interaction.guild)
    channel_members: dict = cfg.get("channel_members")
    success_lines = []
    fail_lines = []

    for ch in channels:
        member_name = get_member_name_from_channel(ch)
        member = discord.utils.find(
            lambda m: m.display_name == member_name or m.name == member_name,
            interaction.guild.members
        )
        if member:
            channel_members[ch.name] = member.id
            success_lines.append(f"✅ #{ch.name} → {member.mention}")
        else:
            fail_lines.append(f"❌ #{ch.name}")

    cfg.set_and_save("channel_members", channel_members)

    embed = discord.Embed(title="👥 참여자 일괄 등록 결과", color=0x2ecc71 if not fail_lines else 0xe67e22)
    if success_lines:
        embed.add_field(name=f"✅ 자동 등록 ({len(success_lines)}명)", value="\n".join(success_lines), inline=False)
    if fail_lines:
        embed.add_field(name=f"❌ 매핑 실패 ({len(fail_lines)}명)", value="\n".join(fail_lines) + "\n\n`/참여자등록` 으로 직접 연결해주세요.", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


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
# 슬래시 커맨드 — 벌금 관리
# =====================================================

UNREGISTERED_MSG = "❗ {name} 님은 등록된 참여자가 아니에요. `/참여자등록`으로 채널과 연결해주세요."
DATE_FORMAT_MSG = "❗ 날짜 형식이 올바르지 않아요. `YYYY-MM-DD` 형식으로 입력해주세요."


def _parse_week_key(주차: str):
    """'YYYY-MM-DD' → 해당 주 월요일 date. 실패 시 None."""
    try:
        return store.week_monday(datetime.strptime(주차, "%Y-%m-%d").date())
    except ValueError:
        return None


@bot.tree.command(name="벌금현황", description="현재 미납 벌금 현황을 보여줍니다.")
async def slash_fine_status(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_fine_summary_embed(interaction.guild))


@bot.tree.command(name="벌금납부", description="벌금 납부를 기록합니다. 주차 생략 시 해당 멤버의 미납 전체를 정리합니다.")
@app_commands.describe(멤버="납부한 참여자", 주차="납부할 주의 날짜 (YYYY-MM-DD, 그 주 아무 날짜나 가능. 생략 시 미납 전체)")
async def slash_fine_pay(interaction: discord.Interaction, 멤버: discord.Member, 주차: str = None):
    name = get_participant_name(멤버)
    if name is None:
        await interaction.response.send_message(UNREGISTERED_MSG.format(name=멤버.display_name), ephemeral=True)
        return

    target_key = None
    if 주차:
        monday = _parse_week_key(주차)
        if monday is None:
            await interaction.response.send_message(DATE_FORMAT_MSG, ephemeral=True)
            return
        target_key = monday.isoformat()

    today_iso = datetime.now(TZ).date().isoformat()
    cleared = []
    for week_key in sorted(store.get_all_fines().keys()):
        if target_key and week_key != target_key:
            continue
        rec = store.get_member_fine(week_key, name)
        if rec is None or rec.get("paid") or rec.get("amount", 0) <= 0:
            continue
        rec["paid"] = True
        rec["paid_at"] = today_iso
        store.set_member_fine(week_key, name, rec)
        cleared.append((week_key, rec["amount"]))

    if not cleared:
        if target_key:
            await interaction.response.send_message(f"❗ **{name}** 님은 {format_week_label(target_key)}에 미납 벌금이 없어요.", ephemeral=True)
        else:
            await interaction.response.send_message(f"🎉 **{name}** 님은 미납 벌금이 없어요!", ephemeral=True)
        return

    total = sum(a for _, a in cleared)
    remaining = get_outstanding_fines().get(name, {}).get("amount", 0)
    lines = [f"• {format_week_label(k)} — {a:,}원" for k, a in cleared]

    embed = discord.Embed(title=f"💰 {name} 벌금 납부 완료", description="\n".join(lines), color=0x2ecc71)
    embed.add_field(name="납부 금액", value=f"**{total:,}원**", inline=True)
    embed.add_field(name="남은 미납", value=f"{remaining:,}원" if remaining else "없음 🎉", inline=True)
    embed.timestamp = datetime.now(TZ)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="벌금내역", description="벌금 발생/납부 전체 내역을 보여줍니다. 멤버 생략 시 전원 요약.")
@app_commands.describe(멤버="조회할 참여자 (생략 시 전원 요약)")
async def slash_fine_history(interaction: discord.Interaction, 멤버: discord.Member = None):
    all_fines = store.get_all_fines()

    if 멤버 is not None:
        name = get_participant_name(멤버)
        if name is None:
            await interaction.response.send_message(UNREGISTERED_MSG.format(name=멤버.display_name), ephemeral=True)
            return

        lines = []
        unpaid_total = 0
        paid_total = 0
        for week_key in sorted(all_fines.keys()):
            rec = all_fines[week_key].get(name)
            if rec is None:
                continue
            amount = rec.get("amount", 0)
            edited_mark = " (수정됨)" if rec.get("edited") else ""
            if rec.get("paid"):
                paid_total += amount
                lines.append(f"• {format_week_label(week_key)} — {amount:,}원{edited_mark} ✅ 납부 ({rec.get('paid_at') or '-'})")
            else:
                unpaid_total += amount
                lines.append(f"• {format_week_label(week_key)} — {amount:,}원{edited_mark} ❌ 미납")

        if not lines:
            await interaction.response.send_message(f"📭 **{name}** 님은 벌금 기록이 없어요!", ephemeral=True)
            return

        total = unpaid_total + paid_total
        lines.append("")
        lines.append(f"**미납 {unpaid_total:,}원 / 누적 납부 {paid_total:,}원 / 총 발생 {total:,}원**")
        embed = discord.Embed(title=f"💸 {name} 벌금 내역", description="\n".join(lines), color=0x3498db)
        embed.set_footer(text="✅ 납부는 /벌금취소 · (수정됨)은 /벌금수정취소 로 되돌릴 수 있어요")
    else:
        # 전원 요약
        per_member = {}  # 이름 → [미납, 납부]
        for week_key, members in all_fines.items():
            for name, rec in members.items():
                amount = rec.get("amount", 0)
                sums = per_member.setdefault(name, [0, 0])
                if rec.get("paid"):
                    sums[1] += amount
                else:
                    sums[0] += amount

        if not per_member:
            await interaction.response.send_message("📭 아직 벌금 기록이 없어요!", ephemeral=True)
            return

        lines = [
            f"• {name} — 미납 {u:,}원 / 납부 {p:,}원 / 총 {u + p:,}원"
            for name, (u, p) in sorted(per_member.items(), key=lambda x: (-x[1][0], x[0]))
        ]
        total_unpaid = sum(u for u, _ in per_member.values())
        total_paid   = sum(p for _, p in per_member.values())
        lines.append("")
        lines.append(f"**전체 미납 {total_unpaid:,}원 / 누적 납부 {total_paid:,}원 / 총 발생 {total_unpaid + total_paid:,}원**")
        embed = discord.Embed(title="💸 전체 벌금 내역 요약", description="\n".join(lines), color=0x3498db)

    embed.set_footer(text="/벌금납부 로 납부 처리 · /벌금내역 멤버:@이름 으로 상세 조회")
    embed.timestamp = datetime.now(TZ)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="벌금취소", description="잘못 기록된 벌금 납부를 취소합니다. (다시 미납 상태로)")
@app_commands.describe(멤버="납부를 취소할 참여자", 주차="해당 주의 날짜 (YYYY-MM-DD, 그 주 아무 날짜나 가능)")
async def slash_fine_unpay(interaction: discord.Interaction, 멤버: discord.Member, 주차: str):
    name = get_participant_name(멤버)
    if name is None:
        await interaction.response.send_message(UNREGISTERED_MSG.format(name=멤버.display_name), ephemeral=True)
        return
    monday = _parse_week_key(주차)
    if monday is None:
        await interaction.response.send_message(DATE_FORMAT_MSG, ephemeral=True)
        return

    rec = store.get_member_fine(monday, name)
    label = format_week_label(monday)
    if rec is None:
        await interaction.response.send_message(f"❗ **{name}** 님은 {label}에 벌금 기록이 없어요.", ephemeral=True)
        return
    if not rec.get("paid"):
        await interaction.response.send_message(f"❗ **{name}** 님의 {label} 벌금은 아직 납부 처리되지 않았어요.", ephemeral=True)
        return

    rec["paid"] = False
    rec["paid_at"] = None
    store.set_member_fine(monday, name, rec)
    await interaction.response.send_message(
        f"↩️ **{name}** 님의 {label} 납부 기록을 취소했어요. ({rec.get('amount', 0):,}원이 다시 미납으로 돌아갔어요)"
    )


@bot.tree.command(name="벌금수정", description="특정 주의 벌금 금액을 직접 수정합니다. (정산 재계산에서 보호됨)")
@app_commands.describe(멤버="수정할 참여자", 주차="해당 주의 날짜 (YYYY-MM-DD)", 금액="새 벌금 금액 (원)", 사유="수정 사유 (선택)")
async def slash_fine_edit(interaction: discord.Interaction, 멤버: discord.Member, 주차: str, 금액: int, 사유: str = None):
    name = get_participant_name(멤버)
    if name is None:
        await interaction.response.send_message(UNREGISTERED_MSG.format(name=멤버.display_name), ephemeral=True)
        return
    monday = _parse_week_key(주차)
    if monday is None:
        await interaction.response.send_message(DATE_FORMAT_MSG, ephemeral=True)
        return
    if 금액 < 0:
        await interaction.response.send_message("❗ 0 이상의 숫자를 입력해주세요.", ephemeral=True)
        return

    rec = store.get_member_fine(monday, name)
    if rec is None:
        rec = _make_fine_record(0, 0, 0)
    old_amount = rec.get("amount", 0)
    rec["amount"] = 금액
    rec["edited"] = True
    rec["edit_reason"] = 사유
    store.set_member_fine(monday, name, rec)

    embed = discord.Embed(title="✏️ 벌금 수정 완료", color=0x2ecc71)
    embed.add_field(name="멤버", value=name, inline=True)
    embed.add_field(name="주차", value=format_week_label(monday), inline=True)
    embed.add_field(name="금액", value=f"{old_amount:,}원 → **{금액:,}원**", inline=True)
    if 사유:
        embed.add_field(name="사유", value=사유, inline=False)
    embed.set_footer(text="수정된 주는 정산 재계산 때 금액이 덮어써지지 않아요 (/벌금수정취소 로 되돌릴 수 있어요)")
    await interaction.response.send_message(embed=embed)


async def _judge_member_week_now(guild: discord.Guild, name: str, monday):
    """
    해당 참여자의 특정 주 판정을 지금 기준으로 다시 계산.
    참여자 채널을 못 찾으면 None 반환.
    """
    channel = discord.utils.get(guild.text_channels, name=cfg.get("channel_prefix") + name)
    if channel is None:
        return None

    today = get_challenge_date()
    ref = min(today, monday + timedelta(days=6))
    week_dates = get_week_dates(ref)
    if not week_dates:
        return {}

    rest_exempt = await get_rest_exempt_dates(guild, cfg.get("rest_channel"), week_dates[0])
    scan = await scan_channel(channel, week_dates)
    judge_counts = build_judge_counts(scan["daily_counts"], name)

    overrides = {}
    for d in week_dates:
        ov = store.get_override(d, name)
        if ov is not None:
            overrides[d] = ov

    return judge_member_week(
        week_dates, judge_counts, rest_exempt.get(name, []), scan["preupload_exempt"], overrides
    )


@bot.tree.command(name="벌금수정취소", description="수동으로 수정한 벌금을 취소하고 자동 계산으로 되돌립니다.")
@app_commands.describe(멤버="수정을 취소할 참여자", 주차="해당 주의 날짜 (YYYY-MM-DD, 그 주 아무 날짜나 가능)")
async def slash_fine_edit_cancel(interaction: discord.Interaction, 멤버: discord.Member, 주차: str):
    name = get_participant_name(멤버)
    if name is None:
        await interaction.response.send_message(UNREGISTERED_MSG.format(name=멤버.display_name), ephemeral=True)
        return
    monday = _parse_week_key(주차)
    if monday is None:
        await interaction.response.send_message(DATE_FORMAT_MSG, ephemeral=True)
        return

    rec = store.get_member_fine(monday, name)
    label = format_week_label(monday)
    if rec is None:
        await interaction.response.send_message(f"❗ **{name}** 님은 {label}에 벌금 기록이 없어요.", ephemeral=True)
        return
    if not rec.get("edited"):
        await interaction.response.send_message(
            f"❗ **{name}** 님의 {label} 벌금은 수동 수정된 기록이 아니에요. 되돌릴 게 없어요.", ephemeral=True
        )
        return
    if rec.get("paid"):
        await interaction.response.send_message(
            f"❗ **{name}** 님의 {label} 벌금은 이미 납부 완료 상태예요.\n"
            f"`/벌금취소` 로 납부를 먼저 되돌린 뒤에 다시 시도해주세요. "
            f"(이미 받은 돈이 재계산으로 바뀌면 안 되니까요)",
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    status = await _judge_member_week_now(interaction.guild, name, monday)
    if status is None:
        await interaction.followup.send(f"❗ **{name}** 님의 참여자 채널을 찾을 수 없어요.", ephemeral=True)
        return

    old_amount = rec.get("amount", 0)
    old_reason = rec.get("edit_reason")
    late, absent, new_amount = count_fine_from_status(status)
    # edited/paid가 모두 False인 새 레코드로 교체 → 다시 자동 계산 대상이 돼요
    store.set_member_fine(monday, name, _make_fine_record(late, absent, new_amount))

    embed = discord.Embed(title="↩️ 벌금 수정 취소 완료", color=0x2ecc71)
    embed.add_field(name="멤버", value=name, inline=True)
    embed.add_field(name="주차", value=label, inline=True)
    embed.add_field(name="금액", value=f"{old_amount:,}원 → **{new_amount:,}원**", inline=True)
    if old_reason:
        embed.add_field(name="취소된 수정 사유", value=old_reason, inline=False)
    embed.add_field(name="자동 판정", value=f"지각 {late}회 / 결석 {absent}회", inline=False)
    embed.set_footer(text="이 주는 다시 자동 계산 대상이에요 — /출석수정 하면 금액에 바로 반영돼요")
    embed.timestamp = datetime.now(TZ)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="벌금정산", description="지난 주의 벌금을 수동으로 정산해 원장에 기록합니다. (기능 도입 전 과거 주 백필용)")
@app_commands.describe(주차="정산할 주의 날짜 (YYYY-MM-DD, 그 주 아무 날짜나 가능)")
async def slash_fine_settle(interaction: discord.Interaction, 주차: str):
    monday = _parse_week_key(주차)
    if monday is None:
        await interaction.response.send_message(DATE_FORMAT_MSG, ephemeral=True)
        return
    challenge_days = cfg.get("challenge_days")
    if not challenge_days:
        await interaction.response.send_message("❗ 챌린지 참여일이 설정되어 있지 않아요.", ephemeral=True)
        return

    # 해당 주의 정산 시점(마지막 챌린지 요일 다음날의 발표 시각)이 지났는지 확인
    now = datetime.now(TZ)
    today = get_challenge_date(now)
    settle_weekday = (max(challenge_days) + 1) % 7
    target = monday + timedelta(days=settle_weekday)  # 정산 기준 챌린지 날짜
    if today < target or (
        today == target and (now.hour, now.minute) < (cfg.get("auto_report_hour"), cfg.get("auto_report_minute"))
    ):
        await interaction.response.send_message(
            "❗ 아직 끝나지 않은 주예요. 이번 주는 자동 정산을 기다려주세요!", ephemeral=True
        )
        return

    await interaction.response.defer()

    # 스냅샷 기록과 last_settled_week은 건드리지 않아요 —
    # 지금 스냅샷을 찍으면 이미 왜곡됐을 수 있는 값이 동결되고,
    # 정산 주를 옮기면 자동 정산이 빠지거나 중복될 수 있어서예요.
    results, week_dates = await calc_weekly_result(interaction.guild, target)
    report_embed = build_weekly_report(results, week_dates)

    # 원장 기록 (납부/수정 완료 기록은 settle_week_fines가 알아서 동결)
    before = store.get_week_fines(monday)
    settle_week_fines(monday, results)

    lines = []
    for name, status in results.items():
        _, _, amount = count_fine_from_status(status)
        existing = before.get(name)
        if existing and (existing.get("paid") or existing.get("edited")):
            reason = "납부 완료" if existing.get("paid") else "수동 수정"
            lines.append(f"• {name} — {existing.get('amount', 0):,}원 유지 (이미 {reason}된 기록이라 건너뜀)")
        elif amount > 0:
            lines.append(f"• {name} — {amount:,}원 기록")

    ledger_embed = discord.Embed(
        title=f"🧾 {format_week_label(monday)} 벌금 원장 기록",
        description="\n".join(lines) if lines else "🎉 이 주는 벌금 대상이 없어요!",
        color=0x9b59b6,
    )
    ledger_embed.add_field(
        name="⚠️ 참고",
        value=(
            "과거 주는 지금 채널에 남아 있는 이미지 기준으로 판정돼요.\n"
            "지웠다가 나중에 다시 올린 이미지는 다시 올린 날짜로 집계되니, "
            "그런 날은 `/출석수정`으로 바로잡아주세요."
        ),
        inline=False,
    )
    ledger_embed.timestamp = datetime.now(TZ)

    # 백필 작업이라 공개 채널을 시끄럽게 하지 않고 이 자리에서 바로 답해요
    await interaction.followup.send(embeds=[report_embed, ledger_embed])


# =====================================================
# 슬래시 커맨드 — 출석 수동 보정
# =====================================================

async def _apply_attendance_override(interaction: discord.Interaction, 멤버: discord.Member, 날짜: str, new_status):
    """출석 수동 보정 적용/해제 공통 처리. new_status가 None이면 보정 해제."""
    await interaction.response.defer()

    name = get_participant_name(멤버)
    if name is None:
        await interaction.followup.send(UNREGISTERED_MSG.format(name=멤버.display_name), ephemeral=True)
        return
    try:
        date = datetime.strptime(날짜, "%Y-%m-%d").date()
    except ValueError:
        await interaction.followup.send(DATE_FORMAT_MSG, ephemeral=True)
        return
    if date.weekday() not in cfg.get("challenge_days"):
        await interaction.followup.send("❗ 해당 날짜는 챌린지 요일이 아니에요.", ephemeral=True)
        return
    today = get_challenge_date()
    if date > today:
        await interaction.followup.send("❗ 아직 지나지 않은 날짜는 보정할 수 없어요.", ephemeral=True)
        return

    old_override = store.get_override(date, name)
    if new_status is None and old_override is None:
        await interaction.followup.send(f"❗ **{name}** 님의 {날짜} 에는 보정 기록이 없어요.", ephemeral=True)
        return

    channel = discord.utils.get(interaction.guild.text_channels, name=cfg.get("channel_prefix") + name)
    if channel is None:
        await interaction.followup.send(f"❗ **{name}** 님의 참여자 채널을 찾을 수 없어요.", ephemeral=True)
        return

    monday = store.week_monday(date)
    ref = min(today, monday + timedelta(days=6))
    week_dates = get_week_dates(ref)

    # 보정 전/후 판정 비교 (해당 멤버 채널만 스캔)
    rest_exempt = await get_rest_exempt_dates(interaction.guild, cfg.get("rest_channel"), week_dates[0] if week_dates else date)
    scan = await scan_channel(channel, week_dates)
    judge_counts = build_judge_counts(scan["daily_counts"], name)
    member_rest_dates = rest_exempt.get(name, [])

    base_overrides = {}
    for d in week_dates:
        ov = store.get_override(d, name)
        if ov is not None:
            base_overrides[d] = ov

    old_status_map = judge_member_week(week_dates, judge_counts, member_rest_dates, scan["preupload_exempt"], base_overrides)

    new_overrides = dict(base_overrides)
    if new_status is None:
        new_overrides.pop(date, None)
    else:
        new_overrides[date] = new_status
    new_status_map = judge_member_week(week_dates, judge_counts, member_rest_dates, scan["preupload_exempt"], new_overrides)

    # 저장
    if new_status is None:
        store.remove_override(date, name)
    else:
        store.set_override(date, name, new_status)

    # 정산된 주라면 벌금 원장에도 반영
    ledger_result = update_member_ledger(monday, name, new_status_map)
    ledger_msg = {
        "unsettled": "이 주는 아직 정산 전이라, 정산 때 자동으로 반영돼요.",
        "frozen": "이미 납부/수정된 주라서 벌금 원장 금액은 그대로 뒀어요.",
        "updated": "벌금 원장에 바로 반영했어요.",
    }[ledger_result]

    _, _, old_amount = count_fine_from_status(old_status_map)
    _, _, new_amount = count_fine_from_status(new_status_map)
    before = old_status_map.get(date, "결석")
    after  = new_status_map.get(date, "결석")

    embed = discord.Embed(
        title="✅ 출석 보정 완료" if new_status else "↩️ 출석 보정 해제 완료",
        color=0x2ecc71,
    )
    embed.add_field(name="멤버", value=name, inline=True)
    embed.add_field(name="날짜", value=날짜, inline=True)
    embed.add_field(name="상태", value=f"{before} → **{after}**", inline=True)
    embed.add_field(name="해당 주 벌금", value=f"{old_amount:,}원 → **{new_amount:,}원**\n{ledger_msg}", inline=False)
    embed.timestamp = datetime.now(TZ)
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="출석수정", description="특정 날짜의 출석 판정을 수동으로 보정합니다. (자동 판정보다 우선)")
@app_commands.describe(멤버="보정할 참여자", 날짜="보정할 날짜 (YYYY-MM-DD)", 상태="적용할 상태")
@app_commands.choices(상태=[
    app_commands.Choice(name="정상", value="정상"),
    app_commands.Choice(name="지각", value="지각"),
    app_commands.Choice(name="결석", value="결석"),
    app_commands.Choice(name="휴식", value="휴식"),
    app_commands.Choice(name="선업로드", value="선업로드"),
])
async def slash_attendance_edit(interaction: discord.Interaction, 멤버: discord.Member, 날짜: str, 상태: app_commands.Choice[str]):
    await _apply_attendance_override(interaction, 멤버, 날짜, 상태.value)


@bot.tree.command(name="출석수정취소", description="출석 수동 보정을 제거하고 자동 판정으로 되돌립니다.")
@app_commands.describe(멤버="보정을 취소할 참여자", 날짜="보정을 취소할 날짜 (YYYY-MM-DD)")
async def slash_attendance_edit_cancel(interaction: discord.Interaction, 멤버: discord.Member, 날짜: str):
    await _apply_attendance_override(interaction, 멤버, 날짜, None)


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
    snapshot_task.start()
    midnight_reminder_task.start()
    weekly_settlement_task.start()
    h = cfg.get('auto_report_hour')
    m = cfg.get('auto_report_minute')
    print(f"✅ 자동 출석 발표: 매일 {h:02d}:{m:02d}")
    print(f"✅ 자정 미참여 알림: 매일 00:00")
    print(f"✅ 일일 기록 동결: 매일 {cfg.get('day_start_hour'):02d}:00 이후 (하루가 끝난 뒤)")
    print(f"✅ 주간 정산 자동 발표: 매주 마지막 챌린지 요일 다음날 {h:02d}:{m:02d} (놓친 주는 재시작 후 보정 실행)")


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
        await handle_rest_message(message)


REST_FORMAT_HINT = "형식: `[2026-09-01]` 또는 `[2026-09-01 ~ 2026-09-03]`"


async def handle_rest_message(message: discord.Message, edited: bool = False):
    """
    휴식 신청 메시지 검증 → ☑️(정상) 또는 ❌(무효) 리액션.
    무효한 신청은 벌금 면제가 안 되므로, 이유를 답글로 알려줘요.
    (연도 오타로 몇 년짜리 휴식이 조용히 등록되던 사고 방지)
    """
    dates, error = parse_rest_dates_checked(message.content or "")

    async def _react(add: str, remove: str):
        try:
            await message.add_reaction(add)
        except (discord.Forbidden, discord.HTTPException):
            pass
        if edited:  # 수정으로 상태가 바뀌었으면 이전 표시 제거
            try:
                await message.remove_reaction(remove, bot.user)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

    if error:
        await _react("❌", "☑️")
        try:
            await message.reply(
                f"⚠️ **휴식 신청이 등록되지 않았어요.**\n{error}\n{REST_FORMAT_HINT}",
                mention_author=True,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    elif dates:
        await _react("☑️", "❌")

    await bot.process_commands(message)


# =====================================================
# 실행
# =====================================================

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """휴식 신청을 고쳤을 때 다시 검증. (오타 수정 후 바로 ☑️로 바뀌도록)"""
    if after.author.bot or not after.guild:
        return
    if after.channel.name != cfg.get("rest_channel"):
        return
    if (before.content or "") == (after.content or ""):
        return
    await handle_rest_message(after, edited=True)


if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN 환경변수가 없습니다. .env 파일을 확인하세요.")
    else:
        bot.run(TOKEN)