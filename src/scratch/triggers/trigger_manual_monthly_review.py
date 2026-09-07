# Path: src/scratch/triggers/trigger_manual_monthly_review.py
import os
import sys
import argparse
from datetime import datetime

# 自動將專案根目錄加入模組搜尋路徑，避免需要額外設定 PYTHONPATH
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.config import config
from src.agents.monthly_review_agent import run_monthly_review
from src.services.discord_notifier import send_monthly_review_notification


def resolve_target_period(target_arg: str = None):
    """解析使用者傳入的月份參數 (例如 '2026-08' 或 '2026 8')，若未指定則預設為前一個月份"""
    now = datetime.now()
    if not target_arg:
        # 預設為前一個月份
        if now.month == 1:
            return now.year - 1, 12
        else:
            return now.year, now.month - 1
    
    clean_arg = target_arg.strip().replace("/", "-")
    if "-" in clean_arg:
        parts = clean_arg.split("-")
        return int(parts[0]), int(parts[1])
    elif len(clean_arg) == 6 and clean_arg.isdigit():
        return int(clean_arg[:4]), int(clean_arg[4:])
    else:
        raise ValueError(f"無法解析目標月份格式: '{target_arg}'，請使用 YYYY-MM (例如 2026-08) 或直接輸入年份與月份。")


def main():
    parser = argparse.ArgumentParser(
        description="【AIAutoStocks】手動觸發月度 AI 復盤與戰術 Skills 演化 CLI 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python src/scratch/triggers/trigger_manual_monthly_review.py              # 預設復盤前一個月份
  python src/scratch/triggers/trigger_manual_monthly_review.py 2026 8       # 復盤 2026 年 8 月
  python src/scratch/triggers/trigger_manual_monthly_review.py 2026-08      # 復盤 2026 年 8 月
  python src/scratch/triggers/trigger_manual_monthly_review.py 2026 8 --paper        # 復盤模擬帳戶
  python src/scratch/triggers/trigger_manual_monthly_review.py 2026 8 --no-discord   # 不發送 Discord 通知
"""
    )
    parser.add_argument("period", nargs="*", help="目標月份，可傳 '2026 8' 或 '2026-08'，不傳則預設為前一個月份")
    parser.add_argument("--paper", action="store_true", help="強制以模擬帳戶 (Paper) 模式執行檢討 (預設依照 config 設定)")
    parser.add_argument("--live", action="store_true", help="強制以實盤帳戶 (Live) 模式執行檢討")
    parser.add_argument("--no-discord", action="store_true", help="僅執行運算並存入資料庫，不推播至 Discord Webhook")

    args = parser.parse_args()

    # 解析月份
    if len(args.period) == 2:
        target_year, target_month = int(args.period[0]), int(args.period[1])
    elif len(args.period) == 1:
        target_year, target_month = resolve_target_period(args.period[0])
    else:
        target_year, target_month = resolve_target_period(None)

    # 解析實盤/模擬
    if args.paper:
        is_paper = True
    elif args.live:
        is_paper = False
    else:
        is_paper = config.limits.is_paper_trading

    mode_label = "模擬帳戶 (Paper Trading)" if is_paper else "實盤帳戶 (Live Trading)"

    print("=" * 66)
    print(f"🚀 【手動月度復盤工具】開始執行 {target_year}-{target_month:02d} 月度 AI 自我檢討與演化")
    print(f"• 執行模式: {mode_label}")
    print(f"• Discord 推播: {'❌ 關閉 (--no-discord)' if args.no_discord else '✅ 開啟'}")
    print("=" * 66)

    try:
        review_result = run_monthly_review(target_year, target_month, is_paper=is_paper)
    except Exception as e:
        print(f"\n❌ 月度檢討執行發生嚴重錯誤: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    if review_result.get("skipped"):
        print(f"\n⚠️ 復盤安全跳過: {review_result.get('message')}")
        return

    print("\n" + "-" * 66)
    print("🏆 Layer 3 月度戰術策略總結：")
    print(review_result.get("overall_summary", "無總結內容"))
    print("-" * 66 + "\n")

    # Discord 推播
    if not args.no_discord:
        print("📢 正在發送至 Discord 頻道...")
        try:
            send_monthly_review_notification(review_result)
            print("✅ 已成功發送月度復盤推播與 Markdown 報告至 Discord！")
        except Exception as notify_err:
            print(f"⚠️ Discord 推播失敗: {notify_err}")
    else:
        print("ℹ️ 已跳過 Discord 發送 (--no-discord)。")

    print("\n🎉 月度檢討與 Skills 演化流程已全數順利完成！")


if __name__ == "__main__":
    main()
