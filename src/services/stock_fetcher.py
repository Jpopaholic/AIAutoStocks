# Path: src/services/stock_fetcher.py
import time
import requests
from datetime import datetime
from typing import List, Dict, Any

# 證交所 API 呼叫頻率限制 (限制每次請求間隔至少 3.0 秒)
_LAST_REQUEST_TIME = 0.0
MIN_REQUEST_INTERVAL = 3.0

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

def fetch_stock_klines(stock_code: str, date_str: str = None) -> List[Dict[str, Any]]:
    """
    從台灣證券交易所 (TWSE) 獲取指定個股當月 (或指定日期所在月份) 的歷史 K 線數據
    :param stock_code: 股票代號 (如 "2330")
    :param date_str: 格式為 YYYYMMDD 的日期字串 (若為 None 則預設為今天)
    :returns: 清理與格式化後的台股 K 線數據列表
    """
    _apply_rate_limit()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_code}"

    try:
        response = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        response.raise_for_status()
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

        return klines
    except Exception as e:
        print(f" [數據擷取器] 擷取 K 線數據時發生異常: {str(e)}")
        return []

def fetch_realtime_quote(stock_code: str) -> Dict[str, Any]:
    """
    自證交所/櫃買中心盤中即時資訊 API 取得個股即時買賣報價與盤口資訊
    :param stock_code: 股票代號 (如 "2330")
    :returns: 清理後的即時股票報價結構
    """
    _apply_rate_limit()

    # 同時支援上市 (tse) 與上櫃 (otc) 股票的即時查詢
    for market in ["tse", "otc"]:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={market}_{stock_code}.tw"
        try:
            response = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            })
            response.raise_for_status()
            data = response.json()

            if "msgArray" in data and len(data["msgArray"]) > 0:
                info = data["msgArray"][0]
                # z: 最近成交價 (即時價), o: 開盤價, h: 最高價, l: 最低價, v: 累積成交張數
                # b: 委買價 (以底線分隔), a: 委賣價 (以底線分隔)
                try:
                    price = float(info.get("z", info.get("y", 0.0)))  # 若無當日成交價則以昨收代替
                    open_val = float(info.get("o", 0.0))
                    high_val = float(info.get("h", 0.0))
                    low_val = float(info.get("l", 0.0))
                    volume = int(info.get("v", 0)) * 1000  # 證交所即時張數轉成股數

                    # 擷取五檔委買委賣價格
                    bids = [float(x) for x in info.get("b", "").split("_") if x]
                    asks = [float(x) for x in info.get("a", "").split("_") if x]

                    # 資料完整性校驗
                    if price <= 0.0:
                        continue

                    return {
                        "stockCode": stock_code,
                        "price": price,
                        "open": open_val,
                        "high": high_val,
                        "low": low_val,
                        "volume": volume,
                        "bids": bids[:5],
                        "asks": asks[:5],
                        "timestamp": datetime.now().isoformat() + "Z"
                    }
                except (ValueError, TypeError):
                    continue
        except Exception:
            continue

    print(f" [數據擷取器] 無法取得 {stock_code} 的即時報價與買賣報價數據結構")
    return {}
