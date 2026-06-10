import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# Load project path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.services.broker_connector import sync_broker_orders, sync_sandbox_orders
from src.config import config

class TestSyncBrokerOrders(unittest.TestCase):

    @patch("src.services.broker_connector.config")
    @patch("src.services.broker_connector.log_system_event")
    def test_sync_broker_orders_paper_trading_skipped(self, mock_log, mock_config):
        """
        Verify that sync_broker_orders is skipped when paper trading mode is enabled.
        """
        mock_config.limits.is_paper_trading = True
        
        sync_broker_orders()
        
        mock_log.assert_any_call("INFO", "[對帳同步] 模擬交易模式，跳過券商對帳同步。")

    @patch("src.services.broker_connector.config")
    @patch("src.services.broker_connector.get_pending_real_orders")
    @patch("src.services.broker_connector.log_system_event")
    def test_sync_broker_orders_no_pending_orders(self, mock_log, mock_get_pending, mock_config):
        """
        Verify that sync_broker_orders returns early if there are no pending real orders.
        """
        mock_config.limits.is_paper_trading = False
        mock_get_pending.return_value = []
        
        sync_broker_orders()
        
        mock_log.assert_any_call("INFO", "[對帳同步] 目前無任何 PENDING 委託訂單，結束同步。")

    @patch("src.services.broker_connector.config")
    @patch("src.services.broker_connector.get_pending_real_orders")
    @patch("src.services.broker_connector.update_order_status")
    @patch("src.services.broker_connector.update_holding_after_fill")
    @patch("src.services.broker_connector.get_holdings")
    @patch("src.services.broker_connector._get_shioaji_api")
    @patch("src.services.broker_connector.log_system_event")
    def test_sync_broker_orders_filled_scenario(
        self, mock_log, mock_get_api, mock_get_holdings, mock_update_holding, mock_update_order, mock_get_pending, mock_config
    ):
        """
        Verify that FILLED status is correctly synchronized, price is calculated, and positions are updated.
        """
        mock_config.limits.is_paper_trading = False
        
        # 1. Setup pending orders
        mock_get_pending.return_value = [
            {
                "id": 42,
                "stock_code": "2330",
                "action": "BUY",
                "price": 600.0,
                "quantity": 1000.0,
                "fee": 513.0,
                "total_amount": 600513.0,
                "status": "PENDING",
                "order_id": "sj-order-123"
            }
        ]
        
        # 2. Setup mock Shioaji API response
        mock_api = MagicMock()
        mock_get_api.return_value = mock_api
        
        # Create a mock Trade object
        mock_trade = MagicMock()
        mock_trade.status.id = "sj-order-123"
        
        # Mock status Enum or class returned
        mock_status_enum = MagicMock()
        mock_status_enum.name = "Filled"
        mock_trade.status.status = mock_status_enum
        
        # Mock Deals list
        mock_deal1 = MagicMock()
        mock_deal1.price = 598.0
        mock_deal1.quantity = 400.0
        
        mock_deal2 = MagicMock()
        mock_deal2.price = 601.0
        mock_deal2.quantity = 600.0
        
        mock_trade.status.deals = [mock_deal1, mock_deal2]
        
        # Return mock list_trades
        mock_api.list_trades.return_value = [mock_trade]
        
        # Execute sync
        sync_broker_orders()
        
        # 3. Assertions
        # Weighted average execution price calculation:
        # (598 * 400 + 601 * 600) / 1000 = (239200 + 360600) / 1000 = 599.8
        expected_avg_price = 599.8
        
        # Check that update_order_status is called for db id 42 with proper filled updates
        mock_update_order.assert_called_once()
        args, kwargs = mock_update_order.call_args
        self.assertEqual(args[0], 42)
        updates = args[1]
        self.assertEqual(updates["status"], "FILLED")
        self.assertAlmostEqual(updates["execution_price"], expected_avg_price)
        self.assertEqual(updates["quantity"], 1000.0)
        
        # Check that update_holding_after_fill is called
        mock_update_holding.assert_called_once_with(
            stock_code="2330",
            action="BUY",
            price=expected_avg_price,
            quantity=1000.0,
            is_paper=False
        )

    @patch("src.services.broker_connector.config")
    @patch("src.services.broker_connector.get_pending_real_orders")
    @patch("src.services.broker_connector.update_order_status")
    @patch("src.services.broker_connector._get_shioaji_api")
    @patch("src.services.broker_connector.log_system_event")
    def test_sync_broker_orders_cancelled_scenario(
        self, mock_log, mock_get_api, mock_update_order, mock_get_pending, mock_config
    ):
        """
        Verify that CANCELLED status correctly releases purchasing power by setting total_amount = 0.0.
        """
        mock_config.limits.is_paper_trading = False
        
        # 1. Setup pending orders
        mock_get_pending.return_value = [
            {
                "id": 43,
                "stock_code": "2454",
                "action": "BUY",
                "price": 1000.0,
                "quantity": 1000.0,
                "fee": 855.0,
                "total_amount": 1000855.0,
                "status": "PENDING",
                "order_id": "sj-order-456"
            }
        ]
        
        # 2. Setup mock Shioaji API response
        mock_api = MagicMock()
        mock_get_api.return_value = mock_api
        
        mock_trade = MagicMock()
        mock_trade.status.id = "sj-order-456"
        
        mock_status_enum = MagicMock()
        mock_status_enum.name = "Cancelled"
        mock_trade.status.status = mock_status_enum
        
        mock_api.list_trades.return_value = [mock_trade]
        
        # Execute sync
        sync_broker_orders()
        
        # 3. Assertions
        mock_update_order.assert_called_once_with(
            43,
            {
                "status": "CANCELLED",
                "total_amount": 0.0,
                "fee": 0.0
            }
        )

    @patch("src.services.broker_connector.supabase")
    @patch("src.services.broker_connector.execute_with_retry")
    @patch("src.services.broker_connector.update_holding_after_fill")
    @patch("src.services.broker_connector.get_holdings")
    @patch("src.services.sandbox_simulator.fetch_realtime_quote")
    @patch("src.services.broker_connector.log_system_event")
    def test_sync_sandbox_orders(
        self, mock_log, mock_fetch_quote, mock_get_holdings, mock_update_holding, mock_execute_retry, mock_supabase
    ):
        """
        Verify that sync_sandbox_orders queries PENDING paper orders, simulates fills, and updates holdings.
        """
        # 1. Setup mock query return (pending paper orders)
        mock_pending_order = {
            "id": 101,
            "stock_code": "2330",
            "action": "BUY",
            "price": 600.0,
            "quantity": 10.0,
            "fee": 20.0,
            "total_amount": 6020.0,
            "status": "PENDING"
        }
        
        # We need mock_execute_retry side effect.
        # First call: get pending orders
        # Second call: update order to FILLED
        def mock_retry_side_effect(query_fn, *args, **kwargs):
            # Evaluate the lambda, but return mock data instead of calling actual supabase
            # To differentiate, we can check if it's select or update query
            return [mock_pending_order]
            
        mock_execute_retry.side_effect = mock_retry_side_effect
        
        # 2. Setup mock quote
        mock_fetch_quote.return_value = {
            "price": 595.0,
            "timestamp": "2022-11-03T13:30:00Z"
        }
        
        # Execute sync
        sync_sandbox_orders("2022-11-03")
        
        # 3. Assertions
        # Check that fetch_realtime_quote was called for 2330
        mock_fetch_quote.assert_called_once_with("2330")
        
        # Check that update_holding_after_fill is called for the sandbox (is_paper=True)
        mock_update_holding.assert_called_once_with(
            stock_code="2330",
            action="BUY",
            price=595.0,
            quantity=10.0,
            is_paper=True
        )
        
        # Check log output
        mock_log.assert_any_call(
            "INFO",
            " [模擬對帳成功] 訂單 ID 101 (2330) 於 2022-11-03 成交 | 成交價: 595.0 | 股數: 10.0"
        )

if __name__ == "__main__":
    unittest.main()
