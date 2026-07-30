# Path: src/agents/decision_agent.py
import json
import math
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config, safe_int, safe_float
from src.services.gemini_rotator import call_gemini_with_rotation, DailyRateLimitExceeded
from src.services.supabase_client import (
    get_orders, 
    get_holdings,
    get_system_fault_status,
    get_pending_liquidation_stocks
)

# =====================================================================
# 2. 定義第三層：投資組合經理決策模型 (Structured Outputs)
# =====================================================================
class PMStockDecision(BaseModel):
    stock_code: str = Field(
        ..., 
        description="必須填寫股票代號字串，例如 '2330' 或 '00878'。此欄位必須與輸入的股票列表代號完全一致。"
    )
    action: str = Field(
        ..., 
        description="你對該股的交易建議。限選: BUY (建議買入的非持股或加碼持股，配合相對排名且評分高於50且技術面無重大缺陷), SELL (建議賣出/減碼持股，或持股總分<60，或為換股調倉而賣出), HOLD (觀望或續抱)。"
    )
    pm_reason: str = Field(
        ..., 
        description="主動投資組合經理對該檔股票的配置或交易決策理由（使用繁體中文，限 100 字內）。請著重於續抱、賣出或調倉理由，且絕對不要在理由中重述或提到股票的技術評分或總分（例如「評分71分」），因為分數會由系統自動標註與合併。"
    )
    allocation_weight: int = Field(
        ..., 
        description="買入配置權重 (1-5)。1 代表最低配置優先度，5 代表最高配置優先度。若 action 不為 BUY，此值應設定為 0。"
    )

class PortfolioDecision(BaseModel):
    ranking_analysis: str = Field(
        ...,
        description="對於所有分析股票由強到弱的相對排名排序與橫向對比綜合解析（使用繁體中文，限制 150 ~ 250 字，此欄位包含今日資金配置的核心想法、為何選出這些買入/賣出標的，以及如何控制大盤風險）。"
    )
    decisions: List[PMStockDecision] = Field(
        ...,
        description="所有股票的交易決策列表。必須包含所有輸入分析的股票，每檔股票各一筆。"
    )

