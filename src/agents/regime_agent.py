# Path: src/agents/regime_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config, safe_float
from src.services.gemini_rotator import call_gemini_with_rotation
from src.services.technical_indicators import compute_all_indicators

# 1. 定義大盤氣候診斷模型
class MarketRegimeAssessment(BaseModel):
    regime: str = Field(
        ...,
        description="市場狀態，必須限為 'STRONG_BULL' (強勢多頭), 'REBOUND_BULL' (驚驚漲/震盪偏多), 'CALM_RANGE' (低波動橫盤), 'VOLATILE_RANGE' (高波動震盪), 'CORRECTION_BEAR' (震盪修正), 'PANIC_BEAR' (恐慌空頭)"
    )
    posture: str = Field(
        ...,
        description="交易姿態，必須限為 'STRONG_ATTACK' (強攻攻勢), 'MODERATE_ATTACK' (穩健進攻), 'CHOPPY_TACTICAL' (震盪靈活), 'DEFENSIVE_ACCUMULATION' (防禦承接), 'STRICT_DEFENSE' (極度保守)"
    )
    risk_multiplier: float = Field(
        ...,
        description="風險限額乘數，介於 0.15 到 1.0 之間。此乘數用來調整買入倉位上限（部位控制），即使在防禦承接或震盪修正大盤中，最小值也應保持在 0.25~0.45 之間，以允許對具有強烈技術支撐或特大個股利多的標的進行微量/零股配置，而非一刀切完全關閉買入功能。"
    )
    target_cash_ratio: float = Field(
        ...,
        description="建議保持的最低現金儲備比例 (%)，介於 0.05 (5%) 到 0.90 (90%) 之間。例如強攻攻勢為 0.05~0.15，穩健進攻為 0.15~0.30，震盪靈活為 0.30~0.50，防禦承接為 0.50~0.75，極度保守為 0.75~0.90。"
    )
    allowed_buy_styles: List[str] = Field(
        ...,
        description="允許的買進動作型態列表，例如 ['BREAKOUT', 'PULLBACK', 'RANGE_LOW', 'DEFENSIVE_VALUE', 'MICRO_POSITION', 'NONE']"
    )
    tactical_directive: str = Field(
        ...,
        description="針對當前大盤氣候給予組合經理人 (Portfolio Manager) 的具體動態姿態操作與風控特別指引 (繁體中文)，說明應採取的控部位、停利停損與策略選股重點。"
    )
    reason: str = Field(
        ...,
        description="判斷當前市場狀態的詳細理由與宏觀分析依據 (繁體中文)，請分析短期均線、價格趨勢與成交量變化"
    )

