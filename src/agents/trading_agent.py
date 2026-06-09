# Path: src/agents/trading_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation, DailyRateLimitExceeded
from src.services.trading_memory import get_experience_context
from src.services.supabase_client import get_orders, get_system_fault_status, get_pending_liquidation_stocks

# 1. 定義單股交易決策模型
class StockDecision(BaseModel):
    stock_code: str = Field(
        ...,
        description="必須填寫 4 碼股票代號字串，例如 '2330'，不可填寫中文名稱或留空。此欄位必須與輸入的股票列表代號完全一致。"
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
    "資金配置策略: 進行多股投資組合分析時，將資金分配給多個標的以分散風險（不要把雞蛋放在同一個籃子裡）。單筆買入之委託總額限制在可用資金之 20% 以內，遵守全局交易防呆上限，禁止單筆重倉孤注一擲。",
    "【停損買回冷卻】：若在「近期帳戶交易歷史」中，某檔股票在當天剛剛執行過賣出 (SELL) 且為虧損平倉（即停損），則今日絕對禁止再次對該檔股票發送買入 (BUY) 決策，避免陷入重複追高殺低。",
    "【大盤趨勢防禦】：大盤加權指數 (TAIEX) 是整體市場走向的風向球。若大盤指數收盤跌破其 MA20 (即大盤收盤價最近 20 天的簡單移動平均)，代表大盤已步入弱勢或空頭排列，此時應嚴格採取防守策略，原則上禁止買入 (BUY) 新增任何持股或部位（除非個股有極強的置信度與特大個股利多），並應主動減持手上已持有的高風險股票以規避市場崩跌風險；若大盤收盤站穩在 MA20 之上，則可正常執行交易與買入評估。"
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
    # 0. 系統性防禦故障阻斷
    try:
        fault_state = get_system_fault_status()
        if fault_state.get("status") == "FAULT":
            print(f" [AI交易代理] 警告: 系統目前處於全局故障鎖定狀態！原因: {fault_state.get('detail')}")
            fallback_decisions = []
            for code in stock_codes:
                klines = klines_map.get(code, [])
                fallback_decisions.append({
                    "stock_code": code,
                    "action": "HOLD",
                    "price": klines[-1]["close"] if klines else 10.0,
                    "quantity": 0.0,
                    "confidence": 0.0,
                    "reason": f"系統處於故障安全防禦鎖定狀態 (SYSTEM FAULT)，已暫停所有交易。故障原因: {fault_state.get('detail')}"
                })
            return {"decisions": fallback_decisions}
    except Exception as e:
        print(f" [AI交易代理] 讀取系統故障狀態失敗: {str(e)}")

    # 1. 處理並合併金融技能
    skills = list(DEFAULT_TRADING_SKILLS)
    if extra_skills:
        skills.extend(extra_skills)
    
    skills_text = "\n".join([f"- {s}" for s in skills])

    # 2. 獲取限額設定 (動態計算) 與帳戶資金狀況
    from src.services.nav_calculator import get_dynamic_limits, calculate_nav
    single_limit, daily_limit = get_dynamic_limits()
    
    try:
        cash_balance, holdings_value, net_asset_value = calculate_nav()
    except Exception:
        cash_balance = config.limits.initial_cash
        holdings_value = 0.0
        net_asset_value = cash_balance

    # 2.1 獲取智慧等候平倉排隊中股票代號
    try:
        pending_stocks = get_pending_liquidation_stocks()
    except Exception as e:
        print(f" [AI交易代理] 獲取等候平倉股票失敗: {str(e)}")
        pending_stocks = []

    pending_instruction = ""
    if pending_stocks:
        pending_instruction = f"\n6. 【智慧平倉排隊】：當前股票 {', '.join(pending_stocks)} 處於等候平倉狀態（因先前停損委託未能成交或跌停鎖死）。對這些處於等候平倉狀態的股票，你「絕對禁止發出買入 (BUY)」決策。請合理評估當前 K 線與大盤買氣：若該股持續疲弱無買氣支撐，請給出 'SELL' 以便系統繼續掛單排隊平倉；若個股出現反彈信號或有暫緩賣出之需要，可給出 'HOLD' 以暫時停在持股中觀望。"

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
   - 本次交易的所有買入委託總金額，絕對不可超出可用現金餘額。
   - 請根據目前多檔股票的走勢，綜合評估相對強弱，合理分配買入額度，以實現資產分散配置（不要把雞蛋放在同一個籃子裡），同時總額不能超出每日限制。{pending_instruction}
"""

    # 4. 準備 User Prompt 變數
    # 格式化帳戶資金狀況
    funds_info = (
        "【當前帳戶資金現況】:\n"
        f"- 可用現金餘額 (Cash): {cash_balance:,.0f} 元新台幣\n"
        f"- 持股總市值 (Portfolio Value): {holdings_value:,.0f} 元新台幣\n"
        f"- 總資產淨值 (NAV): {net_asset_value:,.0f} 元新台幣"
    )

    # 格式化所有持股現況
    holdings_lines = []
    for h in current_holdings:
        if float(h.get("quantity", 0)) > 0:
            stock_code = h["stock_code"]
            status_tag = ""
            if stock_code in pending_stocks:
                status_tag = " [⚠️智慧等候平倉排隊中/跌停鎖死]"
            holdings_lines.append(
                f"- 股票 {stock_code}: 持有 {float(h['quantity']):,.0f} 股，買入均價 {float(h['average_price']):,.2f} 元{status_tag}"
            )
    
    if holdings_lines:
        holdings_info = "【當前帳戶所有持股現況】:\n" + "\n".join(holdings_lines)
    else:
        holdings_info = "【當前帳戶所有持股現況】: 目前帳戶內無任何持股倉位。"

    # 取得交易經驗 Few-shot 上下文
    experience_context = get_experience_context(limit=3)

    # 取得近期交易歷史 (最新 10 筆)
    recent_orders_lines = []
    try:
        all_orders = get_orders()
        recent_orders = all_orders[:10]  # get_orders 預設已依 executed_at 降序排序
        for o in recent_orders:
            exec_time = o.get("executed_at", "")
            time_label = exec_time.replace("T", " ").replace("Z", "")[:19]
            action_label = "買入 (BUY)" if o.get("action") == "BUY" else "賣出 (SELL)"
            pnl_val = float(o.get("realized_pnl") or 0.0)
            pnl_label = f" | 實現損益: {pnl_val:+,.0f} 元" if o.get("action") == "SELL" else ""
            recent_orders_lines.append(
                f"  - {time_label} | {action_label} {o.get('stock_code')} | "
                f"價格: {float(o.get('price') or 0):,.2f} 元 | 股數: {float(o.get('quantity') or 0):,.0f} 股 | "
                f"總金額: {float(o.get('total_amount') or 0):,.0f} 元{pnl_label}"
            )
    except Exception as e:
        print(f" [AI交易代理] 警告: 無法獲取近期委託歷史: {str(e)}")
        
    if recent_orders_lines:
        recent_orders_info = "【近期帳戶交易歷史 (最新 10 筆)】:\n" + "\n".join(recent_orders_lines)
    else:
        recent_orders_info = "【近期帳戶交易歷史 (最新 10 筆)】: 尚無近期交易歷史紀錄。"

    # 格式化大盤加權指數最近 30 天 K 線數據
    taiex_info = ""
    taiex_klines = klines_map.get("TAIEX", [])
    if taiex_klines:
        taiex_recent = taiex_klines[-30:]
        taiex_lines = []
        for k in taiex_recent:
            taiex_lines.append(
                f"  日期: {k['date']} | 開盤指數: {k['open']:.2f} | 最高指數: {k['high']:.2f} | "
                f"最低指數: {k['low']:.2f} | 收盤指數: {k['close']:.2f}"
            )
        taiex_text = "\n".join(taiex_lines)
        taiex_info = f"【大盤加權指數 (TAIEX) 最近 30 天日 K 線數據 (最下方為最新一日行情，供您計算大盤 MA20)】：\n{taiex_text}"
    else:
        taiex_info = "【大盤加權指數 (TAIEX) 最近 30 天日 K 線數據】：目前無可用的歷史大盤加權指數數據。"

    # 格式化各股票最近 30 天日 K 線數據
    klines_sections = []
    for code in stock_codes:
        if code == "TAIEX":
            continue
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

{taiex_info}

{funds_info}

{holdings_info}

{recent_orders_info}

【過往平倉交易記憶】:
{experience_context}

{all_klines_text}

請結合上述多檔股票之 K 線、持股成本、近期交易動作與歷史交易教訓，基於多股資產分散原則與限額規定，發布本次投資組合決策。
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
        
        # 1. 蒐集每個股票代號的特徵（代號與中文名稱）
        from src.config import get_stock_name
        stock_info = []
        for c in stock_codes:
            name = get_stock_name(c)
            stock_info.append({
                "code": c,
                "name": name,
                "matched": False
            })
            
        # 2. 第一階段：精準匹配（如果決策中的 stock_code 欄位直接是 watchlist 中的某個代號）
        possible_keys = ["stock_code", "stockCode", "stockcode", "StockCode", "code", "stock"]
        for d in decisions:
            resolved_code = None
            for key in possible_keys:
                if key in d and d[key]:
                    val = str(d[key]).strip()
                    if val in stock_codes:
                        resolved_code = val
                        break
            if resolved_code:
                d["stock_code"] = resolved_code
                for info in stock_info:
                    if info["code"] == resolved_code:
                        info["matched"] = True
                        break

        # 3. 第二階段：模糊/文字匹配（針對尚未成功設定 stock_code 的決策）
        for d in decisions:
            if d.get("stock_code") in stock_codes:
                continue
                
            reason_text = str(d.get("reason", ""))
            other_vals = []
            for key in possible_keys:
                if key in d and d[key]:
                    other_vals.append(str(d[key]))
            combined_text = reason_text + " " + " ".join(other_vals)
            
            matched_code = None
            for info in stock_info:
                if not info["matched"]:
                    if info["code"] in combined_text or (info["name"] and info["name"] in combined_text):
                        matched_code = info["code"]
                        info["matched"] = True
                        break
            if not matched_code:
                for info in stock_info:
                    if info["code"] in combined_text or (info["name"] and info["name"] in combined_text):
                        matched_code = info["code"]
                        break
                        
            if matched_code:
                d["stock_code"] = matched_code

        # 4. 第三階段：順序/位置匹配（最安全的防線：如果決策個數跟股票個數一致，且仍有 None）
        if len(decisions) == len(stock_codes):
            for i, d in enumerate(decisions):
                if d.get("stock_code") not in stock_codes:
                    d["stock_code"] = stock_codes[i]
                    
        for d in decisions:
            code = d.get("stock_code")
            # 如果是等候平倉的股票，安全防呆：禁止買入 (BUY)
            if code in pending_stocks and d.get("action") == "BUY":
                print(f" [AI交易代理] 警報: 股票 {code} 處於等候平倉排隊中，AI 給出買入(BUY)決策，強制校正為 HOLD。")
                d["action"] = "HOLD"
                d["quantity"] = 0.0

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
    except DailyRateLimitExceeded as rpd_err:
        print(f" [AI交易代理] 警報: Gemini API 每日額度 (RPD) 已達上限，鎖定交易: {str(rpd_err)}")
        fallback_decisions = []
        for code in stock_codes:
            klines = klines_map.get(code, [])
            fallback_decisions.append({
                "stock_code": code,
                "action": "HOLD",
                "price": klines[-1]["close"] if klines else 10.0,
                "quantity": 0,
                "confidence": 0.0,
                "reason": f"Gemini API 每日額度 (RPD) 已用盡。啟動安全鎖定，今日不進行任何交易。"
            })
        return {"decisions": fallback_decisions}
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
