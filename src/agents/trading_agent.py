# Path: src/agents/trading_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation, DailyRateLimitExceeded
from src.services.trading_memory import get_experience_context
from src.services.supabase_client import get_orders, get_system_fault_status, get_pending_liquidation_stocks
from src.services.technical_indicators import compute_all_indicators

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
        description="建議交易委託價格（新台幣，必須大於 0）。如果是買入(BUY)或賣出(SELL)，委託價格必須以最新收盤價為基準，並落在合理波動範圍內（收盤價的 ±2% 內，且符合台股升降單位/tick size 規則）。絕對禁止為了規避單筆限額或資金限制而故意填寫偏離市價（如低於收盤價 10% 以上）的無效價格。觀望(HOLD)時填最新收盤價。"
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
    "平滑異同移動平均線 (MACD): 由快線 (DIFF)、慢線 (DEA) 與柱狀圖 (Histogram) 組成。MACD 柱狀圖紅綠柱方向與長短變化是多空動能強弱的重要參考。當快線向上突破慢線且柱狀圖翻紅時，為黃金交叉買點；反之，跌破且翻綠為死亡交叉賣點。",
    "趨向指標 (DMI): 包含 +DI14、-DI14 與 ADX14。+DI 與 -DI 交叉反映多空力道強弱，ADX 則顯示趨勢強度。當 ADX > 25 代表趨勢顯著，此時若 +DI 向上穿越 -DI，代表多頭強勢；若 -DI 向上穿越 +DI，代表空頭強勢。",
    "成交量均線 (VOL MA): VOL_MA5 與 VOL_MA20 提供成交量量能放大或萎縮的依據。價漲量增（收盤價高於昨日且成交量高於 VOL_MA5）代表多頭動能強，價漲量縮或放量下跌則屬量價背離，須謹慎防守。",
    "動態風險控制與止損停利 (Dynamic Risk Control & Exit Strategy): 請根據每檔股票當前的波動度 (例如技術面、振幅或支撐壓力位) 靈活且動態地制定合適的止損與止盈出場點，不再拘泥於固定的百分比。你必須在分析理由中詳細說明你的風險控制邏輯，並在到達你設定的退場防線時主動發出 SELL 決策以平倉保護資金。",
    "資金配置策略: 進行多股投資組合分析時，將資金分配給多個標的以分散風險（不要把雞蛋放在同一個籃子裡）。單筆買入之委託總額限制在可用資金之 20% 以內，遵守全局交易防呆上限，禁止單筆重倉孤注一擲。",
    "【停損買回冷卻】：若在「近期帳戶交易歷史」中，某檔股票在當天剛剛執行過賣出 (SELL) 且為虧損平倉（即停損），則今日絕對禁止再次對該檔股票發送買入 (BUY) 決策，避免陷入重複追高殺低。",
    "【大盤趨勢防禦】：大盤加權指數 (TAIEX) 是整體市場走向的風向球。若大盤指數收盤跌破其 MA20 (即大盤收盤價最近 20 天的簡單移動平均)，代表大盤已步入弱勢或空頭排列，此時應嚴格採取小額防禦策略，大幅收緊買入標準並降低單次交易規模（僅能小規模買賣，以微量零股做測試性防禦配置），並應主動減持手上已持有的高風險股票以規避市場崩跌風險；若大盤收盤站穩在 MA20 之上，則可正常執行交易與買入評估。"
]

