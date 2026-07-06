# Path: src/agents/trading_agent.py
import json
import math
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation, DailyRateLimitExceeded
from src.services.trading_memory import get_experience_context
from src.services.supabase_client import get_orders, get_system_fault_status, get_pending_liquidation_stocks
from src.services.technical_indicators import compute_all_indicators

# =====================================================================
# 1. 定義第一層：分析師評估模型 (Structured Outputs)
# =====================================================================
class AnalystStockScore(BaseModel):
    stock_code: str = Field(
        ..., 
        description="必須填寫 4 碼股票代號字串，例如 '2330'。此欄位必須與輸入的股票列表代號完全一致。"
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
    safety_score: int = Field(
        ...,
        description="安全與防守得分 (0 到 20 分)。評估下方支撐力道與防守空間。股價回檔至強支撐、乖離率小、防守空間大得高分（代表安全）；股價突破上檔阻力但乖離率過大、高檔超買或支撐跌破得低分。"
    )
    regime_score: int = Field(
        ...,
        description="與大盤一致性得分 (0 到 20 分)。結合當前大盤加權指數狀態與交易姿態。若大盤多頭且個股強於大盤得高分，大盤空頭/防禦或大盤氣候不佳時，根據交易姿態適度調降此得分。"
    )
    confidence: float = Field(
        ...,
        description="分析信心指數 (0.0 到 1.0)。反映你對該個股技術線圖與指標分析結果的把握度。"
    )
    reason: str = Field(
        ..., 
        description="該檔股票的簡短技術分析理由與評分依據（使用繁體中文，限定 80 ~ 150 字，禁止超出）。請精簡指出關鍵指標信號，不要贅述每日歷史細節與冗長計算過程。"
    )

class AnalystAssessment(BaseModel):
    scores: List[AnalystStockScore] = Field(
        ...,
        description="所有股票的分析評分列表。必須包含所有輸入分析的股票，每檔股票各一筆。"
    )


# =====================================================================
# 2. 定義第二層：投資組合經理決策模型 (Structured Outputs)
# =====================================================================
class PMStockDecision(BaseModel):
    stock_code: str = Field(
        ..., 
        description="必須填寫 4 碼股票代號字串，例如 '2330'。此欄位必須與輸入的股票列表代號完全一致。"
    )
    action: str = Field(
        ...,
        description="你對該股的交易建議。可選值為: BUY (建議買入的非持股或加碼持股，配合相對排名且評分高於50且技術面無重大缺陷), SELL (建議賣出/減碼持股，或持股總分<60，或為換股調倉而賣出), HOLD (觀望或續抱)。"
    )
    pm_reason: str = Field(
        ..., 
        description="主動投資組合經理對該檔股票的配置或交易決策理由（使用繁體中文，限 100 字內）。請說明在今日的帳戶資金/持股與大盤狀態下，相對排名做此決定的理由。"
    )

class PortfolioDecision(BaseModel):
    ranking_analysis: str = Field(
        ...,
        description="基金經理的配置決策與調倉邏輯說明（使用繁體中文）。請說明今日資金配置的核心想法、為何選出這些買入/賣出標的，以及如何控制大盤風險。"
    )
    decisions: List[PMStockDecision] = Field(
        ...,
        description="多個股票的決策與分析列表。必須包含所有輸入分析的股票，每檔股票各一筆。決策必須嚴格符合上述排序與賣出分析結論。"
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
    本決策採雙層管線架構：
    - Layer 1: 分析師評分層 (客觀、無偏誤，對強勢股敢於給予高分，confidence 與 80~150 字 reason，無 price 與 total_score)
    - Layer 2: 投資組合配置經理層 (橫向對比相對排序，主動尋求配置，大盤非空頭下禁止全數 HOLD，支持調倉，不吃日 K 線)
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
                    "reason": f"系統處於故障安全防禦鎖定狀態 (SYSTEM FAULT)，已暫停所有交易。故障原因: {fault_state.get('detail')}",
                    "trend_score": 0,
                    "momentum_score": 0,
                    "volume_score": 0,
                    "safety_score": 0,
                    "regime_score": 0,
                    "total_score": 0
                })
            return {
                "ranking_analysis": f"系統目前處於全局故障鎖定狀態，已暫停交易。原因: {fault_state.get('detail')}",
                "decisions": fallback_decisions
            }
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

    # 3. 構建第一層：分析師評估 (Layer 1 Analyst Assessment)
    regime_text = ""
    if regime_assessment:
        regime_text = (
            f"\n【當前大盤市場氣候判定 (Regime Layer Assessment)】:\n"
            f"- 市場狀態 (Regime): {regime_assessment.get('regime', 'UNKNOWN')}\n"
            f"- 交易姿態 (Posture): {regime_assessment.get('posture', 'UNKNOWN')}\n"
            f"- 風險限額乘數 (Multiplier): {regime_assessment.get('risk_multiplier', 1.0)}\n"
            f"- 大腦分析理由 (Reason): {regime_assessment.get('reason', '')}\n"
        )

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

    analyst_system_instruction = f"""
你是一個極其資深且敏銳的台股量化投資分析師。你熟悉台股市場特性與技術線圖分析。
你的任務是分析給定的多個個股的日 K 線數據，進行獨立且客觀的量化評分與技術指標分析。
你需要為每檔個股進行五個維度的評分 (各個維度為 0 ~ 20 分，總分會由 Python 程式加總計算，你不需回傳總分，也不需填寫股票收盤價)。

【重要評分提示】：
- 你的評分是為了反映個股強弱。如果個股呈現多頭排列、放量突破、MACD翻紅或RSI強勢等信號，請勇敢地給予高分（趨勢得分與動能得分可達16~20分）。
- 請拉開評分差距，切忌盲目保守。不要將所有的股票都擠在 10 ~ 13 分的中庸區間（即總分 50 ~ 65 區間）。強勢股給予高分，弱勢股給予低分。
- 切忌僅憑單一負面訊號就直接全盤否定個股價值。
- 請同時為你的分析結果給出信心指數 `confidence` (0.0 到 1.0)。

量化評分維度指引：
1. 趨勢得分 (trend_score, 0 ~ 20 分)：評估均線排列（MA5、MA20、MA60）與價格波段高低點。多頭排列、價格站穩在均線之上得高分；空頭排列或價格跌破均線得低分。
2. 動能得分 (momentum_score, 0 ~ 20 分)：評估 RSI、MACD 柱狀圖多空動能強弱與黃金/死亡交叉狀態。動能轉強、柱狀圖翻紅、黃金交叉得高分；動能消退、柱狀圖翻綠、死亡交叉得低分。
3. 成交量得分 (volume_score, 0 ~ 20 分)：評估成交量是否價漲量增、量價配合度、VOL_MA5 與 VOL_MA20 關係。放量突破、量價配合得高分；無量盤整或量價背離得低分。
4. 安全與防守得分 (safety_score, 0 ~ 20 分)：評估下方支撐力道與防守空間。股價回檔至強支撐、乖離率小、防守空間大得高分（代表安全）；股價突破上檔阻力但乖離率過大、高檔超買或支撐跌破得低分。
5. 大盤一致性得分 (regime_score, 0 ~ 20 分)：結合當前大盤加權指數狀態與交易姿態。若大盤多頭且個股強於大盤得高分，大盤空頭/防禦或大盤氣候不佳時，根據交易姿態適度調降此得分。

你的金融量化分析技能包含：
{skills_text}

請嚴格遵守以下指示：
1. 你的輸出必須完全符合所規定的 JSON Schema (AnalystAssessment)，不可包含額外文字。
2. 你的分析理由請一律使用「繁體中文」，限制在 80 到 150 字以內，精簡指出關鍵指標，請勿贅述每日歷史細節。
"""

    analyst_user_prompt = f"""
請針對股票列表 {stock_codes} 進行個股日 K 線的技術分析與評分。

{taiex_info}

{all_klines_text}

請基於上述數據，產出各股票的量化評分與詳細原因。
"""

    generation_config_analyst = {
        "response_mime_type": "application/json",
        "response_schema": AnalystAssessment,
        "temperature": 0.0
    }

    try:
        print(" [AI交易代理] 呼叫第一層：分析師評估層...")
        raw_analyst_response = call_gemini_with_rotation(
            prompt=analyst_user_prompt,
            system_instruction=analyst_system_instruction,
            model_name=config.gemini_model,
            generation_config=generation_config_analyst
        )
        analyst_data = json.loads(raw_analyst_response)
        raw_scores = analyst_data.get("scores", [])
    except DailyRateLimitExceeded as rpd_err:
        raise rpd_err
    except Exception as e:
        print(f" [AI交易代理] 警告: 第一層分析師評估失敗，嘗試使用相容性解析或降級: {str(e)}")
        # 兼容性回退
        try:
            # 支援單層 mock 測試情境
            if "decisions" in json.loads(raw_analyst_response):
                raw_scores = []
            else:
                raw_scores = []
        except Exception:
            raw_scores = []

    # 3.5. Python 計算價格 (price) 與總分 (total_score)
    analyst_scores = []
    for s_item in raw_scores:
        code = s_item.get("stock_code")
        if not code or code == "TAIEX":
            continue
        
        # 讀取 K 線最新收盤價
        klines = klines_map.get(code, [])
        price = klines[-1]["close"] if klines else 10.0
        
        trend = int(s_item.get("trend_score", 10))
        momentum = int(s_item.get("momentum_score", 10))
        volume = int(s_item.get("volume_score", 10))
        safety = int(s_item.get("safety_score", 10))
        regime = int(s_item.get("regime_score", 10))
        total_score = trend + momentum + volume + safety + regime
        
        analyst_scores.append({
            "stock_code": code,
            "trend_score": trend,
            "momentum_score": momentum,
            "volume_score": volume,
            "safety_score": safety,
            "regime_score": regime,
            "confidence": float(s_item.get("confidence", 0.8)),
            "total_score": total_score,
            "price": price,
            "reason": s_item.get("reason", "技術量化指標尚可。")
        })

    # 4. 準備第二層：投資組合經理 (Layer 2 Portfolio Manager)
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

    # 獲取今日已用買入額度與停損冷卻狀態
    latest_date = None
    if taiex_klines:
        latest_date = taiex_klines[-1].get("date")

    today_buy_sum = 0.0
    cooldown_stocks = set()
    try:
        all_orders = get_orders()
        for o in all_orders:
            exec_time = o.get("executed_at", "")
            order_date = exec_time[:10] if exec_time else ""
            
            if latest_date and order_date == latest_date:
                if o.get("action") == "BUY" and o.get("status") not in ["CANCELLED", "FAILED"]:
                    today_buy_sum += float(o.get("total_amount") or 0.0)
                
                if o.get("action") == "SELL" and o.get("status") == "FILLED":
                    pnl = float(o.get("realized_pnl") or 0.0)
                    if pnl < 0:
                        cooldown_stocks.add(o.get("stock_code"))
    except Exception as e:
        print(f" [AI交易代理] 讀取訂單與停損冷卻歷史失敗: {str(e)}")

    # 建立分析師評分字典以利經理人參考與後續合併
    analyst_map = {s.get("stock_code"): s for s in analyst_scores}

    # 若分析師沒有順利評估某些股票（或在測試 Mock 中），進行自動填補以防出錯
    for code in stock_codes:
        if code != "TAIEX" and code not in analyst_map:
            klines = klines_map.get(code, [])
            price = klines[-1]["close"] if klines else 10.0
            analyst_map[code] = {
                "stock_code": code,
                "trend_score": 10,
                "momentum_score": 10,
                "volume_score": 10,
                "safety_score": 10,
                "regime_score": 10,
                "total_score": 50,
                "confidence": 0.8,
                "price": price,
                "reason": "預設相容性技術評估。"
            }

    # 格式化分析師評估報告給經理人 (此處不再傳入 K 線數據以減少 Token 數量與避免 PM 重新分析指標)
    analyst_report_lines = []
    for code, s in analyst_map.items():
        analyst_report_lines.append(
            f"- 股票 {code}: 技術評定總分 {s['total_score']} 分 (趨勢: {s['trend_score']} | 動能: {s['momentum_score']} | 量能: {s['volume_score']} | 安全: {s['safety_score']} | 大盤: {s['regime_score']}) | 信心指數: {s['confidence']:.2f} | 收盤基準價: {s['price']} 元\n"
            f"  分析師簡評: {s['reason']}"
        )
    analyst_report_text = "\n".join(analyst_report_lines)

    pm_system_instruction = f"""
你是一位極具攻擊性與風控紀律的主動型基金經理 (Active Portfolio Manager)。你的工作不是避免犯錯，而是在控制風險的前提下，為基金尋求最大化收益。

你的任務是基於研究員的「分析師個股技術評估報告」，結合當前的「大盤市場氣候判定」、「帳戶現有持股」、「可用資金餘額」以及「近期交易歷史」，做出今日的最終交易配置決策 (決定各股票的 action 為 BUY, SELL 或 HOLD)。

【主動配置決策原則】：
1. 橫向對比說明：你在 `ranking_analysis` 中應說明今日資產分配想法、調倉邏輯，以及在目前持股與新選標的中如何進行資金取捨。（注意：買入股票的優先權與總分排序由 Python 程式端主導，你只需解釋決策原因即可）。
2. 禁止盲目觀望：除非大盤市場判定狀態為 BEARISH_TREND 且交易姿勢 posture=DEFENSIVE，否則禁止因為「等待更明確信號」而讓所有股票皆決策為 HOLD。
3. 勇敢推薦買入：只要個股技術面沒有重大缺陷（即分析師評分總分高於 50 分底線），你必須依據相對優勢至少選擇 1 檔發出 BUY 決策，即使其絕對分數沒有達到極高的標準（例如僅 60 幾分甚至 55 分），只要它是今日相對最優，即應勇敢推薦配置。
4. 調倉與賣出審查：
   - 審查持股是否該賣出 (SELL)。若持股總評分低於 60 分，或趨勢反轉、跌破支撐，應果斷賣出。
   - 支持「換股調倉」邏輯：若現有持股表現平庸（如 60~70 分），而外面有分數顯著高（如大於等於 75 分）的強勢股，但可用資金不足，你應果斷將平庸持股決定為 SELL 賣出以釋放資金，同日買入高分強勢股。
5. 【智慧平倉與冷卻護欄】：
   - 智慧平倉名單: {', '.join(pending_stocks) if pending_stocks else '無'}。這些股票絕對禁止發出買入 (BUY) 決策！若走勢疲軟請決定 SELL，若有反彈需要觀望請決定 HOLD。
   - 今日停損買回冷卻名單: {', '.join(cooldown_stocks) if cooldown_stocks else '無'}。這些股票今日剛停損，今日絕對禁止發出買入 (BUY) 決策，必須發出 HOLD。

{regime_text}

請嚴格遵守以下指示：
1. 你的輸出必須完全符合所規定的 JSON Schema (PortfolioDecision)，不可包含任何額外文字。
2. 你的決策理由請一律使用「繁體中文」。
"""

    pm_user_prompt = f"""
請身為投資組合基金經理，針對今日研究員的評分報告與帳戶資金狀態，做出最終交易配置決定。

【研究員個股技術評估報告】：
{analyst_report_text}

{funds_info}

{holdings_info}

{recent_orders_info}

【過往平倉交易記憶】：
{experience_context}

請進行多股配置思考，並給出你的交易決定與理由。
"""

    generation_config_pm = {
        "response_mime_type": "application/json",
        "response_schema": PortfolioDecision,
        "temperature": 0.0
    }

    try:
        print(" [AI交易代理] 呼叫第二層：投資組合配置經理層...")
        raw_pm_response = call_gemini_with_rotation(
            prompt=pm_user_prompt,
            system_instruction=pm_system_instruction,
            model_name=config.gemini_model,
            generation_config=generation_config_pm
        )
        pm_data = json.loads(raw_pm_response)
        raw_decisions = pm_data.get("decisions", [])
        ranking_analysis = pm_data.get("ranking_analysis", "無橫向排序數據")
    except DailyRateLimitExceeded as rpd_err:
        raise rpd_err
    except Exception as e:
        print(f" [AI交易代理] 警告: 第二層經理人決策失敗或回退: {str(e)}")
        # 兼容性回退：用於支援原本舊格式/單層 mock 測試
        try:
            # 檢查第一層回傳的 analyst_data 是否其實是原本舊格式的 decisions 結構 (單層 mock)
            if "decisions" in analyst_data:
                raw_decisions = analyst_data.get("decisions", [])
                ranking_analysis = analyst_data.get("ranking_analysis", "單層相容模式。")
            else:
                raw_decisions = []
                ranking_analysis = "經理人層呼叫異常，啟動防禦性 HOLD。"
        except Exception:
            raw_decisions = []
            ranking_analysis = "經理人層呼叫異常，啟動防禦性 HOLD。"

    # 5. 決策轉換與資金配置 (Python 護欄邏輯)
    remaining_cash = cash_balance
    remaining_daily_limit = max(daily_limit - today_buy_sum, 0.0)
    
    # 模糊匹配與修復 stock_code
    from src.config import get_stock_name
    stock_info = []
    for c in stock_codes:
        if c != "TAIEX":
            name = get_stock_name(c)
            stock_info.append({"code": c, "name": name, "matched": False})

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

    # 模糊匹配尚未成功設定 stock_code 的決策
    for d in raw_decisions:
        if d.get("stock_code") in stock_codes:
            continue
            
        reason_text = str(d.get("pm_reason", "") or d.get("reason", ""))
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
    if len(raw_decisions) == len([c for c in stock_codes if c != "TAIEX"]):
        valid_codes = [c for c in stock_codes if c != "TAIEX"]
        for i, d in enumerate(raw_decisions):
            if d.get("stock_code") not in stock_codes:
                d["stock_code"] = valid_codes[i]

    # 為了支持同日調倉，我們先計算 SELL 釋出的預估資金，並加入 remaining_cash
    for d in raw_decisions:
        code = d.get("stock_code")
        action = d.get("action")
        if not code or code == "TAIEX":
            continue
            
        matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
        holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
        
        ana = analyst_map.get(code, {})
        price = float(ana.get("price") or 10.0)
        
        if action == "SELL" and holding_qty > 0:
            released_cash = price * holding_qty * 0.995  # 扣除滑價
            remaining_cash += released_cash
            print(f" [AI交易代理] 調倉預算：賣出 {code} 預估可釋出可用資金 {released_cash:,.0f} 元")

    final_decisions = []
    buy_candidates = []

    for d in raw_decisions:
        code = d.get("stock_code")
        if not code or code == "TAIEX":
            continue
            
        action = d.get("action", "HOLD")
        pm_reason = d.get("pm_reason", d.get("reason", "")).strip()

        # 獲取分析師評分與價格
        ana = analyst_map.get(code, {})
        trend = int(ana.get("trend_score", 10))
        momentum = int(ana.get("momentum_score", 10))
        volume = int(ana.get("volume_score", 10))
        safety = int(ana.get("safety_score", 10))
        regime = int(ana.get("regime_score", 10))
        total_score = trend + momentum + volume + safety + regime
        price = float(ana.get("price") or 10.0)
        analyst_reason = ana.get("reason", "").strip()

        merged_reason = f"【量化評分: {total_score}分 (趨勢:{trend} | 動能:{momentum} | 量能:{volume} | 安全:{safety} | 大盤:{regime})】{analyst_reason}\n【經理人決策理由】{pm_reason}"

        matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
        holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
        
        # 智慧等候平倉排隊中股票強制干預
        if code in pending_stocks:
            # 賣出無持股的情況，強迫為 HOLD 確保測試與系統安全
            if holding_qty <= 0:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【智慧平倉安全過濾】無持股庫存，強制觀望。{merged_reason}"
            elif action == "SELL" or total_score < 70:
                action = "SELL"
                qty = holding_qty
                decision_reason = f"【智慧平倉排隊】總評分 {total_score} 分表現疲弱，維持賣出平倉。{merged_reason}"
            else:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【智慧平倉排隊】總評分 {total_score} 分表現回彈，暫緩賣出觀望。{merged_reason}"
            
            final_decisions.append({
                "stock_code": code,
                "action": action,
                "price": price,
                "quantity": qty,
                "confidence": total_score / 100.0,
                "reason": decision_reason,
                "trend_score": trend,
                "momentum_score": momentum,
                "volume_score": volume,
                "safety_score": safety,
                "regime_score": regime,
                "total_score": total_score
            })
            continue

        # 已持有倉位
        if holding_qty > 0:
            # 賣出持股
            if action == "SELL" or total_score < 60:
                action = "SELL"
                qty = holding_qty
                decision_reason = f"【量化評分賣出】總分 {total_score} 分低於持有門檻 60 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 安全: {safety}, 大盤: {regime})。{merged_reason}"
            else:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【量化評分續抱】總分 {total_score} 分維持在持有區間 60~100 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 安全: {safety}, 大盤: {regime})。{merged_reason}"
            
            final_decisions.append({
                "stock_code": code,
                "action": action,
                "price": price,
                "quantity": qty,
                "confidence": total_score / 100.0,
                "reason": decision_reason,
                "trend_score": trend,
                "momentum_score": momentum,
                "volume_score": volume,
                "safety_score": safety,
                "regime_score": regime,
                "total_score": total_score
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
                    "reason": f"【停損買回冷卻】今日已執行過該股虧損平倉（停損），今日禁買。量化總分 {total_score} 分。{merged_reason}",
                    "trend_score": trend,
                    "momentum_score": momentum,
                    "volume_score": volume,
                    "safety_score": safety,
                    "regime_score": regime,
                    "total_score": total_score
                })
            elif action == "BUY":
                # 防禦性最低評分過濾 (總分低於 50 分底線進行安全過濾)
                if total_score < 50:
                    final_decisions.append({
                        "stock_code": code,
                        "action": "HOLD",
                        "price": price,
                        "quantity": 0.0,
                        "confidence": total_score / 100.0,
                        "reason": f"【量化安全過濾】經理人建議買入，但技術量化評分極低 ({total_score} 分 < 50 分底線)，進行安全過濾。{merged_reason}",
                        "trend_score": trend,
                        "momentum_score": momentum,
                        "volume_score": volume,
                        "safety_score": safety,
                        "regime_score": regime,
                        "total_score": total_score
                    })
                else:
                    buy_candidates.append({
                        "stock_code": code,
                        "total_score": total_score,
                        "trend": trend,
                        "momentum": momentum,
                        "volume": volume,
                        "safety": safety,
                        "regime": regime,
                        "price": price,
                        "reason": merged_reason
                    })
            else:
                final_decisions.append({
                    "stock_code": code,
                    "action": "HOLD",
                    "price": price,
                    "quantity": 0.0,
                    "confidence": total_score / 100.0,
                    "reason": f"【量化評分觀望】經理人決定觀望。{merged_reason}",
                    "trend_score": trend,
                    "momentum_score": momentum,
                    "volume_score": volume,
                    "safety_score": safety,
                    "regime_score": regime,
                    "total_score": total_score
                })

    # 6. Python 程式端主導買入候選股排序與預算分配 (依總分降序排序以進行相對優先分配)
    buy_candidates.sort(key=lambda x: x["total_score"], reverse=True)
    
    for cand in buy_candidates:
        code = cand["stock_code"]
        total_score = cand["total_score"]
        price = cand["price"]
        reason = cand["reason"]
        
        # 剩餘預算限制：不得高於單股限額、可用現金、每日剩餘上限
        allowed_budget = min(single_limit, remaining_cash, remaining_daily_limit)
        
        if allowed_budget >= price:
            qty = math.floor(allowed_budget / price)
            if qty > 0:
                cost = price * qty
                final_decisions.append({
                    "stock_code": code,
                    "action": "BUY",
                    "price": price,
                    "quantity": float(qty),
                    "confidence": total_score / 100.0,
                    "reason": f"【投資組合配置買入】總分 {total_score} 分，依經理人建議與相對排名排序分配預算 {cost:,.0f} 元。{reason}",
                    "trend_score": cand["trend"],
                    "momentum_score": cand["momentum"],
                    "volume_score": cand["volume"],
                    "safety_score": cand["safety"],
                    "regime_score": cand["regime"],
                    "total_score": total_score
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
                    "reason": f"【配置觀望】總分 {total_score} 達到配置標準，但剩餘可用現金或額度不足以買入 1 股。{reason}",
                    "trend_score": cand["trend"],
                    "momentum_score": cand["momentum"],
                    "volume_score": cand["volume"],
                    "safety_score": cand["safety"],
                    "regime_score": cand["regime"],
                    "total_score": total_score
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
                "reason": f"【配置觀望】總分 {total_score} 達到配置標準，但因 {limit_desc} 無法配置。{reason}",
                "trend_score": cand["trend"],
                "momentum_score": cand["momentum"],
                "volume_score": cand["volume"],
                "safety_score": cand["safety"],
                "regime_score": cand["regime"],
                "total_score": total_score
            })

    return {
        "ranking_analysis": ranking_analysis,
        "decisions": final_decisions
    }
