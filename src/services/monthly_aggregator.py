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

def resolve_manual_review_month(target_month_str: Optional[str] = None) -> Tuple[int, int]:
    """
    推算月度檢討的標的年月：
    - 若使用者有傳入 target_month_str (如 "2026-07")，則解析並回傳。
    - 若未傳入：檢查今日是否已達到或超過本月官方週六分析日。
      - 若已達到/超過 ➔ 檢討本月 (curr_year, curr_month)。
      - 若尚未達到 ➔ 自動回溯檢討上個月 (prev_year, prev_month)。
    """
    now = get_local_taiwan_datetime()
    curr_year, curr_month = now.year, now.month

    if target_month_str and target_month_str.strip():
        parts = target_month_str.strip().split("-")
        if len(parts) == 2:
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                pass

    official_review_date = get_monthly_analysis_date(curr_year, curr_month)

    if now.date() >= official_review_date:
        return curr_year, curr_month
    else:
        # 回溯上個月
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
            scores_list = res.data or []
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

    # 9. 個案細節與 Map 階段個股數據切分
    per_stock_data: Dict[str, Dict[str, Any]] = {}
    for sc in all_stocks:
        sc_scores = [s for s in scores_list if s.get("stock_code") == sc]
        sc_orders = [o for o in all_orders if o.get("stock_code") == sc]
        sc_klines = klines_map.get(sc, [])
        sc_range = stock_price_ranges.get(sc, 0.0)

        per_stock_data[sc] = {
            "stock_code": sc,
            "scores": sc_scores,
            "orders": sc_orders,
            "klines": sc_klines,
            "price_range_ratio": round(sc_range, 4)
        }

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
        },
        "score_calibration": {
            "high_score_count": len(high_scores),
            "mid_score_count": len(mid_scores),
            "low_score_count": len(low_scores)
        },
        "daily_analyses": daily_analyses,
        "per_stock_data": per_stock_data
    }
