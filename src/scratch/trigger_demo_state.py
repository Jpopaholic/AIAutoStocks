import os
import sys

# 載入專案路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.supabase_client import (
    set_system_fault_status,
    add_pending_liquidation_stock,
    remove_pending_liquidation_stock,
    get_pending_liquidation_stocks,
    get_system_fault_status
)

def print_status():
    print("\n--- 目前資料庫防禦狀態 ---")
    fault = get_system_fault_status()
    print(f"全局系統故障狀態: {fault.get('status')} (原因: {fault.get('detail')})")
    stocks = get_pending_liquidation_stocks()
    print(f"等候平倉股票清單: {stocks}")
    print("---------------------------\n")

def main():
    if len(sys.argv) < 2:
        print("使用說明: venv/bin/python src/scratch/trigger_demo_state.py [選項]")
        print("選項:")
        print("  status        : 顯示資料庫中目前的狀態")
        print("  set-fault     : 模擬注入系統級連線故障鎖 (FAULT)")
        print("  set-pending   : 模擬將 2330 股票加入等候平倉鎖中")
        print("  clear-all     : 清除故障鎖與平倉鎖，恢復 OK 狀態")
        return

    cmd = sys.argv[1].lower()
    if cmd == "status":
        print_status()
    elif cmd == "set-fault":
        set_system_fault_status("FAULT", "Shioaji API connection timed out (Simulated error for UI testing)")
        print("已將系統故障狀態設為 FAULT！")
        print_status()
    elif cmd == "set-pending":
        add_pending_liquidation_stock("2330")
        print("已將股票 2330 加入等候平倉名單！")
        print_status()
    elif cmd == "clear-all":
        set_system_fault_status("OK")
        remove_pending_liquidation_stock("2330")
        # 同時試圖清除其他股票以防萬一
        stocks = get_pending_liquidation_stocks()
        for s in stocks:
            remove_pending_liquidation_stock(s)
        print("已成功清除所有平倉股票鎖與全局系統故障鎖！")
        print_status()
    else:
        print(f"未知指令: {cmd}")

if __name__ == "__main__":
    main()
