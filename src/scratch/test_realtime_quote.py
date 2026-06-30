import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.stock_fetcher import fetch_realtime_quote

def main():
    print("Fetching realtime quote for 2618...")
    quote = fetch_realtime_quote("2618")
    print(quote)

if __name__ == "__main__":
    main()
