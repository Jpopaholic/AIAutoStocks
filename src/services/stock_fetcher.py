# Path: src/services/stock_fetcher.py
import time
import math
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

def _safe_json(response: requests.Response) -> Dict[str, Any]:
    """
    安全解析 requests 回傳的 JSON 內容，防止空字串或無效格式導致例外。
    """
    text = response.text
    if not text or not text.strip():
        return {}
    try:
        return response.json()
    except Exception as e:
        # 紀錄錯誤以供日後診斷，但不要直接噴錯
        print(f" [數據擷取器] 解析 JSON 失敗，狀態碼={response.status_code}，內容長度={len(text)}，前100個字元={repr(text[:100])}")
        return {}

def _fetch_tpex_klines(stock_code: str, date_str: str) -> List[Dict[str, Any]]:
    """
    從櫃買中心 (TPEx) 獲取指定上櫃個股的歷史 K 線數據
    """
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        api_date = dt.strftime("%Y/%m/01")
        url = f"https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={stock_code}&date={api_date}"
        
        response = _get_with_retry(url)
        data = _safe_json(response)
        
        if data.get("stat") != "ok" or "tables" not in data or not data["tables"]:
            print(f" [數據擷取器] 無法取得 {stock_code} 的 K 線數據，櫃買中心回應: {data.get('stat')}")
            return []
            
        table = data["tables"][0]
        if "data" not in table or not table["data"]:
            return []
            
        klines = []
        for row in table["data"]:
            try:
                # row 格式: ["日期", "成交張數", "成交仟元", "開盤", "最高", "最低", "收盤", "漲跌", "筆數"]
                # 1. 解析與校正民國日期: "115/06/01" -> "2026-06-01"
                date_parts = row[0].split("/")
                roc_year = int(date_parts[0])
                ad_year = roc_year + 1911
                iso_date = f"{ad_year}-{date_parts[1]}-{date_parts[2]}"
                
                # 2. 轉換欄位為數值 (張數 * 1000 轉換為股數)
                volume = int(row[1].replace(",", "")) * 1000
                open_val = float(row[3].replace(",", ""))
                high_val = float(row[4].replace(",", ""))
                low_val = float(row[5].replace(",", ""))
                close_val = float(row[6].replace(",", ""))
                
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
                continue
        return klines
    except Exception as e:
        print(f" [數據擷取器] 擷取櫃買中心 K 線時發生異常: {str(e)}")
        return []

