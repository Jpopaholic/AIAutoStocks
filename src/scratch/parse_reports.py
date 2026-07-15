import re

file_path = "/Users/jpopaholic/Documents/AIAutoStocks/src/scratch/last_user_message.txt"

def parse():
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 將文字按報告區塊拆分
    sections = re.split(r"【AI(?:手動)?交易報告】", content)
    
    parsed_data = {}

    for sec in sections:
        if not sec.strip():
            continue
        
        # 尋找日期，格式為 YYYY-MM-DD
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", sec)
        if not date_match:
            continue
        date_str = date_match.group(1)
        
        if date_str not in parsed_data:
            parsed_data[date_str] = {
                "scores": {},
                "decisions": {}
            }
            
        # 1. 解析第二層評分與相對排名 (評分排名列表)
        # 支援有無排行序號 (e.g. "1. 2330 台積電" 或 "2330 台積電")
        score_pattern = r"(?:\d+\.\s+)?([A-Za-z0-9]+)\s+(.*?)\s*\|\s*總分:\s*\d+\s*\(趨勢:(\d+)\s+動能:(\d+)\s+成交量:(\d+)\s+安全:(\d+)\s+大盤:(\d+)\)"
        scores = re.findall(score_pattern, sec)
        for stock_code, name, trend, momentum, volume, safety, regime in scores:
            parsed_data[date_str]["scores"][stock_code] = {
                "trend": int(trend),
                "momentum": int(momentum),
                "volume": int(volume),
                "safety": int(safety),
                "regime": int(regime)
            }
            
        # 2. 解析第三層決策 (BUY/SELL/HOLD) 及其內嵌評分 (逐行處理)
        lines = sec.split("\n")
        current_code = None
        for line in lines:
            # 支援各種前綴，像是 "⚪ HOLD 3711" 或 "🔴 SELL 2618" 或 "* BUY 2330"
            dec_match = re.search(r"\b(BUY|SELL|HOLD)\s+([A-Za-z0-9]+)\b", line)
            if dec_match:
                action = dec_match.group(1)
                current_code = dec_match.group(2)
                parsed_data[date_str]["decisions"][current_code] = action
            elif current_code:
                # 檢查這行是否有內嵌評分
                # 範例: └ 原因: 【量化評分觀望】...【量化評分: 31分 (趨勢:7 | 動能:6 | 量能:5 | 安全:7 | 大盤:6)】
                inline_score_match = re.search(
                    r"【量化評分:\s*\d+\s*分\s*\(趨勢:(\d+)[\s|]*動能:(\d+)[\s|]*(?:量能|成交量|量力):(\d+)[\s|]*安全:(\d+)[\s|]*大盤:(\d+)\)】",
                    line
                )
                if inline_score_match:
                    trend, momentum, volume, safety, regime = inline_score_match.groups()
                    if current_code not in parsed_data[date_str]["scores"]:
                        parsed_data[date_str]["scores"][current_code] = {
                            "trend": int(trend),
                            "momentum": int(momentum),
                            "volume": int(volume),
                            "safety": int(safety),
                            "regime": int(regime)
                        }

    # 輸出解析結果摘要
    for date_str, data in sorted(parsed_data.items()):
        print(f"\n日期: {date_str}")
        print(f"  解析到評分的股票數量: {len(data['scores'])}")
        print(f"  解析到決策的股票數量: {len(data['decisions'])}")
        
        # 列出比對結果
        all_codes = set(data["scores"].keys()) | set(data["decisions"].keys())
        print(f"  總股票清單 (共 {len(all_codes)} 檔):")
        for code in sorted(all_codes):
            sc = data["scores"].get(code)
            dec = data["decisions"].get(code, "UNKNOWN")
            if sc:
                score_str = f"評分: 趨勢:{sc['trend']} 動能:{sc['momentum']} 量能:{sc['volume']} 安全:{sc['safety']} 大盤:{sc['regime']}"
            else:
                score_str = "無評分"
            print(f"    - {code}: 決策: {dec} | {score_str}")

if __name__ == "__main__":
    parse()
