# Path: src/services/monthly_aggregator.py
import calendar
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

TAIWAN_TZ = pytz.timezone("Asia/Taipei")

DEFAULT_REVIEW_HOUR = 0    # 官方預設時間：週六凌晨 00:00 起 (週六全天皆可隨時分析)
DEFAULT_REVIEW_MINUTE = 0

def get_monthly_analysis_date(year: int, month: int) -> date:
    """
    計算指定年月的「週六分析日 (Saturday Review Day)」：
    - 一律在分析日（週六）執行。
    - 若當月最後一天為星期日 (Sunday)，則分析日為上個禮拜六 (last_day - 1天)。
    - 若當月最後一天為其餘 (星期一至星期六)，則分析日為該週的禮拜六。
    """
    _, last_day_num = calendar.monthrange(year, month)
    last_day = date(year, month, last_day_num)
    weekday = last_day.weekday()  # Monday is 0, Saturday is 5, Sunday is 6
    
    if weekday == 6:  # 星期日
        return last_day - timedelta(days=1)
    else:
        days_to_saturday = (5 - weekday) % 7
        return last_day + timedelta(days=days_to_saturday)

def get_monthly_analysis_datetime(year: int, month: int, hour: int = DEFAULT_REVIEW_HOUR, minute: int = DEFAULT_REVIEW_MINUTE) -> datetime:
    """
    計算指定年月的「週六分析日」官方定時執行時間點帶時區 (預設為週六凌晨 00:00 Asia/Taipei 起)。
    """
    d = get_monthly_analysis_date(year, month)
    return TAIWAN_TZ.localize(datetime.combine(d, time(hour, minute)))

def resolve_manual_review_month(target_month_str: Optional[str] = None) -> Tuple[int, int]:
    """
    推算月度檢討的標的年月：
    - 若使用者有傳入 target_month_str (如 "2026-07")，則解析並回傳。
    - 若未傳入：檢查當前台灣時間是否已達到或超過本月官方週六分析日 (週六 00:00 起)。
      - 若已達到/超過 ➔ 檢討本月 (curr_year, curr_month)。
      - 若尚未達到 ➔ 自動回溯檢討上個月 (prev_year, prev_month)。
    """
    now = get_local_taiwan_datetime()
    curr_year, curr_month = now.year, now.month

    if target_month_str and target_month_str.strip():
        parts = target_month_str.strip().split("-")
        if len(parts) >= 2:
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                pass

    official_review_dt = get_monthly_analysis_datetime(curr_year, curr_month)

    if now >= official_review_dt:
        return curr_year, curr_month
    else:
        # 尚未達到本月週六 20:00 ➔ 回溯檢討上個月
        if curr_month == 1:
            return curr_year - 1, 12
        else:
            return curr_year, curr_month - 1

def get_review_date_range(year: int, month: int) -> Tuple[date, date]:
    """
    計算指定月度檢討的無縫日期區間：[上個分析日, 本次分析日]
    """
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    prev_review_date = get_monthly_analysis_date(prev_year, prev_month)
    curr_review_date = get_monthly_analysis_date(year, month)
    return prev_review_date, curr_review_date

def calculate_mean_and_std(values: List[float]) -> Tuple[float, float]:
    """計算浮點數列表之平均值 (Mean) 與標準差 (Standard Deviation)"""
    clean_vals = [v for v in values if v is not None and not math.isnan(v) and not math.isinf(v)]
    if not clean_vals:
        return 0.0, 0.0
    n = len(clean_vals)
    mean_val = sum(clean_vals) / n
    if n < 2:
        return mean_val, 0.0
    variance = sum((x - mean_val) ** 2 for x in clean_vals) / (n - 1)
    std_val = math.sqrt(variance)
    return mean_val, std_val