def fetch_stock_klines(stock_code: str, date_str: str = None) -> List[Dict[str, Any]]:
    """
    從台灣證券交易所 (TWSE) 獲取指定個股當月 (或指定日期所在月份) 的歷史 K 線數據
    :param stock_code: 股票代號 (如 "2330")
    :param date_str: 格式為 YYYYMMDD 的日期字串 (若為 None 則預設為今天)
    :returns: 清理與格式化後的台股 K 線數據列表
    """
    is_today_query = not date_str
    if not date_str:
        date_str = get_local_taiwan_date_str().replace("-", "")

    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_code}"

    klines = []

    # 1. 嘗試從證交所 / 櫃買中心網路 API 獲取官方歷史月 K 線
    try:
        response = _get_with_retry(url)
        data = _safe_json(response)

        if data.get("stat") != "OK" or "data" not in data:
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
                data = _safe_json(response)

            if data.get("stat") != "OK" or "data" not in data:
                print(f" [數據擷取器] {stock_code} 在證交所查無資料，嘗試從櫃買中心 (TPEx) 獲取...")
                klines = _fetch_tpex_klines(stock_code, date_str)

        if "data" in data:
            for row in data["data"]:
                try:
                    date_parts = row[0].split("/")
                    roc_year = int(date_parts[0])
                    ad_year = roc_year + 1911
                    iso_date = f"{ad_year}-{date_parts[1]}-{date_parts[2]}"

                    volume = int(row[1].replace(",", ""))
                    open_val = float(row[3].replace(",", ""))
                    high_val = float(row[4].replace(",", ""))
                    low_val = float(row[5].replace(",", ""))
                    close_val = float(row[6].replace(",", ""))

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
                    continue
    except Exception as fetch_err:
        print(f" [數據擷取器] 警告: 證交所/櫃買網路 API 擷取 {stock_code} 失敗 (將使用資料庫與即時報價備援): {fetch_err}")

    # 2. 若網路 API 失敗/熔斷導致 klines 為空，自動從 Supabase 資料庫載入歷史 K 線作為基礎
    if not klines:
        try:
            from src.services import supabase_client
            db_records = supabase_client.get_stock_klines(stock_code, limit=60)
            if db_records:
                for k in db_records:
                    klines.append({
                        "stockCode": k["stock_code"],
                        "date": str(k["date"]),
                        "open": float(k["open"]),
                        "high": float(k["high"]),
                        "low": float(k["low"]),
                        "close": float(k["close"]),
                        "volume": int(k["volume"] or 0)
                    })
                klines.sort(key=lambda x: x["date"])
        except Exception as db_err:
            print(f" [數據擷取器] 從資料庫載入 {stock_code} 歷史時發生異常: {db_err}")

    # 3. 核心補建：若為今日查詢且 klines 缺乏今日資料，一律呼叫即時報價 (優先採用永豐 API) 補建今日 K 線
    if is_today_query:
        try:
            today_str = get_local_taiwan_date_str()
            latest_k_date = klines[-1]["date"] if klines else None
            if latest_k_date != today_str:
                quote = fetch_realtime_quote(stock_code, force_refresh=True)
                if quote and quote.get("price", 0) > 0:
                    q_date = quote.get("date")
                    if q_date and q_date != today_str:
                        print(f" [數據擷取器] 警告: {stock_code} 即時報價日期 ({q_date}) 非今日 ({today_str})，跳過當日 K 線補建以防寫入舊資料。")
                    else:
                        target_date = today_str
                        if not any(k["date"] == target_date for k in klines):
                            open_p = quote.get("open") if quote.get("open", 0) > 0 else quote["price"]
                            high_p = quote.get("high") if quote.get("high", 0) > 0 else quote["price"]
                            low_p = quote.get("low") if quote.get("low", 0) > 0 else quote["price"]
                            klines.append({
                                "stockCode": stock_code,
                                "date": target_date,
                                "open": open_p,
                                "high": high_p,
                                "low": low_p,
                                "close": quote["price"],
                                "volume": quote.get("volume", 0)
                            })
                            print(f" [數據擷取器] 從即時報價(永豐/MIS)成功補建今日 ({target_date}) K 線數據: 開={open_p}, 收={quote['price']}, 量={quote.get('volume')}")
        except Exception as quote_err:
            print(f" [數據擷取器] 嘗試補建今日 {stock_code} 的 K 線時發生異常: {quote_err}")

    return klines

_QUOTE_CACHE = {}  # maps stock_code -> (quote_dict, timestamp)
QUOTE_CACHE_TTL = 60.0  # cache for 60 seconds

