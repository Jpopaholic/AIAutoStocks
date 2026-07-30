# Path: src/agents/analyst_agent.py
import json
import time
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config, safe_int, safe_float
from src.services.gemini_rotator import call_gemini_with_rotation, DailyRateLimitExceeded

# =====================================================================
# 1. 定義第二層：分析師評估模型 (Structured Outputs)
# =====================================================================
class AnalystStockScore(BaseModel):
    stock_code: str = Field(
        ..., 
        description="必須填寫股票代號字串，例如 '2330' 或 '00878'。此欄位必須與輸入的股票列表代號完全一致."
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

# 系統預設的金融交易技能清單 (引導評分)
DEFAULT_TRADING_SKILLS = [
    "均線交叉策略 (Moving Average Cross): 當短線均線 (MA5) 向上突破長線均線 (MA20) 且有量能配合時，視為黃金交叉（應給予高趨勢/動能得分）；跌破時為死亡交叉（應降低趨勢/動能得分，提高風險得分並調降總分）。",
    "相對強弱指標 (RSI): 評估短期超買與超賣狀態。RSI > 70 視為超買過熱（應調低風險/防守得分），RSI < 30 視為超賣超跌（若出現止跌訊號，可調高風險得分代表防守空間大，且分批佈局機會好）。",
    "平滑異同移動平均線 (MACD): MACD 柱狀圖紅綠柱代表多空動能。柱狀圖翻紅、快線向上突破慢線時動能轉強（應給予高動能得分）；柱狀圖翻綠、快線跌破慢線時動能走弱（應調低動能得分）。",
    "趨向指標 (DMI): ADX > 25 代表趨勢顯著。+DI 向上穿越 -DI 代表多頭強勢（趨勢/動能得分高）；-DI 向上穿越 +DI 代表空頭強勢（趨勢得分低，風險得分低）。",
    "成交量均線 (VOL MA): VOL_MA5 與 VOL_MA20 判斷量能配合。價漲量增（收盤價高於昨日且成交量大於 VOL_MA5）代表多頭動能強（成交量得分高），價漲量縮或放量下跌為量價背離（成交量得分低）。",
    "動態風險控制與止損停利 (Dynamic Risk Control): 評估當前股價相較於支撐線、壓力線或波段低點的距離。若股價接近支撐區且乖離率小，則防守空間好（風險得分高）；若已突破上檔壓力且無支撐，或面臨高檔乖離過大，則防守空間極差（風險得分低）。"
]

def generate_analyst_assessments(
    stock_codes: List[str],
    klines_map: Dict[str, List[Dict[str, Any]]],
    extra_skills: Optional[List[str]] = None,
    regime_assessment: Optional[Dict[str, Any]] = None,
    call_gemini_fn: Optional[Any] = None
) -> List[Dict[str, Any]]:
    """
    第二層：量化技術分析師。針對觀察名單與持股進行評分 (Temperature = 0.0)
    並在評估前動態將「今日已停損股票」與「過往平倉交易得失記憶」注入 Skills 清單。
    """
    if not stock_codes:
        return []

    if call_gemini_fn is None:
        call_gemini_fn = call_gemini_with_rotation

    # 1. 技術指標補算
    from src.services.technical_indicators import compute_all_indicators
    for code in stock_codes + ["TAIEX"]:
        klines = klines_map.get(code, [])
        if klines:
            try:
                compute_all_indicators(klines)
            except Exception as e:
                print(f" [分析師代理] 警告: 計算股票 {code} 的技術指標失敗: {e}")

    # 2. 準備金融分析技能與動態前置 Skills 注入
    single_pct = config.limits.single_stock_pct
    pct_val = (single_pct * 100.0 if single_pct <= 1.0 else single_pct) if (single_pct is not None and single_pct > 0) else 20.0

    skills = list(DEFAULT_TRADING_SKILLS)
    for idx, s in enumerate(skills):
        if "可用資金之 20% 以內" in s:
            skills[idx] = s.replace("可用資金之 20% 以內", f"可用資金之 {pct_val:.0f}% 以內")

    if extra_skills:
        skills.extend(extra_skills)

    # ── [前置注入：今日已停損清單] ────────────────────────────────────
    try:
        from src.services.supabase_client import get_stop_loss_stocks_today
        stop_loss_stocks = get_stop_loss_stocks_today()
        if stop_loss_stocks:
            skills.append(f"今日已停損股票: {', '.join(stop_loss_stocks)}。若該股票出現在候選中，分析師應極其謹慎。")
    except Exception as e:
        print(f" [分析師代理] 獲取今日停損清單失敗: {e}")

    # 過濾只保留有 K 線數據的個股，避免空數據干擾
    valid_stock_codes = [c for c in stock_codes if c != "TAIEX" and klines_map.get(c)]
    if not valid_stock_codes:
        return []

    generation_config_analyst = {
        "response_mime_type": "application/json",
        "response_schema": AnalystStockScore,
        "temperature": 0.1
    }

    raw_scores = []

    skills_text = "\n".join([f"{idx+1}. {s}" for idx, s in enumerate(skills)])

    # 獲取大盤資訊
    taiex_info = ""
    taiex_klines = klines_map.get("TAIEX", [])
    if taiex_klines:
        latest_taiex = taiex_klines[-1]
        taiex_info = f"當前大盤加權指數 (TAIEX) 收盤價: {latest_taiex['close']} 元 | MA20: {latest_taiex.get('ma20', 'N/A')} | MA60: {latest_taiex.get('ma60', 'N/A')}"

    for idx, code in enumerate(valid_stock_codes):
        print(f" [分析師代理] 正在呼叫分析師評估單股 ({idx+1}/{len(valid_stock_codes)}): {code}...")
        
        klines = klines_map.get(code, [])
        recent_klines = klines[-15:]
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

        analyst_system_instruction = f"""
你是一個極其資深且敏銳的台股量化投資分析師。你熟悉台股市場特性與技術線圖分析。
你的任務是分析給定個股的日 K 線數據，進行獨立且客觀的量化評分與技術指標分析。
你需要為該檔個股進行五個維度的評分 (各個維度為 0 ~ 20 分，總分會由 Python 程式加總計算，你不需回傳總分，也不需填寫股票收盤價)。

【重要評分提示】：
- 你的評分是為了反映個股強弱。如果個股呈現多頭排列、放量突破、MACD翻紅或RSI強勢等信號，請勇敢地給予高分（趨勢得分與動能得分可達16~20分）。
- 請拉開評分差距，切忌盲目保守。強勢股給予高分，弱勢股給予低分。
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
1. 你的輸出必須完全符合所規定的 JSON Schema (AnalystStockScore)，不可包含額外文字。
2. 你的分析理由請一律使用「繁體中文」，限制在 80 到 150 字以內，精簡指出關鍵指標，請勿贅述每日歷史細節。
"""

        analyst_user_prompt = f"""
請針對股票代號 {code} 進行個股日 K 線的技術分析與評分。

{taiex_info}

● 股票代號 {code} 最近 15 天 K 線數據 (最下方為最新一日行情)：
{klines_text}

請基於上述數據，產出該股票的量化評分與詳細原因。
"""

        try:
            raw_analyst_response = call_gemini_fn(
                prompt=analyst_user_prompt,
                system_instruction=analyst_system_instruction,
                model_name=config.gemini_model,
                generation_config=generation_config_analyst
            )
            s_item = json.loads(raw_analyst_response)
            if isinstance(s_item, dict):
                s_item["stock_code"] = code
                raw_scores.append(s_item)
            else:
                raise ValueError("LLM 未返回字典格式")
        except DailyRateLimitExceeded as rpd_err:
            raise rpd_err
        except Exception as e:
            print(f" [分析師代理] 警告: 分析股票 {code} 失敗: {str(e)}，補上預設中性分。")
            raw_scores.append({
                "stock_code": code,
                "trend_score": 13,
                "momentum_score": 13,
                "volume_score": 13,
                "safety_score": 13,
                "regime_score": 13,
                "confidence": 0.3,
                "reason": f"（本股 Gemini 呼叫失敗，已補上預設中性評分 65 分，系統強制觀望。原因: {str(e)[:80]}）"
            })

        # 主動平滑停頓 10.0 秒，避免平滑發送時觸發 Gemini API 伺服器端排隊或 Rate Limit
        time.sleep(10.0)

    # Python 計算價格與總分
    analyst_scores = []
    seen_codes = set()
    for s_item in raw_scores:
        code = s_item.get("stock_code")
        if not code or code == "TAIEX":
            continue
        
        if code in seen_codes:
            print(f" [分析師代理] 警告: 偵測到重複的評分結果 {code}，自動忽略重複項。")
            continue
        seen_codes.add(code)
        
        klines = klines_map.get(code, [])
        price = safe_float(klines[-1]["close"] if klines else 10.0, default=10.0, min_val=0.01)
        
        trend = safe_int(s_item.get("trend_score"), default=10, min_val=0, max_val=20)
        momentum = safe_int(s_item.get("momentum_score"), default=10, min_val=0, max_val=20)
        volume = safe_int(s_item.get("volume_score"), default=10, min_val=0, max_val=20)
        safety = safe_int(s_item.get("safety_score"), default=10, min_val=0, max_val=20)
        regime = safe_int(s_item.get("regime_score"), default=10, min_val=0, max_val=20)
        total_score = trend + momentum + volume + safety + regime
        confidence = safe_float(s_item.get("confidence"), default=0.8, min_val=0.0, max_val=1.0)
        
        analyst_scores.append({
            "stock_code": code,
            "trend_score": trend,
            "momentum_score": momentum,
            "volume_score": volume,
            "safety_score": safety,
            "regime_score": regime,
            "confidence": confidence,
            "total_score": total_score,
            "price": price,
            "reason": s_item.get("reason", "技術量化指標尚可。")
        })

    analyst_scores.sort(key=lambda x: x["total_score"], reverse=True)
    return analyst_scores
