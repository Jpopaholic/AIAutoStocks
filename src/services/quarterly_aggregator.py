# Path: src/services/quarterly_aggregator.py
import math
from datetime import date, datetime, time, timedelta
from typing import List, Dict, Any, Tuple, Optional
import pytz

from src.time_manager import get_local_taiwan_datetime
from src.services.supabase_client import (
    supabase,
    get_orders,
    get_holdings
)
from src.services.technical_indicators import calculate_atr
from src.services.monthly_aggregator import (
    get_monthly_analysis_date,
    get_monthly_analysis_datetime,
    calculate_mean_and_std,
    aggregate_daily_scores
)

TAIWAN_TZ = pytz.timezone("Asia/Taipei")

def get_quarter_months(quarter: int) -> List[int]:
    """回傳指定季度 (1..4) 對應的 3 個月份數字列表"""
    if quarter == 1:
        return [1, 2, 3]
    elif quarter == 2:
        return [4, 5, 6]
    elif quarter == 3:
        return [7, 8, 9]
    elif quarter == 4:
        return [10, 11, 12]
    else:
        raise ValueError(f"無效的季度: {quarter}，必須為 1, 2, 3 或 4")

def get_quarterly_review_date_range(year: int, quarter: int) -> Tuple[date, date, List[str]]:
    """
    計算指定季度檢討的無縫日期區間（以週六復盤日為基準，3 格跨度）：
    - Q1: 前一年 12 月週六分析日 ~ 本年 3 月週六分析日 (涵蓋 1, 2, 3 月)
    - Q2: 本年 3 月週六分析日 ~ 本年 6 月週六分析日 (涵蓋 4, 5, 6 月)
    - Q3: 本年 6 月週六分析日 ~ 本年 9 月週六分析日 (涵蓋 7, 8, 9 月)
    - Q4: 本年 9 月週六分析日 ~ 本年 12 月週六分析日 (涵蓋 10, 11, 12 月)
    回傳: (start_date, end_date, months_included_str_list)
    """
    months = get_quarter_months(quarter)
    months_str_list = [f"{year}-{m:02d}" for m in months]

    if quarter == 1:
        start_date = get_monthly_analysis_date(year - 1, 12)
        end_date = get_monthly_analysis_date(year, 3)
    elif quarter == 2:
        start_date = get_monthly_analysis_date(year, 3)
        end_date = get_monthly_analysis_date(year, 6)
    elif quarter == 3:
        start_date = get_monthly_analysis_date(year, 6)
        end_date = get_monthly_analysis_date(year, 9)
    else:  # quarter == 4
        start_date = get_monthly_analysis_date(year, 9)
        end_date = get_monthly_analysis_date(year, 12)

    return start_date, end_date, months_str_list

