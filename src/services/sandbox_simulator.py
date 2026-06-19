# Path: src/services/sandbox_simulator.py
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from zoneinfo import ZoneInfo
from src.config import config
from src.services.supabase_client import get_stock_klines as db_get_klines
from src.services import stock_fetcher

# 沙盒演練與歷史數據模擬器 狀態變數
_simulation_active: bool = False
_current_sim_date: str = "2026-05-01"  # 格式為 YYYY-MM-DD
_sim_start_date: str = "2026-05-01"
_sim_end_date: str = "2026-06-08"
_simulation_start_dt: Optional[datetime] = None
_simulation_end_dt: Optional[datetime] = None
_server_start_dt: Optional[datetime] = None
_simulation_time_scale: float = 1.0
_use_time_scaling: bool = True
_trading_days: List[str] = []
_current_day_idx: int = 0
_attempted_fetches = set()  # 保存已嘗試從網路獲取 (stock_code, YYYY-MM) 的紀錄，避免重複請求



def _as_taiwan_datetime(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str).replace(tzinfo=ZoneInfo(config.timezone))


def _resolve_sim_date_from_wallclock() -> str:
    if not _simulation_active or not _simulation_start_dt or not _simulation_end_dt or not _server_start_dt:
        return _current_sim_date

    now_utc = datetime.now(timezone.utc)
    elapsed = now_utc - _server_start_dt
    scaled_delta = timedelta(seconds=elapsed.total_seconds() * _simulation_time_scale)
    sim_dt = _simulation_start_dt + scaled_delta
    if sim_dt >= _simulation_end_dt:
        return _sim_end_date

    return sim_dt.date().isoformat()


def set_simulation_mode(active: bool) -> None:
    """
    設定是否開啟沙盒模擬演練模式
    """
    global _simulation_active
    _simulation_active = active
    print(f" [模擬器] 已切換模擬模式狀態：{_simulation_active}")

def is_simulation_active() -> bool:
    """
    檢查目前是否處於沙盒模擬演練模式
    """
    # 如果系統配置已切換為實盤模式，則不論記憶體狀態為何，皆強制關閉模擬模式
    if not config.limits.is_paper_trading:
        global _simulation_active
        if _simulation_active:
            _simulation_active = False
            print(" [模擬器] 偵測到交易模式已切換為實盤，自動將模擬模式設為 False。")
        return False
    return _simulation_active

def get_current_day_index() -> int:
    """返回已處理的當前模擬交易日索引"""
    return _current_day_idx

def get_current_target_day_index() -> int:
    """根據真實時間與比例返回應該達到的模擬交易日索引"""
    if not _simulation_active or not _trading_days:
        return _current_day_idx

    target_date = _resolve_sim_date_from_wallclock()
    valid_days = [d for d in _trading_days if d <= target_date]
    if valid_days:
        return len(valid_days) - 1
    return 0

def has_reached_simulation_end() -> bool:
    """檢查是否已經到達模擬結束時間且已處理最後一個交易日"""
    if not _simulation_active or not _simulation_start_dt or not _simulation_end_dt or not _server_start_dt:
        return False

    now_utc = datetime.now(timezone.utc)
    elapsed = now_utc - _server_start_dt
    scaled_delta = timedelta(seconds=elapsed.total_seconds() * _simulation_time_scale)
    sim_dt = _simulation_start_dt + scaled_delta
    return sim_dt >= _simulation_end_dt and _current_day_idx >= len(_trading_days) - 1

def get_current_sim_date() -> str:
    """
    獲取目前已處理的模擬時間軸日期
    """
    return _current_sim_date

def initialize_simulation(start_date: str, end_date: str, trading_days: List[str], scale: float = 1.0) -> None:
    """
    初始化沙盒演練參數與時間軸
    :param start_date: 模擬開始日期 YYYY-MM-DD
    :param end_date: 模擬結束日期 YYYY-MM-DD
    :param trading_days: 包含該區間所有台股交易日的有序字串列表
    :param scale: 模擬時間與真實時間的比例 (1.0 = 1 秒真實時間 = 1 秒模擬時間)
    """
    global _sim_start_date, _sim_end_date, _current_sim_date, _simulation_start_dt, _simulation_end_dt
    global _server_start_dt, _simulation_time_scale, _trading_days, _current_day_idx, _simulation_active

    _sim_start_date = start_date
    _sim_end_date = end_date
    _trading_days = sorted(list(set(trading_days)))  # 確保有序且無重複
    _current_day_idx = 0
    _simulation_time_scale = max(float(scale), 0.0) if scale is not None else 1.0
    _simulation_start_dt = _as_taiwan_datetime(start_date)
    _simulation_end_dt = _as_taiwan_datetime(end_date)
    _server_start_dt = datetime.now(timezone.utc)

    _current_sim_date = _trading_days[0] if _trading_days else start_date
    _simulation_active = True
    print(
        f" [模擬器] 沙盒初始化完成。區間: {start_date} 至 {end_date}，" \
        f"共 {_trading_days} 個交易日。目前模擬日期: {_current_sim_date}，"
        f"時間比例: {_simulation_time_scale}x"
    )

