import os
import sys

os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services import supabase_client

def main():
    print("--- 查詢今日特定時段之系統日誌 ---")
    try:
        # Fetch system logs from 2026-07-13 06:00:00 to 06:10:00 UTC
        logs_res = supabase_client.supabase.table("system_logs")\
            .select("*")\
            .gte("created_at", "2026-07-13T06:00:00+00:00")\
            .lte("created_at", "2026-07-13T06:10:00+00:00")\
            .order("created_at")\
            .execute()
            
        for l in logs_res.data:
            print(f"[{l['created_at']}] [{l['level']}] {l['message']}")
            
    except Exception as e:
        print(f"查詢失敗: {e}")

if __name__ == "__main__":
    main()
