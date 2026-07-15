import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase

def check_relations():
    try:
        # 1. 取得所有 daily_analysis 紀錄
        analyses = execute_with_retry(
            lambda: supabase.table("daily_analysis")
            .select("id, analysis_date, trigger_type, is_paper, created_at")
            .order("analysis_date", desc=True)
            .execute()
        )
        
        # 2. 取得所有的評分資料計數，按 daily_analysis_id 分組
        scores = execute_with_retry(
            lambda: supabase.table("stock_analysis_scores")
            .select("daily_analysis_id")
            .execute()
        )
        
        # 統計每個 ID 的分數筆數
        count_map = {}
        for s in scores:
            da_id = s.get("daily_analysis_id")
            if da_id is not None:
                count_map[da_id] = count_map.get(da_id, 0) + 1
        
        print("=== daily_analysis 與 stock_analysis_scores 關聯狀態 ===")
        print(f"{'日期':<12} | {'ID':<4} | {'類型':<6} | {'模擬':<6} | {'關聯分數筆數':<8} | {'建立時間 (UTC)':<25}")
        print("-" * 80)
        for r in analyses:
            da_id = r["id"]
            cnt = count_map.get(da_id, 0)
            status = f"✅ {cnt} 筆" if cnt > 0 else "❌ 無分數"
            
            print(f"{r['analysis_date']:<12} | {da_id:<4} | {r['trigger_type']:<6} | {str(r['is_paper']):<6} | {status:<12} | {r['created_at']:<25}")
            
    except Exception as e:
        print(f"檢查失敗: {e}")

if __name__ == "__main__":
    check_relations()
