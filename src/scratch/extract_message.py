import os
import json

log_path = "/Users/jpopaholic/.gemini/antigravity-ide/brain/affe4956-5a83-474d-8357-462fff87fb4b/.system_generated/logs/transcript_full.jsonl"
out_path = "/Users/jpopaholic/Documents/AIAutoStocks/src/scratch/last_user_message.txt"

def extract():
    last_user_content = None
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if data.get("type") == "USER_INPUT":
                    last_user_content = data.get("content")
            except Exception as e:
                pass
                
    if last_user_content:
        with open(out_path, "w", encoding="utf-8") as out:
            out.write(last_user_content)
        print(f"成功提取最後一筆用戶訊息，長度：{len(last_user_content)} 字元，已儲存至 {out_path}")
    else:
        print("未找到 USER_INPUT 類型的日誌紀錄。")

if __name__ == "__main__":
    extract()
