# Path: src/scratch/triggers/trigger_manual_quarterly_review.py
import os
import sys
import argparse

# 自動將專案根目錄加入模組搜尋路徑，避免需要額外設定 PYTHONPATH
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.config import config
from src.agents.quarterly_review_agent import run_quarterly_review
from src.services.quarterly_aggregator import resolve_manual_review_quarter
from src.services.discord_notifier import send_quarterly_review_notification


def main():
    parser = argparse.ArgumentParser(
        description="【AIAutoStocks】手動觸發季度 AI 復盤與戰術 Skills 演化 CLI 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""範例：
  python src/scratch/triggers/trigger_manual_quarterly_review.py                  # 自動推算最新已結束之季度
  python src/scratch/triggers/trigger_manual_quarterly_review.py 2026 Q3          # 復盤 2026 年 Q3 (涵蓋 7, 8, 9 月)
  python src/scratch/triggers/trigger_manual_quarterly_review.py 2026 3           # 復盤 2026 年 Q3
  python src/scratch/triggers/trigger_manual_quarterly_review.py 2026-Q3          # 復盤 2026 年 Q3
  python src/scratch/triggers/trigger_manual_quarterly_review.py 2026 Q3 --paper        # 復盤模擬帳戶
  python src/scratch/triggers/trigger_manual_quarterly_review.py 2026 Q3 --no-discord   # 不發送 Discord 通知
"""
    )
    parser.add_argument("period", nargs="*", help="目標季度，可傳 '2026 Q3'、'2026 3' 或 '2026-Q3'，不傳則自動推算")
    parser.add_argument("--paper", action="store_true", help="強制以模擬帳戶 (Paper) 模式執行檢討 (預設依照 config 設定)")
    parser.add_argument("--live", action="store_true", help="強制以實盤帳戶 (Live) 模式執行檢討")
    parser.add_argument("--no-discord", action="store_true", help="僅執行運算並存入資料庫，不推播至 Discord Webhook")

    args = parser.parse_args()

    # 解析目標季度
    if len(args.period) == 2:
        y_str = args.period[0].strip()
        q_str = args.period[1].strip().upper().replace("Q", "")
        target_year, target_quarter = int(y_str), int(q_str)
    elif len(args.period) == 1:
        target_year, target_quarter = resolve_manual_review_quarter(args.period[0])
    else:
        target_year, target_quarter = resolve_manual_review_quarter(None)

    # 決定交易帳戶模式
    if args.paper:
        is_paper = True
    elif args.live:
        is_paper = False
    else:
        is_paper = config.limits.is_paper_trading

    mode_label = "【模擬交易 Paper】" if is_paper else "【實盤交易 Live】"
    review_quarter_str = f"{target_year}-Q{target_quarter}"

    print("=" * 70)
    print(f"🚀 AIAutoStocks 手動觸發季度 AI 復盤與 Skills 演化")
    print(f"📅 目標季度: {review_quarter_str} {mode_label}")
    print(f"📡 Discord 通知: {'關閉 (--no-discord)' if args.no_discord else '啟用 (DISCORD_WEBHOOK_QUARTERLY_REVIEW)'}")
    print("=" * 70)

    try:
        review_result = run_quarterly_review(target_year, target_quarter, is_paper=is_paper)

        if review_result.get("skipped"):
            print(f"\n⚠️ 提示: {review_result.get('message', '該季度門檻未滿足，已跳過檢討。')}")
            return

        print("\n" + "=" * 70)
        print("✅ 季度 AI 決策復盤與動態 Skills 演化完成！")
        print("=" * 70)
        print(f"• 標的季度: {review_result.get('review_quarter')}")
        print(f"• 涵蓋月份: {', '.join(review_result.get('months_included', []))}")
        metrics = review_result.get("metrics", {})
        print(f"• 全季交易筆數: {metrics.get('total_trades')} | 勝率: {metrics.get('win_rate')}% | 總實現損益: {metrics.get('total_realized_pnl'):,.0f} 元")
        
        # 顯示月度技能成效回顧
        retro = review_result.get("monthly_skills_retrospective", {})
        if retro and isinstance(retro, dict):
            print("\n⭐【3 個月份月度 Skills 成效回顧】:")
            print(f"  {retro.get('trajectory_summary')}")
            if retro.get("monthly_adjustments_verdict"):
                for v in retro.get("monthly_adjustments_verdict", []):
                    print(f"  - {v}")

        # 顯示戰術總結
        print("\n" + review_result.get("overall_summary", ""))

        # Discord 通知
        if not args.no_discord:
            print("\n📤 正在發送季度復盤卡片與 .md 報告至專屬 Discord Webhook (DISCORD_WEBHOOK_QUARTERLY_REVIEW)...")
            send_quarterly_review_notification(review_result)
            print("🎉 Discord 季度復盤通知發送成功！")

    except Exception as e:
        print(f"\n❌ 執行季度 AI 復盤時發生異常: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