def resolve_manual_review_quarter(target_quarter_str: Optional[str] = None) -> Tuple[int, int]:
    """
    推算季度檢討的標的年份與季度 (year, quarter)：
    - 若傳入 target_quarter_str (如 '2026-Q3', '2026-3', '2026Q3')，解析回傳。
    - 若未傳入：根據當前台灣時間判斷最新已結束之季度：
      - 1, 2 月 ➔ 前一年 Q4
      - 3 月 ➔ 若已達到/超過 3 月週六復盤日 ➔ 本年 Q1；否則 ➔ 前一年 Q4
      - 4, 5 月 ➔ 本年 Q1
      - 6 月 ➔ 若已達到/超過 6 月週六復盤日 ➔ 本年 Q2；否則 ➔ 本年 Q1
      - 7, 8 月 ➔ 本年 Q2
      - 9 月 ➔ 若已達到/超過 9 月週六復盤日 ➔ 本年 Q3；否則 ➔ 本年 Q2
      - 10, 11 月 ➔ 本年 Q3
      - 12 月 ➔ 若已達到/超過 12 月週六復盤日 ➔ 本年 Q4；否則 ➔ 本年 Q3
    """
    now = get_local_taiwan_datetime()
    curr_year, curr_month = now.year, now.month

    if target_quarter_str and target_quarter_str.strip():
        t_clean = target_quarter_str.strip().upper().replace(" ", "")
        if "Q" in t_clean:
            parts = t_clean.split("Q")
            try:
                y = int(parts[0].replace("-", ""))
                q = int(parts[1])
                if 1 <= q <= 4:
                    return y, q
            except Exception:
                pass
        elif "-" in t_clean:
            parts = t_clean.split("-")
            try:
                y = int(parts[0])
                q = int(parts[1])
                if 1 <= q <= 4:
                    return y, q
            except Exception:
                pass

    if curr_month in (1, 2):
        return curr_year - 1, 4
    elif curr_month == 3:
        review_dt = get_monthly_analysis_datetime(curr_year, 3, hour=9, minute=0)
        return (curr_year, 1) if now >= review_dt else (curr_year - 1, 4)
    elif curr_month in (4, 5):
        return curr_year, 1
    elif curr_month == 6:
        review_dt = get_monthly_analysis_datetime(curr_year, 6, hour=9, minute=0)
        return (curr_year, 2) if now >= review_dt else (curr_year, 1)
    elif curr_month in (7, 8):
        return curr_year, 2
    elif curr_month == 9:
        review_dt = get_monthly_analysis_datetime(curr_year, 9, hour=9, minute=0)
        return (curr_year, 3) if now >= review_dt else (curr_year, 2)
    elif curr_month in (10, 11):
        return curr_year, 3
    else:  # curr_month == 12
        review_dt = get_monthly_analysis_datetime(curr_year, 12, hour=9, minute=0)
        return (curr_year, 4) if now >= review_dt else (curr_year, 3)

def check_quarterly_review_threshold(months_included: List[str], is_paper: bool = False) -> Tuple[bool, List[Dict[str, Any]], List[str]]:
    """
    季度復盤門檻檢查：
    對應的該季度 3 個月份中，每個月都必須至少有一筆有效月度檢討紀錄 (在 monthly_skills 中)，缺一不可！
    回傳: (threshold_met, monthly_skills_records, missing_months)
    """
    monthly_skills_records: List[Dict[str, Any]] = []
    missing_months: List[str] = []

    try:
        res = supabase.table("monthly_skills") \
            .select("review_month, skills, daily_analysis_count, created_at") \
            .eq("is_paper", is_paper) \
            .in_("review_month", months_included) \
            .order("review_month", desc=False) \
            .execute()
        records = res.data or []
        
        found_months = set()
        for r in records:
            rm = r.get("review_month")
            if rm in months_included and rm not in found_months:
                found_months.add(rm)
                monthly_skills_records.append(r)

        for m in months_included:
            if m not in found_months:
                missing_months.append(m)

    except Exception as e:
        print(f" [Quarterly Aggregator] 查詢 monthly_skills 門檻失敗: {e}")
        missing_months = list(months_included)

    threshold_met = (len(missing_months) == 0 and len(monthly_skills_records) == 3)
    return threshold_met, monthly_skills_records, missing_months

