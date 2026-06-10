import os
import sys

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import supabase

def main():
    try:
        res = supabase.table("system_logs").select("*").order("created_at", desc=True).limit(5).execute()
        for log in res.data:
            print(f"Time: {log['created_at']}")
            print(f"Level: {log['level']}")
            print(f"Message: {log['message']}")
            print(f"Details: {log['details']}")
            print("-" * 50)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
