# Path: src/services/stock_fetcher.py
import time
import requests
from datetime import datetime
from typing import List, Dict, Any

from src.time_manager import get_local_taiwan_date_str, get_utc_now

# 證交所 API 呼叫頻率限制 (限制每次請求間隔至少 3.0 秒)
_LAST_REQUEST_TIME = 0.0
MIN_REQUEST_INTERVAL = 3.0

# 熔斷降級狀態變數
_NETWORK_DISABLED_UNTIL = 0.0
_CONSECUTIVE_FAILURES = 0
MAX_CONSECUTIVE_FAILURES = 2  # 連續失敗幾次就觸發熔斷
DISABLE_DURATION = 300.0      # 熔斷時間 (秒)

def _apply_rate_limit():
    """
    確保請求間隔符合規定，遵守外部 API 的呼叫頻率限制
    """
    global _LAST_REQUEST_TIME
    now = time.time()
    elapsed = now - _LAST_REQUEST_TIME
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _LAST_REQUEST_TIME = time.time()

def _get_with_retry(url: str, retries: int = 3, timeout: float = 10.0) -> requests.Response:
    global _LAST_REQUEST_TIME, _NETWORK_DISABLED_UNTIL, _CONSECUTIVE_FAILURES
    
    # 檢查是否處於熔斷降級狀態
    now = time.time()
    if now < _NETWORK_DISABLED_UNTIL:
        remaining = int(_NETWORK_DISABLED_UNTIL - now)
        print(f" [數據擷取器] 外部網路請求目前處於熔斷狀態 (剩餘 {remaining} 秒)，直接跳過外部請求: {url}")
        raise requests.exceptions.RequestException("外部網路請求因超時/限制已啟動防禦性熔斷，暫停連線中。")

    last_err = None
    for attempt in range(1, retries + 1):
        _apply_rate_limit()
        try:
            response = requests.get(url, timeout=timeout, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })
            response.raise_for_status()
            
            # 成功時重置連續失敗計數
            _CONSECUTIVE_FAILURES = 0
            return response
        except (requests.exceptions.RequestException, requests.exceptions.Timeout) as err:
            last_err = err
            
            # 判斷是否為嚴重網路問題（超時或 403 Forbidden 或 429 Too Many Requests）
            is_critical = False
            if isinstance(err, requests.exceptions.Timeout):
                is_critical = True
            elif hasattr(err, 'response') and err.response is not None:
                if err.response.status_code in [403, 429]:
                    is_critical = True

            if is_critical:
                _CONSECUTIVE_FAILURES += 1
                
            print(f" [數據擷取器] 請求失敗 (第 {attempt}/{retries} 次嘗試): {err}。將在 3 秒後重試...")
            
            if _CONSECUTIVE_FAILURES >= MAX_CONSECUTIVE_FAILURES:
                _NETWORK_DISABLED_UNTIL = time.time() + DISABLE_DURATION
                print(f" [數據擷取器] 偵測到連續 {MAX_CONSECUTIVE_FAILURES} 次網路異常/超時，啟動熔斷防禦機制，將暫停外部 API 請求 {int(DISABLE_DURATION/60)} 分鐘以保護 IP。")
                break
                
            time.sleep(3.0)
            
    raise last_err

