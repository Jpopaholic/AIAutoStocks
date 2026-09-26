# Path: src/scratch/tests/test_holiday_safeguard.py
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from src.services.market_calendar import is_market_closed, is_trading_day
from src.services.stock_fetcher import _extract_snap_date, fetch_stock_klines, fetch_taiex_klines
from src.main import run_live_trading_job

class TestHolidaySafeguard(unittest.TestCase):
    def test_market_calendar_identification(self):
        """驗證國定假日與週末休市日曆判別"""
        # 中秋節 (2026-09-25, 週五)
        closed, reason = is_market_closed("2026-09-25")
        self.assertTrue(closed)
        self.assertEqual(reason, "中秋節")
        self.assertFalse(is_trading_day("2026-09-25"))

        # 前一日 (2026-09-24, 週四) 正常交易日
        closed, _ = is_market_closed("2026-09-24")
        self.assertFalse(closed)
        self.assertTrue(is_trading_day("2026-09-24"))

        # 週六 (2026-09-26) 週末休市
        closed, reason = is_market_closed("2026-09-26")
        self.assertTrue(closed)
        self.assertIn("週末", reason)

        # 國慶日連假 (2026-10-09, 週五)
        closed, reason = is_market_closed("2026-10-09")
        self.assertTrue(closed)
        self.assertIn("國慶日", reason)

    def test_extract_snap_date(self):
        """驗證從 Shioaji Snapshot ts 解析撮合時間"""
        mock_snap = MagicMock()
        # 1790260200000000000 奈秒 -> 2026-09-24 22:30:00+08:00
        mock_snap.ts = 1790260200000000000
        snap_date = _extract_snap_date(mock_snap)
        self.assertEqual(snap_date, "2026-09-24")

        # 無 ts 或異常
        mock_snap.ts = 0
        self.assertEqual(_extract_snap_date(mock_snap), "")

    @patch("src.services.stock_fetcher.get_local_taiwan_date_str", return_value="2026-09-25")
    @patch("src.services.stock_fetcher.fetch_realtime_quote")
    def test_klines_no_autofill_on_holiday(self, mock_realtime_quote, mock_date_str):
        """驗證在休市日，fetch_stock_klines 絕不會自動補建今日假 K 線"""
        # 假定資料庫中最新 K 線為前一天 2026-09-24
        existing_klines = [
            {"stockCode": "2330", "date": "2026-09-24", "open": 2480.0, "high": 2490.0, "low": 2470.0, "close": 2475.0, "volume": 10000}
        ]
        
        with patch("src.services.supabase_client.get_batch_stock_klines", return_value={"2330": existing_klines}):
            klines = fetch_stock_klines("2330", date_str="20260925")
            # 驗證 klines 中最新一筆仍是 2026-09-24，並未被補建成 2026-09-25
            self.assertEqual(klines[-1]["date"], "2026-09-24")
            # 並且沒有呼叫即時報價進行補建
            mock_realtime_quote.assert_not_called()

    @patch("src.main.supabase_client.log_system_event")
    @patch("src.main.config")
    def test_run_live_trading_job_blocks_mid_autumn_holiday(self, mock_config, mock_log):
        """驗證 run_live_trading_job 在中秋節 2026-09-25 觸發時會被成功攔截並退出"""
        mock_config.is_auto_trading_active = True
        mid_autumn_time = datetime(2026, 9, 25, 16, 0, 0)

        with patch("src.main.get_taiwan_time", return_value=mid_autumn_time):
            run_live_trading_job(["2330"])

            # 驗證日誌中有記錄中秋節休市並跳過
            logged_messages = [args[1] for args, _ in mock_log.call_args_list if len(args) > 1]
            self.assertTrue(any("中秋節" in msg and "休市日" in msg for msg in logged_messages))
