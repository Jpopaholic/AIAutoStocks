import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from fastapi import HTTPException

# 載入專案路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import src.web_server as web_server
from src.main import run_live_trading_job
from src.config import config

class TestSchedulerSafeguards(unittest.TestCase):

    def setUp(self):
        # 重置 global state
        web_server.is_running = False
        web_server.stop_requested = False

    def test_api_update_config_lock_when_running(self):
        """
        測試當交易任務執行中，禁止修改交易模式 (PAPER_TRADING_MODE)
        """
        web_server.is_running = True
        
        # 模擬修改其他無關的設定（應該允許）
        # 先 Mock supabase set_db_config 避免拋出網路錯誤
        with patch("src.web_server.set_db_config") as mock_set_db:
            payload_ok = web_server.ConfigUpdate(settings={"GEMINI_MODEL": "gemini-2.0-flash"})
            res = web_server.api_update_config(payload_ok)
            self.assertEqual(res["status"], "ok")
            mock_set_db.assert_called()

        # 模擬修改 PAPER_TRADING_MODE 為與當前不同的值（應該拋出 400 錯誤）
        current_paper_mode = config.limits.is_paper_trading
        new_paper_mode_str = "false" if current_paper_mode else "true"
        
        payload_fail = web_server.ConfigUpdate(settings={"PAPER_TRADING_MODE": new_paper_mode_str})
        with self.assertRaises(HTTPException) as ctx:
            web_server.api_update_config(payload_fail)
        
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("交易任務正在執行中，禁止修改交易模式", ctx.exception.detail)

    @patch("src.web_server.get_db_watchlist")
    @patch("src.web_server.get_holdings")
    @patch("src.services.supabase_client.log_system_event")
    @patch("src.main.run_live_trading_job")
    def test_thread_safeguard_exit_on_mode_change(self, mock_run_live, mock_log, mock_holdings, mock_watchlist):
        """
        測試當背景永動機執行中，若交易模式突然被動態竄改，執行緒是否會主動退出以防誤傷
        """
        mock_watchlist.return_value = ["2330"]
        mock_holdings.return_value = []
        
        # 模擬啟動時為實盤模式 (is_paper_trading = False)
        # 我們需要 Mock config.limits.is_paper_trading 的 getter
        # 透過 patch.object Mock 唯讀 property 比較繁瑣，我們可以直接 mock config 物件
        original_limits = config.limits
        mock_limits = MagicMock()
        mock_limits.is_paper_trading = False
        
        with patch.object(config, "limits", mock_limits):
            # 為了測試 while 循環自檢，我們讓 time.sleep 立刻返回
            with patch("time.sleep") as mock_sleep:
                # 第一次執行前，檢查是正常的。進入循環後，我們將 is_paper_trading 竄改為 True
                # 這會讓 while 循環中的自檢失敗並 break
                def side_effect(*args, **kwargs):
                    mock_limits.is_paper_trading = True
                
                mock_run_live.side_effect = side_effect
                
                # 呼叫背景運算
                web_server.run_trading_job_in_background()
                
                # 驗證是否因為偵測到竄改而中斷，且 last_run_status 更新為錯誤狀態
                self.assertIn("錯誤：運行中交易模式被修改，已強制停止", web_server.last_run_status)
                mock_log.assert_any_call("ERROR", unittest.mock.ANY)

    @patch("src.services.stock_fetcher.fetch_stock_klines")
    @patch("src.main.supabase_client.log_system_event")
    @patch("src.services.broker_connector.sync_broker_orders")
    @patch("src.main.config")
    def test_run_live_trading_job_holiday_skip(self, mock_config, mock_sync, mock_log, mock_fetch_klines):
        """
        測試當今日無最新交易數據時（最新為昨日），是否會被識別為休市/節假日並跳過交易
        """
        from datetime import datetime
        # 模擬今天日期為 2026-06-10
        tw_now = datetime(2026, 6, 10, 14, 0, 0)
        
        # 模擬自動交易為啟用狀態
        mock_config.is_auto_trading_active = True
        
        # 情況 A：回傳的最新 K 線是昨天 2026-06-09 (代表今天休市)
        mock_fetch_klines.return_value = [
            {"date": "2026-06-08", "open": 600.0, "high": 600.0, "low": 600.0, "close": 600.0, "volume": 100},
            {"date": "2026-06-09", "open": 610.0, "high": 610.0, "low": 610.0, "close": 610.0, "volume": 100}
        ]
        
        # 執行任務 (傳入空代號列表避免後續流程報錯)
        with patch("src.main.get_taiwan_time", return_value=tw_now):
            run_live_trading_job([])
            
            # 驗證日誌中有輸出跳過休市任務的訊息，且沒有執行後續動作
            print("MOCK LOG CALLS:", mock_log.call_args_list)
            any_skip_log = any(len(args) > 1 and "自動跳過今日任務" in args[1] for args, kwargs in mock_log.call_args_list)
            self.assertTrue(any_skip_log)

if __name__ == "__main__":
    unittest.main()
