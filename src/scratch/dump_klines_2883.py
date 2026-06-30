import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import get_stock_klines

def main():
    t2883 = get_stock_klines("2883", limit=5)
    print("=== 2883 (Latest 5) ===")
    for k in t2883:
        print(f"Date: {k['date']}, Close: {k['close']}")

if __name__ == "__main__":
    main()