def advance_simulation_step() -> Optional[str]:
    """
    將模擬時間軸前進一個交易日
    :returns: 前進後的日期字串 (YYYY-MM-DD)，若已達結束日期則返回 None
    """
    global _current_day_idx, _current_sim_date
    if not _trading_days:
        return None
        
    if _current_day_idx < len(_trading_days) - 1:
        _current_day_idx += 1
        _current_sim_date = _trading_days[_current_day_idx]
        print(f" [模擬器] 時間前進至下一個交易日: {_current_sim_date}")
        return _current_sim_date
    
    print(" [模擬器] 模擬已達到設定的結束日期")
    return None

# ==========================================================================
# 暴露與 stock_fetcher 一致的 API 接口，實現真實與模擬數據窗口無縫切換
# ==========================================================================

def fetch_stock_klines(stock_code: str, date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    模擬獲取指定股票的歷史 K 線數據（資料模擬窗口介面）。
    在模擬模式下，將只返回小於等於目前模擬日期的歷史 K 線以防未來函數 (Look-ahead bias) 發生。
    """
    if not _simulation_active:
        return stock_fetcher.fetch_stock_klines(stock_code, date_str)

    # 模擬模式：自資料庫載入所有已存 K 線，並根據模擬日期做篩選
    try:
        # 取得充足數量的 K 線，例如前 200 筆
        klines = db_get_klines(stock_code, limit=200)
        
        # 檢查資料庫中是否有目前模擬日期的 K 線數據。如果沒有，主動嘗試從網路補建
        has_sim_date = any(k["date"] == _current_sim_date for k in klines)
        if not has_sim_date:
            month_str = _current_sim_date[:7]  # 例如 "2026-05"
            fetch_key = (stock_code, month_str)
            global _attempted_fetches
            if fetch_key not in _attempted_fetches:
                _attempted_fetches.add(fetch_key)
                print(f" [模擬器] 偵測到資料庫中缺乏 {stock_code} 在模擬日期 {_current_sim_date} 的 K 線，嘗試從證交所網路抓取...")
                try:
                    # 轉化日期格式 YYYY-MM-DD -> YYYYMMDD
                    api_date = _current_sim_date.replace("-", "")
                    if stock_code == "TAIEX":
                        fetched = stock_fetcher.fetch_taiex_klines(api_date)
                    else:
                        fetched = stock_fetcher.fetch_stock_klines(stock_code, api_date)
                        
                    if fetched:
                        from src.services.supabase_client import save_stock_klines
                        save_stock_klines(fetched)
                        print(f" [模擬器] 成功從網路補建 {stock_code} 的 K 線數據（共 {len(fetched)} 筆）並寫入資料庫")
                        # 重新載入
                        klines = db_get_klines(stock_code, limit=200)
                    else:
                        print(f" [模擬器] 證交所 API 未能返回 {stock_code} 在 {api_date} 所在的月 K 線數據。")
                except Exception as fetch_err:
                    print(f" [模擬器] 警告: 嘗試從網路補建 {stock_code} 的 K 線時出錯（將繼續使用已有資料）: {fetch_err}")

        # 轉換資料庫回傳的蛇形命名格式為駝峰式以符合 API 輸出一致性
        formatted_klines = []
        for k in klines:
            formatted_klines.append({
                "stockCode": k["stock_code"],
                "date": k["date"],
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": int(k["volume"])
            })
            
        # 核心約束：過濾掉晚於模擬日期的資料
        filtered = [k for k in formatted_klines if k["date"] < _current_sim_date]
        # 回傳按日期升序或降序？TWSE 返回是升序（時間由舊到新），故我們保持與真實介面一致
        # 證交所 API 一般是一整個月份由舊到新
        filtered.sort(key=lambda x: x["date"])
        return filtered
    except Exception as e:
        print(f" [模擬器] 模擬獲取 K 線數據失敗: {str(e)}")
        return []

def fetch_realtime_quote(stock_code: str) -> Dict[str, Any]:
    """
    模擬獲取指定股票的盤中即時報價與買賣報價數據結構（資料模擬窗口介面）。
    模擬模式下，將以目前模擬日期的 K 線收盤價作為即時價格，並模擬盤口五檔委託。
    """
    if not _simulation_active:
        return stock_fetcher.fetch_realtime_quote(stock_code)

    # 模擬模式：取得小於等於目前模擬日期的最後一筆 K 線作為當前即時價
    history = fetch_stock_klines(stock_code)
    if not history:
        print(f" [模擬器] 警告: 資料庫中無 {_current_sim_date} 之前 {stock_code} 的 K 線數據，無法模擬即時報價")
        return {}

    # 最後一筆 K 線即為最新模擬報價
    latest = history[-1]
    price = latest["close"]
    
    # 模擬五檔買賣盤口
    bids = [round(price - 0.05 * i, 2) for i in range(1, 6)]
    asks = [round(price + 0.05 * i, 2) for i in range(1, 6)]

    return {
        "stockCode": stock_code,
        "price": price,
        "open": latest["open"],
        "high": latest["high"],
        "low": latest["low"],
        "volume": latest["volume"],
        "bids": bids,
        "asks": asks,
        "timestamp": f"{_current_sim_date}T13:30:00Z"  # 模擬台股收盤時間戳
    }
