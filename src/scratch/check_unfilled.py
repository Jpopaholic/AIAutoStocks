import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase

def check_unfilled():
    try:
        res = execute_with_retry(
            lambda: supabase.table("unfilled_orders")
            .select("*")
            .execute()
        )
        print("=== 所有未成交訂單 (unfilled_orders) ===")
        for o in res:
            print(f"ID: {o['id']} | 股票: {o['stock_code']} | 動作: {o['action']} | 價格: {o['price']} | 數量: {o['quantity']} | 原因: {o['reason']} | 建立時間: {o['created_at']}")
    except Exception as e:
        print(f"查詢失敗: {e}")

if __name__ == "__main__":
    check_unfilled()
