# Path: src/agents/trading_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation
from src.services.trading_memory import get_experience_context

# 定義 Gemini 輸出的 Pydantic BaseModel (強制 Structured Outputs)
class TradingDecision(BaseModel):
    action: str = Field(
        ..., 
        description="交易決策，必須限為 'BUY' (買入)、'SELL' (賣出) 或 'HOLD' (觀望/持股)"
    )
    price: float = Field(
        ..., 
        description="建議交易委託價格（新台幣，必須大於 0）。觀望時可填目前最新收盤價。"
    )
    quantity: float = Field(
        ..., 
        description="建議交易股數（必須為 1000 的倍數，例如 1000 代表 1 張）。觀望或不操作時填 0。"
    )
    confidence: float = Field(
        ..., 
        description="決策置信度，介於 0.0 到 1.0 之間，數值越高代表買點/賣點越明確"
    )
    reason: str = Field(
        ..., 
        description="詳細分析理由與決策依據（使用繁體中文）。請結合技術指標（如均線、RSI、成交量變化）與持股成本進行論述。"
    )

# 系統預設的金融交易技能清單
DEFAULT_TRADING_SKILLS = [
    "均線交叉策略 (Moving Average Cross): 當短線均線 (MA5) 向上突破長線均線 (MA20) 且有量能配合時，視為潛在黃金交叉買點；反之，跌破時視為死亡交叉賣點。",
    "相對強弱指標 (RSI): 評估短期超買與超賣狀態。RSI > 70 視為超買過熱（注意賣出/拉回），RSI < 30 視為超賣超跌（注意買入分批佈局）。",
    "嚴格風險控制與止損停利: 當帳戶目前持股之跌幅大於 5% 時，必須無條件發送 SELL 決策以停損；當持股獲利達 15% 時，考慮分批停利入袋為安。",
    "資金配置策略: 單筆買入之委託總額限制在可用資金之 20% 以內，遵守全局交易防呆上限，禁止單筆重倉孤注一擲。"
]

def generate_trading_decision(
    stock_code: str,
    klines: List[Dict[str, Any]],
    current_holding: Optional[Dict[str, Any]] = None,
    extra_skills: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    整合股票歷史 K 線、目前持股、過去交易記憶，利用 Gemini 進行量化決策生成。
    :param stock_code: 股票代號 (如 "2330")
    :param klines: 歷史 K 線與即時價格數據
    :param current_holding: 當前持股明細 (包含 quantity, average_price)，若無則為 None
    :param extra_skills: 使用者自訂多載入的額外金融交易技能列表
    :returns: 解析後的交易決策 JSON 字典
    """
    # 1. 處理並合併金融技能
    skills = list(DEFAULT_TRADING_SKILLS)
    if extra_skills:
        skills.extend(extra_skills)
    
    skills_text = "\n".join([f"- {s}" for s in skills])

    # 2. 構建 System Instruction (系統提示詞)
    system_instruction = f"""
你是一個資深的台股量化投資分析與下單決策專家。你熟悉台股市場特性、技術線圖分析與風控原則。
你的任務是分析給定個股的 K 線數據、目前持股現況與過往的平倉成敗經驗，生成一份標準的交易決策 JSON。

你的金融分析技能與風控準則包含：
{skills_text}

請嚴格遵守以下交易限制與指示：
1. 你的輸出必須完全符合所規定的 JSON Schema，不可包含額外文字。
2. 委託股數 (quantity) 必須是 1000 的倍數（台股整股委託以 1000 股即 1 張為單位）。若無操作 (HOLD)，股數必須填 0。
3. 若你的決策是賣出 (SELL)，委託股數絕對不可以大於目前持有的股數。
4. 你的分析與理由請一律使用「繁體中文」。請詳細說明你基於哪些 K 線現象、技術指標與獲利/虧損率做出此決定。
5. 單筆委託金額 (price * quantity) 必須符合防呆機制，若估計超額應自動調降股數。
"""

    # 3. 準備 User Prompt 變數
    # 格式化持股現況
    if current_holding and float(current_holding.get("quantity", 0)) > 0:
        holding_info = (
            f"目前持有該股 {float(current_holding['quantity']):,.0f} 股，"
            f"買入均價為 {float(current_holding['average_price']):,.2f} 元。"
        )
    else:
        holding_info = "目前未持有該股倉位。"

    # 取得交易經驗 Few-shot 上下文
    experience_context = get_experience_context(limit=3)

    # 格式化 K 線歷史數據 (只取最近 30 天以節省 token)
    recent_klines = klines[-30:]
    klines_lines = []
    for k in recent_klines:
        klines_lines.append(
            f"日期: {k['date']} | 開盤: {k['open']} | 最高: {k['high']} | "
            f"最低: {k['low']} | 收盤: {k['close']} | 成交量: {k['volume']:,.0f}"
        )
    klines_text = "\n".join(klines_lines)

    user_prompt = f"""
請針對股票代號 {stock_code} 進行交易分析與決策。

【當前持股現況】:
{holding_info}

【過往平倉交易記憶】:
{experience_context}

【最近 30 天日 K 線歷史數據】(最下方為最新一日行情)：
{klines_text}

請結合上述 K 線、持股成本與歷史交易教訓，基於你的量化金融技能發布本次交易決策。
"""

    # 4. 調用 Gemini 金鑰輪替調用器，強制使用 Structured Outputs (TradingDecision)
    generation_config = {
        "response_mime_type": "application/json",
        "response_schema": TradingDecision
    }

    try:
        raw_response = call_gemini_with_rotation(
            prompt=user_prompt,
            system_instruction=system_instruction,
            model_name="gemini-1.5-flash",
            generation_config=generation_config
        )
        # 解析返回的 JSON 結構
        decision_data = json.loads(raw_response)
        
        # 進行最後安全覆核 (避免 AI 違反規則)
        # 例如：賣出股數不得大於持股數
        if decision_data.get("action") == "SELL":
            holding_qty = float(current_holding.get("quantity", 0)) if current_holding else 0.0
            if float(decision_data.get("quantity", 0)) > holding_qty:
                print(" [AI交易代理] 警報: AI 賣出股數大於目前持有股數，自動校正為全部賣出。")
                decision_data["quantity"] = holding_qty
            if holding_qty <= 0:
                print(" [AI交易代理] 警報: 帳戶無持股，AI 仍決策賣出，自動校正為 HOLD。")
                decision_data["action"] = "HOLD"
                decision_data["quantity"] = 0
                
        return decision_data
    except Exception as e:
        print(f" [AI交易代理] 決策生成失敗: {str(e)}")
        # 回退至安全觀望決策，確保系統定時任務不中斷崩潰
        return {
            "action": "HOLD",
            "price": klines[-1]["close"] if klines else 10.0,
            "quantity": 0,
            "confidence": 0.0,
            "reason": f"決策引擎調用出錯，自動回退至觀望模式。錯誤詳情: {str(e)}"
        }