def fetch_stock_klines(stock_code: str, date_str: str = None) -> List[Dict[str, Any]]:
    """
    從台灣證券交易所 (TWSE) 獲取指定個股當月 (或指定日期所在月份) 的歷史 K 線數據
    :param stock_code: 股票代號 (如 "2330")
    :param date_str: 格式為 YYYYMMDD 的日期字串 (若為 None 則預設為今天)
    :returns: 清理與格式化後的台股 K 線數據列表
    """
    if not date_str:
        date_str = get_local_taiwan_date_str().replace("-", "")

    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_code}"

    try:
        response = _get_with_retry(url)
        data = response.json()

        if data.get("stat") != "OK" or "data" not in data:
            # 證交所 API 常在當日資料未準備好或尚未整理完成時回傳「查詢日期小於99年1月4日」等錯誤。
            # 若發生錯誤且傳入日期為今天，嘗試退回前一日重新查詢以載入整月至昨日的歷史數據。
            from datetime import timedelta
            fallback_date_str = None
            try:
                dt = datetime.strptime(date_str, "%Y%m%d")
                fallback_dt = dt - timedelta(days=1)
                fallback_date_str = fallback_dt.strftime("%Y%m%d")
            except Exception:
                pass
                
            if fallback_date_str:
                print(f" [數據擷取器] 查詢 {stock_code} 回應 {data.get('stat')}，嘗試回退至前一日 {fallback_date_str} 重新擷取...")
                url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={fallback_date_str}&stockNo={stock_code}"
                response = _get_with_retry(url)
                data = response.json()

            if data.get("stat") != "OK" or "data" not in data:
                print(f" [數據擷取器] 無法取得 {stock_code} 的 K 線數據，證交所回應: {data.get('stat')}")
                return []

        klines = []
        for row in data["data"]:
            # row 格式: ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數"]
            try:
                # 1. 解析與校正民國日期: "115/06/01" -> "2026-06-01"
                date_parts = row[0].split("/")
                roc_year = int(date_parts[0])
                ad_year = roc_year + 1911
                iso_date = f"{ad_year}-{date_parts[1]}-{date_parts[2]}"

                # 2. 轉換欄位為數值並去除千分位逗號
                volume = int(row[1].replace(",", ""))
                open_val = float(row[3].replace(",", ""))
                high_val = float(row[4].replace(",", ""))
                low_val = float(row[5].replace(",", ""))
                close_val = float(row[6].replace(",", ""))

                # 3. 嚴格的資料完整性校驗與防呆
                # 價格必須大於 0，且最高價不得低於開盤、收盤、最低價
                if open_val <= 0 or high_val <= 0 or low_val <= 0 or close_val <= 0:
                    continue
                if high_val < low_val or high_val < open_val or high_val < close_val:
                    continue

                klines.append({
                    "stockCode": stock_code,
                    "date": iso_date,
                    "open": open_val,
                    "high": high_val,
                    "low": low_val,
                    "close": close_val,
                    "volume": volume
                })
            except (ValueError, IndexError):
                # 遇到解析錯誤時跳過該行，保證最終產出資料的完整性
                continue

        # 如果是查詢今天（即沒有指定 date_str），且回傳的 K 線中最後一筆日期不是今天，
        # 則嘗試透過即時報價補建今天的 K 線（適用於證交所 STOCK_DAY API 尚未更新，但今天確實為交易日的情況）
        if not date_str:
            try:
                today_str = get_local_taiwan_date_str()
                latest_k_date = klines[-1]["date"] if klines else None
                if latest_k_date != today_str:
                    quote = fetch_realtime_quote(stock_code)
                    if quote and quote.get("date") == today_str:
                        if not any(k["date"] == today_str for k in klines):
                            klines.append({
                                "stockCode": stock_code,
                                "date": today_str,
                                "open": quote["open"],
                                "high": quote["high"],
                                "low": quote["low"],
                                "close": quote["price"],
                                "volume": quote["volume"]
                            })
                            print(f" [數據擷取器] 從即時報價補建今日 ({today_str}) K 線數據: 開={quote['open']}, 收={quote['price']}, 量={quote['volume']}")
            except Exception as quote_err:
                print(f" [數據擷取器] 嘗試補建今日 {stock_code} 的 K 線時發生異常: {quote_err}")

        return klines
    except Exception as e:
        print(f" [數據擷取器] 擷取 K 線數據時發生異常: {str(e)}")
        return []

_QUOTE_CACHE = {}  # maps stock_code -> (quote_dict, timestamp)
QUOTE_CACHE_TTL = 60.0  # cache for 60 seconds

