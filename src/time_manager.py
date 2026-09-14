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


def get_previous_trading_day_str(date_str: str = None) -> str:
    """
    計算給定台灣日期的前一個交易日（YYYY-MM-DD）。
    優先自資料庫 (daily_analysis) 查詢實際開盤交易日，
    自動適應週末、颱風假、國定連假與春節等任何長短休市情境；
    若無資料庫連線或查詢失敗，則回退至星期規則推算。
    """
    tz = get_taiwan_timezone()
    if date_str:
        dt = datetime.fromisoformat(date_str).replace(tzinfo=tz)
    else:
        dt = get_local_taiwan_datetime()
    curr_date_str = dt.strftime("%Y-%m-%d")

    # 1. 優先嘗試從資料庫讀取真實歷史交易日（能 100% 覆蓋颱風假與國定長假）
    try:
        from src.services.supabase_client import supabase, execute_with_retry
        res = execute_with_retry(
            lambda: supabase.table("daily_analysis")
            .select("analysis_date")
            .lt("analysis_date", curr_date_str)
            .order("analysis_date", desc=True)
            .limit(1)
            .execute()
        )
        # execute_with_retry 直接回傳 response.data
        if isinstance(res, list) and res and res[0].get("analysis_date"):
            return res[0]["analysis_date"]
    except Exception:
        pass

    # 2. 回退：依標準日曆與週末規則推算
    weekday = dt.weekday()
    if weekday == 0:  # 週一 -> 上週五
        prev_dt = dt - timedelta(days=3)
    elif weekday == 6:  # 週日 -> 上週五
        prev_dt = dt - timedelta(days=2)
    elif weekday == 5:  # 週六 -> 上週五
        prev_dt = dt - timedelta(days=1)
    else:  # 週二至週五 -> 前一天
        prev_dt = dt - timedelta(days=1)
    return prev_dt.strftime("%Y-%m-%d")


def get_report_lookback_range(date_str: str = None) -> tuple[str, str]:
    """
    計算每日報告（Discord 與 Markdown）所需的資料庫查詢 UTC 起訖時間 (start_utc, end_utc)。
    - 交易週期起點錨定在「前一營業日的盤後 14:00:00 (台灣時間)」，能精準捕捉該日盤後產生的次日預約委託單。
    - 終點為「當日 23:59:59 (台灣時間)」。
    - 透過 get_previous_trading_day_str 動態解析前一交易日，無論是跨週末、遇颱風休市或國定長假，
      皆能準確將起點錨定在最近一個有效交易日的 14:00:00，確保次日開盤成交之訂單能 100% 被捕獲。
    - 若當日為週末（週六或週日），代表非交易日產出的報告，此時自動將報告基準日錨定至「最新營業日（即上週五）」，
      其起點延伸至週四 14:00:00，終點為當日 23:59:59，確保週末手動產生報告時，能完整展現上週五的成交與實現損益。
    """
    tz = get_taiwan_timezone()
    if date_str:
        dt = datetime.fromisoformat(date_str).replace(tzinfo=tz)
    else:
        dt = get_local_taiwan_datetime()

    weekday = dt.weekday()
    # 若當日為週末，報告基準為最近營業日（上週五），因此其前一營業日為週四
    if weekday in (5, 6):
        friday_dt = dt - timedelta(days=1 if weekday == 5 else 2)
        prev_trading_str = get_previous_trading_day_str(friday_dt.strftime("%Y-%m-%d"))
    else:
        prev_trading_str = get_previous_trading_day_str(dt.strftime("%Y-%m-%d"))

    prev_trading_dt = datetime.fromisoformat(prev_trading_str).replace(tzinfo=tz)
    start_local = prev_trading_dt.replace(hour=14, minute=0, second=0, microsecond=0)
    end_local = dt.replace(hour=23, minute=59, second=59, microsecond=999999)

    start_utc = start_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = end_local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return start_utc, end_utc

