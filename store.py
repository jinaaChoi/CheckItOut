# =====================================================
# 영속 데이터 관리 (JSON 파일 기반)
# - attendance_log.json : 일일 출석 스냅샷 (기록 동결용)
# - fines.json          : 주차별 벌금 원장 (키: 해당 주 월요일)
# - overrides.json      : 수동 출석 보정
# - meta.json           : 정산 메타 정보 (마지막 정산 주 등)
# =====================================================
import json
import os
from datetime import datetime, date as date_type, timedelta

ATTENDANCE_LOG_FILE = "attendance_log.json"
FINES_FILE = "fines.json"
OVERRIDES_FILE = "overrides.json"
META_FILE = "meta.json"


def _load(path: str) -> dict:
    """JSON 파일 로드. 없거나 깨져 있으면 빈 dict 반환 (봇이 죽지 않도록)."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save(path: str, data: dict):
    """JSON 파일 저장. 임시 파일에 쓴 뒤 교체해서 저장 도중 문제가 생겨도 원본을 보호해요."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        # 저장 실패로 봇이 죽지 않도록 무시 (다음 저장 때 재시도됨)
        pass


def _iso(d) -> str:
    """date → 'YYYY-MM-DD' 문자열. 이미 문자열이면 그대로."""
    return d.isoformat() if isinstance(d, date_type) else str(d)


def _from_iso(s):
    """'YYYY-MM-DD' 문자열 → date. 실패 시 None."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def week_monday(d: date_type) -> date_type:
    """해당 날짜가 속한 주의 월요일 반환."""
    return d - timedelta(days=d.weekday())


# 모듈 로드 시 파일에서 읽어 메모리에 보관 (settings.py 방식)
_attendance = _load(ATTENDANCE_LOG_FILE)
_fines = _load(FINES_FILE)
_overrides = _load(OVERRIDES_FILE)
_meta = _load(META_FILE)


# =====================================================
# 일일 출석 스냅샷 (attendance_log.json)
# =====================================================

def has_snapshot(date) -> bool:
    """해당 날짜의 스냅샷이 이미 기록됐는지 확인."""
    return _iso(date) in _attendance


def get_snapshot(date):
    """해당 날짜의 스냅샷 { 이름: {"count", "preupload", "rest"} } 반환. 없으면 None."""
    return _attendance.get(_iso(date))


def get_member_snapshot(date, name: str):
    """해당 날짜 특정 참여자의 스냅샷 반환. 없으면 None."""
    day = _attendance.get(_iso(date))
    if day is None:
        return None
    return day.get(name)


def save_snapshot(date, data: dict):
    """
    일일 스냅샷 저장.
    이미 기록된 날짜는 절대 덮어쓰지 않아요 (기록 동결 — 사후 삭제/재업로드 방지).
    """
    key = _iso(date)
    if key in _attendance:
        return
    _attendance[key] = data
    _save(ATTENDANCE_LOG_FILE, _attendance)


# =====================================================
# 벌금 원장 (fines.json)
# =====================================================

def get_week_fines(monday) -> dict:
    """해당 주(월요일 키)의 벌금 기록 { 이름: 레코드 } 반환 (사본)."""
    return dict(_fines.get(_iso(monday), {}))


def set_week_fines(monday, data: dict):
    """해당 주의 벌금 기록 전체 저장."""
    _fines[_iso(monday)] = data
    _save(FINES_FILE, _fines)


def get_member_fine(monday, name: str):
    """해당 주 특정 참여자의 벌금 레코드 반환. 없으면 None."""
    return _fines.get(_iso(monday), {}).get(name)


def set_member_fine(monday, name: str, record: dict):
    """해당 주 특정 참여자의 벌금 레코드 저장."""
    week = _fines.setdefault(_iso(monday), {})
    week[name] = record
    _save(FINES_FILE, _fines)


def has_week_fines(monday) -> bool:
    """해당 주가 이미 정산되어 원장에 존재하는지 확인."""
    return _iso(monday) in _fines


def get_all_fines() -> dict:
    """전체 벌금 원장 { 'YYYY-MM-DD'(월요일): { 이름: 레코드 } } 반환 (사본)."""
    return {w: dict(members) for w, members in _fines.items()}


# =====================================================
# 수동 출석 보정 (overrides.json)
# =====================================================

def get_override(date, name: str):
    """해당 날짜/참여자의 수동 보정 상태 반환. 없으면 None."""
    return _overrides.get(_iso(date), {}).get(name)


def set_override(date, name: str, status: str):
    """수동 보정 저장."""
    day = _overrides.setdefault(_iso(date), {})
    day[name] = status
    _save(OVERRIDES_FILE, _overrides)


def remove_override(date, name: str) -> bool:
    """수동 보정 삭제. 실제로 삭제됐으면 True."""
    key = _iso(date)
    day = _overrides.get(key)
    if not day or name not in day:
        return False
    del day[name]
    if not day:
        del _overrides[key]
    _save(OVERRIDES_FILE, _overrides)
    return True


# =====================================================
# 정산 메타 (meta.json)
# =====================================================

def get_last_settled_week():
    """마지막으로 정산된 주의 월요일 date 반환. 기록 없으면 None."""
    return _from_iso(_meta.get("last_settled_week"))


def set_last_settled_week(monday):
    """마지막 정산 주(월요일) 기록."""
    _meta["last_settled_week"] = _iso(monday)
    _save(META_FILE, _meta)
