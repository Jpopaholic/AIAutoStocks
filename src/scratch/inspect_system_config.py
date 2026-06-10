import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.supabase_client import supabase

def main():
    try:
        res = supabase.table("system_config").select("*").execute()
        print("System Config Table:")
        for r in res.data:
            print(f"{r['key']}: {repr(r['value'])}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