def fetch_realtime_quotes_batch(stock_codes: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    批次獲取多檔股票的即時報價，大幅減少網路請求次數，避免觸發頻率限制。
    """
    if not stock_codes:
        return {}

    global _QUOTE_CACHE
    now = time.time()
    results = {}
    missing_codes = []

    for code in stock_codes:
        if code in _QUOTE_CACHE:
            cached_val, timestamp = _QUOTE_CACHE[code]
            if now - timestamp < QUOTE_CACHE_TTL:
                results[code] = cached_val
                continue
        missing_codes.append(code)

    if not missing_codes:
        return results

    # Build the ex_ch parameter containing both tse and otc for all missing stocks
    ex_ch_list = []
    for code in missing_codes:
        ex_ch_list.append(f"tse_{code}.tw")
        ex_ch_list.append(f"otc_{code}.tw")
    
    ex_ch_str = "|".join(ex_ch_list)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch_str}"

    try:
        response = _get_with_retry(url)
        data = response.json()

        if "msgArray" in data and len(data["msgArray"]) > 0:
            for info in data["msgArray"]:
                code = info.get("c")
                if not code:
                    continue
                try:
                    price = float(info.get("z", info.get("y", 0.0)))
                    open_val = float(info.get("o", 0.0))
                    high_val = float(info.get("h", 0.0))
                    low_val = float(info.get("l", 0.0))
                    volume = int(info.get("v", 0)) * 1000

                    bids = [float(x) for x in info.get("b", "").split("_") if x]
                    asks = [float(x) for x in info.get("a", "").split("_") if x]

                    if price <= 0.0:
                        continue

                    # 解析即時成交日期，例如 "20260617" -> "2026-06-17"
                    d_val = info.get("d", "")
                    quote_date = ""
                    if len(d_val) == 8:
                        quote_date = f"{d_val[:4]}-{d_val[4:6]}-{d_val[6:]}"

                    quote = {
                        "stockCode": code,
                        "price": price,
                        "open": open_val,
                        "high": high_val,
                        "low": low_val,
                        "volume": volume,
                        "bids": bids[:5],
                        "asks": asks[:5],
                        "timestamp": get_utc_now().isoformat().replace("+00:00", "Z"),
                        "date": quote_date
                    }
                    _QUOTE_CACHE[code] = (quote, now)
                    results[code] = quote
                except (ValueError, TypeError):
                    continue
    except Exception as e:
        print(f" [數據擷取器] 批次獲取即時報價失敗: {str(e)}")

    # For any stock that failed to fetch, cache empty result for 10 seconds to avoid spamming
    for code in missing_codes:
        if code not in results:
            _QUOTE_CACHE[code] = ({}, now - QUOTE_CACHE_TTL + 10.0)
            results[code] = {}

    return results

def fetch_realtime_quote(stock_code: str) -> Dict[str, Any]:
    """
    自證交所/櫃買中心盤中即時資訊 API 取得個股即時買賣報價與盤口資訊
    :param stock_code: 股票代號 (如 "2330")
    :returns: 清理後的即時股票報價結構
    """
    batch_res = fetch_realtime_quotes_batch([stock_code])
    return batch_res.get(stock_code, {})

def fetch_taiex_realtime_quote() -> Dict[str, Any]:
    """
    獲取大盤加權指數的即時點數與日期資訊 (對應 tse_t00.tw)
    """
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw"
    try:
        response = _get_with_retry(url)
        data = response.json()
        if "msgArray" in data and len(data["msgArray"]) > 0:
            info = data["msgArray"][0]
            price = float(info.get("z", info.get("y", 0.0)))
            open_val = float(info.get("o", 0.0))
            high_val = float(info.get("h", 0.0))
            low_val = float(info.get("l", 0.0))
            d_val = info.get("d", "")
            quote_date = ""
            if len(d_val) == 8:
                quote_date = f"{d_val[:4]}-{d_val[4:6]}-{d_val[6:]}"
            
            if price > 0.0:
                return {
                    "stockCode": "TAIEX",
                    "price": price,
                    "open": open_val,
                    "high": high_val,
                    "low": low_val,
                    "volume": 0,
                    "date": quote_date
                }
    except Exception as e:
        print(f" [數據擷取器] 獲取大盤即時指數失敗: {str(e)}")
    return {}

def fetch_taiex_klines(date_str: str = None) -> List[Dict[str, Any]]:
    """
    從台灣證券交易所 (TWSE) 獲取大盤加權指數當月 (或指定日期所在月份) 的歷史 K 線數據。
    :param date_str: 格式為 YYYYMMDD 的日期字串 (若為 None 則預設為今天)
    :returns: 清理與格式化後的大盤 K 線數據列表 (以 stockCode 'TAIEX' 表示)
    """
    if not date_str:
        date_str = get_local_taiwan_date_str().replace("-", "")

    url = f"https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={date_str}"

    try:
        response = _get_with_retry(url)
        data = response.json()

        if data.get("stat") != "OK" or "data" not in data:
            print(f" [數據擷取器] 無法取得大盤加權指數的 K 線數據，證交所回應: {data.get('stat')}")
            return []

        klines = []
        for row in data["data"]:
            # row 格式: ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"]
            try:
                # 1. 解析與校正民國日期: "115/06/01" -> "2026-06-01"
                date_parts = row[0].split("/")
                roc_year = int(date_parts[0])
                ad_year = roc_year + 1911
                iso_date = f"{ad_year}-{date_parts[1]}-{date_parts[2]}"

                # 2. 轉換欄位為數值並去除千分位逗號
                open_val = float(row[1].replace(",", ""))
                high_val = float(row[2].replace(",", ""))
                low_val = float(row[3].replace(",", ""))
                close_val = float(row[4].replace(",", ""))

                # 3. 驗證數據
                if open_val <= 0 or high_val <= 0 or low_val <= 0 or close_val <= 0:
                    continue

                klines.append({
                    "stockCode": "TAIEX",
                    "date": iso_date,
                    "open": open_val,
                    "high": high_val,
                    "low": low_val,
                    "close": close_val,
                    "volume": 0  # 大盤以 0 作為成交股數
                })
            except (ValueError, IndexError):
                continue

        # 如果是查詢今天（即沒有指定 date_str），且回傳的 K 線中最後一筆日期不是今天，
        # 則嘗試透過即時報價補建今天的大盤 K 線
        if not date_str:
            try:
                today_str = get_local_taiwan_date_str()
                latest_k_date = klines[-1]["date"] if klines else None
                if latest_k_date != today_str:
                    quote = fetch_taiex_realtime_quote()
                    if quote and quote.get("date") == today_str:
                        if not any(k["date"] == today_str for k in klines):
                            klines.append({
                                "stockCode": "TAIEX",
                                "date": today_str,
                                "open": quote["open"],
                                "high": quote["high"],
                                "low": quote["low"],
                                "close": quote["price"],
                                "volume": 0
                            })
                            print(f" [數據擷取器] 從即時報價補建今日 ({today_str}) 大盤 K 線數據: 開={quote['open']}, 收={quote['price']}")
            except Exception as quote_err:
                print(f" [數據擷取器] 嘗試補建今日大盤 K 線時發生異常: {quote_err}")

        return klines
    except Exception as e:
        print(f" [數據擷取器] 擷取大盤加權指數 K 線時發生異常: {str(e)}")
        return []


_DISPLAY_PRICE_CACHE = {}  # maps stock_code -> (price, timestamp)
DISPLAY_PRICE_CACHE_TTL = 60.0  # 60 seconds cache

def get_display_price(stock_code: str, fallback_price: float = 0.0) -> float:
    """
    依據使用者需求取得網頁面板顯示/市值計算所使用的價格：
    - 若正在當沖中（台股交易日 09:00~13:30 之前，或當日 13:30 之前），顯示最近一天（昨收/前一交易日）的收盤價。
    - 若下盤後（13:30 之後）或非交易日，顯示今天（交易完）的收盤價。
    """
    from src.services import sandbox_simulator
    # 1. 判斷是否處於沙盒模擬模式，如果是，直接回傳沙盒報價
    if sandbox_simulator.is_simulation_active():
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        return float(quote.get("price") or fallback_price)

    global _DISPLAY_PRICE_CACHE
    now = time.time()
    if stock_code in _DISPLAY_PRICE_CACHE:
        cached_val, timestamp = _DISPLAY_PRICE_CACHE[stock_code]
        if now - timestamp < DISPLAY_PRICE_CACHE_TTL:
            return cached_val

    # 2. 取得台灣目前本地時間
    from src.time_manager import get_local_taiwan_datetime, get_local_taiwan_date_str
    from datetime import time as dt_time
    local_dt = get_local_taiwan_datetime()
    today_str = get_local_taiwan_date_str()
    
    # 判斷是否為週一至週五且在 13:30 之前
    is_trading_hours = (local_dt.weekday() < 5 and local_dt.time() < dt_time(13, 30))

    from src.services import supabase_client
    try:
        # 從資料庫載入最新數筆 K 線
        db_klines = supabase_client.get_stock_klines(stock_code, limit=5)
    except Exception as e:
        print(f" [數據擷取器] 無法自資料庫讀取 {stock_code} 的 K 線: {e}")
        db_klines = []

    price = 0.0
    if is_trading_hours:
        # 當沖交易進行中（或開盤前）：顯示最近一天（昨收/前一交易日）的收盤價
        # 過濾日期小於今天的紀錄
        past_klines = [k for k in db_klines if k["date"] < today_str]
        if past_klines:
            price = float(past_klines[0]["close"])
        else:
            # 若本月資料庫中沒有今天之前的 K 線（例如月初），嘗試從 API 補建/查詢
            try:
                klines = fetch_stock_klines(stock_code)
                past_klines = [k for k in klines if k["date"] < today_str]
                if not past_klines:
                    # 依然沒有（月初 1 號），嘗試取得 7 天前（前月）的 K 線
                    from datetime import timedelta
                    fallback_dt = local_dt - timedelta(days=7)
                    fallback_date_str = fallback_dt.strftime("%Y%m%d")
                    prev_klines = fetch_stock_klines(stock_code, fallback_date_str)
                    past_klines = [k for k in prev_klines if k["date"] < today_str]
                
                if past_klines:
                    price = float(past_klines[-1]["close"])
            except Exception as fetch_err:
                print(f" [數據擷取器] 當沖中嘗試向 API 獲取 {stock_code} 歷史收盤價失敗: {fetch_err}")

            if price <= 0.0:
                # 若都失敗，回退使用實時 API 價格
                quote = fetch_realtime_quote(stock_code)
                price = float(quote.get("price") or fallback_price)
    else:
        # 下盤後或假日：顯示今天交易完（或最新已收盤）的價格
        # 我們優先嘗試取得實時報價（這能保證拿到今天交易完的現價，且不依賴證交所歷史 K 線 API 的更新延遲）
        try:
            quote = fetch_realtime_quote(stock_code)
            if quote and quote.get("price"):
                price = float(quote["price"])
        except Exception as quote_err:
            print(f" [數據擷取器] 收盤後嘗試獲取 {stock_code} 實時報價失敗: {quote_err}")

        # 若實時報價失敗，回退使用資料庫中最新的 K 線收盤價
        if price <= 0.0 and db_klines:
            price = float(db_klines[0]["close"])
            
        if price <= 0.0:
            price = float(fallback_price)

    # 寫入快取
    if price > 0.0:
        _DISPLAY_PRICE_CACHE[stock_code] = (price, now)

    return price


