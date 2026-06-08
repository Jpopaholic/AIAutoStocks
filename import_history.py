#!/usr/bin/env python
# Path: import_history.py
"""
AIAutoStocks 歷史數據導入腳本
能自動根據起訖日期計算所需月份，並從台灣證券交易所 (TWSE) 下載多檔個股的 K 線數據存入 Supabase。
用法：
python import_history.py --stocks 2330,2454,2317 --start-date 2026-05-01 --end-date 2026-06-08
"""
import argparse
import time
from datetime import datetime
from typing import List

from src.services.stock_fetcher import fetch_stock_klines
from src.services.supabase_client import save_stock_klines
from src.config import resolve_stock_codes

def get_monthly_dates(start_str: str, end_str: str) -> List[str]:
    """
    依據 YYYY-MM-DD 的起訖日期，計算期間所有月份 of YYYYMM01 查詢日期字串
    """
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d")
        end = datetime.strptime(end_str, "%Y-%m-%d")
    except ValueError as e:
        raise ValueError(f"日期格式不正確 (必須為 YYYY-MM-DD): {e}")

    dates = []
    curr = start
    while curr <= end:
        date_str = curr.strftime("%Y%m01")
        if date_str not in dates:
            dates.append(date_str)
        
        # 推進到下個月的 1 號
        if curr.month == 12:
            curr = curr.replace(year=curr.year + 1, month=1, day=1)
        else:
            curr = curr.replace(month=curr.month + 1, day=1)
            
    return dates

def main():
    parser = argparse.ArgumentParser(description="AIAutoStocks 歷史數據批次下載導入器")
    parser.add_argument(
        "--stocks", 
        default="top5", 
        help="股票代號列表或套餐名稱 (例如: 2330,2454 或 top5, finance, semiconductor, apple, highdividend)"
    )
    parser.add_argument(
        "--start-date", 
        default="2026-05-01", 
        help="下載起始日期 (YYYY-MM-DD, 預設: 2026-05-01)"
    )
    parser.add_argument(
        "--end-date", 
        default="2026-06-08", 
        help="下載結束日期 (YYYY-MM-DD, 預設: 2026-06-08)"
    )

    args = parser.parse_args()
    stock_codes = resolve_stock_codes(args.stocks)
    
    # 1. 計算需要查詢的月份字串列表
    try:
        query_months = get_monthly_dates(args.start_date, args.end_date)
    except ValueError as err:
        print(f" [錯誤] {err}")
        return

    print("====== 開始批次導入台股歷史數據 ======")
    print(f"下載區間: {args.start_date} 至 {args.end_date}")
    print(f"查詢月份: {query_months}")
    print(f"下載目標個股: {stock_codes}")
    
    total_inserted = 0

    # 2. 雙重迴圈逐股、逐月抓取
    for code in stock_codes:
        print(f"\n------------------ 開始下載股票: {code} ------------------")
        for month_str in query_months:
            print(f"正在從證交所下載 {code} 於 {month_str[:4]}年{month_str[4:6]}月 的數據...")
            
            # 呼叫數據擷取器 (已內建 3 秒防被鎖延遲機制)
            klines = fetch_stock_klines(code, month_str)
            
            if klines:
                # 篩選掉超出使用者指定起訖日期範圍的日 K 線（因為 API 返回整個月）
                filtered_klines = [
                    k for k in klines 
                    if args.start_date <= k["date"] <= args.end_date
                ]
                
                if filtered_klines:
                    print(f"  成功取得 {len(filtered_klines)} 筆範圍內數據，正在存入 Supabase...")
                    save_stock_klines(filtered_klines)
                    total_inserted += len(filtered_klines)
                else:
                    print("  ⚠️ 該月份無指定日期範圍內的交易數據。")
            else:
                print(f"  ⚠️ 無法取得 {code} 於 {month_str} 的資料，請確認網路或 API 限制。")
                
    print("\n====== 歷史數據導入結束 ======")
    print(f"共成功匯入 {total_inserted} 筆日 K 線資料至 Supabase。")
    print("\n您現在可以執行更長週期的沙盒回測：")
    print(f"python main.py --mode sandbox --stocks {args.stocks} --start-date {args.start_date} --end-date {args.end_date}")

if __name__ == "__main__":
    main()