def aggregate_daily_scores(scores_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    同日多筆打分紀錄聚合與去重：
    1. 同一股票在同一個交易日 (stock_code, analysis_date) 若有多筆分析紀錄：
       - 各項分數 (trend, momentum, volume, safety, regime) 取算術平均值 (round 到整數)。
       - 總分 total_score 為各項平均分數之總和。
    2. 策略決策 (decision / action)：
       - 若當日任一筆分析結果為 'BUY'，則當日聚合決策優先判定為 'BUY'。
       - 否則若當日任一筆分析結果為 'SELL'，則當日聚合決策優先判定為 'SELL'。
       - 若全部為 'HOLD' (或無動作)，則判定為 'HOLD'。
    """
    if not scores_list:
        return []

    from collections import defaultdict
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)

    for item in scores_list:
        sc = item.get("stock_code")
        an_date = str(item.get("analysis_date"))
        if sc and an_date:
            grouped[(sc, an_date)].append(item)

    aggregated: List[Dict[str, Any]] = []

    for (sc, an_date), items in grouped.items():
        n = len(items)
        
        # 1. 各項分數算術平均
        avg_trend = round(sum(int(x.get("trend_score", 0)) for x in items) / n)
        avg_momentum = round(sum(int(x.get("momentum_score", 0)) for x in items) / n)
        avg_volume = round(sum(int(x.get("volume_score", 0)) for x in items) / n)
        avg_safety = round(sum(int(x.get("safety_score", 0)) for x in items) / n)
        avg_regime = round(sum(int(x.get("regime_score", 0)) for x in items) / n)
        total_score = avg_trend + avg_momentum + avg_volume + avg_safety + avg_regime

        # 2. 策略決策優先級判定 (BUY > SELL > HOLD)
        decisions = [str(x.get("decision") or x.get("action") or x.get("strategy") or "HOLD").upper() for x in items]
        if "BUY" in decisions:
            merged_decision = "BUY"
        elif "SELL" in decisions:
            merged_decision = "SELL"
        else:
            merged_decision = "HOLD"

        aggregated.append({
            "stock_code": sc,
            "analysis_date": an_date,
            "trend_score": avg_trend,
            "momentum_score": avg_momentum,
            "volume_score": avg_volume,
            "safety_score": avg_safety,
            "regime_score": avg_regime,
            "total_score": total_score,
            "decision": merged_decision,
            "sample_count": n
        })

    # 按日期與股票代號排序
    aggregated.sort(key=lambda x: (x["analysis_date"], x["stock_code"]))
    return aggregated

def aggregate_monthly_data(year: int, month: int, is_paper: bool = False) -> Dict[str, Any]:
    """
    聚合當月實盤 (is_paper=False) 所有硬指標與歷史資料：
    1. 讀取 daily_analysis, trade_orders, stock_analysis_scores, stock_klines
    2. 算基本統計 (勝率, 實現總損益, Payoff Ratio, Profit Factor)
    3. 算每日 AI 分析期望值指標 (Upside & Drawdown 之 Mean 與 Std Dev)
    4. 算個股整月總振幅 (monthly_price_range_ratio)
    5. 算 AI 打分效能與偏斜分佈
    6. 切分個股 Map 數據 context
    """
    review_month_str = f"{year:04d}-{month:02d}"
    prev_date, curr_date = get_review_date_range(year, month)
    prev_date_str = prev_date.strftime("%Y-%m-%d")
    curr_date_str = curr_date.strftime("%Y-%m-%d")

    # 1. 從 Supabase 撈取每日分析紀錄 daily_analysis
    daily_analyses: List[Dict[str, Any]] = []
    daily_analysis_ids: List[int] = []
    try:
        res = supabase.table("daily_analysis") \
            .select("*") \
            .eq("is_paper", is_paper) \
            .gte("analysis_date", prev_date_str) \
            .lte("analysis_date", curr_date_str) \
            .order("analysis_date", desc=False) \
            .execute()
        daily_analyses = res.data or []
        daily_analysis_ids = [item["id"] for item in daily_analyses if "id" in item]
    except Exception as e:
        print(f" [Monthly Aggregator] 警告: 撈取 daily_analysis 失敗: {e}")

    # 2. 撈取股票打分紀錄 stock_analysis_scores
    scores_list: List[Dict[str, Any]] = []
    if daily_analysis_ids:
        try:
            res = supabase.table("stock_analysis_scores") \
                .select("*") \
                .in_("daily_analysis_id", daily_analysis_ids) \
                .eq("is_paper", is_paper) \
                .execute()
            raw_scores = res.data or []
            # 同日多筆分析聚合：各項分數算術平均，策略若有 BUY/SELL 則優先判定為 BUY/SELL
            scores_list = aggregate_daily_scores(raw_scores)
        except Exception as e:
            print(f" [Monthly Aggregator] 警告: 撈取 stock_analysis_scores 失敗: {e}")

    # 3. 撈取交易訂單 trade_orders
    all_orders: List[Dict[str, Any]] = []
    try:
        res = supabase.table("trade_orders") \
            .select("*") \
            .eq("is_paper", is_paper) \
            .gte("executed_at", f"{prev_date_str}T00:00:00") \
            .lte("executed_at", f"{curr_date_str}T23:59:59") \
            .order("executed_at", desc=False) \
            .execute()
        all_orders = res.data or []
    except Exception as e:
        print(f" [Monthly Aggregator] 警告: 撈取 trade_orders 失敗: {e}")

    # 4. 撈取相關 K 線歷史數據 stock_klines
    all_stocks = list(set([s["stock_code"] for s in scores_list] + [o["stock_code"] for o in all_orders]))
    klines_map: Dict[str, List[Dict[str, Any]]] = {}
    if all_stocks:
        try:
            res = supabase.table("stock_klines") \
                .select("*") \
                .in_("stock_code", all_stocks) \
                .gte("date", prev_date_str) \
                .lte("date", curr_date_str) \
                .order("date", desc=False) \
                .execute()
            klines_data = res.data or []
            for k in klines_data:
                sc = k["stock_code"]
                if sc not in klines_map:
                    klines_map[sc] = []
                klines_map[sc].append(k)
        except Exception as e:
            print(f" [Monthly Aggregator] 警告: 撈取 stock_klines 失敗: {e}")

    # 5. 硬指標計算：基礎交易統計 (平倉單 & realized_pnl)
    filled_sell_orders = [o for o in all_orders if o.get("action") == "SELL" and o.get("status") == "FILLED"]
    total_trades = len(filled_sell_orders)
    winning_trades = 0
    losing_trades = 0
    total_realized_pnl = 0.0
    win_pnls: List[float] = []
    loss_pnls: List[float] = []
    win_rois: List[float] = []
    loss_rois: List[float] = []

    for o in filled_sell_orders:
        pnl = float(o.get("realized_pnl") or 0.0)
        amt = float(o.get("total_amount") or 0.0)
        cost = amt - pnl if amt > pnl else amt
        roi = pnl / cost if cost > 0 else 0.0
        total_realized_pnl += pnl

        if pnl > 0:
            winning_trades += 1
            win_pnls.append(pnl)
            win_rois.append(roi)
        elif pnl < 0:
            losing_trades += 1
            loss_pnls.append(pnl)
            loss_rois.append(roi)

    win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0
    avg_win_pnl = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0
    avg_loss_pnl = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0
    avg_win_roi = (sum(win_rois) / len(win_rois) * 100.0) if win_rois else 0.0
    avg_loss_roi = (sum(loss_rois) / len(loss_rois) * 100.0) if loss_rois else 0.0
    
    payoff_ratio = (avg_win_pnl / abs(avg_loss_pnl)) if abs(avg_loss_pnl) > 0 else 0.0
    gross_profit = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    # 5b. 成交滑價 (Slippage) 統計計算 (以委託預估價與實際成交價之偏差率計算)
    filled_all_orders = [o for o in all_orders if o.get("status") == "FILLED"]
    slippage_ratios: List[float] = []
    for o in filled_all_orders:
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
            slippage_ratios.append(slip)
    
    mean_slippage_ratio, std_slippage_ratio = calculate_mean_and_std(slippage_ratios)

    # 5c. 統計未成交與被取消之委託單 (Cancelled / Rejected / Expired Orders)
    cancelled_orders = [
        o for o in all_orders 
        if str(o.get("status") or "").upper() in ("CANCELLED", "REJECTED", "EXPIRED", "UNFILLED")
    ]
    total_cancelled_orders = len(cancelled_orders)
    total_orders_count = len(all_orders)
    cancellation_rate = (total_cancelled_orders / total_orders_count * 100.0) if total_orders_count > 0 else 0.0

    # 6. 計算每日 AI 分析推薦之【期望值 (Expected Value)】 (Upside & Drawdown Mean & Std Dev)
    upside_ratios: List[float] = []
    drawdown_ratios: List[float] = []

    for score_item in scores_list:
        sc = score_item.get("stock_code")
        an_date = str(score_item.get("analysis_date"))
        stock_klines = klines_map.get(sc, [])
        if not stock_klines:
            continue

        after_klines = [k for k in stock_klines if str(k.get("date")) >= an_date]
        if not after_klines:
            continue

        base_kline = after_klines[0]
        hypothetical_buy_price = float(base_kline.get("close") or base_kline.get("open") or 0.0)
        if hypothetical_buy_price <= 0:
            continue

        max_price_after = max([float(k.get("high") or hypothetical_buy_price) for k in after_klines])
        min_price_after = min([float(k.get("low") or hypothetical_buy_price) for k in after_klines])

        up_ratio = (max_price_after - hypothetical_buy_price) / hypothetical_buy_price
        down_ratio = (min_price_after - hypothetical_buy_price) / hypothetical_buy_price

        upside_ratios.append(up_ratio)
        drawdown_ratios.append(down_ratio)

    mean_upside_ratio, std_upside_ratio = calculate_mean_and_std(upside_ratios)
    mean_drawdown_ratio, std_drawdown_ratio = calculate_mean_and_std(drawdown_ratios)

    # 7. 計算各個股整月總振幅 monthly_price_range_ratio
    stock_price_ranges: Dict[str, float] = {}
    for sc, klines in klines_map.items():
        if not klines:
            continue
        m_high = max([float(k.get("high") or 0.0) for k in klines])
        m_low = min([float(k.get("low") or 999999.0) for k in klines])
        if m_low > 0 and m_high >= m_low:
            stock_price_ranges[sc] = (m_high - m_low) / m_low

    # 8. 打分效能與分佈 (Score Calibration Breakdown)
    high_scores = [s for s in scores_list if (s.get("trend_score", 0) + s.get("momentum_score", 0) + s.get("volume_score", 0) + s.get("safety_score", 0) + s.get("regime_score", 0)) >= 80]
    mid_scores = [s for s in scores_list if 60 <= (s.get("trend_score", 0) + s.get("momentum_score", 0) + s.get("volume_score", 0) + s.get("safety_score", 0) + s.get("regime_score", 0)) < 80]
    low_scores = [s for s in scores_list if (s.get("trend_score", 0) + s.get("momentum_score", 0) + s.get("volume_score", 0) + s.get("safety_score", 0) + s.get("regime_score", 0)) < 60]

    # 8b. 大盤氣候與保守防禦錯失機會診斷 (Defensive Regime & Missed Opportunity Metrics)
    defensive_scores = [s for s in scores_list if s.get("regime_score", 15) < 12]
    defensive_days_count = len(set(str(s.get("analysis_date")) for s in defensive_scores))
    
    # 防禦天數內的潛在漲幅 (Upside) 與買單統計
    defensive_upsides: List[float] = []
    defensive_buy_count = 0
    for s in defensive_scores:
        sc = s.get("stock_code")
        an_date = str(s.get("analysis_date"))
        stock_klines = klines_map.get(sc, [])
        if stock_klines:
            after_klines = [k for k in stock_klines if str(k.get("date")) >= an_date]
            if after_klines:
                base_k = after_klines[0]
                base_p = float(base_k.get("close") or base_k.get("open") or 0.0)
                if base_p > 0:
                    max_p = max([float(k.get("high") or base_p) for k in after_klines])
                    defensive_upsides.append((max_p - base_p) / base_p)

    for o in all_orders:
        if str(o.get("action") or "").upper() == "BUY" and o.get("status") == "FILLED":
            exec_dt = str(o.get("executed_at") or "")
            exec_date = exec_dt.split("T")[0] if "T" in exec_dt else exec_dt.split(" ")[0]
    defensive_mean_upside, _ = calculate_mean_and_std(defensive_upsides)

    # 8c. 大盤多頭氣候與大盤好卻買入虧損診斷 (Bullish Regime Trap Metrics)
    bullish_scores = [s for s in scores_list if s.get("regime_score", 15) >= 15]
    bullish_days_count = len(set(str(s.get("analysis_date")) for s in bullish_scores))
    bullish_buy_count = 0
    bullish_losing_trade_count = 0

    for o in all_orders:
        if str(o.get("action") or "").upper() == "BUY" and o.get("status") == "FILLED":
            exec_dt = str(o.get("executed_at") or "")
            exec_date = exec_dt.split("T")[0] if "T" in exec_dt else exec_dt.split(" ")[0]
            if any(str(bs.get("analysis_date")) == exec_date for bs in bullish_scores):
                bullish_buy_count += 1
                # 檢查該筆交易最終是否虧損 (realized_pnl < 0)
                pnl = float(o.get("realized_pnl") or 0.0)
                if pnl < 0:
                    bullish_losing_trade_count += 1

    bullish_loss_rate = (bullish_losing_trade_count / bullish_buy_count * 100.0) if bullish_buy_count > 0 else 0.0

    # 9. 個案細節與 Map 階段個股數據切分 (含進場 Timing / 追高 / 太晚入場診斷指標)
    per_stock_data: Dict[str, Dict[str, Any]] = {}
    all_portfolio_chasing_high_count = 0
    all_portfolio_late_entry_count = 0
    all_portfolio_entry_percentiles: List[float] = []

    for sc in all_stocks:
        sc_scores = [s for s in scores_list if s.get("stock_code") == sc]
        sc_orders = [o for o in all_orders if o.get("stock_code") == sc]
        sc_filled_orders = [o for o in sc_orders if o.get("status") == "FILLED"]
        sc_cancelled_orders = [o for o in sc_orders if str(o.get("status") or "").upper() in ("CANCELLED", "REJECTED", "EXPIRED", "UNFILLED")]
        sc_klines = klines_map.get(sc, [])
        sc_range = stock_price_ranges.get(sc, 0.0)

        # 計算進場時機指標 (Timing & Anti-Chasing metrics)
        sc_filled_buys = [o for o in sc_filled_orders if str(o.get("action") or "").upper() == "BUY"]
        sc_chasing_high_count = 0
        sc_late_entry_count = 0
        sc_entry_percentiles: List[float] = []

        m_high = max([float(k.get("high") or 0.0) for k in sc_klines]) if sc_klines else 0.0
        m_low = min([float(k.get("low") or 999999.0) for k in sc_klines]) if sc_klines else 0.0

        for buy_o in sc_filled_buys:
            entry_p = float(buy_o.get("execution_price") or buy_o.get("price") or 0.0)
            exec_dt = str(buy_o.get("executed_at") or buy_o.get("created_at") or "")
            exec_date_str = exec_dt.split("T")[0] if "T" in exec_dt else exec_dt.split(" ")[0]

            if entry_p > 0 and m_high > m_low:
                percentile = (entry_p - m_low) / (m_high - m_low)
                percentile = max(0.0, min(1.0, percentile))
            else:
                percentile = 0.5
            
            sc_entry_percentiles.append(percentile)
            all_portfolio_entry_percentiles.append(percentile)

            # 後續 K 線評估 post-entry drawdown vs upside
            after_klines = [k for k in sc_klines if str(k.get("date")) >= exec_date_str]
            if after_klines and entry_p > 0:
                max_p_after = max([float(k.get("high") or entry_p) for k in after_klines])
                min_p_after = min([float(k.get("low") or entry_p) for k in after_klines])
                post_upside = (max_p_after - entry_p) / entry_p
                post_drawdown = (min_p_after - entry_p) / entry_p
            else:
                post_upside = 0.0
                post_drawdown = 0.0

            # 判定追高與太晚入場
            # 追高：買在該月高低點區間 top 20% (Percentile >= 0.80)
            if percentile >= 0.80:
                sc_chasing_high_count += 1
                all_portfolio_chasing_high_count += 1

            # 太晚入場：買進後拉回 > 5% 且短期極致上揚 < 3% (波段頂點買入)
            if post_drawdown < -0.05 and post_upside < 0.03:
                sc_late_entry_count += 1
                all_portfolio_late_entry_count += 1

        avg_sc_percentile = (sum(sc_entry_percentiles) / len(sc_entry_percentiles)) if sc_entry_percentiles else 0.5

        per_stock_data[sc] = {
            "stock_code": sc,
            "scores": sc_scores,
            "orders": sc_orders,
            "filled_orders": sc_filled_orders,
            "cancelled_orders": sc_cancelled_orders,
            "klines": sc_klines,
            "price_range_ratio": round(sc_range, 4),
            "entry_timing_summary": {
                "filled_buy_count": len(sc_filled_buys),
                "avg_entry_percentile": round(avg_sc_percentile * 100.0, 1),
                "chasing_high_count": sc_chasing_high_count,
                "late_entry_count": sc_late_entry_count
            }
        }

    avg_portfolio_percentile = (sum(all_portfolio_entry_percentiles) / len(all_portfolio_entry_percentiles)) if all_portfolio_entry_percentiles else 0.5

    return {
        "review_month": review_month_str,
        "date_range": {
            "start_date": prev_date_str,
            "end_date": curr_date_str
        },
        "is_paper": is_paper,
        "daily_analysis_ids": daily_analysis_ids,
        "metrics": {
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": round(win_rate, 2),
            "total_realized_pnl": round(total_realized_pnl, 2),
            "avg_win_roi_pct": round(avg_win_roi, 2),
            "avg_loss_roi_pct": round(avg_loss_roi, 2),
            "payoff_ratio": round(payoff_ratio, 2),
            "profit_factor": round(profit_factor, 2),
            "mean_upside_ratio": round(mean_upside_ratio, 4),
            "std_upside_ratio": round(std_upside_ratio, 4),
            "mean_drawdown_ratio": round(mean_drawdown_ratio, 4),
            "std_drawdown_ratio": round(std_drawdown_ratio, 4),
            "mean_slippage_ratio": round(mean_slippage_ratio, 4),
            "std_slippage_ratio": round(std_slippage_ratio, 4),
            "total_cancelled_orders": total_cancelled_orders,
            "cancellation_rate_pct": round(cancellation_rate, 2),
            "total_chasing_high_trades": all_portfolio_chasing_high_count,
            "total_late_entry_trades": all_portfolio_late_entry_count,
            "avg_portfolio_entry_percentile": round(avg_portfolio_percentile * 100.0, 1),
            "defensive_days_count": defensive_days_count,
            "defensive_period_mean_upside_pct": round(defensive_mean_upside * 100.0, 2),
            "defensive_period_buy_count": defensive_buy_count,
            "bullish_days_count": bullish_days_count,
            "bullish_period_buy_count": bullish_buy_count,
            "bullish_period_losing_trades": bullish_losing_trade_count,
            "bullish_loss_rate_pct": round(bullish_loss_rate, 2),
        },
        "score_calibration": {
            "high_score_count": len(high_scores),
            "mid_score_count": len(mid_scores),
            "low_score_count": len(low_scores)
        },
        "daily_analyses": daily_analyses,
        "per_stock_data": per_stock_data
    }
