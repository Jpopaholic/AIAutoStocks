import sys
import os

# Set mock environment variables before importing config to pass startup validation
os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.config import config
from src.services.discord_notifier import send_daily_report, send_emergency_alert

# Mocking database services
mock_orders = [
    {
        "id": 1,
        "stock_code": "2330",
        "action": "BUY",
        "price": 950.0,
        "quantity": 1000,
        "fee": 810,
        "total_amount": 950810,
        "realized_pnl": 0.0,
        "status": "FILLED",
        "execution_price": 948.0
    },
    {
        "id": 2,
        "stock_code": "2454",
        "action": "SELL",
        "price": 1200.0,
        "quantity": 1000,
        "fee": 1030,
        "total_amount": 1198970,
        "realized_pnl": 5000.0,
        "status": "FILLED",
        "execution_price": 1205.0
    },
    {
        "id": 3,
        "stock_code": "2308",
        "action": "BUY",
        "price": 320.0,
        "quantity": 1000,
        "fee": 274,
        "total_amount": 320274,
        "realized_pnl": 0.0,
        "status": "PENDING",
        "execution_price": None
    }
]

mock_holdings = [
    {
        "stock_code": "2330",
        "quantity": 1000,
        "average_price": 948.0
    }
]

mock_nav = (50000.0, 950000.0, 1000000.0)  # cash_balance, holdings_value, net_asset_value

@patch("src.services.discord_notifier.get_orders", return_value=mock_orders)
@patch("src.services.discord_notifier.get_holdings", return_value=mock_holdings)
@patch("src.services.nav_calculator.calculate_nav", return_value=mock_nav)
@patch("src.services.sandbox_simulator.fetch_realtime_quote", return_value={"price": 952.0})
@patch("requests.post")
def run_test(mock_post, mock_quote, mock_nav_calc, mock_holdings_query, mock_orders_query):
    # Setup mock webhook URL in config
    test_webhook = sys.argv[1] if len(sys.argv) > 1 else "https://discord.com/api/webhooks/test/test"
    
    # Override configuration values dynamically for the test
    from src.config import DiscordConfig
    object.__setattr__(config, "discord", DiscordConfig(
        webhook_sandbox=test_webhook,
        webhook_live=test_webhook
    ))
    
    # 1. Test Daily Report
    print("--- 測試 1: 每日交易報告 ---")
    mock_post.return_value.status_code = 204
    
    # Set sandbox mode to True to test Sandbox webhook routing
    with patch("src.services.sandbox_simulator.is_simulation_active", return_value=True):
        send_daily_report(ai_outlook="今日行情強勢整理，大盤站穩五日線。AI 決策為買進台積電。")
        
    if mock_post.called:
        args, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        print("發送成功！Payload 預覽:")
        import json
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("未發送 Discord Webhook！")

    mock_post.reset_mock()

    # 2. Test Emergency Email
    print("\n--- 測試 2: 緊急警報通知 ---")
    send_emergency_alert(
        subject="系統下單發生系統級故障！",
        message="連線至永豐 API 逾時，已自動鎖定全局交易。"
    )
    if mock_post.called:
        args, kwargs = mock_post.call_args
        payload = kwargs.get("json", {})
        print("發送成功！Payload 預覽:")
        import json
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print("未發送 Discord Webhook！")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        # If user passed a real webhook url, we do not mock requests.post
        print(f"使用實體 Webhook 進行真實發送測試: {sys.argv[1]}")
        with patch("src.services.discord_notifier.get_orders", return_value=mock_orders), \
             patch("src.services.discord_notifier.get_holdings", return_value=mock_holdings), \
             patch("src.services.nav_calculator.calculate_nav", return_value=mock_nav), \
             patch("src.services.sandbox_simulator.fetch_realtime_quote", return_value={"price": 952.0}):
            from src.config import DiscordConfig
            object.__setattr__(config, "discord", DiscordConfig(
                webhook_sandbox=sys.argv[1],
                webhook_live=sys.argv[1]
            ))
            
            print("發送每日報告中...")
            with patch("src.services.sandbox_simulator.is_simulation_active", return_value=True):
                send_daily_report(ai_outlook="今日行情強勢整理，大盤站穩五日線。AI 決策為買進台積電。")
                
            print("發送緊急警報中...")
            send_emergency_alert(
                subject="永豐 API 帳號登入逾時",
                message="實盤下單模組初始化逾時，已自動暫停後續排程。"
            )
            print("測試發送完畢，請至 Discord 頻道查看呈現結果。")
    else:
        print("未傳入實體 Webhook URL，執行 Mock 驗證：")
        run_test()
