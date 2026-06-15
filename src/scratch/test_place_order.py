import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Set mock environment variables before importing config to pass startup validation
os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.broker_connector import place_order
from shioaji import OrderStatus

class TestPlaceOrder(unittest.TestCase):

    @patch("src.services.broker_connector.config")
    @patch("src.services.broker_connector._get_shioaji_api")
    @patch("src.services.broker_connector._validate_trading_limits")
    @patch("src.services.broker_connector.execute_trade_transaction")
    @patch("src.services.broker_connector.log_system_event")
    def test_place_order_fails_immediately(
        self, mock_log, mock_execute_transaction, mock_validate_limits, mock_get_api, mock_config
    ):
        """
        Verify that if the real broker order fails immediately with OrderStatus.Failed,
        place_order raises RuntimeError and does NOT write the order to the database.
        """
        # 1. Configure for real trading
        mock_config.limits.is_paper_trading = False
        
        # 2. Setup mock Shioaji API
        mock_api = MagicMock()
        mock_get_api.return_value = mock_api
        
        # Setup mock account
        mock_account = MagicMock()
        mock_account.person_id = "test-person"
        mock_api.stock_account = mock_account
        
        # Setup mock contract
        mock_contract = MagicMock()
        mock_api.Contracts.Stocks = {"2330": mock_contract}
        
        # 3. Setup mock trade with Failed status
        mock_trade = MagicMock()
        mock_trade.status.id = ""
        mock_trade.status.status = OrderStatus.Failed
        mock_trade.status.msg = "餘額不足或憑證無效"
        mock_trade.status.status_code = "1001"
        mock_api.place_order.return_value = mock_trade
        
        # 4. Call place_order and expect RuntimeError
        with self.assertRaises(RuntimeError) as context:
            place_order(stock_code="2330", action="BUY", price=2310.0, quantity=3)
            
        self.assertIn("永豐證券下單失敗：餘額不足或憑證無效 (代碼: 1001)", str(context.exception))
        
        # 5. Assertions
        # execute_trade_transaction should NOT be called
        mock_execute_transaction.assert_not_called()
        
        # log_system_event should be called to log the error
        mock_log.assert_any_call("ERROR", "真實下單委託異常失敗: 永豐證券下單失敗：餘額不足或憑證無效 (代碼: 1001)")

if __name__ == "__main__":
    unittest.main()
