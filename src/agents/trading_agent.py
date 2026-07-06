# Path: src/agents/trading_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation, DailyRateLimitExceeded
from src.services.trading_memory import get_experience_context
from src.services.supabase_client import get_orders, get_system_fault_status, get_pending_liquidation_stocks
from src.services.technical_indicators import compute_all_indicators

# # 1. 定義單股交易量化評分模型
class StockDecision(BaseModel):
    stock_code: str = Field(
        ...,
        description="必須填寫 4 碼股票代號字串，例如 '2330'，不可填寫中文名稱或留空。此欄位必須與輸入的股票列表代號完全一致。"
    )
    trend_score: int = Field(
        ...,
        description="趨勢得分 (0 到 20 分)。評估均線排列（MA5、MA20、MA60）與價格波段高低點。多頭排列、價格站穩在均線之上得高分；空頭排列或價格跌破均線得低分。"
    )
    momentum_score: int = Field(
        ...,
        description="動能得分 (0 到 20 分)。評估 RSI、MACD 柱狀圖多空動能強弱與黃金/死亡交叉狀態。動能轉強、柱狀圖翻紅、黃金交叉得高分；動能消退、柱狀圖翻綠、死亡交叉得低分。"
    )
    volume_score: int = Field(
        ...,
        description="成交量得分 (0 到 20 分)。評估成交量是否價漲量增、量價配合度、VOL_MA5 與 VOL_MA20 關係。放量突破、量價配合得高分；無量盤整或量價背離得低分。"
    )
    risk_score: int = Field(
        ...,
        description="風險與防守得分 (0 到 20 分)。評估乖離率、過度超買/超賣狀態、下方支撐力道與波動風險。回檔至強支撐、乖離率小得高分；乖離率過大、高檔超買或支撐跌破得低分。"
    )
    regime_score: int = Field(
        ...,
        description="與大盤一致性得分 (0 到 20 分)。結合當前大盤加權指數狀態與交易姿態。若大盤多頭且個股強於大盤得高分，大盤空頭/防禦或大盤氣候不佳時，根據交易姿態適度調降此得分。"
    )
    total_score: int = Field(
        ...,
        description="總得分 (0 到 100 分)。必須嚴格等於 trend_score + momentum_score + volume_score + risk_score + regime_score 的加總。"
    )
    price: float = Field(
        ..., 
        description="最新收盤基準價（新台幣，必須大於 0）。用作委託申報的基準價格。"
    )
    reason: str = Field(
        ..., 
        description="該檔股票的詳細分析理由與評分依據（使用繁體中文）。請條列說明五個評分維度的給分理由與量化數據指標（如 MA、RSI、MACD、成交量等數值）。"
    )

# 2. 定義多股組合評分模型 (強制 Structured Outputs)
class PortfolioDecision(BaseModel):
    decisions: List[StockDecision] = Field(
        ...,
        description="多個股票的評分與分析列表。必須包含所有輸入分析的股票，每檔股票各一筆。"
    )