def generate_portfolio_decisions(
    stock_codes: List[str],
    analyst_scores: List[Dict[str, Any]],
    klines_map: Dict[str, List[Dict[str, Any]]],
    current_holdings: List[Dict[str, Any]],
    regime_assessment: Optional[Dict[str, Any]] = None,
    call_gemini_fn: Optional[Any] = None,
    pending_stocks: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    第三層：投資組合配置經理人。根據分析師打分數橫向對比做交易決策與部位比例分配 (Temperature = 0.2)
    """
    if not stock_codes:
        return {"ranking_analysis": "無分析標的。", "decisions": []}

    if call_gemini_fn is None:
        call_gemini_fn = call_gemini_with_rotation

    # 1. 取得動態限額與目前的 NAV / 資金狀況
    from src.services.nav_calculator import calculate_nav, get_dynamic_limits
    cash_balance, total_equity, net_asset_value = calculate_nav()
    
    # 2. 獲取限額與安全限期設定 (配合大盤氣候風險乘數)
    single_limit, daily_limit = get_dynamic_limits()
    if regime_assessment:
        multiplier = max(float(regime_assessment.get("risk_multiplier", 1.0)), 0.15)
        single_limit *= multiplier
    
    # 3. 取得動態風控每日剩餘可交易限額
    try:
        from src.services.nav_calculator import get_today_remaining_limit
        remaining_daily_limit = get_today_remaining_limit()
    except Exception:
        _, remaining_daily_limit = get_dynamic_limits()

    # 4. 取得智慧等候平倉名單
    if pending_stocks is None:
        try:
            pending_stocks = get_pending_liquidation_stocks()
        except Exception as e:
            print(f" [決策代理] 警告: 無法讀取智慧平倉列表: {e}")
            pending_stocks = []

    # 5. 載入今日停損買回冷卻名單
    cooldown_stocks = set()
    try:
        from src.time_manager import get_local_taiwan_midnight_utc_range
        start_utc, end_utc = get_local_taiwan_midnight_utc_range()
        orders = get_orders(start_date=start_utc, end_date=end_utc)
        for o in orders:
            if o.get("action") == "SELL" and o.get("status", "FILLED") == "FILLED":
                realized_pnl = float(o.get("realized_pnl") or 0.0)
                if realized_pnl < 0:
                    cooldown_stocks.add(o.get("stock_code"))
    except Exception as e:
        print(f" [決策代理] 警告: 載入今日停損冷卻名單失敗: {str(e)}")

    # 6. 準備近期訂單資訊供 PM 參考
    recent_orders_info = ""
    try:
        start_utc, end_utc = get_local_taiwan_midnight_utc_range()
        orders = get_orders(start_date=start_utc, end_date=end_utc)
        if orders:
            order_lines = []
            for o in orders:
                order_lines.append(f"  代號: {o['stock_code']} | 動作: {o['action']} | 數量: {o['quantity']} | 委託價: {o['price']} | 狀態: {o.get('status', 'FILLED')}")
            recent_orders_info = "\n".join(order_lines)
    except Exception as e:
        print(f" [決策代理] 警告: 載入今日委託資訊失敗: {e}")

    if not recent_orders_info:
        recent_orders_info = "今日尚無委託下單紀錄。"

    # 7. 格式化分析師技術評分報告
    analyst_report_lines = []
    analyst_map = {}
    # 按總分降序排列，方便 PM 橫向比較
    sorted_analyst_scores = sorted(analyst_scores, key=lambda x: x.get("total_score", 0), reverse=True)
    for idx, s in enumerate(sorted_analyst_scores):
        analyst_map[s["stock_code"]] = s
        analyst_report_lines.append(
            f"排名 {idx+1}. 股票 {s['stock_code']} | 技術總分: {s['total_score']} "
            f"(趨勢:{s['trend_score']}, 動能:{s['momentum_score']}, 成交量:{s['volume_score']}, 安全:{s['safety_score']}, 大盤:{s['regime_score']}) "
            f"| 最新收盤價: {s['price']} 元\n  分析理由: {s['reason']}"
        )
    analyst_report_text = "\n".join(analyst_report_lines)

    # 8. 格式化目前持股現況
    holdings_lines = []
    for h in current_holdings:
        code = h["stock_code"]
        qty = h["quantity"]
        avg_price = h["average_price"]
        current_price = avg_price
        for s in analyst_scores:
            if s["stock_code"] == code:
                current_price = s["price"]
                break
        cost = qty * avg_price
        mkt_val = qty * current_price
        pnl = mkt_val - cost
        pnl_pct = (pnl / cost * 100.0) if cost > 0 else 0.0
        holdings_lines.append(
            f"  股票: {code} | 庫存數量: {qty:,.0f} 股 | 平均成本: {avg_price:.2f} 元 | 目前現價: {current_price:.2f} 元 | 帳面損益: {pnl:+,.0f} 元 ({pnl_pct:+.2f}%)"
        )
    holdings_text = "\n".join(holdings_lines) if holdings_lines else "目前無任何股票持股。"

    # 9. 準備大盤氣候環境
    regime_text = "目前無可用大盤氣候判定。"
    if regime_assessment:
        regime_text = (
            f"市場狀態: {regime_assessment.get('regime', 'UNKNOWN')} | "
            f"交易姿態: {regime_assessment.get('posture', 'UNKNOWN')} | "
            f"風險限額乘數: {regime_assessment.get('risk_multiplier', 1.0)}\n"
            f"氣候分析理由: {regime_assessment.get('reason', '')}"
        )

    # 10. 構建經理人系統指令
    from src.services.trading_memory import get_experience_context, get_active_skills_context
    active_skills_text = get_active_skills_context(is_paper=False)
    experience_text = get_experience_context(limit=3)

    pm_system_instruction = f"""
你是一個極其資深且穩健的台股投資組合配置經理 (Portfolio Manager)。
你的任務是審查分析師提供的個股技術評分報告，並根據當前大盤氣候環境、可用資金及目前持股，產出最終的交易決策與部位資金分配比例。

{active_skills_text}

{experience_text}

【中長期穩健投資哲學】：
1. 你的投資風格是「中長期穩健投資」，必須極力避免頻繁交易、微調調倉及短線投機。交易印花稅與手續費滑價是利潤的殺手。
2. 你必須進行多檔個股的橫向相對比較。除非某檔股票的技術總分在所有分析個股中「顯著突出」（即在技術面上具有絕對強勢的突破優勢），否則不應發出 BUY 決策。
3. 除非某檔持股技術面極度崩壞跌破防線，否則不應輕易發出 SELL 決策。
4. 在一般評分或無極端行情下，你應該極度傾向給予 `HOLD` (觀望續抱/不做買賣)，以控制週轉率。
5. 買入決策時，技術面總分必須是群體中最優秀的前列；賣出決策時，總分必須顯著低於其他標的。

請嚴格遵守以下交易配置限制：
1. 配置權重 `allocation_weight` 代表買入優先度 (1-5)。若 action 為 SELL 或 HOLD 且你打算出清此檔，權重應填寫 0。
2. 【大盤防禦降額】：如果大盤氣候呈現 BEARISH_TREND 或交易姿態為 DEFENSIVE / STRONG_DEFENSIVE，請大幅降低持股權重或增加現金比例，在此氣候下原則上「禁止新買入任何個股」。

請嚴格遵守以下指示：
1. 你的 decisions 列表中，必須包含所有輸入研究員評估之股票的決策，每檔股票必須且只能出現一次，絕對不可有任何漏遺或省略！即使該檔股票的決策是 HOLD，也必須包含在 decisions 列表中。
2. 你的輸出必須完全符合規定的 JSON Schema (PortfolioDecision)，不可包含額外文字。
3. `ranking_analysis` 需使用繁體中文，簡明扼要說明大盤與個股橫向排名的綜合配置邏輯，長度約 150-250 字左右。
4. 個股 `pm_reason` 需使用繁體中文，詳細指出該股相對其他標的之優劣與此交易決策之心路歷程，長度約 80-100 字左右。絕對不要在 pm_reason 中重述或提到股票的技術評分或總分。
"""

    pm_user_prompt = f"""
請根據以下資訊，產出最終的投資組合交易決策與比例權重分配。

【🌦️ 當前大盤氣候與交易姿態】：
{regime_text}

【💰 帳戶資金狀態】：
- 資產淨總值 (NAV): {net_asset_value:,.0f} 元
- 可用現金餘額: {cash_balance:,.0f} 元
- 單一股票最大可分配限額: {single_limit:,.0f} 元

【📈 目前持股現況】：
{holdings_text}

【🔄 今日已完成與預約之交易委託】：
{recent_orders_info}

【🧠 研究員個股量化技術評估 (按技術總分降序排列，供您橫向對比)】：
{analyst_report_text}

【智慧平倉等候清單 (若列表中股票有 BUY，應忽略並改為 HOLD/SELL)】:
{pending_stocks}

請基於上述資訊進行橫向對比與中長期資金配置，產出最終的決策。
"""

    generation_config_pm = {
        "response_mime_type": "application/json",
        "response_schema": PortfolioDecision,
        "temperature": 0.2  # 恢復為預設 0.2 配合穩定模型
    }

    try:
        print(" [決策代理] 呼叫投資組合經理決策層 (Gemini)...")
        raw_pm_response = call_gemini_fn(
            prompt=pm_user_prompt,
            system_instruction=pm_system_instruction,
            model_name=config.gemini_model,
            generation_config=generation_config_pm
        )
        clean_str = raw_pm_response.strip()
        if clean_str.startswith("```"):
            lines = clean_str.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_str = "\n".join(lines).strip()
        pm_data = json.loads(clean_str)
        ranking_analysis = pm_data.get("ranking_analysis", "多股橫向配置分析。")
        raw_decisions = pm_data.get("decisions", [])
    except DailyRateLimitExceeded as rpd_err:
        raise rpd_err
    except Exception as e:
        print(f" [決策代理] 警告: 經理人決策失敗: {str(e)}")
        ranking_analysis = "經理人層呼叫異常，啟動防禦性安全機制。"
        raw_decisions = []

    # 11. 預算估計與賣出部位釋放
    # 賣出動作若成立，可當場釋放現金，加入本次買入預算中
    remaining_cash = cash_balance
    for d in raw_decisions:
        code = d.get("stock_code")
        if not code or code == "TAIEX":
            continue
            
        action = d.get("action", "HOLD").upper()
        matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
        holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
        
        ana = analyst_map.get(code, {})
        price = float(ana.get("price") or 10.0)
        
        if action == "SELL" and holding_qty > 0:
            released_cash = price * holding_qty * 0.995  # 扣除滑價
            remaining_cash += released_cash
            print(f" [決策代理] 調倉預算：賣出 {code} 預估可釋出可用資金 {released_cash:,.0f} 元")

    final_decisions = []
    buy_candidates = []

    # 12. 逐一處理個股決策與 Python 護欄過濾
    regime = regime_assessment.get("regime", "UNKNOWN") if regime_assessment else "UNKNOWN"
    posture = regime_assessment.get("posture", "UNKNOWN") if regime_assessment else "UNKNOWN"

    for d in raw_decisions:
        code = d.get("stock_code")
        if not code or code == "TAIEX":
            continue
            
        action = d.get("action", "HOLD").upper()
        pm_reason = d.get("pm_reason", d.get("reason", "")).strip()

        # 獲取分析師評分與價格
        ana = analyst_map.get(code, {})
        if not ana:
            continue
            
        trend = safe_int(ana.get("trend_score"), default=10, min_val=0, max_val=20)
        momentum = safe_int(ana.get("momentum_score"), default=10, min_val=0, max_val=20)
        volume = safe_int(ana.get("volume_score"), default=10, min_val=0, max_val=20)
        safety = safe_int(ana.get("safety_score"), default=10, min_val=0, max_val=20)
        regime_s = safe_int(ana.get("regime_score"), default=10, min_val=0, max_val=20)
        total_score = trend + momentum + volume + safety + regime_s
        price = safe_float(ana.get("price"), default=10.0, min_val=0.01)
        analyst_reason = ana.get("reason", "").strip()

        merged_reason = f"【量化評分: {total_score}分 (趨勢:{trend} | 動能:{momentum} | 量能:{volume} | 安全:{safety} | 大盤:{regime_s})】{analyst_reason}\n【經理人決策理由】{pm_reason}"

        matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
        holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
        
        # 護欄 1：智慧等候平倉排隊中股票處理 (尊重 AI 決策，移除硬編碼 score < 70 強退)
        if code in pending_stocks:
            if holding_qty <= 0:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【智慧平倉安全過濾】無持股庫存，強制觀望。{merged_reason}"
            elif action == "SELL":
                qty = holding_qty
                decision_reason = f"【智慧平倉排隊賣出】總評分 {total_score} 分，經理人建議賣出平倉。{merged_reason}"
            else:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【智慧平倉排隊暫緩】總評分 {total_score} 分，經理人評估暫緩賣出觀望。{merged_reason}"
            
            final_decisions.append({
                "stock_code": code,
                "action": action,
                "price": price,
                "quantity": safe_float(qty, default=0.0, min_val=0.0),
                "confidence": safe_float(total_score / 100.0, default=0.5, min_val=0.0, max_val=1.0),
                "reason": decision_reason,
                "trend_score": trend,
                "momentum_score": momentum,
                "volume_score": volume,
                "safety_score": safety,
                "regime_score": regime_s,
                "total_score": total_score
            })
            continue

        # 護欄 2：大盤防守風控
        if action == "BUY" and (regime == "BEARISH_TREND" or posture in ("DEFENSIVE", "STRONG_DEFENSIVE")):
            action = "HOLD"
            final_decisions.append({
                "stock_code": code,
                "action": "HOLD",
                "price": price,
                "quantity": 0.0,
                "confidence": total_score / 100.0,
                "reason": f"【大盤防禦控險】大盤氣候處於空頭或防守，限制新倉買入。{merged_reason}",
                "trend_score": trend,
                "momentum_score": momentum,
                "volume_score": volume,
                "safety_score": safety,
                "regime_score": regime_s,
                "total_score": total_score
            })
            continue

        # 已持有倉位 (解耦 score < 60 強制賣出，全權依據經理人動態 Skills 與 action 推理)
        if holding_qty > 0:
            if action == "SELL":
                qty = holding_qty
                decision_reason = f"【經理人決策賣出】總分 {total_score} 分，經理人依據動態戰術 Skills 建議賣出平倉。{merged_reason}"
            else:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【經理人決策續抱】總分 {total_score} 分，經理人依據動態戰術 Skills 建議觀望續抱。{merged_reason}"
            
            final_decisions.append({
                "stock_code": code,
                "action": action,
                "price": price,
                "quantity": safe_float(qty, default=0.0, min_val=0.0),
                "confidence": safe_float(total_score / 100.0, default=0.5, min_val=0.0, max_val=1.0),
                "reason": decision_reason,
                "trend_score": trend,
                "momentum_score": momentum,
                "volume_score": volume,
                "safety_score": safety,
                "regime_score": regime_s,
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
                    "regime_score": regime_s,
                    "total_score": total_score
                })
            elif action == "BUY":
                # 解耦 score < 50 強制禁買，全權依據經理人動態 Skills 與 action 推理 (後端保留資金餘額與單筆限額防線)
                raw_w = d.get("allocation_weight")
                alloc_weight = safe_int(raw_w, default=3, min_val=1, max_val=5)

                if alloc_weight < 1:
                    alloc_weight = 1
                elif alloc_weight > 5:
                    alloc_weight = 5
                    
                buy_candidates.append({
                    "stock_code": code,
                        "total_score": total_score,
                        "allocation_weight": alloc_weight,
                        "trend": trend,
                        "momentum": momentum,
                        "volume": volume,
                        "safety": safety,
                        "regime": regime_s,
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
                    "regime_score": regime_s,
                    "total_score": total_score
                })

    # ── [自動補全缺失的個股決策] ──────────────────────────────────────────
    # 如果分析師評估的個股未出現在經理人決策中，自動補上預設的 HOLD (觀望) 或 SELL (風控) 決策
    decided_codes = {d.get("stock_code") for d in raw_decisions if d.get("stock_code")}
    for code, ana in analyst_map.items():
        if code not in decided_codes:
            trend = safe_int(ana.get("trend_score"), default=10, min_val=0, max_val=20)
            momentum = safe_int(ana.get("momentum_score"), default=10, min_val=0, max_val=20)
            volume = safe_int(ana.get("volume_score"), default=10, min_val=0, max_val=20)
            safety = safe_int(ana.get("safety_score"), default=10, min_val=0, max_val=20)
            regime_s = safe_int(ana.get("regime_score"), default=10, min_val=0, max_val=20)
            total_score = trend + momentum + volume + safety + regime_s
            price = safe_float(ana.get("price"), default=10.0, min_val=0.01)
            analyst_reason = ana.get("reason", "").strip()
            
            merged_reason = f"【量化評分: {total_score}分 (趨勢:{trend} | 動能:{momentum} | 量能:{volume} | 安全:{safety} | 大盤:{regime_s})】{analyst_reason}\n【經理人決策理由】（經理人未明確提及，系統自動判定為觀望）"
            
            matching_holding = next((h for h in current_holdings if h["stock_code"] == code), None)
            holding_qty = float(matching_holding.get("quantity", 0)) if matching_holding else 0.0
            
            # 若目前持有該股，需要檢查是否觸發風控賣出
            if holding_qty > 0:
                if total_score < 60:
                    action = "SELL"
                    qty = holding_qty
                    decision_reason = f"【風控強制賣出】總分 {total_score} 分低於持有門檻 60 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 安全: {safety}, 大盤: {regime_s})。{merged_reason}"
                else:
                    action = "HOLD"
                    qty = 0.0
                    decision_reason = f"【量化評分續抱】總分 {total_score} 分維持在持有區間 60~100 (趨勢: {trend}, 動能: {momentum}, 量能: {volume}, 安全: {safety}, 大盤: {regime_s})。{merged_reason}"
            else:
                action = "HOLD"
                qty = 0.0
                decision_reason = f"【量化評分觀望】自動判定為觀望。{merged_reason}"
                
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
                "regime_score": regime_s,
                "total_score": total_score
            })

    # 13. Python 程式端主導買入候選股預算分配 (水箱分配 Water-Filling)
    total_budget = min(remaining_cash, remaining_daily_limit)
    
    # 計算每檔股票的加權因子
    for cand in buy_candidates:
        cand["weight_factor"] = safe_float(cand["total_score"] * cand["allocation_weight"], default=0.0, min_val=0.0)
        
    # 用於分配預算的候選清單
    alloc_candidates = list(buy_candidates)
    budgets = {cand["stock_code"]: 0.0 for cand in alloc_candidates}
    
    # 比例分配限制單股上限
    remaining_alloc_budget = total_budget
    uncapped = list(alloc_candidates)
    
    while uncapped and remaining_alloc_budget > 0:
        total_factor = sum(c["weight_factor"] for c in uncapped)
        if total_factor <= 0:
            break
            
        new_uncapped = []
        any_capped = False
        for c in uncapped:
            share = remaining_alloc_budget * (c["weight_factor"] / total_factor)
            if share > single_limit:
                budgets[c["stock_code"]] = single_limit
                remaining_alloc_budget -= single_limit
                any_capped = True
            else:
                new_uncapped.append((c, share))
                
        if not any_capped:
            for c, share in new_uncapped:
                budgets[c["stock_code"]] = share
            break
        else:
            uncapped = [item[0] for item in new_uncapped]

    # 計算初步股數與剩餘零星預算
    quantities = {}
    costs = {}
    for cand in buy_candidates:
        code = cand["stock_code"]
        price = cand["price"]
        allocated = budgets[code]
        
        qty = math.floor(allocated / price) if price > 0 else 0
        quantities[code] = qty
        costs[code] = qty * price

    # 處理因無條件捨去而留下來的零星預算
    leftover = total_budget - sum(costs.values())
    
    # 依加權因子降序排序，嘗試追加剩餘零星預算
    leftover_candidates = sorted(buy_candidates, key=lambda x: x["weight_factor"], reverse=True)
    for cand in leftover_candidates:
        code = cand["stock_code"]
        price = cand["price"]
        if price <= 0:
            continue
            
        while leftover >= price and (costs[code] + price) <= single_limit:
            quantities[code] += 1
            costs[code] += price
            leftover -= price

    # 生成最終決策與原因描述
    for cand in buy_candidates:
        code = cand["stock_code"]
        total_score = cand["total_score"]
        alloc_weight = cand["allocation_weight"]
        price = cand["price"]
        reason = cand["reason"]
        qty = quantities[code]
        cost = costs[code]
        
        if qty > 0:
            final_decisions.append({
                "stock_code": code,
                "action": "BUY",
                "price": price,
                "quantity": safe_float(qty, default=0.0, min_val=0.0),
                "confidence": safe_float(total_score / 100.0, default=0.5, min_val=0.0, max_val=1.0),
                "reason": f"【投資組合加權分配買入】技術評定總分 {total_score} 分，經理人權重 {alloc_weight}，融合分配預算 {cost:,.0f} 元。{reason}",
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
            if single_limit < price:
                limit_desc = f"單股交易限額 {single_limit:,.0f} 元低於股票單價 {price:,.0f} 元"
            else:
                limit_desc = f"融合分配比例過低且可用資金不足以買入 1 股 (分配額 {budgets[code]:,.0f} 元 < 單價 {price:,.0f} 元)"
                
            final_decisions.append({
                "stock_code": code,
                "action": "HOLD",
                "price": price,
                "quantity": 0.0,
                "confidence": total_score / 100.0,
                "reason": f"【配置觀望】評分 {total_score} 達到配置標準，但因 {limit_desc} 無法配置。{reason}",
                "trend_score": cand["trend"],
                "momentum_score": cand["momentum"],
                "volume_score": cand["volume"],
                "safety_score": cand["safety"],
                "regime_score": cand["regime"],
                "total_score": total_score
            })

    # ── 14. 檢查是否有風控覆寫，若有則動態追加提示至 ranking_analysis ──
    try:
        from src.config import get_stock_name
        overridden_items = []
        
        # 建立經理人原始決策對照表
        raw_action_map = {}
        for d in raw_decisions:
            code = d.get("stock_code")
            if code:
                raw_action_map[code] = str(d.get("action", "HOLD")).strip().upper()
                
        for fd in final_decisions:
            code = fd["stock_code"]
            final_act = fd["action"].upper()
            stock_name = get_stock_name(code)
            name_display = f" {stock_name}" if stock_name else ""
            
            # 情況 A: 經理人有原始決策，但被修改了
            if code in raw_action_map:
                raw_act = raw_action_map[code]
                if final_act != raw_act:
                    overridden_items.append(f"{code}{name_display} ({raw_act} ➔ {final_act})")
            # 情況 B: 經理人漏掉了這檔，且系統最終自動判定了 SELL 或與預設 HOLD 不同的動作
            else:
                if final_act != "HOLD":
                    overridden_items.append(f"{code}{name_display} (無原始決策 ➔ {final_act})")
                    
        if overridden_items:
            override_note = (
                f"\n\n⚠️ **[風控護欄提示]**：今日有 {len(overridden_items)} 檔標的之最終決策因觸發系統量化安全/風控規則而進行覆寫調整："
                f"「{ '、'.join(overridden_items) }」。經理人原始橫向配置說明未包含此風控考量，請以最終個別標的執行決策為準。"
            )
            ranking_analysis += override_note
    except Exception as override_err:
        print(f" [決策代理] 警告: 產生風控覆寫提示失敗: {override_err}")

    return {
        "ranking_analysis": ranking_analysis,
        "decisions": final_decisions
    }
