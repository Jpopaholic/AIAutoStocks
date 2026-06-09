# Path: src/scratch/prepopulate_taiex.py
import sys
import os

# 確保可以匯入 src 模組
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.services import stock_fetcher, supabase_client

def main():
    print("=== 開始補登大盤加權指數 (TAIEX) 歷史數據 ===")
    
    # 補登 2026 年 4、5、6 月的數據
    target_dates = ["20260401", "20260501", "20260601"]
    all_klines = []
    
    for date_str in target_dates:
        print(f"正在擷取 {date_str[:4]}年{date_str[4:6]}月 的大盤數據...")
        klines = stock_fetcher.fetch_taiex_klines(date_str)
        if klines:
            print(f" -> 成功擷取 {len(klines)} 筆資料")
            all_klines.extend(klines)
        else:
            print(f" -> 警告: 擷取失敗或該月份無資料")
            
    if all_klines:
        print(f"\n準備將 {len(all_klines)} 筆大盤 K 線寫入 Supabase 資料庫...")
        try:
            res = supabase_client.save_stock_klines(all_klines)
            print(" -> 寫入完成！")
        except Exception as e:
            print(f" -> 寫入失敗，錯誤: {str(e)}")
    else:
        print("\n未能擷取到任何大盤數據。")

if __name__ == "__main__":
    main()
