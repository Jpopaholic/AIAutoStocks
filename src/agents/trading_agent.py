# Path: src/agents/trading_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation
from src.services.trading_memory import get_experience_context

# 1. 定義單股交易決策模型
class StockDecision(BaseModel):
    stock_code: str = Field(
        ...,
        description="股票代號，例如 '2330'"
    )
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
        description="建議交易股數（支援零股交易，可為任意正整數，例如 10 代表 10 股；整股為 1000 股）。觀望或不操作時填 0。"
    )
    confidence: float = Field(
        ..., 
        description="決策置信度，介於 0.0 到 1.0 之間，數值越高代表買點/賣點越明確"
    )
    reason: str = Field(
        ..., 
        description="該檔股票的詳細分析理由與決策依據（使用繁體中文）。請結合技術指標與持股成本進行論述。"
    )

# 2. 定義多股組合決策模型 (強制 Structured Outputs)
class PortfolioDecision(BaseModel):
    decisions: List[StockDecision] = Field(
        ...,
        description="多個股票的決策列表。必須包含所有輸入分析的股票，每檔股票各一筆決策。"
    )

# 系統預設的金融交易技能清單
DEFAULT_TRADING_SKILLS = [
    "均線交叉策略 (Moving Average Cross): 當短線均線 (MA5) 向上突破長線均線 (MA20) 且有量能配合時，視為潛在黃金交叉買點；反之，跌破時視為死亡交叉賣點。",
    "相對強弱指標 (RSI): 評估短期超買與超賣狀態。RSI > 70 視為超買過熱（注意賣出/拉回），RSI < 30 視為超賣超跌（注意買入分批佈局）。",
    "嚴格風險控制與止損停利: 當帳戶目前持股之跌幅大於 5% 時，必須無條件發送 SELL 決策以停損；當持股獲利達 15% 時，考慮分批停利入袋為安。",
    "資金配置策略: 進行多股投資組合分析時，將資金分配給多個標的以分散風險（不要把雞蛋放在同一個籃子裡）。單筆買入之委託總額限制在可用資金之 20% 以內，遵守全局交易防呆上限，禁止單筆重倉孤注一擲。"
]

