# 🎨 크로키 챌린지 출석 봇

디스코드 그림 공부 챌린지의 참여 여부를 자동으로 확인해주는 봇입니다.

---

## 📁 파일 구조

```
croquis_bot/
├── bot.py          # 봇 메인 코드
├── config.py       # 설정 파일 (여기서 모든 설정 변경)
├── .env            # 봇 토큰 (직접 생성 필요)
├── .env.example    # .env 샘플
└── requirements.txt
```

---

## 🚀 실행 방법

### 1. 패키지 설치
```bash
pip install -r requirements.txt
```

### 2. 봇 토큰 설정
```bash
cp .env.example .env
# .env 파일을 열어서 DISCORD_TOKEN= 뒤에 봇 토큰 입력
```

Discord Developer Portal → 봇 페이지 → **Token** → Copy

### 3. 봇 권한 설정 (Developer Portal)
Bot 탭에서 아래 **Privileged Gateway Intents** 활성화:
- ✅ `MESSAGE CONTENT INTENT`
- ✅ `SERVER MEMBERS INTENT` (선택)

### 4. 봇 서버 초대
OAuth2 → URL Generator에서 아래 권한 체크 후 초대 링크 생성:
- Scopes: `bot`, `applications.commands`
- Bot Permissions: `Read Messages/View Channels`, `Read Message History`, `Send Messages`, `Embed Links`

### 5. 실행
```bash
python bot.py
```

---

## ⚙️ 설정 변경 (`config.py`)

| 항목 | 변수명 | 기본값 | 설명 |
|------|--------|--------|------|
| 하루 기준 시각 | `DAY_START_HOUR` | `6` | 이 시각 이전은 전날로 처리 |
| 자동 발표 시각 | `AUTO_REPORT_HOUR` / `AUTO_REPORT_MINUTE` | `23:50` | 매일 자동 발표 시각 |
| 참여일 | `CHALLENGE_DAYS` | `[0,1,2,3,4]` (월~금) | 0=월 … 6=일 |
| 채널 접두사 | `CHANNEL_PREFIX` | `"크로키-"` | 참여자 채널 이름의 공통 앞부분 |
| 출석 채널 | `ATTENDANCE_CHANNEL_NAME` | `"출석체크"` | 결과를 보낼 채널 이름 |
| 타임존 | `TIMEZONE` | `"Asia/Seoul"` | pytz 형식 |

---

## 💬 슬래시 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/출석확인` | 오늘의 출석 현황 조회 |
| `/출석확인 날짜:2025-06-10` | 특정 날짜 출석 현황 조회 |
| `/채널목록` | 현재 감지된 참여자 채널 목록 |
| `/설정확인` | 현재 봇 설정 확인 |

---

## 🗂️ 채널 이름 규칙

`config.py`의 `CHANNEL_PREFIX`에 설정한 접두사로 시작하는 채널을 자동으로 감지합니다.

```
# 기본 설정: CHANNEL_PREFIX = "크로키-"
크로키-진아   →  참여자: 진아
크로키-민지   →  참여자: 민지
크로키-수현   →  참여자: 수현
```

접두사를 바꾸면 다른 이름 규칙에도 대응 가능합니다.  
예: `"드로잉-"`, `"study-"` 등

---

## 📋 출석 판정 기준

- 해당 챌린지 날짜 범위 내에 **이미지 파일 첨부** 메시지가 있으면 ✅ 참여
- 없으면 ❌ 미참여
- 토/일(휴식일)이면 별도 휴식일 메시지 표시

> **날짜 범위 예시** (DAY_START_HOUR=6):  
> 6월 10일(월) = 6월 10일 06:00 ~ 6월 11일 05:59