def generate_portfolio_decisions(
    stock_codes: List[str],
    klines_map: Dict[str, List[Dict[str, Any]]],
    current_holdings: List[Dict[str, Any]],
    extra_skills: Optional[List[str]] = None,
    regime_assessment: Optional[Dict[str, Any]] = None
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

    # 0.5. 計算所有股票與大盤加權指數的技術指標
    for code, klines in klines_map.items():
        try:
            compute_all_indicators(klines)
        except Exception as indicator_err:
            print(f" [AI交易代理] 警告: 計算股票 {code} 的技術指標失敗: {indicator_err}")

    # 1. 處理並合併金融技能
    skills = list(DEFAULT_TRADING_SKILLS)
    if extra_skills:
        skills.extend(extra_skills)
    
    skills_text = "\n".join([f"- {s}" for s in skills])

    # 2. 獲取限額設定 (動態計算) 與帳戶資金狀況
    from src.services.nav_calculator import get_dynamic_limits, calculate_nav
    single_limit, daily_limit = get_dynamic_limits()
    
    if regime_assessment:
        try:
            multiplier = float(regime_assessment.get("risk_multiplier", 1.0))
            single_limit = single_limit * multiplier
            daily_limit = daily_limit * multiplier
        except Exception as mult_err:
            print(f" [AI交易代理] 警告: 套用風險限額乘數失敗: {mult_err}")

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
    regime_text = ""
    if regime_assessment:
        regime_text = (
            f"\n【當前大盤市場氣候判定 (Regime Layer Assessment)】:\n"
            f"- 市場狀態 (Regime): {regime_assessment.get('regime', 'UNKNOWN')}\n"
            f"- 交易姿態 (Posture): {regime_assessment.get('posture', 'UNKNOWN')}\n"
            f"- 風險限額乘數 (Multiplier): {regime_assessment.get('risk_multiplier', 1.0)}\n"
            f"- 大腦分析理由 (Reason): {regime_assessment.get('reason', '')}\n"
            f"請務必將上述大盤氣候（特別是交易姿態與分析理由）以及風險限額乘數作為最高量化風控指令！\n"
            f"如果交易姿態為 DEFENSIVE，代表此時大盤走勢極差或劇烈震盪，你的個股操作應「極度保守且降低規模」，強烈傾向 HOLD 或 SELL 避險。若決定買入 (BUY)，則該股必須有極強的技術支撐或特大個股利多，且買入總金額必須受到已乘以風險乘數後縮小的低限額嚴格約束，執行『買賣小小的』防禦性微量零股配置。\n"
        )

    system_instruction = f"""
你是一個資深的台股量化投資與多股投資組合（Portfolio）配置分析專家。你熟悉台股市場特性、技術線圖分析與風控原則。
你的任務是分析給定的多個個股的 K 線數據、目前帳戶的所有持股現況與過往的平倉成敗經驗，生成一份標準的多股交易決策 JSON。
{regime_text}
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
   - 請根據目前多檔股票的走勢，綜合評估相對強弱，合理分配買入額度，以實現資產分散配置（不要把雞蛋放在同一個籃子裡），同時總額不能超出每日限制。
   - 價格合理性重要規則：委託價格必須符合市場行情（收盤價的 ±2% 內）。若因為單筆上限限制（如 {single_limit:,.0f} 元），導致剩餘額度「不足以合理市價買入該個股之最少單位（1 股）」，你必須對該個股給出 HOLD（觀望）決策，股數填 0，並在理由中說明額度不足。絕對禁止調低委託價格來強行買入！{pending_instruction}
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
            exec_price_val = o.get("execution_price")
            limit_price = float(o.get("price") or 0.0)
            status = o.get("status", "FILLED")
            if status == "FILLED" and exec_price_val is not None:
                price_text = f"委託價: {limit_price:,.2f} 元 (成交價: {float(exec_price_val):,.2f} 元)"
            else:
                price_text = f"委託價: {limit_price:,.2f} 元"

            recent_orders_lines.append(
                f"  - {time_label} | {action_label} {o.get('stock_code')} | "
                f"{price_text} | 股數: {float(o.get('quantity') or 0):,.0f} 股 | "
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
            ma5_str = f"{k['ma5']:.2f}" if k.get('ma5') is not None else "N/A"
            ma20_str = f"{k['ma20']:.2f}" if k.get('ma20') is not None else "N/A"
            ma60_str = f"{k['ma60']:.2f}" if k.get('ma60') is not None else "N/A"
            rsi_str = f"{k['rsi14']:.2f}" if k.get('rsi14') is not None else "N/A"
            macd_str = f"(快線:{k['macd']:.2f}, 慢線:{k['macd_signal']:.2f}, 柱狀圖:{k['macd_hist']:.2f})" if (k.get('macd') is not None and k.get('macd_signal') is not None and k.get('macd_hist') is not None) else "N/A"
            dmi_str = f"(+DI:{k['plus_di']:.1f}, -DI:{k['minus_di']:.1f}, ADX:{k['adx']:.1f})" if (k.get('adx') is not None and k.get('plus_di') is not None and k.get('minus_di') is not None) else "N/A"
            
            taiex_lines.append(
                f"  日期: {k['date']} | 收盤指數: {k['close']:.2f} | MA5: {ma5_str} | MA20: {ma20_str} | MA60 (季線): {ma60_str} | RSI: {rsi_str} | MACD: {macd_str} | DMI: {dmi_str}"
            )
        taiex_text = "\n".join(taiex_lines)
        taiex_info = f"【大盤加權指數 (TAIEX) 最近 30 天日 K 線數據 (最下方為最新一日行情，供您判定大盤走勢)】：\n{taiex_text}"
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
            ma5_str = f"{k['ma5']:.2f}" if k.get('ma5') is not None else "N/A"
            ma20_str = f"{k['ma20']:.2f}" if k.get('ma20') is not None else "N/A"
            ma60_str = f"{k['ma60']:.2f}" if k.get('ma60') is not None else "N/A"
            rsi_str = f"{k['rsi14']:.2f}" if k.get('rsi14') is not None else "N/A"
            vol_ma5_str = f"{k['vol_ma5']:,.0f}" if k.get('vol_ma5') is not None else "N/A"
            vol_ma20_str = f"{k['vol_ma20']:,.0f}" if k.get('vol_ma20') is not None else "N/A"
            macd_str = f"(快線:{k['macd']:.2f}, 慢線:{k['macd_signal']:.2f}, 柱狀圖:{k['macd_hist']:.2f})" if (k.get('macd') is not None and k.get('macd_signal') is not None and k.get('macd_hist') is not None) else "N/A"
            dmi_str = f"(+DI:{k['plus_di']:.1f}, -DI:{k['minus_di']:.1f}, ADX:{k['adx']:.1f})" if (k.get('adx') is not None and k.get('plus_di') is not None and k.get('minus_di') is not None) else "N/A"
            
            klines_lines.append(
                f"  日期: {k['date']} | 開盤: {k['open']} | 最高: {k['high']} | 最低: {k['low']} | 收盤: {k['close']} | MA5: {ma5_str} | MA20: {ma20_str} | MA60 (季線): {ma60_str} | RSI: {rsi_str} | "
                f"成交量: {k['volume']:,.0f} (VOL_MA5: {vol_ma5_str}, VOL_MA20: {vol_ma20_str}) | MACD: {macd_str} | DMI: {dmi_str}"
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
        "response_schema": PortfolioDecision,
        "temperature": 0.0
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
