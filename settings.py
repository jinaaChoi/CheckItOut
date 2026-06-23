# =====================================================
# 설정 관리 (JSON 파일 기반, 디스코드 내에서 수정 가능)
# =====================================================
import json
import os
import config

SETTINGS_FILE = "settings.json"

# 기본값은 config.py에서 가져옴
DEFAULT_SETTINGS = {
    "attendance_channel": config.ATTENDANCE_CHANNEL_NAME,
    "day_start_hour": config.DAY_START_HOUR,
    "challenge_days": config.CHALLENGE_DAYS,
    "channel_members": {},  # { "크로키-진아": 123456789 (user_id) }
    "channel_prefix": config.CHANNEL_PREFIX,
    "challenge_topic": "크로키",  # 챌린지 주제 이름 (출석/정산 메시지에 표시)
    "fine_late": 1000,    # 지각 벌금 (원)
    "fine_absent": 2000,  # 결석 벌금 (원)
    "weekly_channel": "",  # 주간 정산 채널 (비어있으면 attendance_channel 사용)
    "auto_report_hour": config.AUTO_REPORT_HOUR,
    "auto_report_minute": config.AUTO_REPORT_MINUTE,
}

def load() -> dict:
    """settings.json 로드. 없으면 기본값 반환."""
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 새 키가 추가됐을 경우 기본값으로 보완
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save(data: dict):
    """설정을 settings.json에 저장."""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# 전역 설정 객체 (봇 실행 중 메모리에서 참조)
_cfg = load()

def get(key: str):
    return _cfg.get(key, DEFAULT_SETTINGS.get(key))

def set_and_save(key: str, value):
    _cfg[key] = value
    save(_cfg)

def all_settings() -> dict:
    return dict(_cfg)