def fetch_realtime_quotes_batch(stock_codes: List[str], force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
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
        if not force_refresh and code in _QUOTE_CACHE:
            cached_val, timestamp = _QUOTE_CACHE[code]
            if cached_val and (now - timestamp < QUOTE_CACHE_TTL):
                results[code] = cached_val
                continue
        missing_codes.append(code)

    if not missing_codes:
        return results

    # 優先嘗試透過永豐證券 (Shioaji) API 取得高清即時快照 (避免證交所 Web API 延遲與快取問題)
    try:
        from src.services import broker_connector
        api = broker_connector._get_shioaji_api()
        if api and hasattr(api, "Contracts") and hasattr(api, "snapshots"):
            contracts = []
            for code in missing_codes:
                contract = api.Contracts.Stocks.get(code)
                if contract:
                    contracts.append(contract)
            
            if contracts:
                snapshots = api.snapshots(contracts)
                today_str = get_local_taiwan_date_str()
                for snap in snapshots:
                    if snap and getattr(snap, "close", 0) > 0:
                        code = snap.code
                        price = float(snap.close)
                        open_val = float(snap.open) if getattr(snap, "open", 0) > 0 else price
                        high_val = float(snap.high) if getattr(snap, "high", 0) > 0 else price
                        low_val = float(snap.low) if getattr(snap, "low", 0) > 0 else price
                        volume = int(getattr(snap, "total_volume", 0)) * 1000
                        bids = [float(snap.buy_price)] if getattr(snap, "buy_price", 0) > 0 else []
                        asks = [float(snap.sell_price)] if getattr(snap, "sell_price", 0) > 0 else []
                        quote = {
                            "stockCode": code,
                            "price": price,
                            "open": open_val,
                            "high": high_val,
                            "low": low_val,
                            "volume": volume,
                            "bids": bids,
                            "asks": asks,
                            "timestamp": get_utc_now().isoformat().replace("+00:00", "Z"),
                            "date": today_str
                        }
                        _QUOTE_CACHE[code] = (quote, now)
                        results[code] = quote
            
            missing_codes = [c for c in missing_codes if c not in results]
            if not missing_codes:
                return results
    except Exception:
        # Shioaji 未登入或不可用時，自動 Fallback 至證交所 Web API
        pass

    # Build the ex_ch parameter containing both tse and otc for all missing stocks
    ex_ch_list = []
    for code in missing_codes:
        ex_ch_list.append(f"tse_{code}.tw")
        ex_ch_list.append(f"otc_{code}.tw")
    
    ex_ch_str = "|".join(ex_ch_list)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch_str}"

    try:
        response = _get_with_retry(url)
        data = _safe_json(response)

        if "msgArray" in data and len(data["msgArray"]) > 0:
            for info in data["msgArray"]:
                code = info.get("c")
                if not code:
                    continue
                try:
                    def _to_float(v, default=0.0):
                        if v is None or v == "" or v == "-":
                            return default
                        try:
                            res = float(str(v).replace(",", ""))
                            if math.isnan(res) or math.isinf(res):
                                return default
                            return res
                        except (ValueError, TypeError, OverflowError):
                            return default

                    # Determine price: check z (latest price), then y (yesterday's close), then o (open)
                    price = _to_float(info.get("z"), -1.0)
                    if price <= 0.0:
                        price = _to_float(info.get("y"), -1.0)
                    if price <= 0.0:
                        price = _to_float(info.get("o"), 0.0)

                    if price <= 0.0:
                        continue

                    open_val = _to_float(info.get("o"), price)
                    high_val = _to_float(info.get("h"), price)
                    low_val = _to_float(info.get("l"), price)
                    
                    raw_v = info.get("v", "0")
                    volume = int(_to_float(raw_v, 0.0)) * 1000

                    bids = []
                    for x in info.get("b", "").split("_"):
                        if x and x != "-":
                            try:
                                bids.append(float(x))
                            except ValueError:
                                pass

                    asks = []
                    for x in info.get("a", "").split("_"):
                        if x and x != "-":
                            try:
                                asks.append(float(x))
                            except ValueError:
                                pass

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

    # 計算失敗的快取時間：盤中交易時段 30 秒，非交易時段 300 秒 (5分鐘)
    from src.time_manager import get_local_taiwan_datetime
    from datetime import time as dt_time
    local_dt = get_local_taiwan_datetime()
    is_trading_hours = (local_dt.weekday() < 5 and dt_time(9, 0) <= local_dt.time() <= dt_time(13, 30))
    fail_cache_ttl = 30.0 if is_trading_hours else 300.0

    # 對於獲取失敗的股票，快取空結果以防頻繁請求
    for code in missing_codes:
        if code not in results:
            _QUOTE_CACHE[code] = ({}, now - QUOTE_CACHE_TTL + fail_cache_ttl)
            results[code] = {}

    return results

def fetch_realtime_quote(stock_code: str, force_refresh: bool = False) -> Dict[str, Any]:
    """
    自證交所/櫃買中心盤中即時資訊 API 取得個股即時買賣報價與盤口資訊
    :param stock_code: 股票代號 (如 "2330")
    :param force_refresh: 是否強制繞過快取向 API 發送請求
    :returns: 清理後的即時股票報價結構
    """
    batch_res = fetch_realtime_quotes_batch([stock_code], force_refresh=force_refresh)
    return batch_res.get(stock_code, {})

