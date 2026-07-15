import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase

def list_records():
    try:
        res = execute_with_retry(
            lambda: supabase.table("daily_analysis")
            .select("id, analysis_date, trigger_type, is_paper, created_at")
            .order("analysis_date", desc=True)
            .execute()
        )
        print("=== daily_analysis 紀錄 ===")
        for r in res:
            print(f"ID: {r['id']} | 日期: {r['analysis_date']} | 類型: {r['trigger_type']} | 模擬: {r['is_paper']} | 建立時間: {r['created_at']}")
    except Exception as e:
        print(f"查詢失敗: {e}")

if __name__ == "__main__":
    list_records()
