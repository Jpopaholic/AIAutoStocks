import os
import sys

# Load local environment parameters
os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services import supabase_client
from src.config import config

def main():
    print("--- 查詢今日 AI 決策狀態 ---")
    try:
        # 1. 取得最新的 daily_analysis 紀錄
        res = supabase_client.supabase.table("daily_analysis").select("*").order("created_at", desc=True).limit(5).execute()
        print("\n最近的 5 筆 AI 氣候決策 (daily_analysis):")
        for idx, row in enumerate(res.data):
            print(f"  {idx+1}. 日期: {row['analysis_date']} | 氣候: {row['regime']} | 姿態: {row['posture']} | 風險乘數: {row['risk_multiplier']} | 模擬/實盤: {'沙盒' if row['is_paper'] else '實盤'} | 觸發: {row['trigger_type']} | 建立時間: {row['created_at']}")
            
        # 2. 取得今日的系統日誌
        from src.time_manager import get_local_taiwan_midnight_utc_range
        start_utc, end_utc = get_local_taiwan_midnight_utc_range()
        logs_res = supabase_client.supabase.table("system_logs").select("*").gte("created_at", start_utc).order("created_at", desc=True).limit(15).execute()
        print("\n今日系統日誌 (system_logs):")
        if logs_res.data:
            for l in logs_res.data:
                print(f"  [{l['created_at']}] [{l['level']}] {l['message']}")
        else:
            print("  今日無任何系統日誌紀錄。")
            
        # 3. 取得今日委託單
        orders_res = supabase_client.supabase.table("trade_orders").select("*").gte("executed_at", start_utc).order("executed_at", desc=True).execute()
        print("\n今日委託單 (trade_orders):")
        if orders_res.data:
            for o in orders_res.data:
                print(f"  [{o['executed_at']}] {o['action']} | {o['stock_code']} | 價格: {o['price']} | 數量: {o['quantity']} | 狀態: {o['status']} | 理由/單號: {o.get('order_id')}")
        else:
            print("  今日無任何委託單紀錄。")

    except Exception as e:
        print(f"查詢失敗: {e}")

if __name__ == "__main__":
    main()
