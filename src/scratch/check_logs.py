import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase

def check_logs():
    try:
        # 查詢今天的日誌 (2026-07-15)
        res = execute_with_retry(
            lambda: supabase.table("system_logs")
            .select("*")
            .gte("created_at", "2026-07-15T00:00:00Z")
            .order("created_at", desc=False)
            .execute()
        )
        print("=== 2026-07-15 系統日誌 ===")
        for log in res:
            print(f"[{log['created_at']}] [{log['level']}] {log['message']}")
    except Exception as e:
        print(f"查詢失敗: {e}")

if __name__ == "__main__":
    check_logs()
