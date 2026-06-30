import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import get_stock_klines

def main():
    t2618 = get_stock_klines("2618", limit=20)
    print("=== 2618 (Latest 20) ===")
    for k in t2618:
        print(f"Date: {k['date']}, Close: {k['close']}")

if __name__ == "__main__":
    main()
