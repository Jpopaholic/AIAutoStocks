import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

class TestDiscordDiffColors(unittest.TestCase):
    @patch("src.services.supabase_client.get_unfilled_orders", return_value=[])
    @patch("src.services.supabase_client.get_orders")
    @patch("src.services.supabase_client.get_holdings")
    @patch("src.services.nav_calculator.calculate_nav", return_value=(50000.0, 950000.0, 1000000.0))
    @patch("src.services.stock_fetcher.get_display_price")
    @patch("src.services.stock_fetcher.fetch_realtime_quotes_batch")
    @patch("requests.post")
    def test_diff_color_prefixes(
        self, mock_post, mock_batch_quotes, mock_disp_price,
        mock_nav, mock_get_holdings, mock_get_orders, mock_get_unfilled
    ):
        from src.config import config, DiscordConfig
        from src.services.discord_notifier import send_daily_report

        mock_post.return_value.status_code = 204
        object.__setattr__(config, "discord", DiscordConfig(
            webhook_sandbox="https://discord.com/api/webhooks/mock",
            webhook_live="https://discord.com/api/webhooks/mock"
        ))

        # 1. 設置持股 (2330 成本 1000, 2454 成本 1500)
        mock_get_holdings.return_value = [
            {"stock_code": "2330", "quantity": 1000, "average_price": 1000.0},
            {"stock_code": "2454", "quantity": 1000, "average_price": 1500.0},
        ]
        # 設置現價：2330 現價 1050 (獲利), 2454 現價 1400 (虧損)
        def fake_display_price(code, fallback_price=0.0):
            if code == "2330":
                return 1050.0
            elif code == "2454":
                return 1400.0
            return fallback_price
        mock_disp_price.side_effect = fake_display_price

        # 2. 設置本日交易明細 (executed_orders)
        # 訂單 1: 買進 -> 應為白色 (" ")
        # 訂單 2: 賣出獲利 5000 -> 應為紅色 ("-")
        # 訂單 3: 賣出虧損 3000 -> 應為綠色 ("+")
        # 訂單 4: 賣出平手 0 -> 應為白色 (" ")
        mock_get_orders.return_value = [
            {"id": 1, "stock_code": "2330", "action": "BUY", "quantity": 10, "price": 1000.0, "execution_price": 1000.0, "realized_pnl": 0.0, "status": "FILLED"},
            {"id": 2, "stock_code": "2382", "action": "SELL", "quantity": 10, "price": 300.0, "execution_price": 300.0, "realized_pnl": 5000.0, "status": "FILLED"},
            {"id": 3, "stock_code": "2454", "action": "SELL", "quantity": 10, "price": 1400.0, "execution_price": 1400.0, "realized_pnl": -3000.0, "status": "FILLED"},
            {"id": 4, "stock_code": "2881", "action": "SELL", "quantity": 10, "price": 90.0, "execution_price": 90.0, "realized_pnl": 0.0, "status": "FILLED"},
        ]

        # 3. 設置次日預約委託單 (portfolio_decision)
        # 決策 1: 買進 2308 -> 應為白色 (" ")
        # 決策 2: 賣出 2330 停利 (委託價 1100 > 成本 1000) -> 應為紅色 ("-") 且標示 賣(停利)
        # 決策 3: 賣出 2454 停損 (理由包含停損) -> 應為綠色 ("+") 且標示 賣(停損)
        mock_portfolio_decision = {
            "decisions": [
                {"stock_code": "2308", "action": "BUY", "quantity": 100, "price": 350.0, "reason": "看好動能進場配置", "total_score": 85.0, "trend_score": 20, "momentum_score": 20, "volume_score": 15, "safety_score": 15, "regime_score": 15},
                {"stock_code": "2330", "action": "SELL", "quantity": 1000, "price": 1100.0, "reason": "達波段滿足點，獲利了結平倉", "total_score": 75.0, "trend_score": 15, "momentum_score": 15, "volume_score": 15, "safety_score": 15, "regime_score": 15},
                {"stock_code": "2454", "action": "SELL", "quantity": 1000, "price": 1380.0, "reason": "跌破均線支撐，執行嚴格風控強制停損", "total_score": 60.0, "trend_score": 10, "momentum_score": 10, "volume_score": 15, "safety_score": 15, "regime_score": 10},
            ]
        }

        with patch("src.services.sandbox_simulator.is_simulation_active", return_value=True):
            send_daily_report(
                ai_outlook="測試紅綠白色彩邏輯",
                portfolio_decision=mock_portfolio_decision
            )

        self.assertTrue(mock_post.called, "Discord Webhook 應被調用")
        
        # 提取 payload 或附件 full_report_md
        call_args = mock_post.call_args
        data = call_args[1].get("data", {})
        import json
        payload = json.loads(data.get("payload_json", "{}"))
        fields = []
        for emb in payload.get("embeds", []):
            fields.extend(emb.get("fields", []))

        holdings_f = next((f["value"] for f in fields if "手持股票明細" in f["name"]), "")
        trades_f = next((f["value"] for f in fields if "本日交易明細" in f["name"]), "")
        next_day_f = next((f["value"] for f in fields if "次日預約委託單" in f["name"]), "")

        print("\n--- [測試輸出] 目前手持股票明細 ---")
        print(holdings_f)
        print("\n--- [測試輸出] 本日交易明細 ---")
        print(trades_f)
        print("\n--- [測試輸出] 本日AI擬定之次日預約委託單 ---")
        print(next_day_f)

        # 驗證手持股票：2330 獲利為 '-' (紅)，2454 虧損為 '+' (綠)
        self.assertIn("- 2330", holdings_f, "持股 2330 獲利應為 '-' 前綴 (紅色)")
        self.assertIn("+ 2454", holdings_f, "持股 2454 虧損應為 '+' 前綴 (綠色)")

        # 驗證本日交易明細：
        # 2330 買進 -> 白色 (' ')
        self.assertIn("  買 2330", trades_f, "交易 2330 買進應為 ' ' 前綴 (白色)")
        # 2382 賣出獲利 -> 紅色 ('-')
        self.assertIn("- 賣 2382", trades_f, "交易 2382 賣出獲利應為 '-' 前綴 (紅色)")
        # 2454 賣出虧損 -> 綠色 ('+')
        self.assertIn("+ 賣 2454", trades_f, "交易 2454 賣出虧損應為 '+' 前綴 (綠色)")
        # 2881 賣出平盤 -> 白色 (' ')
        self.assertIn("  賣 2881", trades_f, "交易 2881 賣出平盤應為 ' ' 前綴 (白色)")

        # 驗證次日預約單：
        # 2308 買進 -> 白色 (' ')
        self.assertIn("  買 2308", next_day_f, "預約 2308 買進應為 ' ' 前綴 (白色)")
        # 2330 停利 -> 紅色 ('-') 且標註 (停利)
        self.assertIn("- 賣(停利) 2330", next_day_f, "預約 2330 停利應為 '-' 前綴 (紅色) 且標註 (停利)")
        # 2454 停損 -> 綠色 ('+') 且標註 (停損)
        self.assertIn("+ 賣(停損) 2454", next_day_f, "預約 2454 停損應為 '+' 前綴 (綠色) 且標註 (停損)")

        print("\n🎉 全部紅綠白色彩邏輯驗證成功通過！")

if __name__ == "__main__":
    unittest.main()
