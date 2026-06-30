import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import get_stock_klines

def main():
    t2618 = get_stock_klines("2618", limit=100)
    for k in t2618:
        if k['close'] == 28.7 or k['close'] == 28.70:
            print(f"Match found: Date {k['date']}, Close {k['close']}")

if __name__ == "__main__":
    main()
