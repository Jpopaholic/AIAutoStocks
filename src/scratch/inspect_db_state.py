import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import supabase, get_holdings, get_orders

def main():
    try:
        holdings = get_holdings()
        print("Holdings:")
        print(holdings)
        
        orders = get_orders()
        print("\nOrders:")
        print(orders)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
