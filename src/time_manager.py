from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from src.config import config


def get_taiwan_timezone():
    """取得目前專案配置所使用的台灣時區物件。"""
    return ZoneInfo(config.timezone)


def get_local_taiwan_datetime() -> datetime:
    """取得目前台灣本地時間，並帶有時區資訊。"""
    tz = get_taiwan_timezone()
    return datetime.now(tz)


def get_local_taiwan_date_str() -> str:
    """取得目前台灣本地日期字串 (YYYY-MM-DD)。"""
    return get_local_taiwan_datetime().strftime("%Y-%m-%d")


def get_local_taiwan_datetime_str() -> str:
    """取得目前台灣本地時間字串 (YYYY-MM-DD HH:MM:SS)。"""
    return get_local_taiwan_datetime().strftime("%Y-%m-%d %H:%M:%S")


def get_utc_now() -> datetime:
    """取得目前 UTC 時間。"""
    return datetime.now(timezone.utc)


def get_utc_today_str() -> str:
    """取得目前 UTC 日期字串 (YYYY-MM-DD)。"""
    return get_utc_now().strftime("%Y-%m-%d")


def get_local_taiwan_midnight_utc_range(date_str: str = None) -> tuple[str, str]:
    """取得指定台灣日期對應的 UTC 起訖區間，方便用於資料庫時間查詢。"""
    if date_str is None:
        date_str = get_local_taiwan_date_str()

    local_tz = get_taiwan_timezone()
    local_date = datetime.fromisoformat(date_str)
    local_start = local_date.replace(tzinfo=local_tz, hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1) - timedelta(microseconds=1)
    utc_start = local_start.astimezone(timezone.utc)
    utc_end = local_end.astimezone(timezone.utc)
    return utc_start.isoformat().replace("+00:00", "Z"), utc_end.isoformat().replace("+00:00", "Z")


def is_sandbox_active() -> bool:
    """判斷目前是否正處於沙盒模擬時間軸模式。"""
    try:
        from src.services.sandbox_simulator import is_simulation_active
        return is_simulation_active()
    except Exception:
        return False


def get_simulation_date() -> str:
    """取得目前沙盒模擬的虛擬日期 (YYYY-MM-DD)。"""
    from src.services.sandbox_simulator import get_current_sim_date
    return get_current_sim_date()


def get_effective_datetime() -> datetime:
    """取得目前應用層次的時間：沙盒模式回傳模擬日期、真實模式回傳台灣本地時間。"""
    if is_sandbox_active():
        sim_date = get_simulation_date()
        tz = get_taiwan_timezone()
        return datetime.fromisoformat(sim_date).replace(tzinfo=tz)
    return get_local_taiwan_datetime()


def get_effective_date_str() -> str:
    """取得目前應用層次的日期字串，沙盒模式回傳虛擬日期。"""
    return get_effective_datetime().strftime("%Y-%m-%d")


def get_effective_datetime_iso() -> str:
    """取得目前應用層次的 ISO 時間字串。"""
    return get_effective_datetime().isoformat()
