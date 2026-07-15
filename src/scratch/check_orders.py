import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase
from datetime import datetime, timezone

def check_orders():
    try:
        # 查詢今天 (2026-07-15) 的所有訂單
        res = execute_with_retry(
            lambda: supabase.table("trade_orders")
            .select("*")
            .execute()
        )
        print("=== 所有交易訂單 ===")
        for o in res:
            print(f"ID: {o['id']} | 股票: {o['stock_code']} | 動作: {o['action']} | 狀態: {o['status']} | 時間: {o['executed_at']} | 模擬: {o['is_paper']}")
    except Exception as e:
        print(f"查詢失敗: {e}")

if __name__ == "__main__":
    check_orders()
