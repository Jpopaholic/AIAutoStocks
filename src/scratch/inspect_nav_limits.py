import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.nav_calculator import calculate_nav, get_dynamic_limits
from src.config import config

def main():
    cash, holdings_val, nav = calculate_nav()
    single_limit, daily_limit = get_dynamic_limits()
    print("=== ASSET STATUS ===")
    print(f"Initial Cash: {config.limits.initial_cash:,.2f}")
    print(f"Current Cash Balance: {cash:,.2f}")
    print(f"Holdings Value: {holdings_val:,.2f}")
    print(f"Net Asset Value (NAV): {nav:,.2f}")
    print(f"Single Stock Limit Pct: {config.limits.single_stock_pct}")
    print(f"Daily Total Limit Pct: {config.limits.daily_total_pct}")
    print(f"Single Stock Purchase Limit: {single_limit:,.2f}")
    print(f"Daily Total Purchase Limit: {daily_limit:,.2f}")

if __name__ == "__main__":
    main()