def aggregate_quarterly_data(year: int, quarter: int, is_paper: bool = False) -> Dict[str, Any]:
    """
    聚合指定季度的全域回測與交易數據：
    1. 驗證 3 個月份門檻規範（缺一不可）
    2. 撈取全季度日分析、訂單與 K 線
    3. 計算全季硬指標與個股指標
    4. 整理該季 3 個月份的月度技能演化軌跡
    """
    review_quarter_str = f"{year}-Q{quarter}"
    start_date, end_date, months_included = get_quarterly_review_date_range(year, quarter)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    threshold_met, monthly_skills_records, missing_months = check_quarterly_review_threshold(months_included, is_paper=is_paper)

    if not threshold_met:
        return {
            "review_quarter": review_quarter_str,
            "quarter": quarter,
            "year": year,
            "months_included": months_included,
            "threshold_met": False,
            "missing_months": missing_months,
            "date_range": {"start_date": start_str, "end_date": end_str},
            "monthly_skills_trajectory": monthly_skills_records,
            "daily_analysis_ids": [],
            "daily_analysis_count": 0,
            "per_stock_data": {},
            "metrics": {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_realized_pnl": 0.0,
                "payoff_ratio": 0.0,
                "profit_factor": 0.0,
                "mean_upside_ratio": 0.0,
                "std_upside_ratio": 0.0,
                "mean_drawdown_ratio": 0.0,
                "std_drawdown_ratio": 0.0,
                "bullish_days_count": 0,
                "defensive_days_count": 0,
                "cancellation_rate_pct": 0.0,
                "mean_slippage_ratio": 0.0
            },
            "score_calibration": {
                "high_score_count": 0,
                "mid_score_count": 0,
                "low_score_count": 0
            }
        }

    # 門檻滿足，進行全季度資料庫撈取
    # 1. 撈取全季度 daily_analysis
    raw_analysis_res = supabase.table("daily_analysis") \
        .select("id, stock_code, analysis_date, trend_score, momentum_score, volume_score, safety_score, regime_score, total_score, decision, action, created_at") \
        .gte("analysis_date", start_str) \
        .lte("analysis_date", end_str) \
        .order("analysis_date", desc=False) \
        .execute()
    raw_scores_list = raw_analysis_res.data or []
    scores_list = aggregate_daily_scores(raw_scores_list)

    daily_analysis_ids = list(set([s.get("id") for s in raw_scores_list if s.get("id")]))
    daily_analysis_count = len(set([str(s.get("analysis_date")) for s in scores_list]))

    # 2. 撈取全季度訂單 (orders)
    local_tz = TAIWAN_TZ
    start_local = local_tz.localize(datetime.combine(start_date, time(0, 0, 0)))
    end_local = local_tz.localize(datetime.combine(end_date, time(23, 59, 59, 999999)))
    start_utc = start_local.astimezone(pytz.utc).isoformat().replace("+00:00", "Z")
    end_utc = end_local.astimezone(pytz.utc).isoformat().replace("+00:00", "Z")

    all_orders = get_orders(start_date=start_utc, end_date=end_utc, is_paper=is_paper)

    filled_orders = [o for o in all_orders if o.get("status") == "FILLED"]
    cancelled_orders = [o for o in all_orders if o.get("status") in ("CANCELLED", "REJECTED")]
    total_cancelled_orders = len(cancelled_orders)
    total_submitted_orders = len(all_orders)
    cancellation_rate_pct = round((total_cancelled_orders / total_submitted_orders * 100.0), 2) if total_submitted_orders > 0 else 0.0

    # 3. 獲取標的清單
    active_stocks = sorted(list(set(
        [s["stock_code"] for s in scores_list if s.get("stock_code")] +
        [o["stock_code"] for o in all_orders if o.get("stock_code")]
    )))

    # 4. 抓取各標的全季度歷史日 K 線
    from src.services.sinopac_client import sinopac_client
    klines_map: Dict[str, List[Dict[str, Any]]] = {}
    for sc in active_stocks:
        try:
            klines = sinopac_client.get_daily_klines(sc, start_date=start_str, end_date=end_str)
            klines_map[sc] = klines or []
        except Exception as e:
            print(f" [Quarterly Aggregator] 抓取個股 {sc} K線失敗: {e}")
            klines_map[sc] = []

    # 5. 計算全季度平倉交易硬指標 (Realized PnL, Win Rate, Payoff, Profit Factor)
    closed_trades = [o for o in filled_orders if str(o.get("action") or "").upper() == "SELL" and o.get("realized_pnl") is not None]
    total_trades = len(closed_trades)
    winning_trades = [o for o in closed_trades if float(o.get("realized_pnl") or 0.0) > 0]
    losing_trades = [o for o in closed_trades if float(o.get("realized_pnl") or 0.0) < 0]

    win_rate = round(len(winning_trades) / total_trades * 100, 2) if total_trades > 0 else 0.0
    total_realized_pnl = sum([float(o.get("realized_pnl") or 0.0) for o in closed_trades])

    avg_win = (sum([float(o.get("realized_pnl") or 0.0) for o in winning_trades]) / len(winning_trades)) if winning_trades else 0.0
    avg_loss = (abs(sum([float(o.get("realized_pnl") or 0.0) for o in losing_trades])) / len(losing_trades)) if losing_trades else 0.0
    payoff_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else (999.0 if avg_win > 0 else 0.0)

    total_gross_loss = abs(sum([float(o.get("realized_pnl") or 0.0) for o in losing_trades]))
    total_gross_win = sum([float(o.get("realized_pnl") or 0.0) for o in winning_trades])
    profit_factor = round(total_gross_win / total_gross_loss, 2) if total_gross_loss > 0 else (999.0 if total_gross_win > 0 else 0.0)

    # 成交滑價計算
    slippages: List[float] = []
    for o in filled_orders:
        target_p = float(o.get("price") or 0.0)
        exec_p = float(o.get("execution_price") or target_p)
        action = str(o.get("action") or "").upper()
        if target_p > 0 and exec_p > 0:
            if action == "BUY":
                slip = (exec_p - target_p) / target_p
            elif action == "SELL":
                slip = (target_p - exec_p) / target_p
            else:
                slip = 0.0
            slippages.append(slip)
    mean_slippage_ratio = round(sum(slippages) / len(slippages), 5) if slippages else 0.0

    # 6. 計算期望潛在獲利與最大回撤
    upside_ratios: List[float] = []
    drawdown_ratios: List[float] = []
    for s in scores_list:
        sc = s.get("stock_code")
        an_date = str(s.get("analysis_date"))
        stock_klines = klines_map.get(sc, [])
        if not stock_klines:
            continue
        after_klines = [k for k in stock_klines if str(k.get("date")) >= an_date]
        if not after_klines:
            continue
        first_k = after_klines[0]
        hypothetical_buy_price = float(first_k.get("close") or first_k.get("open") or 0.0)
        if hypothetical_buy_price <= 0:
            continue
        max_price_after = max([float(k.get("high") or hypothetical_buy_price) for k in after_klines])
        min_price_after = min([float(k.get("low") or hypothetical_buy_price) for k in after_klines])
        upside_ratios.append((max_price_after - hypothetical_buy_price) / hypothetical_buy_price)
        drawdown_ratios.append((min_price_after - hypothetical_buy_price) / hypothetical_buy_price)

    mean_upside_ratio, std_upside_ratio = calculate_mean_and_std(upside_ratios)
    mean_drawdown_ratio, std_drawdown_ratio = calculate_mean_and_std(drawdown_ratios)

    # 7. 個股總振幅與 ATR 波動度
    stock_price_ranges: Dict[str, float] = {}
    stock_atr_metrics: Dict[str, Dict[str, Any]] = {}
    portfolio_atrs: List[float] = []

    for sc, klines in klines_map.items():
        if not klines:
            continue
        m_high = max([float(k.get("high") or 0.0) for k in klines])
        m_low = min([float(k.get("low") or 999999.0) for k in klines])
        if m_low > 0 and m_high >= m_low:
            stock_price_ranges[sc] = (m_high - m_low) / m_low

        highs = [float(k.get("high") or 0.0) for k in klines]
        lows = [float(k.get("low") or 0.0) for k in klines]
        closes = [float(k.get("close") or 0.0) for k in klines]

        atr14_val = 0.0
        atr_pct_val = 0.0
        mean_atr_pct = 0.0
        tier = "NORMAL"

        if len(closes) >= 2:
            period = 14 if len(closes) >= 14 else len(closes)
            atr_series = calculate_atr(highs, lows, closes, period)
            valid_atrs = [a for a in atr_series if a is not None]
            if not valid_atrs:
                trs = [highs[0] - lows[0]] + [
                    max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
                    for i in range(1, len(closes))
                ]
                valid_atrs = [sum(trs) / len(trs)] if trs else []

            if valid_atrs:
                atr14_val = valid_atrs[-1]
                latest_close = closes[-1] if closes[-1] > 0 else 1.0
                atr_pct_val = (atr14_val / latest_close) * 100.0
                sub_closes = closes[-len(valid_atrs):]
                mean_close = sum(sub_closes) / len(sub_closes) if sub_closes else latest_close
                mean_atr_pct = (sum(valid_atrs) / len(valid_atrs) / mean_close * 100.0) if mean_close > 0 else atr_pct_val

                if atr_pct_val >= 3.5:
                    tier = "HIGH"
                elif atr_pct_val < 2.0:
                    tier = "LOW"
                else:
                    tier = "NORMAL"
                portfolio_atrs.append(atr_pct_val)

        stock_atr_metrics[sc] = {
            "atr14": round(atr14_val, 2),
            "atr_pct": round(atr_pct_val, 2),
            "mean_atr_pct": round(mean_atr_pct, 2),
            "volatility_tier": tier
        }

    # 8. 打分效能與大盤氣候
    high_scores = [s for s in scores_list if s.get("total_score", 0) >= 80]
    mid_scores = [s for s in scores_list if 60 <= s.get("total_score", 0) < 80]
    low_scores = [s for s in scores_list if s.get("total_score", 0) < 60]

    defensive_scores = [s for s in scores_list if s.get("regime_score", 15) < 12]
    defensive_days_count = len(set(str(s.get("analysis_date")) for s in defensive_scores))

    bullish_scores = [s for s in scores_list if s.get("regime_score", 15) >= 15]
    bullish_days_count = len(set(str(s.get("analysis_date")) for s in bullish_scores))

    # 9. 彙整個股細項
    per_stock_data: Dict[str, Any] = {}
    portfolio_entry_percentiles: List[float] = []
    total_chasing_high_trades = 0
    total_late_entry_trades = 0

    for sc in active_stocks:
        s_scores = [s for s in scores_list if s.get("stock_code") == sc]
        s_filled = [o for o in filled_orders if o.get("stock_code") == sc]
        s_cancelled = [o for o in cancelled_orders if o.get("stock_code") == sc]

        # Timing 分析
        timing_records: List[Dict[str, Any]] = []
        sc_klines = klines_map.get(sc, [])
        m_high = max([float(k.get("high") or 0.0) for k in sc_klines]) if sc_klines else 0.0
        m_low = min([float(k.get("low") or 999999.0) for k in sc_klines]) if sc_klines else 0.0

        for o in s_filled:
            if str(o.get("action") or "").upper() == "BUY":
                exec_p = float(o.get("execution_price") or o.get("price") or 0.0)
                exec_dt = str(o.get("executed_at") or o.get("created_at") or "")
                exec_date = exec_dt.split("T")[0] if "T" in exec_dt else exec_dt.split(" ")[0]
                if exec_p > 0 and m_high > m_low:
                    percentile = (exec_p - m_low) / (m_high - m_low)
                    percentile = max(0.0, min(1.0, percentile))
                else:
                    percentile = 0.5

                after_klines = [k for k in sc_klines if str(k.get("date")) >= exec_date]
                if after_klines and exec_p > 0:
                    max_p_after = max([float(k.get("high") or exec_p) for k in after_klines])
                    min_p_after = min([float(k.get("low") or exec_p) for k in after_klines])
                    post_upside = (max_p_after - exec_p) / exec_p
                    post_drawdown = (min_p_after - exec_p) / exec_p
                else:
                    post_upside = 0.0
                    post_drawdown = 0.0

                is_chasing = (percentile >= 0.80)
                is_late = (post_drawdown < -0.05 and post_upside < 0.03)

                timing_records.append({
                    "entry_percentile": round(percentile * 100.0, 1),
                    "is_chasing_high": is_chasing,
                    "is_late_entry": is_late
                })
                portfolio_entry_percentiles.append(percentile * 100.0)
                if is_chasing:
                    total_chasing_high_trades += 1
                if is_late:
                    total_late_entry_trades += 1

        avg_entry_percentile = round(sum([t["entry_percentile"] for t in timing_records]) / len(timing_records), 1) if timing_records else 50.0

        # 個股潛在獲利與回撤
        s_ups: List[float] = []
        s_downs: List[float] = []
        for s in s_scores:
            an_date = str(s.get("analysis_date"))
            stock_klines = klines_map.get(sc, [])
            after_klines = [k for k in stock_klines if str(k.get("date")) >= an_date]
            if after_klines:
                base_p = float(after_klines[0].get("close") or after_klines[0].get("open") or 0.0)
                if base_p > 0:
                    max_p = max([float(k.get("high") or base_p) for k in after_klines])
                    min_p = min([float(k.get("low") or base_p) for k in after_klines])
                    s_ups.append((max_p - base_p) / base_p)
                    s_downs.append((min_p - base_p) / base_p)

        mean_s_up, std_s_up = calculate_mean_and_std(s_ups)
        mean_s_down, std_s_down = calculate_mean_and_std(s_downs)

        # 實際平倉損益
        s_closed = [o for o in s_filled if str(o.get("action") or "").upper() == "SELL" and o.get("realized_pnl") is not None]
        s_realized_pnl = sum([float(o.get("realized_pnl") or 0.0) for o in s_closed])

        atr_info = stock_atr_metrics.get(sc, {})

        per_stock_data[sc] = {
            "stock_code": sc,
            "scores": s_scores,
            "filled_orders": s_filled,
            "cancelled_orders": s_cancelled,
            "price_range_ratio": stock_price_ranges.get(sc, 0.0),
            "atr14": atr_info.get("atr14", 0.0),
            "atr_pct": atr_info.get("atr_pct", 0.0),
            "volatility_tier": atr_info.get("volatility_tier", "NORMAL"),
            "expected_upside_str": f"+{mean_s_up*100:.2f}% (±{std_s_up*100:.2f}%)" if s_ups else "--",
            "expected_drawdown_str": f"{mean_s_down*100:.2f}% (±{std_s_down*100:.2f}%)" if s_downs else "--",
            "actual_pnl_str": f"{s_realized_pnl:+.0f}元 (平倉 {len(s_closed)} 筆)" if s_closed else "無平倉交易",
            "entry_timing_summary": {
                "avg_entry_percentile": avg_entry_percentile,
                "chasing_high_count": sum([1 for t in timing_records if t["is_chasing_high"]]),
                "late_entry_count": sum([1 for t in timing_records if t["is_late_entry"]])
            }
        }

    return {
        "review_quarter": review_quarter_str,
        "quarter": quarter,
        "year": year,
        "months_included": months_included,
        "threshold_met": True,
        "missing_months": [],
        "date_range": {"start_date": start_str, "end_date": end_str},
        "monthly_skills_trajectory": monthly_skills_records,
        "daily_analysis_ids": daily_analysis_ids,
        "daily_analysis_count": daily_analysis_count,
        "per_stock_data": per_stock_data,
        "metrics": {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "total_realized_pnl": total_realized_pnl,
            "payoff_ratio": payoff_ratio,
            "profit_factor": profit_factor,
            "mean_upside_ratio": mean_upside_ratio,
            "std_upside_ratio": std_upside_ratio,
            "mean_drawdown_ratio": mean_drawdown_ratio,
            "std_drawdown_ratio": std_drawdown_ratio,
            "bullish_days_count": bullish_days_count,
            "defensive_days_count": defensive_days_count,
            "cancellation_rate_pct": cancellation_rate_pct,
            "mean_slippage_ratio": mean_slippage_ratio,
            "total_cancelled_orders": total_cancelled_orders,
            "avg_portfolio_entry_percentile": round(sum(portfolio_entry_percentiles) / len(portfolio_entry_percentiles), 1) if portfolio_entry_percentiles else 50.0,
            "total_chasing_high_trades": total_chasing_high_trades,
            "total_late_entry_trades": total_late_entry_trades,
            "portfolio_mean_atr_pct": round(sum(portfolio_atrs) / len(portfolio_atrs), 2) if portfolio_atrs else 0.0
        },
        "score_calibration": {
            "high_score_count": len(high_scores),
            "mid_score_count": len(mid_scores),
            "low_score_count": len(low_scores)
        }
    }
