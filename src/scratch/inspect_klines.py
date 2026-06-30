import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import get_stock_klines

def main():
    print("=== TAIEX Latest Klines ===")
    taiex = get_stock_klines("TAIEX", limit=5)
    for k in taiex:
        print(f"Date: {k['date']}, Close: {k['close']}, MA20: {k.get('ma20')}")

    print("\n=== 2618 Latest Klines ===")
    t2618 = get_stock_klines("2618", limit=5)
    for k in t2618:
        print(f"Date: {k['date']}, Close: {k['close']}, MA20: {k.get('ma20')}")

    print("\n=== 0050 Latest Klines ===")
    t0050 = get_stock_klines("0050", limit=5)
    for k in t0050:
        print(f"Date: {k['date']}, Close: {k['close']}, MA20: {k.get('ma20')}")

if __name__ == "__main__":
    main()
