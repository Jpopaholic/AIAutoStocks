import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.time_manager import get_previous_trading_day_str, get_report_lookback_range


class TestWeekendLookback(unittest.TestCase):
    def test_get_previous_trading_day_str(self):
        # 2026-09-14 is Monday -> previous trading day is 2026-09-11 (Friday)
        self.assertEqual(get_previous_trading_day_str("2026-09-14"), "2026-09-11")
        # 2026-09-15 is Tuesday -> previous trading day is 2026-09-14 (Monday)
        self.assertEqual(get_previous_trading_day_str("2026-09-15"), "2026-09-14")
        # 2026-09-11 is Friday -> previous trading day is 2026-09-10 (Thursday)
        self.assertEqual(get_previous_trading_day_str("2026-09-11"), "2026-09-10")
        # 2026-09-12 is Saturday -> previous trading day is 2026-09-11 (Friday)
        self.assertEqual(get_previous_trading_day_str("2026-09-12"), "2026-09-11")
        # 2026-09-13 is Sunday -> previous trading day is 2026-09-11 (Friday)
        self.assertEqual(get_previous_trading_day_str("2026-09-13"), "2026-09-11")

    def test_get_report_lookback_range_monday(self):
        # Monday: Should start from Friday 14:00 (UTC 06:00) to Monday 23:59:59 (UTC 15:59:59)
        start_utc, end_utc = get_report_lookback_range("2026-09-14")
        self.assertTrue(start_utc.startswith("2026-09-11T06:00:00"))
        self.assertTrue(end_utc.startswith("2026-09-14T15:59:59"))

    def test_get_report_lookback_range_tuesday(self):
        # Tuesday: Should start from Monday 14:00 (UTC 06:00) to Tuesday 23:59:59 (UTC 15:59:59)
        start_utc, end_utc = get_report_lookback_range("2026-09-15")
        self.assertTrue(start_utc.startswith("2026-09-14T06:00:00"))
        self.assertTrue(end_utc.startswith("2026-09-15T15:59:59"))

    def test_get_report_lookback_range_weekend(self):
        # Sunday: Should anchor to last active cycle (Thursday 14:00 -> Sunday 23:59:59)
        start_utc, end_utc = get_report_lookback_range("2026-09-13")
        self.assertTrue(start_utc.startswith("2026-09-10T06:00:00"))
        self.assertTrue(end_utc.startswith("2026-09-13T15:59:59"))

        # Saturday: Should anchor to last active cycle (Thursday 14:00 -> Saturday 23:59:59)
        start_utc, end_utc = get_report_lookback_range("2026-09-12")
        self.assertTrue(start_utc.startswith("2026-09-10T06:00:00"))
        self.assertTrue(end_utc.startswith("2026-09-12T15:59:59"))

    @patch("src.services.supabase_client.supabase")
    def test_get_report_lookback_range_typhoon_closure(self, mock_sb):
        # Scenario: Wednesday 07-22 & Thursday 07-23 were typhoon holidays in Taiwan (e.g. Typhoon Gaemi)
        # Market re-opens on Friday 07-24.
        # Prior trading day in daily_analysis is 2026-07-21 (Tuesday)
        mock_table = MagicMock()
        mock_select = MagicMock()
        mock_lt = MagicMock()
        mock_order = MagicMock()
        mock_limit = MagicMock()
        mock_limit.execute.return_value.data = [{"analysis_date": "2026-07-21"}]

        mock_sb.table.return_value = mock_table
        mock_table.select.return_value = mock_select
        mock_select.lt.return_value = mock_lt
        mock_lt.order.return_value = mock_order
        mock_order.limit.return_value = mock_limit

        # Friday 2026-07-24
        start_utc, end_utc = get_report_lookback_range("2026-07-24")
        # Start should dynamically anchor to Tuesday 07-21 14:00 (UTC 06:00) across the 2 typhoon days!
        self.assertTrue(start_utc.startswith("2026-07-21T06:00:00"))
        self.assertTrue(end_utc.startswith("2026-07-24T15:59:59"))

    @patch("src.services.broker_connector.config")
    @patch("src.services.broker_connector.delete_order_db")
    @patch("src.services.broker_connector.log_unfilled_order_db")
    @patch("src.services.broker_connector.update_holding_after_fill")
    @patch("src.services.broker_connector.update_order_status")
    @patch("src.services.broker_connector.get_pending_real_orders")
    @patch("src.services.broker_connector._get_shioaji_api")
    def test_sync_broker_orders_updates_executed_at(
        self, mock_get_api, mock_get_pending, mock_update_status, mock_update_holding, mock_log_unfilled, mock_delete_order, mock_config
    ):
        from src.services.broker_connector import sync_broker_orders

        mock_config.limits.is_paper_trading = False

        mock_order = {
            "id": 123,
            "order_id": "sj-123",
            "stock_code": "2330",
            "action": "SELL",
            "quantity": 1000,
            "price": 950.0,
            "status": "PENDING",
            "executed_at": "2026-09-11T15:30:00Z"
        }
        mock_get_pending.return_value = [mock_order]

        mock_trade = MagicMock()
        mock_trade.status.id = "sj-123"
        mock_trade.status.status = "Filled"
        deal_mock = MagicMock()
        deal_mock.quantity = 1000
        deal_mock.price = 955.0
        mock_trade.status.deals = [deal_mock]

        mock_api = MagicMock()
        mock_api.list_trades.return_value = [mock_trade]
        mock_get_api.return_value = mock_api

        with patch("src.services.broker_connector.get_holdings", return_value=[{"stock_code": "2330", "average_price": 900.0}]):
            sync_broker_orders()

        mock_update_status.assert_called_once()
        call_args = mock_update_status.call_args[0]
        order_db_id = call_args[0]
        updates = call_args[1]

        self.assertEqual(order_db_id, 123)
        self.assertEqual(updates["status"], "FILLED")
        self.assertEqual(updates["execution_price"], 955.0)
        self.assertIn("executed_at", updates)
        # Should be a valid ISO string ending with Z
        self.assertTrue(updates["executed_at"].endswith("Z"))

    @patch("src.services.discord_notifier._send_discord_webhook", return_value=True)
    @patch("src.services.discord_notifier.get_orders")
    @patch("src.services.discord_notifier.get_unfilled_orders", return_value=[])
    @patch("src.services.discord_notifier.get_holdings", return_value=[])
    @patch("src.services.nav_calculator.calculate_nav", return_value=(100000.0, 0.0, 100000.0))
    @patch("src.services.sandbox_simulator.is_simulation_active", return_value=False)
    @patch("src.time_manager.get_local_taiwan_date_str", return_value="2026-09-14")  # Monday
    def test_discord_notifier_uses_lookback_range(
        self, mock_date_str, mock_sim, mock_nav, mock_holdings, mock_unfilled, mock_get_orders, mock_send_webhook
    ):
        from src.services.discord_notifier import send_daily_report

        mock_order = {
            "id": 1,
            "stock_code": "3711",
            "action": "SELL",
            "price": 670.0,
            "execution_price": 670.0,
            "quantity": 10.0,
            "fee": 20.0,
            "total_amount": 6680.0,
            "realized_pnl": 500.0,
            "status": "FILLED",
            "executed_at": "2026-09-11T15:30:00Z"
        }
        mock_get_orders.return_value = [mock_order]

        send_daily_report(ai_outlook="測試週一發送報告")

        # Verify get_orders was called with the lookback range (start_date starting on Friday 2026-09-11)
        mock_get_orders.assert_called_once()
        _, kwargs = mock_get_orders.call_args
        self.assertTrue(kwargs["start_date"].startswith("2026-09-11T06:00:00"))
        self.assertTrue(kwargs["end_date"].startswith("2026-09-14T15:59:59"))

    @patch("src.services.discord_notifier._send_discord_webhook", return_value=True)
    @patch("src.services.discord_notifier.get_orders")
    @patch("src.services.discord_notifier.get_unfilled_orders", return_value=[])
    @patch("src.services.discord_notifier.get_holdings", return_value=[])
    @patch("src.services.nav_calculator.calculate_nav", return_value=(100000.0, 0.0, 100000.0))
    @patch("src.services.sandbox_simulator.is_simulation_active", return_value=False)
    @patch("src.time_manager.get_local_taiwan_date_str", return_value="2026-09-16")  # Wednesday
    @patch("src.services.supabase_client.supabase")
    def test_stale_executed_orders_filtered_out_on_next_day(
        self, mock_sb, mock_date_str, mock_sim, mock_nav, mock_holdings, mock_unfilled, mock_get_orders, mock_send_webhook
    ):
        from src.services.discord_notifier import send_daily_report

        # 模擬資料庫中前一次每日分析完成於 2026-09-15 15:05:10 (07:05:10 UTC)
        mock_table = MagicMock()
        mock_select = MagicMock()
        mock_lt = MagicMock()
        mock_eq = MagicMock()
        mock_order1 = MagicMock()
        mock_order2 = MagicMock()
        mock_limit = MagicMock()
        mock_limit.execute.return_value.data = [{
            "id": 113,
            "analysis_date": "2026-09-15",
            "created_at": "2026-09-15T07:05:10.374458+00:00"
        }]

        mock_sb.table.return_value = mock_table
        mock_table.select.return_value = mock_select
        mock_select.lt.return_value = mock_lt
        mock_lt.eq.return_value = mock_eq
        mock_eq.order.return_value = mock_order1
        mock_order1.order.return_value = mock_order2
        mock_order2.limit.return_value = mock_limit

        # 查詢回傳的訂單是前一天 (2026-09-15 07:00:10 UTC) 已成交項目
        stale_order = {
            "id": 400,
            "stock_code": "2891",
            "action": "BUY",
            "price": 68.3,
            "execution_price": 68.3,
            "quantity": 100.0,
            "fee": 20.0,
            "total_amount": 6850.0,
            "realized_pnl": 0.0,
            "status": "FILLED",
            "executed_at": "2026-09-15T07:00:10.250959+00:00"
        }
        mock_get_orders.return_value = [stale_order]

        send_daily_report(ai_outlook="今日全數觀望")

        # 驗證送出報告
        mock_send_webhook.assert_called_once()
        call_args = mock_send_webhook.call_args
        file_tuple = call_args[1].get("file_tuple")
        self.assertIsNotNone(file_tuple)
        report_filename, full_report_md, _ = file_tuple

        # 驗證前一日已成交之 2891 被準確排除，今日交易明細顯示無成交，今日實現損益為 0
        self.assertIn("今日無任何交易委託成交。", full_report_md)
        self.assertNotIn("買 2891", full_report_md)
        self.assertIn("今日實現損益**: **`+0`** 元", full_report_md)


if __name__ == "__main__":
    unittest.main()