# 系統預設的金融交易技能清單 (引導評分)
DEFAULT_TRADING_SKILLS = [
    "均線交叉策略 (Moving Average Cross): 當短線均線 (MA5) 向上突破長線均線 (MA20) 且有量能配合時，視為黃金交叉（應給予高趨勢/動能得分）；跌破時為死亡交叉（應降低趨勢/動能得分，提高風險得分並調降總分）。",
    "相對強弱指標 (RSI): 評估短期超買與超賣狀態。RSI > 70 視為超買過熱（應調低風險/防守得分），RSI < 30 視為超賣超跌（若出現止跌訊號，可調高風險得分代表防守空間大，且分批佈局機會好）。",
    "平滑異同移動平均線 (MACD): MACD 柱狀圖紅綠柱代表多空動能。柱狀圖翻紅、快線向上突破慢線時動能轉強（應給予高動能得分）；柱狀圖翻綠、快線跌破慢線時動能走弱（應調低動能得分）。",
    "趨向指標 (DMI): ADX > 25 代表趨勢顯著。+DI 向上穿越 -DI 代表多頭強勢（趨勢/動能得分高）；-DI 向上穿越 +DI 代表空頭強勢（趨勢得分低，風險得分低）。",
    "成交量均線 (VOL MA): VOL_MA5 與 VOL_MA20 判斷量能配合。價漲量增（收盤價高於昨日且成交量大於 VOL_MA5）代表多頭動能強（成交量得分高），價漲量縮或放量下跌為量價背離（成交量得分低）。",
    "動態風險控制與止損停利 (Dynamic Risk Control): 評估當前股價相較於支撐線、壓力線或波段低點的距離。若股價接近支撐區且乖離率小，則防守空間好（風險得分高）；若已突破上檔壓力且無支撐，或面臨高檔乖離過大，則防守空間極差（風險得分低）。"
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
    single_pct = config.limits.single_stock_pct
    if single_pct is not None and single_pct > 0:
        pct_val = single_pct * 100.0 if single_pct <= 1.0 else single_pct
    else:
        pct_val = 20.0

    skills = list(DEFAULT_TRADING_SKILLS)
    for idx, s in enumerate(skills):
        if "可用資金之 20% 以內" in s:
            skills[idx] = s.replace("可用資金之 20% 以內", f"可用資金之 {pct_val:.0f}% 以內")

    if extra_skills:
        skills.extend(extra_skills)
    
    skills_text = "\n".join([f"- {s}" for s in skills])

    # 2. 獲取限額設定 (動態計算) 與帳戶資金狀況
    from src.services.nav_calculator import get_dynamic_limits, calculate_nav
    single_limit, daily_limit = get_dynamic_limits()
    
    if regime_assessment:
        try:
            multiplier = max(float(regime_assessment.get("risk_multiplier", 1.0)), 0.15)
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
        )

    system_instruction = f"""
你是一個資深的台股量化投資與多股投資組合（Portfolio）配置分析專家。你熟悉台股市場特性、技術線圖分析與風控原則。
你的任務是分析給定的多個個股的 K 線數據、目前帳戶的所有持股現況與過往的平倉成敗經驗，為每檔股票進行五個維度的量化評分 (0 ~ 20 分)，並計算出總分 (0 ~ 100 分)。
你的分析必須非常客觀、全面且具有深度，切忌「僅憑單一負面訊號 (例如 MACD 負值或短期震盪) 就直接全盤否定個股價值」的分析師思維。你必須進行多維度的加權平衡評估。

{regime_text}

量化評分維度指引：
1. 趨勢得分 (trend_score, 0 ~ 20 分)：評估均線排列（MA5、MA20、MA60）與價格波段高低點。多頭排列、價格站穩在均線之上得高分；空頭排列或價格跌破均線得低分。
2. 動能得分 (momentum_score, 0 ~ 20 分)：評估 RSI、MACD 柱狀圖多空動能強弱與黃金/死亡交叉狀態。動能轉強、柱狀圖翻紅、黃金交叉得高分；動能消退、柱狀圖翻綠、死亡交叉得低分。
3. 成交量得分 (volume_score, 0 ~ 20 分)：評估成交量是否價漲量增、量價配合度、VOL_MA5 與 VOL_MA20 關係。放量突破、量價配合得高分；無量盤整或量價背離得低分。
4. 風險與防守得分 (risk_score, 0 ~ 20 分)：評估乖離率、過度超買/超賣狀態、下方支撐力道與波動風險。回檔至強支撐、乖離率小得高分；乖離率過大、高檔超買或支撐跌破得低分。
5. 大盤一致性得分 (regime_score, 0 ~ 20 分)：結合當前大盤加權指數狀態與交易姿態。若大盤多頭且個股強於大盤得高分，大盤空頭/防禦或大盤氣候不佳時，根據交易姿態適度調降此得分。

你的金融量化分析技能包含：
{skills_text}

請嚴格遵守以下指示：
1. 你的輸出必須完全符合所規定的 JSON Schema，不可包含額外文字。
2. `total_score` 必須嚴格等於 `trend_score + momentum_score + volume_score + risk_score + regime_score` 的加總。
3. 你的分析與理由請一律使用「繁體中文」。
4. 價格合理性重要規則：`price` 必須符合市場行情（最新收盤價的 ±2% 內），請填寫最新收盤價。
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
        raw_decisions = decision_data.get("decisions", [])
        
        # 1. 蒐集每個股票代號的特徵（代號與中文名稱）以進行模糊匹配
        from src.config import get_stock_name
        stock_info = []
        for c in stock_codes:
            if c != "TAIEX":
                name = get_stock_name(c)
                stock_info.append({
                    "code": c,
                    "name": name,
                    "matched": False
                })

        # 匹配股票代號
        possible_keys = ["stock_code", "stockCode", "stockcode", "StockCode", "code", "stock"]
        for d in raw_decisions:
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

        # 模糊/文字匹配尚未成功設定 stock_code 的評分
        for d in raw_decisions:
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

        # 順序/位置匹配
        if len(raw_decisions) == len(stock_codes):
            for i, d in enumerate(raw_decisions):
                if d.get("stock_code") not in stock_codes:
                    d["stock_code"] = stock_codes[i]

        # 2. 獲取大盤加權指數的最新日期
        latest_date = None
        taiex_klines = klines_map.get("TAIEX", [])
        if taiex_klines:
            latest_date = taiex_klines[-1].get("date")

        # 3. 獲取今日已用買入額度與停損冷卻狀態
        today_buy_sum = 0.0
        cooldown_stocks = set()
        all_orders = []
        try:
            all_orders = get_orders()
            for o in all_orders:
                exec_time = o.get("executed_at", "")
                order_date = exec_time[:10] if exec_time else ""
                
                # 計算今日累計買入金額 (只統計成功或掛單中的 BUY)
                if latest_date and order_date == latest_date:
                    if o.get("action") == "BUY" and o.get("status") not in ["CANCELLED", "FAILED"]:
                        today_buy_sum += float(o.get("total_amount") or 0.0)
                    
                    # 檢查今日停損 (SELL 且 realized_pnl < 0)
                    if o.get("action") == "SELL" and o.get("status") == "FILLED":
                        pnl = float(o.get("realized_pnl") or 0.0)
                        if pnl < 0:
                            cooldown_stocks.add(o.get("stock_code"))
        except Exception as e:
            print(f" [AI交易代理] 讀取訂單與停損冷卻歷史失敗: {str(e)}")

        # 4. 決策轉換與資金配置
        remaining_cash = cash_balance
        remaining_daily_limit = max(daily_limit - today_buy_sum, 0.0)
        
        final_decisions = []
        buy_candidates = []

        for d in raw_decisions:
            code = d.get("stock_code")
            if not code or code == "TAIEX":
                continue
                
            trend = int(d.get("trend_score", 0))
            momentum = int(d.get("momentum_score", 0))
            volume = int(d.get("volume_score", 0))
            risk = int(d.get("risk_score", 0))
            regime = int(d.get("regime_score", 0))
            
            # 強制加總驗證
            total_score = trend + momentum + volume + risk + regime
            d["total_score"] = total_score
            
            price = float(d.get("price") or 0.0)
            reason = d.get("reason", "")
            
            matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
            holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
            
            # 如果是智慧等候平倉排隊中的股票
            if code in pending_stocks:
                if total_score < 70:
                    action = "SELL"
                    qty = holding_qty
                    decision_reason = f"【智慧平倉排隊】總評分 {total_score} 分表現疲弱，維持賣出平倉。{reason}"
                else:
                    action = "HOLD"
                    qty = 0.0
                    decision_reason = f"【智慧平倉排隊】總評分 {total_score} 分表現回彈，暫緩賣出觀望。{reason}"
                
                final_decisions.append({
                    "stock_code": code,
                    "action": action,
                    "price": price,
                    "quantity": qty,
                    "confidence": total_score / 100.0,
                    "reason": decision_reason
                })
                continue

            # 已持有倉位
            if holding_qty > 0:
                if total_score < 60:
                    action = "SELL"
                    qty = holding_qty
                    decision_reason = f"【量化評分賣出】總分 {total_score} 分低於持有門檻 60 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 風險: {risk}, 大盤: {regime})。{reason}"
                else:
                    action = "HOLD"
                    qty = 0.0
                    decision_reason = f"【量化評分續抱】總分 {total_score} 分維持在持有區間 60~100 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 風險: {risk}, 大盤: {regime})。{reason}"
                
                final_decisions.append({
                    "stock_code": code,
                    "action": action,
                    "price": price,
                    "quantity": qty,
                    "confidence": total_score / 100.0,
                    "reason": decision_reason
                })
            
            # 未持有倉位
            else:
                if code in cooldown_stocks:
                    final_decisions.append({
                        "stock_code": code,
                        "action": "HOLD",
                        "price": price,
                        "quantity": 0.0,
                        "confidence": total_score / 100.0,
                        "reason": f"【停損買回冷卻】今日已執行過該股虧損平倉（停損），今日禁買。量化總分 {total_score} 分。{reason}"
                    })
                elif total_score >= 80:
                    buy_candidates.append({
                        "stock_code": code,
                        "total_score": total_score,
                        "trend": trend,
                        "momentum": momentum,
                        "volume": volume,
                        "risk": risk,
                        "regime": regime,
                        "price": price,
                        "reason": reason
                    })
                else:
                    final_decisions.append({
                        "stock_code": code,
                        "action": "HOLD",
                        "price": price,
                        "quantity": 0.0,
                        "confidence": total_score / 100.0,
                        "reason": f"【量化評分觀望】總分 {total_score} 分未達買入門檻 80 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 風險: {risk}, 大盤: {regime})。{reason}"
                    })

        # 5. 針對買入候選股進行排序與資金分配
        # 依總分降序排序
        buy_candidates.sort(key=lambda x: x["total_score"], reverse=True)
        
        for cand in buy_candidates:
            code = cand["stock_code"]
            total_score = cand["total_score"]
            price = cand["price"]
            reason = cand["reason"]
            
            # 剩餘預算限制：不得高於單股限額、可用現金、每日剩餘上限
            allowed_budget = min(single_limit, remaining_cash, remaining_daily_limit)
            
            if allowed_budget >= price:
                import math
                qty = math.floor(allowed_budget / price)
                if qty > 0:
                    cost = price * qty
                    final_decisions.append({
                        "stock_code": code,
                        "action": "BUY",
                        "price": price,
                        "quantity": float(qty),
                        "confidence": total_score / 100.0,
                        "reason": f"【量化評分買入】總分 {total_score} 分達到買入門檻 80 (趨勢: {cand['trend']}, 動能: {cand['momentum']}, 量能: {cand['volume']}, 風險: {cand['risk']}, 大盤: {cand['regime']})，分配預算 {cost:,.0f} 元。{reason}"
                    })
                    remaining_cash -= cost
                    remaining_daily_limit -= cost
                else:
                    final_decisions.append({
                        "stock_code": code,
                        "action": "HOLD",
                        "price": price,
                        "quantity": 0.0,
                        "confidence": total_score / 100.0,
                        "reason": f"【量化評分觀望】總分 {total_score} 達買入門檻，但剩餘可用額度不足以買入 1 股。{reason}"
                    })
            else:
                if single_limit < price:
                    limit_desc = f"單股交易限額 {single_limit:,.0f} 元低於股票單價 {price:,.0f} 元"
                else:
                    limit_desc = "可用資金或每日限額不足"
                
                final_decisions.append({
                    "stock_code": code,
                    "action": "HOLD",
                    "price": price,
                    "quantity": 0.0,
                    "confidence": total_score / 100.0,
                    "reason": f"【量化評分觀望】總分 {total_score} 達到買入門檻，但因 {limit_desc} 無法配置。{reason}"
                })

        return {"decisions": final_decisions}

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