def generate_portfolio_decisions(
    stock_codes: List[str],
    klines_map: Dict[str, List[Dict[str, Any]]],
    current_holdings: List[Dict[str, Any]],
    extra_skills: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    整合多個股票的歷史 K 線、當前所有持股、過去交易記憶，利用 Gemini 進行多股聯合量化與資金配置決策。
    :param stock_codes: 股票代號列表 (如 ["2330", "2454"])
    :param klines_map: 各股票的 K 線歷史數據字典 (key: 股票代號, value: K線列表)
    :param current_holdings: 當前帳戶所有持股明細列表
    :param extra_skills: 使用者自訂多載入的額外金融交易技能列表
    :returns: 解析後的 PortfolioDecision JSON 字典
    """
    # 1. 處理並合併金融技能
    skills = list(DEFAULT_TRADING_SKILLS)
    if extra_skills:
        skills.extend(extra_skills)
    
    skills_text = "\n".join([f"- {s}" for s in skills])

    # 2. 獲取限額設定 (動態計算)
    from src.services.nav_calculator import get_dynamic_limits
    single_limit, daily_limit = get_dynamic_limits()

    # 3. 構建 System Instruction (系統提示詞)
    system_instruction = f"""
你是一個資深的台股量化投資與多股投資組合（Portfolio）配置分析專家。你熟悉台股市場特性、技術線圖分析與風控原則。
你的任務是分析給定的多個個股的 K 線數據、目前帳戶的所有持股現況與過往的平倉成敗經驗，生成一份標準的多股交易決策 JSON。

你的金融分析技能與風控準則包含：
{skills_text}

請嚴格遵守以下交易限制與指示：
1. 你的輸出必須完全符合所規定的 JSON Schema，不可包含額外文字。
2. 本系統支援「零股交易」，你可以指定任意股數（例如 10 股、100 股或整股 1000 股）。若無操作 (HOLD)，股數必須填 0。
3. 若你的決策是賣出 (SELL)，委託股數絕對不可以大於目前持有的股數。
4. 你的分析與理由請一律使用「繁體中文」。
5. 【金額安全限制與資金配置限制】：
   - 本帳戶單筆交易最大金額上限為：{single_limit:,.0f} 元新台幣。
   - 本帳戶每日累計交易最大金額上限為：{daily_limit:,.0f} 元新台幣。
   - 若你決定對某些股票進行買入 (BUY)，該筆買入委託金額（建議價格 * 建議股數）絕對不可超過單筆上限（{single_limit:,.0f} 元）。
   - 請根據目前多檔股票的走勢，綜合評估相對強弱，合理分配買入額度，以實現資產分散配置（不要把雞蛋放在同一個籃子裡），同時總額不能超出每日限制。
"""

    # 4. 準備 User Prompt 變數
    # 格式化所有持股現況
    holdings_lines = []
    for h in current_holdings:
        if float(h.get("quantity", 0)) > 0:
            holdings_lines.append(
                f"- 股票 {h['stock_code']}: 持有 {float(h['quantity']):,.0f} 股，買入均價 {float(h['average_price']):,.2f} 元"
            )
    
    if holdings_lines:
        holdings_info = "【當前帳戶所有持股現況】:\n" + "\n".join(holdings_lines)
    else:
        holdings_info = "【當前帳戶所有持股現況】: 目前帳戶內無任何持股倉位。"

    # 取得交易經驗 Few-shot 上下文
    experience_context = get_experience_context(limit=3)

    # 格式化各股票最近 30 天日 K 線數據
    klines_sections = []
    for code in stock_codes:
        klines = klines_map.get(code, [])
        recent_klines = klines[-30:]
        klines_lines = []
        for k in recent_klines:
            klines_lines.append(
                f"  日期: {k['date']} | 開盤: {k['open']} | 最高: {k['high']} | "
                f"最低: {k['low']} | 收盤: {k['close']} | 成交量: {k['volume']:,.0f}"
            )
        klines_text = "\n".join(klines_lines)
        klines_sections.append(
            f"● 股票代號 {code} 最近 30 天 K 線數據 (最下方為最新一日行情)：\n{klines_text}"
        )
    all_klines_text = "\n\n".join(klines_sections)

    user_prompt = f"""
請針對股票列表 {stock_codes} 進行多股投資組合分析與配置決策。

{holdings_info}

【過往平倉交易記憶】:
{experience_context}

{all_klines_text}

請結合上述多檔股票之 K 線、持股成本與歷史交易教訓，基於多股資產分散原則與限額規定，發布本次投資組合決策。
"""

    # 5. 調用 Gemini 金鑰輪替調用器，強制使用 Structured Outputs (PortfolioDecision)
    generation_config = {
        "response_mime_type": "application/json",
        "response_schema": PortfolioDecision
    }

    try:
        raw_response = call_gemini_with_rotation(
            prompt=user_prompt,
            system_instruction=system_instruction,
            model_name=config.gemini_model,
            generation_config=generation_config
        )
        # 解析返回的 JSON 結構
        decision_data = json.loads(raw_response)
        
        # 進行最後安全覆核 (避免 AI 違反規則)
        # 例如：賣出股數不得大於持股數
        decisions = decision_data.get("decisions", [])
        for d in decisions:
            # 相容各種股票代號鍵名與大小寫
            possible_keys = ["stock_code", "stockCode", "stockcode", "StockCode", "code", "stock"]
            resolved_code = None
            for key in possible_keys:
                if key in d and d[key]:
                    resolved_code = str(d[key]).strip()
                    break
            
            # 若仍未解析成功，嘗試從 reason 內容比對
            if not resolved_code:
                reason_text = str(d.get("reason", ""))
                for c in stock_codes:
                    if c in reason_text:
                        resolved_code = c
                        break
            
            if resolved_code:
                d["stock_code"] = resolved_code
                
            code = d.get("stock_code")
            if d.get("action") == "SELL":
                matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
                holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
                if float(d.get("quantity", 0)) > holding_qty:
                    print(f" [AI交易代理] 警報: AI 賣出 {code} 股數大於目前持有股數，自動校正為全部賣出。")
                    d["quantity"] = holding_qty
                if holding_qty <= 0:
                    print(f" [AI交易代理] 警報: 帳戶無 {code} 持股，AI 仍決策賣出，自動校正為 HOLD。")
                    d["action"] = "HOLD"
                    d["quantity"] = 0
                    
        return decision_data
    except Exception as e:
        print(f" [AI交易代理] 投資組合決策生成失敗: {str(e)}")
        # 回退至安全觀望決策 (所有股票皆 HOLD)
        fallback_decisions = []
        for code in stock_codes:
            klines = klines_map.get(code, [])
            fallback_decisions.append({
                "stock_code": code,
                "action": "HOLD",
                "price": klines[-1]["close"] if klines else 10.0,
                "quantity": 0,
                "confidence": 0.0,
                "reason": f"決策引擎調用出錯，自動回退至觀望模式。錯誤詳情: {str(e)}"
            })
        return {"decisions": fallback_decisions}
