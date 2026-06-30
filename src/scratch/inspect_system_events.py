import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import supabase

def main():
    try:
        res = supabase.table("system_logs").select("*").order("created_at", desc=True).limit(20).execute()
        print("=== System Events (Latest 20) ===")
        for log in res.data:
            print(f"[{log['created_at']}] [{log['level']}] {log['message']}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
