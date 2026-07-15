import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase

def cleanup():
    try:
        # 獲取所有記錄
        res = execute_with_retry(
            lambda: supabase.table("stock_analysis_scores")
            .select("id, daily_analysis_id, stock_code")
            .execute()
        )
        
        seen = set()
        duplicates = []
        for r in res:
            key = (r["daily_analysis_id"], r["stock_code"])
            if key in seen:
                duplicates.append(r["id"])
            else:
                seen.add(key)
                
        if duplicates:
            print(f"找到 {len(duplicates)} 筆重複記錄，正在進行清理...")
            # 刪除重複記錄
            for dup_id in duplicates:
                execute_with_retry(
                    lambda: supabase.table("stock_analysis_scores")
                    .delete()
                    .eq("id", dup_id)
                    .execute()
                )
            print("清理完成！")
        else:
            print("沒有找到任何重複的記錄。")
            
    except Exception as e:
        print(f"清理失敗: {e}")

if __name__ == "__main__":
    cleanup()
