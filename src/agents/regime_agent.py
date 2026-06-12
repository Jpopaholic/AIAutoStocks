# Path: src/agents/regime_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation

# 1. 定義大盤氣候診斷模型
class MarketRegimeAssessment(BaseModel):
    regime: str = Field(
        ...,
        description="市場狀態，必須限為 'BULLISH_TREND' (多頭趨勢), 'BEARISH_TREND' (空頭趨勢), 'CALM_RANGE' (低波動盤整), 'VOLATILE_RANGE' (高波動震盪)"
    )
    posture: str = Field(
        ...,
        description="交易姿態，必須限為 'AGGRESSIVE' (積極進攻), 'NORMAL' (正常操作), 'DEFENSIVE' (防禦空倉)"
    )
    risk_multiplier: float = Field(
        ...,
        description="風險限額乘數，介於 0.0 到 1.0 之間。0.0 代表完全空手防禦不進行任何新增買入，1.0 代表維持正常交易額度"
    )
    reason: str = Field(
        ...,
        description="判斷當前市場狀態的詳細理由與宏觀分析依據 (繁體中文)，請分析短期均線、價格趨勢與成交量變化"
    )

def generate_market_regime(taiex_klines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    分析大盤 K 線數據，判定當前市場 Regime、交易姿態與風險乘數。
    :param taiex_klines: 大盤加權指數的 K 線歷史數據列表
    :returns: 解析後的 MarketRegimeAssessment JSON 字典
    """
    if not taiex_klines:
        print(" [Regime Layer] 警告: 未收到大盤 K 線數據，將退回預設正常狀態")
        return {
            "regime": "CALM_RANGE",
            "posture": "NORMAL",
            "risk_multiplier": 1.0,
            "reason": "無可用的大盤 K 線數據，自動退回到預設正常盤整狀態。"
        }

    # 僅取最近 30 天的大盤 K 線，避免 context 長度過長
    recent_taiex = taiex_klines[-30:]
    
    taiex_lines = []
    for k in recent_taiex:
        # 相容兩種欄位命名格式 (DB 用底線，程式用駝峰)
        o_price = k.get("open") or k.get("openPrice") or 0.0
        h_price = k.get("high") or k.get("highPrice") or 0.0
        l_price = k.get("low") or k.get("lowPrice") or 0.0
        c_price = k.get("close") or k.get("closePrice") or 0.0
        volume = k.get("volume") or 0
        
        taiex_lines.append(
            f"  日期: {k.get('date', '')} | 開盤: {float(o_price):.2f} | 最高: {float(h_price):.2f} | "
            f"最低: {float(l_price):.2f} | 收盤: {float(c_price):.2f} | 成交量: {int(volume):,}"
        )
    taiex_text = "\n".join(taiex_lines)

    system_instruction = (
        "你是一個資深的台股宏觀市場分析專家，擅長透過大盤指數走勢、成交量變化、均線排列與波動趨勢來判斷當前的市場狀態 (Market Regime)。\n"
        "你的任務是分析給定的大盤指數 K 線數據，判定目前的市場狀態 (Regime)、應採取的交易姿態 (Posture) 與風險限額乘數 (Risk Multiplier)。\n\n"
        "市場狀態 (regime) 定義：\n"
        "- 'BULLISH_TREND': 大盤均線呈現多頭排列，或收盤價高於移動平均線，且近期呈現明顯上漲趨勢。\n"
        "- 'BEARISH_TREND': 大盤均線呈現空頭排列，或收盤價低於移動平均線，且近期呈現明顯下跌趨勢。\n"
        "- 'CALM_RANGE': 大盤無明顯趨勢，價格在一定區間內窄幅波動，成交量偏低，波動率低。\n"
        "- 'VOLATILE_RANGE': 大盤波動劇烈，單日大漲大跌，方向不明，市場情緒恐慌或極度不穩定。\n\n"
        "交易姿態 (posture) 與風險乘數 (risk_multiplier) 建議指引：\n"
        "- BULLISH_TREND (多頭): posture = 'AGGRESSIVE' 或 'NORMAL'，risk_multiplier = 0.8 ~ 1.0。\n"
        "- BEARISH_TREND (空頭): posture = 'DEFENSIVE'，risk_multiplier = 0.0 ~ 0.2。請強烈傾向於 0.0 (空手防守) 除非大盤有極強反彈信號，否則絕不可大於 0.2。\n"
        "- CALM_RANGE (盤整): posture = 'NORMAL'，risk_multiplier = 0.5 ~ 0.8。\n"
        "- VOLATILE_RANGE (劇烈波動): posture = 'DEFENSIVE'，risk_multiplier = 0.1 ~ 0.4。\n\n"
        "你的輸出必須完全符合所規規定之 JSON Schema，分析理由與依據請一律使用「繁體中文」。"
    )

    user_prompt = (
        f"請根據以下大盤加權指數 (TAIEX) 最近 30 天日 K 線數據，分析當前的市場狀態與風險限額乘數：\n\n"
        f"【大盤加權指數 (TAIEX) 最近 30 天數據 (最下方為最新一日行情)】：\n"
        f"{taiex_text}\n"
    )

    generation_config = {
        "response_mime_type": "application/json",
        "response_schema": MarketRegimeAssessment,
        "temperature": 0.0
    }

    try:
        raw_response = call_gemini_with_rotation(
            prompt=user_prompt,
            system_instruction=system_instruction,
            model_name=config.gemini_model,
            generation_config=generation_config
        )
        return json.loads(raw_response)
    except Exception as e:
        print(f" [Regime Layer] 大盤氣候判定失敗: {str(e)}")
        return {
            "regime": "CALM_RANGE",
            "posture": "NORMAL",
            "risk_multiplier": 1.0,
            "reason": f"大盤氣候判定調用出錯，自動回退至預設正常狀態。錯誤詳情: {str(e)}"
        }