def generate_market_regime(taiex_klines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    分析大盤 K 線數據，判定當前細粒度市場 Regime、交易姿態、目標現金比率與姿態戰術指令。
    :param taiex_klines: 大盤加權指數的 K 線歷史數據列表
    :returns: 解析後的 MarketRegimeAssessment JSON 字典
    """
    if not taiex_klines:
        print(" [Regime Layer] 警告: 未收到大盤 K 線數據，將退回預設正常狀態")
        return {
            "regime": "CALM_RANGE",
            "posture": "CHOPPY_TACTICAL",
            "risk_multiplier": 0.7,
            "target_cash_ratio": 0.35,
            "allowed_buy_styles": ["PULLBACK", "DEFENSIVE_VALUE"],
            "tactical_directive": "無大盤數據，採取中性盤整姿態，維持約 35% 現金儲備，優先考慮逢低拉回標的。",
            "reason": "無可用的大盤 K 線數據，自動退回到預設正常盤整狀態。"
        }

    # 計算大盤的所有技術指標（包含 MA5、MA20、RSI、MACD、DMI）
    try:
        compute_all_indicators(taiex_klines)
    except Exception as taiex_ind_err:
        print(f" [Regime Layer] 警告: 計算大盤技術指標失敗: {taiex_ind_err}")

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
        
        ma5_str = f"{k['ma5']:.2f}" if k.get('ma5') is not None else "N/A"
        ma20_str = f"{k['ma20']:.2f}" if k.get('ma20') is not None else "N/A"
        ma60_str = f"{k['ma60']:.2f}" if k.get('ma60') is not None else "N/A"
        rsi_str = f"{k['rsi14']:.2f}" if k.get('rsi14') is not None else "N/A"
        macd_str = f"(快線:{k['macd']:.2f}, 慢線:{k['macd_signal']:.2f}, 柱狀圖:{k['macd_hist']:.2f})" if (k.get('macd') is not None and k.get('macd_signal') is not None and k.get('macd_hist') is not None) else "N/A"
        dmi_str = f"(+DI:{k['plus_di']:.1f}, -DI:{k['minus_di']:.1f}, ADX:{k['adx']:.1f})" if (k.get('adx') is not None and k.get('plus_di') is not None and k.get('minus_di') is not None) else "N/A"
        
        taiex_lines.append(
            f"  日期: {k.get('date', '')} | 開盤: {float(o_price):.2f} | 最高: {float(h_price):.2f} | 最低: {float(l_price):.2f} | 收盤: {float(c_price):.2f} | "
            f"成交量: {int(volume):,} | MA5: {ma5_str} | MA20: {ma20_str} | MA60 (季線): {ma60_str} | RSI: {rsi_str} | MACD: {macd_str} | DMI: {dmi_str}"
        )
    taiex_text = "\n".join(taiex_lines)

    system_instruction = (
        "你是一個資深的台股宏觀市場分析專家，擅長透過大盤指數走勢、成交量變化、均線排列與波動趨勢來判斷當前細粒度的市場狀態 (Market Regime) 與具體的姿態操作戰術。\n"
        "你的任務是分析給定的大盤指數 K 線數據，判定目前的市場狀態 (Regime)、應採取的交易姿態 (Posture)、風險限額乘數 (Risk Multiplier)、建議目標現金比例 (Target Cash Ratio)、允許買進動作型態 (Allowed Buy Styles) 與戰術特別指令 (Tactical Directive)。\n\n"
        "數據中已為您計算好了 MA5、MA20 (月線)、MA60 (季線)、RSI、MACD 與 DMI 等指標，請務必將這些指標作為您的重要評判依據！\n\n"
        "【六階市場狀態 (regime) 定義】：\n"
        "- 'STRONG_BULL': 強勢多頭。均線呈強勢多頭排列 (MA5 > MA20 > MA60)，且收盤價站穩所有均線之上，帶量攻高。\n"
        "- 'REBOUND_BULL': 驚驚漲/震盪偏多。價格站穩月線 MA20 上方，但上方有季線反壓或呈現拉回打底後再彈升走勢。\n"
        "- 'CALM_RANGE': 低波動橫盤。無明顯趨勢，價格在 MA20 附近窄幅整理，成交量低迷。\n"
        "- 'VOLATILE_RANGE': 高波動震盪。單日上下波幅劇烈，多空洗盤，方向不明。\n"
        "- 'CORRECTION_BEAR': 震盪修正/初跌段。跌破月線 MA20，高點逐漸降低，但季線 MA60 仍具緩衝力道或尚未出現全面恐慌潰敗。\n"
        "- 'PANIC_BEAR': 恐慌空頭/主跌段。均線空頭排列，收盤價跌破 MA60 季線，長黑帶量下探。\n\n"
        "【五階交易姿態 (posture) 與對應姿態操作指引】：\n"
        "1. STRONG_ATTACK (強攻攻勢)：\n"
        "   - 適用氣候：STRONG_BULL\n"
        "   - risk_multiplier = 0.85 ~ 1.00，target_cash_ratio = 0.05 ~ 0.15\n"
        "   - allowed_buy_styles = ['BREAKOUT', 'PULLBACK', 'MOMENTUM']\n"
        "   - 指令重點：積極布局主攻族群，允許追高與突破買進，保持低現金比例擴大獲利。\n\n"
        "2. MODERATE_ATTACK (穩健進攻)：\n"
        "   - 適用氣候：REBOUND_BULL\n"
        "   - risk_multiplier = 0.70 ~ 0.85，target_cash_ratio = 0.15 ~ 0.30\n"
        "   - allowed_buy_styles = ['PULLBACK', 'SUPPORT_REBOUND']\n"
        "   - 指令重點：偏好逢拉回打底買進，不盲目追高，維繫中等部位。\n\n"
        "3. CHOPPY_TACTICAL (震盪靈活)：\n"
        "   - 適用氣候：CALM_RANGE, VOLATILE_RANGE\n"
        "   - risk_multiplier = 0.50 ~ 0.65，target_cash_ratio = 0.30 ~ 0.50\n"
        "   - allowed_buy_styles = ['RANGE_LOW', 'DEFENSIVE_VALUE']\n"
        "   - 指令重點：區間操作、快進快出，嚴格執行停利與停損，維持三成以上現金靈活調度。\n\n"
        "4. DEFENSIVE_ACCUMULATION (防禦承接)：\n"
        "   - 適用氣候：CORRECTION_BEAR, 高波動劇烈洗盤\n"
        "   - risk_multiplier = 0.25 ~ 0.45，target_cash_ratio = 0.50 ~ 0.75\n"
        "   - allowed_buy_styles = ['DEFENSIVE_VALUE', 'MICRO_POSITION']\n"
        "   - 指令重點：大盤修正但非暴跌，保留五成以上現金。非一刀切禁止買入！允許對技術總分與安全分極優個股進行少量/零股建倉。\n\n"
        "5. STRICT_DEFENSE (極度保守)：\n"
        "   - 適用氣候：PANIC_BEAR\n"
        "   - risk_multiplier = 0.15 ~ 0.25，target_cash_ratio = 0.75 ~ 0.90\n"
        "   - allowed_buy_styles = ['HIGH_SAFETY_ONLY', 'NONE']\n"
        "   - 指令重點：現金為王，暫停常規大額開倉，優先減碼弱勢部位，保留七成五以上最高現金水位。\n\n"
        "你的輸出必須完全符合所規規定之 JSON Schema，分析理由與戰術特別指令請一律使用「繁體中文」。"
    )

    user_prompt = (
        f"請根據以下大盤加權指數 (TAIEX) 最近 30 天日 K 線數據與預估技術指標，分析當前的市場狀態與細粒度姿態操作戰術：\n\n"
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
        res = json.loads(raw_response)
        res["risk_multiplier"] = safe_float(res.get("risk_multiplier"), default=0.7, min_val=0.15, max_val=1.0)
        res["target_cash_ratio"] = safe_float(res.get("target_cash_ratio"), default=0.35, min_val=0.05, max_val=0.90)
        if not isinstance(res.get("allowed_buy_styles"), list):
            res["allowed_buy_styles"] = ["PULLBACK"]
        if not res.get("tactical_directive"):
            res["tactical_directive"] = f"當前處於 {res.get('posture', 'NORMAL')} 姿態，請維持風險限額乘數 {res['risk_multiplier']:.2f} 並保持相應現金比率。"
        if not res.get("reason"):
            res["reason"] = res.get("tactical_directive") or f"大盤最新氣候判定為 {res.get('regime')} ({res.get('posture')})，風險限額乘數調整為 {res['risk_multiplier']:.2f}。"
        return res
    except Exception as e:
        print(f" [Regime Layer] 大盤氣候判定失敗: {str(e)}")
        return {
            "regime": "CALM_RANGE",
            "posture": "CHOPPY_TACTICAL",
            "risk_multiplier": 0.7,
            "target_cash_ratio": 0.35,
            "allowed_buy_styles": ["PULLBACK", "DEFENSIVE_VALUE"],
            "tactical_directive": "大盤氣候調用異常，自動退回震盪靈活姿態，維護 35% 現金儲備。",
            "reason": f"大盤氣候判定調用出錯，自動回退至預設正常狀態。錯誤詳情: {str(e)}"
        }

