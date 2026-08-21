import sys
import os

# Set mock environment variables before importing config to pass startup validation
os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

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

mock_unfilled_orders = [
    {
        "id": 101,
        "stock_code": "2317",
        "action": "BUY",
        "price": 180.0,
        "quantity": 1000,
        "fee": 150,
        "total_amount": 180150,
        "executed_at": "2026-07-09T09:05:00Z",
        "order_id": "sj-order-999",
        "reason": "CANCELLED"
    }
]

mock_nav = (50000.0, 950000.0, 1000000.0)  # cash_balance, holdings_value, net_asset_value

@patch("src.services.discord_notifier.get_unfilled_orders", return_value=mock_unfilled_orders)
@patch("src.services.discord_notifier.get_orders", return_value=mock_orders)
@patch("src.services.discord_notifier.get_holdings", return_value=mock_holdings)
@patch("src.services.nav_calculator.calculate_nav", return_value=mock_nav)
@patch("src.services.sandbox_simulator.fetch_realtime_quote", return_value={"price": 952.0})
@patch("requests.post")
def run_test(mock_post, mock_quote, mock_nav_calc, mock_holdings_query, mock_orders_query, mock_unfilled_query):
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

    # 1b. Test Decision Sorting in Layer 3 by Layer 2 Scores
    print("\n--- 測試 1b: 驗證第三層報告個股顯示順序依據第二層分數排名 ---")
    mock_analyst_scores = [
        {"stock_code": "2330", "total_score": 75.0, "trend_score": 20, "momentum_score": 20, "volume_score": 15, "safety_score": 20},
        {"stock_code": "2454", "total_score": 95.0, "trend_score": 25, "momentum_score": 25, "volume_score": 25, "safety_score": 20},
        {"stock_code": "2308", "total_score": 60.0, "trend_score": 15, "momentum_score": 15, "volume_score": 15, "safety_score": 15},
    ]
    # Intentionally provide decisions in random order (2308 first, 2330 second, 2454 last)
    mock_portfolio_decision = {
        "ranking_analysis": "綜合排名對比測試。",
        "decisions": [
            {"stock_code": "2308", "action": "HOLD", "quantity": 0, "reason": "分數最低觀望", "total_score": 60.0},
            {"stock_code": "2330", "action": "BUY", "quantity": 1000, "reason": "中等分數加碼", "total_score": 75.0},
            {"stock_code": "2454", "action": "BUY", "quantity": 2000, "reason": "最高分強力買進", "total_score": 95.0},
        ]
    }
    with patch("src.services.sandbox_simulator.is_simulation_active", return_value=True):
        send_daily_report(
            ai_outlook="測試分數排序",
            analyst_scores=mock_analyst_scores,
            portfolio_decision=mock_portfolio_decision
        )

    assert mock_post.called
    found_section2 = None
    found_section4 = None
    for call in mock_post.call_args_list:
        _, kwargs = call
        if "json" in kwargs and kwargs["json"] and "embeds" in kwargs["json"]:
            p = kwargs["json"]
        elif "data" in kwargs and "payload_json" in kwargs["data"]:
            p = json.loads(kwargs["data"]["payload_json"])
        else:
            continue
        for emb in p.get("embeds", []):
            for f in emb.get("fields", []):
                if "評分與相對排名" in f.get("name", ""):
                    found_section2 = f["value"]
                if "經理人交易配置與理由" in f.get("name", ""):
                    found_section4 = f["value"]
                    
    assert found_section4 is not None, "未找到 Section 4 欄位內容！"
    pos_2454 = found_section4.find("2454")
    pos_2330 = found_section4.find("2330")
    pos_2308 = found_section4.find("2308")
    assert pos_2454 < pos_2330 < pos_2308, f"排序錯誤! 2454(95分) 應優先於 2330(75分) 優先於 2308(60分)。內容:\n{found_section4}"
    print(" ✅ 驗證成功：第三層報告個股順序 (2454 -> 2330 -> 2308) 完美依照第二層分數降序排名！")

    assert found_section2 is not None, "未找到 Section 2 欄位內容！"
    assert "__2330 台積電" in found_section2 and "大盤:10)  __" in found_section2, f"第二層報告未整行底線標記持股 2330! 內容:\n{found_section2}"
    assert "2330" in found_section4 and "| [現正持有]" in found_section4, f"第三層報告未標記持股 2330! 內容:\n{found_section4}"
    print(" ✅ 驗證成功：第二層 (底線 __股票__) 與第三層 (| [現正持有] 管道標籤) 皆精準標記目前持有之股票！")

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
        
    # 3. Test Test Notification
    print("\n--- 測試 3: 測試 Webhook 通知 ---")
    from src.services.discord_notifier import send_test_notification
    res = send_test_notification("monthly_review")
    print(f"測試結果: {res}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].startswith("http"):
        # If user passed a real webhook url, we do not mock requests.post
        print(f"使用實體 Webhook 進行真實發送測試: {sys.argv[1]}")
        with patch("src.services.discord_notifier.get_orders", return_value=mock_orders), \
             patch("src.services.discord_notifier.get_unfilled_orders", return_value=mock_unfilled_orders), \
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