def fetch_taiex_realtime_quote() -> Dict[str, Any]:
    """
    獲取大盤加權指數的即時點數與日期資訊 (優先使用 Shioaji Index 001，Fallback 至 tse_t00.tw)
    """
    try:
        from src.services import broker_connector
        api = broker_connector._get_shioaji_api()
        if api and hasattr(api, "Contracts") and hasattr(api, "snapshots"):
            contract = api.Contracts.Indexs.TSE.get('001')
            if contract:
                snaps = api.snapshots([contract])
                if snaps and getattr(snaps[0], "close", 0) > 0:
                    snap = snaps[0]
                    price = float(snap.close)
                    open_val = float(snap.open) if getattr(snap, "open", 0) > 0 else price
                    high_val = float(snap.high) if getattr(snap, "high", 0) > 0 else price
                    low_val = float(snap.low) if getattr(snap, "low", 0) > 0 else price
                    today_str = get_local_taiwan_date_str()
                    return {
                        "stockCode": "TAIEX",
                        "price": price,
                        "open": open_val,
                        "high": high_val,
                        "low": low_val,
                        "volume": 0,
                        "date": today_str
                    }
    except Exception:
        pass

    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw"
    try:
        response = _get_with_retry(url)
        data = _safe_json(response)
        if "msgArray" in data and len(data["msgArray"]) > 0:
            info = data["msgArray"][0]
            
            def _to_float(v, default=0.0):
                if v is None or v == "" or v == "-":
                    return default
                try:
                    res = float(str(v).replace(",", ""))
                    if math.isnan(res) or math.isinf(res):
                        return default
                    return res
                except (ValueError, TypeError, OverflowError):
                    return default

            price = _to_float(info.get("z"), -1.0)
            if price <= 0.0:
                price = _to_float(info.get("y"), -1.0)
            if price <= 0.0:
                price = _to_float(info.get("o"), 0.0)
            
            if price > 0.0:
                open_val = _to_float(info.get("o"), price)
                high_val = _to_float(info.get("h"), price)
                low_val = _to_float(info.get("l"), price)
                d_val = info.get("d", "")
                quote_date = ""
                if len(d_val) == 8:
                    quote_date = f"{d_val[:4]}-{d_val[4:6]}-{d_val[6:]}"
                
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
    is_today_query = not date_str
    if not date_str:
        date_str = get_local_taiwan_date_str().replace("-", "")

    url = f"https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={date_str}"

    klines = []

    # 1. 嘗試從證交所 API 獲取大盤歷史月 K 線
    try:
        response = _get_with_retry(url)
        data = _safe_json(response)

        if "data" in data:
            for row in data["data"]:
                try:
                    date_parts = row[0].split("/")
                    roc_year = int(date_parts[0])
                    ad_year = roc_year + 1911
                    iso_date = f"{ad_year}-{date_parts[1]}-{date_parts[2]}"

                    open_val = float(row[1].replace(",", ""))
                    high_val = float(row[2].replace(",", ""))
                    low_val = float(row[3].replace(",", ""))
                    close_val = float(row[4].replace(",", ""))

                    if open_val <= 0 or high_val <= 0 or low_val <= 0 or close_val <= 0:
                        continue

                    klines.append({
                        "stockCode": "TAIEX",
                        "date": iso_date,
                        "open": open_val,
                        "high": high_val,
                        "low": low_val,
                        "close": close_val,
                        "volume": 0
                    })
                except (ValueError, IndexError):
                    continue
    except Exception as fetch_err:
        print(f" [數據擷取器] 警告: 證交所大盤 K 線 API 擷取失敗 (將使用資料庫與即時報價備援): {fetch_err}")

    # 2. 若網路 API 失敗/熔斷導致 klines 為空，自動從 Supabase 資料庫載入歷史 K 線
    if not klines:
        try:
            from src.services import supabase_client
            db_records = supabase_client.get_stock_klines("TAIEX", limit=60)
            if db_records:
                for k in db_records:
                    klines.append({
                        "stockCode": "TAIEX",
                        "date": str(k["date"]),
                        "open": float(k["open"]),
                        "high": float(k["high"]),
                        "low": float(k["low"]),
                        "close": float(k["close"]),
                        "volume": 0
                    })
                klines.sort(key=lambda x: x["date"])
        except Exception as db_err:
            print(f" [數據擷取器] 從資料庫載入大盤歷史時發生異常: {db_err}")

    # 3. 核心補建：若為今日查詢且 klines 缺乏今日資料，一律呼叫即時報價 (優先採用永豐 API 001 快照) 補建今日大盤 K 線
    if is_today_query:
        try:
            today_str = get_local_taiwan_date_str()
            latest_k_date = klines[-1]["date"] if klines else None
            if latest_k_date != today_str:
                quote = fetch_taiex_realtime_quote()
                if quote and quote.get("price", 0) > 0:
                    q_date = quote.get("date")
                    if q_date and q_date != today_str:
                        print(f" [數據擷取器] 警告: 大盤即時點數日期 ({q_date}) 非今日 ({today_str})，跳過當日 K 線補建以防寫入舊資料。")
                    else:
                        target_date = today_str
                        if not any(k["date"] == target_date for k in klines):
                            open_p = quote.get("open") if quote.get("open", 0) > 0 else quote["price"]
                            high_p = quote.get("high") if quote.get("high", 0) > 0 else quote["price"]
                            low_p = quote.get("low") if quote.get("low", 0) > 0 else quote["price"]
                            klines.append({
                                "stockCode": "TAIEX",
                                "date": target_date,
                                "open": open_p,
                                "high": high_p,
                                "low": low_p,
                                "close": quote["price"],
                                "volume": 0
                            })
                            print(f" [數據擷取器] 從即時報價(永豐/MIS)成功補建今日 ({target_date}) 大盤 K 線數據: 開={open_p}, 收={quote['price']}")
        except Exception as quote_err:
            print(f" [數據擷取器] 嘗試補建今日大盤 K 線時發生異常: {quote_err}")

    return klines


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


