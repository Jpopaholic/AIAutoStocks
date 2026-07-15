import os
import sys
import re
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import execute_with_retry, supabase

file_path = "/Users/jpopaholic/Documents/AIAutoStocks/src/scratch/last_user_message.txt"

def get_analysis_id(date_str, report_type):
    try:
        res = execute_with_retry(
            lambda: supabase.table("daily_analysis")
            .select("id")
            .eq("analysis_date", date_str)
            .eq("trigger_type", report_type)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res:
            return res[0]["id"]
        
        # 降級查找任何類型的分析
        res_any = execute_with_retry(
            lambda: supabase.table("daily_analysis")
            .select("id")
            .eq("analysis_date", date_str)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res_any:
            return res_any[0]["id"]
    except Exception as e:
        print(f"查詢 daily_analysis ID 失敗 ({date_str}): {e}")
    return None

def run_import():
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 將文字按報告區塊拆分
    sections = re.split(r"【AI(?:手動)?交易報告】", content)
    
    total_inserted = 0

    for sec in sections:
        if not sec.strip():
            continue
        
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", sec)
        if not date_match:
            continue
        date_str = date_match.group(1)
        
        # 判斷是否為手動交易報告
        is_manual_report = "手動交易報告" in sec or "手動分析" in sec
        report_type = "manual" if is_manual_report else "auto"
        
        analysis_id = get_analysis_id(date_str, report_type)
        print(f"\n處理日期: {date_str} (報告類型: {report_type}) -> 關聯 daily_analysis ID: {analysis_id}")
        
        parsed_scores = {}
        parsed_decisions = {}
            
        # 1. 解析評分排名列表 (支援選配排行序號)
        score_pattern = r"(?:\d+\.\s+)?([A-Za-z0-9]+)\s+(.*?)\s*\|\s*總分:\s*\d+\s*\(趨勢:(\d+)\s+動能:(\d+)\s+成交量:(\d+)\s+安全:(\d+)\s+大盤:(\d+)\)"
        scores = re.findall(score_pattern, sec)
        for stock_code, name, trend, momentum, volume, safety, regime in scores:
            parsed_scores[stock_code] = {
                "trend_score": int(trend),
                "momentum_score": int(momentum),
                "volume_score": int(volume),
                "safety_score": int(safety),
                "regime_score": int(regime)
            }
            
        # 2. 解析決策與內嵌評分 (逐行處理，使用 \b 匹配以支援 emoji 標記)
        lines = sec.split("\n")
        current_code = None
        for line in lines:
            dec_match = re.search(r"\b(BUY|SELL|HOLD)\s+([A-Za-z0-9]+)\b", line)
            if dec_match:
                action = dec_match.group(1)
                current_code = dec_match.group(2)
                parsed_decisions[current_code] = action
            elif current_code:
                inline_score_match = re.search(
                    r"【量化評分:\s*\d+\s*分\s*\(趨勢:(\d+)[\s|]*動能:(\d+)[\s|]*(?:量能|成交量|量力):(\d+)[\s|]*安全:(\d+)[\s|]*大盤:(\d+)\)】",
                    line
                )
                if inline_score_match and current_code not in parsed_scores:
                    trend, momentum, volume, safety, regime = inline_score_match.groups()
                    parsed_scores[current_code] = {
                        "trend_score": int(trend),
                        "momentum_score": int(momentum),
                        "volume_score": int(volume),
                        "safety_score": int(safety),
                        "regime_score": int(regime)
                    }

        # 批次組裝 records 並寫入資料庫
        records = []
        all_codes = set(parsed_scores.keys()) | set(parsed_decisions.keys())
        for code in sorted(all_codes):
            sc = parsed_scores.get(code)
            dec = parsed_decisions.get(code, "HOLD")
            
            trend = sc["trend_score"] if sc else 0
            momentum = sc["momentum_score"] if sc else 0
            volume = sc["volume_score"] if sc else 0
            safety = sc["safety_score"] if sc else 0
            regime = sc["regime_score"] if sc else 0
            
            records.append({
                "daily_analysis_id": analysis_id,
                "analysis_date": date_str,
                "stock_code": code,
                "trend_score": trend,
                "momentum_score": momentum,
                "volume_score": volume,
                "safety_score": safety,
                "regime_score": regime,
                "decision": dec,
                "is_paper": False,  # 實際操盤 is_paper = False
            })

        if records:
            try:
                execute_with_retry(
                    lambda: supabase.table("stock_analysis_scores")
                    .insert(records)
                    .execute()
                )
                print(f"  成功寫入 {len(records)} 筆個股評分與決策紀錄至 stock_analysis_scores。")
                total_inserted += len(records)
            except Exception as e:
                print(f"  寫入資料庫失敗: {e}")

    print(f"\n總共成功匯入 {total_inserted} 筆資料。")

if __name__ == "__main__":
    run_import()
