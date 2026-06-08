# Path: src/services/nav_calculator.py
from typing import Tuple, Dict, Any
from src.config import config
from src.services.supabase_client import get_holdings, get_orders, log_system_event
from src.services import sandbox_simulator

def calculate_nav() -> Tuple[float, float, float]:
    """
    計算目前投資組合的可用現金、持股總市值與資產淨值 (NAV)。
    回傳: (cash_balance, holdings_value, net_asset_value)
    """
    # 1. 取得持股明細，並動態查詢現價以估算市值
    try:
        holdings = get_holdings()
    except Exception as e:
        log_system_event("WARN", f"[NAV計算器] 無法取得持股明細，市值估計為 0: {str(e)}")
        holdings = []

    holdings_value = 0.0
    for h in holdings:
        stock_code = h["stock_code"]
        qty = float(h["quantity"])
        
        # 依據目前是沙盒還是真實模式，動態讀取即時報價/模擬報價
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        current_price = float(quote.get("price") or h["average_price"])
        
        market_value = qty * current_price
        holdings_value += market_value

    # 2. 計算剩餘現金：
    # 買入時 Cash 扣除 total_amount (含手續費)；賣出時 Cash 增加 total_amount (扣除手續費/稅金)
    initial_cash = config.limits.initial_cash
    try:
        all_orders = get_orders()
        cash_balance = initial_cash
        for o in all_orders:
            amt = float(o["total_amount"])
            if o["action"] == "BUY":
                cash_balance -= amt
            elif o["action"] == "SELL":
                cash_balance += amt
    except Exception as e:
        log_system_event("WARN", f"[NAV計算器] 無法取得歷史訂單，使用初始本金 {initial_cash:,.0f} 元作為現金餘額: {str(e)}")
        cash_balance = initial_cash

    net_asset_value = cash_balance + holdings_value
    return cash_balance, holdings_value, net_asset_value

def get_dynamic_limits() -> Tuple[float, float]:
    """
    依據比例與當前 NAV 動態計算單筆交易上限及每日累計上限。
    如果未設定比例或比例小於等於 0，則退回到 config 的絕對數值上限。
    
    比例規則：
    - 若 pct > 1.0，視為百分比 (例如 5.0 代表 5%，轉換為 0.05)
    - 若 pct <= 1.0 且 > 0，視為比例 (例如 0.05 代表 5%，直接使用)
    
    回傳: (single_limit, daily_limit)
    """
    single_pct = config.limits.single_stock_pct
    daily_pct = config.limits.daily_total_pct

    # 判斷是否需要計算 NAV
    has_single_pct = single_pct is not None and single_pct > 0
    has_daily_pct = daily_pct is not None and daily_pct > 0

    if has_single_pct or has_daily_pct:
        _, _, nav = calculate_nav()
        
        if has_single_pct:
            factor = single_pct / 100.0 if single_pct > 1.0 else single_pct
            single_limit = nav * factor
        else:
            single_limit = config.limits.single_stock

        if has_daily_pct:
            factor = daily_pct / 100.0 if daily_pct > 1.0 else daily_pct
            daily_limit = nav * factor
        else:
            daily_limit = config.limits.daily_total

        return single_limit, daily_limit
    else:
        # 退回絕對數值限制
        return config.limits.single_stock, config.limits.daily_total
