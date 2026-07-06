import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import json
from src.agents.trading_agent import AnalystAssessment, DEFAULT_TRADING_SKILLS
from src.services.gemini_rotator import call_gemini_with_rotation
from src.config import config

stock_codes = ["2303", "2618"]
klines_map = {}
for code in stock_codes + ["TAIEX"]:
    klines_map[code] = [
        {
            "date": "2026-07-06",
            "open": 50.0,
            "high": 51.0,
            "low": 49.5,
            "close": 50.5,
            "volume": 100000.0,
            "ma5": 50.2,
            "ma20": 49.8,
            "ma60": 49.0,
            "rsi14": 55.0,
            "vol_ma5": 90000.0,
            "vol_ma20": 85000.0,
            "macd": 0.5,
            "macd_signal": 0.3,
            "macd_hist": 0.2,
            "plus_di": 22.0,
            "minus_di": 18.0,
            "adx": 26.0
        }
    ]

skills_text = "\n".join([f"- {s}" for s in DEFAULT_TRADING_SKILLS])

analyst_system_instruction = f"""
你是一個極其資深且敏銳的台股量化投資分析師。你熟悉台股市場特性與技術線圖分析。
你的任務是分析給定的多個個股的日 K 線數據，進行獨立且客觀的量化評分與技術指標分析。
你需要為每檔個股進行五個維度的評分 (各個維度為 0 ~ 20 分，總分 0 ~ 100 分)。

【重要評分提示】：
- 你的評分是為了反映個股強弱。如果個股呈現多頭排列、放量突破、MACD翻紅或RSI強勢等信號，請勇敢地給予高分（趨勢得分與動能得分可達16~20分，總分達80~95分）。
- 請拉開評分差距，切忌盲目保守。不要將所有的股票都擠在 10 ~ 13 分的中庸區間（導致總分都在 50 ~ 65 分徘徊）。強勢股給予高分，弱勢股給予低分。
- 切忌僅憑單一負面訊號就直接全盤否定個股價值。

量化評分維度指引：
1. 趨勢得分 (trend_score, 0 ~ 20 分)：評估均線排列（MA5、MA20、MA60）與價格波段高低點。多頭排列、價格站穩在均線之上得高分；空頭排列或價格跌破均線得低分。
2. 動能得分 (momentum_score, 0 ~ 20 分)：評估 RSI、MACD 柱狀圖多空動能強弱與黃金/死亡交叉狀態。動能轉強、柱狀圖翻紅、黃金交叉得高分；動能消退、柱狀圖翻綠、死亡交叉得低分。
3. 成交量得分 (volume_score, 0 ~ 20 分)：評估成交量是否價漲量增、量價配合度、VOL_MA5 與 VOL_MA20 關係。放量突破、量價配合得高分；無量盤整或量價背離得低分。
4. 安全與防守得分 (safety_score, 0 ~ 20 分)：評估下方支撐力道與防守空間。股價回檔至強支撐、乖離率小、防守空間大得高分（代表安全）；股價突破上檔阻力但乖離率過大、高檔超買或支撐跌破得低分。
5. 大盤一致性得分 (regime_score, 0 ~ 20 分)：結合當前大盤加權指數狀態。若大盤多頭且個股強於大盤得高分，若大盤氣候不佳但個股展現出強悍的抗跌性與獨立行情，亦應給予合理高得分。

你的金融量化分析技能包含：
{skills_text}

請嚴格遵守以下指示：
1. 你的輸出必須完全符合所規定的 JSON Schema (AnalystAssessment)，不可包含額外文字。
2. `total_score` 必須嚴格等於 `trend_score + momentum_score + volume_score + safety_score + regime_score` 的加總。
3. 你的分析與理由請一律使用「繁體中文」。
4. 價格合理性重要規則：`price` 必須符合市場行情（最新收盤價的 ±2% 內），請填寫最新收盤價。
"""

analyst_user_prompt = f"""
請針對股票列表 {stock_codes} 進行個股日 K 線的技術分析與評分。
"""

generation_config_analyst = {
    "response_mime_type": "application/json",
    "response_schema": AnalystAssessment,
    "temperature": 0.0
}

try:
    print("呼叫 Gemini...")
    raw = call_gemini_with_rotation(
        prompt=analyst_user_prompt,
        system_instruction=analyst_system_instruction,
        model_name=config.gemini_model,
        generation_config=generation_config_analyst
    )
    print("--- RAW RESPONSE ---")
    print(f"Length: {len(raw)}")
    print(raw[:1000])
    if len(raw) > 1000:
        print("...")
        print(raw[-500:])
    # 測試解析
    json.loads(raw)
    print("✅ 解析成功！")
except Exception as e:
    print(f"❌ 錯誤: {e}")
