# Path: src/scratch/test_tick_size.py
import os
import sys
import unittest
from unittest.mock import patch

# Set mock environment variables before importing config to pass startup validation
os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.broker_connector import align_to_tw_tick_size
from src.services.health_check import audit_proposed_order

class TestTickSizeAndAudit(unittest.TestCase):

    def test_stock_tick_size(self):
        """
        測試一般股票的升降單位對齊 (Tick Size)
        """
        # < 10: 0.01 元
        self.assertEqual(align_to_tw_tick_size(5.543, "2330"), 5.54)
        self.assertEqual(align_to_tw_tick_size(9.997, "2330"), 10.0) # 會進到下一個區間

        # 10 ~ 50: 0.05 元
        self.assertEqual(align_to_tw_tick_size(12.33, "2330"), 12.35)
        self.assertEqual(align_to_tw_tick_size(49.98, "2330"), 50.0)

        # 50 ~ 100: 0.1 元
        self.assertEqual(align_to_tw_tick_size(75.24, "2330"), 75.2)
        self.assertEqual(align_to_tw_tick_size(99.96, "2330"), 100.0)

        # 100 ~ 500: 0.5 元
        self.assertEqual(align_to_tw_tick_size(105.24, "2330"), 105.0)
        self.assertEqual(align_to_tw_tick_size(105.25, "2330"), 105.0)  # Python banker's rounding for 210.5
        self.assertEqual(align_to_tw_tick_size(105.26, "2330"), 105.5)
        self.assertEqual(align_to_tw_tick_size(105.75, "2330"), 106.0)

        # 500 ~ 1000: 1.0 元
        self.assertEqual(align_to_tw_tick_size(750.4, "2330"), 750.0)
        self.assertEqual(align_to_tw_tick_size(750.6, "2330"), 751.0)

        # >= 1000: 5.0 元
        self.assertEqual(align_to_tw_tick_size(1502.0, "2330"), 1500.0)
        self.assertEqual(align_to_tw_tick_size(1503.0, "2330"), 1505.0)

    def test_etf_tick_size(self):
        """
        測試 ETF 的升降單位對齊 (以 '00' 開頭的股票代號)
        """
        # < 50: 0.01 元
        self.assertEqual(align_to_tw_tick_size(45.123, "0050"), 45.12)
        self.assertEqual(align_to_tw_tick_size(45.128, "00878"), 45.13)
        self.assertEqual(align_to_tw_tick_size(49.994, "0056"), 49.99)

        # >= 50: 0.05 元
        self.assertEqual(align_to_tw_tick_size(105.23, "0050"), 105.25)
        self.assertEqual(align_to_tw_tick_size(105.27, "0050"), 105.25)
        self.assertEqual(align_to_tw_tick_size(105.28, "0050"), 105.30)
        self.assertEqual(align_to_tw_tick_size(105.25, "0050"), 105.25)

    @patch("src.services.nav_calculator.get_dynamic_limits")
    def test_audit_proposed_order_etf_and_stock(self, mock_limits):
        """
        測試下單前安全審查在 ETF 與一般股票下的價格驗證
        """
        # 設定交易限額 50,000 元
        mock_limits.return_value = (50000.0, 150000.0)

        # 1. 測試 0050 ETF 價格 105.25 (對齊後為 105.25，應通過)
        is_valid, reason = audit_proposed_order(
            stock_code="0050",
            action="BUY",
            price=105.25,
            quantity=70,
            close_price=105.0
        )
        self.assertTrue(is_valid)
        self.assertEqual(reason, "通過下單前安全審查")

        # 2. 測試 2330 股票 價格 105.25 (對齊後為 105.0，應攔截)
        is_valid, reason = audit_proposed_order(
            stock_code="2330",
            action="BUY",
            price=105.25,
            quantity=70,
            close_price=105.0
        )
        self.assertFalse(is_valid)
        self.assertIn("不符合台股升降單位規範，應調整為 105.0", reason)

if __name__ == "__main__":
    unittest.main()
