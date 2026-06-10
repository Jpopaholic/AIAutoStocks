import os
import sys
import traceback

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

from src.web_server import run_trading_job_in_background

def main():
    try:
        print("--- STARTING TRADING JOB LOCAL TRIGGER ---", flush=True)
        run_trading_job_in_background()
        print("--- TRADING JOB COMPLETED ---", flush=True)
    except Exception as e:
        print("--- EXCEPTION CAUGHT ---", flush=True)
        traceback.print_exc()

if __name__ == "__main__":
    main()
