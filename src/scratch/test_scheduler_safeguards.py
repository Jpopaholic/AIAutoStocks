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

    @patch("src.services.sandbox_simulator.config")
    def test_is_simulation_active_safeguard(self, mock_config):
        """
        測試當 config 設為實盤模式時，is_simulation_active 必須強制回傳 False，且能自動重設內部狀態
        """
        from src.services.sandbox_simulator import is_simulation_active, set_simulation_mode
        
        # 設為模擬模式
        mock_config.limits.is_paper_trading = True
        set_simulation_mode(True)
        self.assertTrue(is_simulation_active())
        
        # 切換至實盤模式
        mock_config.limits.is_paper_trading = False
        self.assertFalse(is_simulation_active())
        
        # 再次切回模擬模式以驗證內部 _simulation_active 已被設為 False
        mock_config.limits.is_paper_trading = True
        self.assertFalse(is_simulation_active())

    @patch("src.main._run_sandbox_simulation_internal")
    def test_run_sandbox_simulation_finally_block(self, mock_run_internal):
        """
        測試 run_sandbox_simulation 無論正常結束或遭遇例外，皆會關閉模擬狀態 (finally block)
        """
        from src.main import run_sandbox_simulation
        from src.services.sandbox_simulator import set_simulation_mode, is_simulation_active
        
        # 1. 正常執行
        set_simulation_mode(True)
        run_sandbox_simulation(["2330"], "2026-06-01", "2026-06-02")
        self.assertFalse(is_simulation_active())
        
        # 2. 拋出例外
        set_simulation_mode(True)
        mock_run_internal.side_effect = Exception("Simulated error")
        with self.assertRaises(Exception):
            run_sandbox_simulation(["2330"], "2026-06-01", "2026-06-02")
        
        self.assertFalse(is_simulation_active())

    @patch("src.services.sandbox_simulator.stock_fetcher.fetch_stock_klines")
    @patch("src.services.sandbox_simulator.stock_fetcher.fetch_taiex_klines")
    @patch("src.services.sandbox_simulator.db_get_klines")
    @patch("src.services.supabase_client.save_stock_klines")
    def test_fetch_missing_klines_during_simulation(self, mock_save, mock_db_get, mock_fetch_taiex, mock_fetch_stock):
        """
        測試在模擬模式下，若資料庫中缺乏當日 K 線，會自動從網路抓取並儲存到資料庫
        """
        from src.services.sandbox_simulator import fetch_stock_klines, set_simulation_mode, _current_sim_date
        
        # 模擬開啟模擬模式
        set_simulation_mode(True)
        
        # 1. 模擬資料庫缺乏 `_current_sim_date` 的資料
        mock_db_get.return_value = [
            {"stock_code": "2330", "date": "2026-04-30", "open": 500, "high": 500, "low": 500, "close": 500, "volume": 100}
        ]
        mock_fetch_stock.return_value = [
            {"stockCode": "2330", "date": _current_sim_date, "open": 510, "high": 515, "low": 508, "close": 512, "volume": 200}
        ]
        
        # 呼叫獲取 K 線
        fetch_stock_klines("2330")
        
        # 驗證是否從網路抓取，且有儲存至資料庫
        mock_fetch_stock.assert_called_once()
        mock_save.assert_called_once()
        
        # 2. 測試 TAIEX 情況下抓取大盤 API
        mock_save.reset_mock()
        mock_db_get.return_value = [
            {"stock_code": "TAIEX", "date": "2026-04-30", "open": 16000, "high": 16000, "low": 16000, "close": 16000, "volume": 0}
        ]
        mock_fetch_taiex.return_value = [
            {"stockCode": "TAIEX", "date": _current_sim_date, "open": 16100, "high": 16150, "low": 16080, "close": 16120, "volume": 0}
        ]
        
        fetch_stock_klines("TAIEX")
        mock_fetch_taiex.assert_called_once()
        mock_save.assert_called_once()

    @patch("src.main.supabase_client.get_stock_klines")
    @patch("src.main.supabase_client.save_stock_klines")
    @patch("src.services.stock_fetcher.fetch_stock_klines")
    @patch("src.services.stock_fetcher.fetch_taiex_klines")
    @patch("src.services.sandbox_simulator.initialize_simulation")
    def test_run_sandbox_simulation_prefetch(self, mock_init, mock_fetch_taiex, mock_fetch_stock, mock_save, mock_get_klines):
        """
        測試沙盒模擬啟動時的預抓取邏輯。
        當資料庫缺乏區間內的資料時，應呼叫 stock_fetcher 抓取並儲存。
        """
        from src.main import _run_sandbox_simulation_internal
        
        # 1. 模擬資料庫缺乏 2026-04 月的資料 (回傳 K 線很少，代表不完整)
        # 第一個呼叫是 get_stock_klines("2330")，第二個是 get_stock_klines("TAIEX")
        mock_get_klines.side_effect = [
            # 第一次檢查：資料庫中 2330 只有 1 筆在 2026-04 的資料 (小於 3 筆，觸發預抓)
            [{"stock_code": "2330", "date": "2026-04-01", "open": 500, "high": 500, "low": 500, "close": 500, "volume": 100}],
            # 第二次檢查：資料庫中 TAIEX 有 0 筆在 2026-04 的資料 (小於 3 筆，觸發預抓)
            [],
            # 重新載入時的回傳
            [{"stock_code": "2330", "date": "2026-04-01", "open": 500, "high": 500, "low": 500, "close": 500, "volume": 100},
             {"stock_code": "2330", "date": "2026-04-02", "open": 505, "high": 505, "low": 505, "close": 505, "volume": 100}]
        ]
        
        # 模擬 fetcher 回傳
        mock_fetch_stock.return_value = [
            {"stockCode": "2330", "date": "2026-04-02", "open": 505, "high": 505, "low": 505, "close": 505, "volume": 100}
        ]
        mock_fetch_taiex.return_value = [
            {"stockCode": "TAIEX", "date": "2026-04-02", "open": 16000, "high": 16000, "low": 16000, "close": 16000, "volume": 0}
        ]
        
        # 為了不真正跑完 while 循環，Mock sandbox_simulator.is_simulation_active 回傳 False
        with patch("src.services.sandbox_simulator.is_simulation_active", return_value=False):
            _run_sandbox_simulation_internal(["2330"], "2026-04-01", "2026-04-05")
            
        # 驗證 fetchers 被呼叫
        mock_fetch_stock.assert_called_with("2330", "20260401")
        mock_fetch_taiex.assert_called_with("20260401")
        # 驗證有儲存到資料庫
        self.assertEqual(mock_save.call_count, 2)

    @patch("src.main.supabase_client.get_holdings")
    @patch("src.main.supabase_client.get_stock_klines")
    @patch("src.main.supabase_client.save_stock_klines")
    @patch("src.services.stock_fetcher.fetch_stock_klines")
    @patch("src.services.stock_fetcher.fetch_taiex_klines")
    @patch("src.main.config")
    @patch("src.main.supabase_client.log_system_event")
    def test_run_live_trading_job_backfill(self, mock_log, mock_config, mock_fetch_taiex, mock_fetch_stock, mock_save, mock_get_klines, mock_get_holdings):
        """
        測試實盤自動交易中，當資料庫歷史 K 線筆數小於 80 筆時，會觸發回溯補建邏輯。
        """
        from src.main import run_live_trading_job
        from datetime import datetime
        
        # 0. 模擬持股為空
        mock_get_holdings.return_value = []
        
        # 1. 模擬自動交易已開啟
        mock_config.is_auto_trading_active = True
        
        # 2. 模擬今日日期 (並非週末)
        tw_now = datetime(2026, 6, 10, 14, 0, 0)
        
        # 3. 模擬基準股 2330 已經有今日最新資料 (不跳過休市自檢)
        # 第一個 call 是休市自檢 fetch_stock_klines("2330")
        mock_fetch_stock.return_value = [
            {"stockCode": "2330", "date": "2026-06-10", "open": 610.0, "high": 610.0, "low": 610.0, "close": 610.0, "volume": 100}
        ]
        
        # 4. 模擬資料庫獲取 K 線
        # 依序呼叫 get_stock_klines:
        # - get_stock_klines("TAIEX", limit=120) -> 回傳 10 筆 (少於 80，觸發 TAIEX 補建)
        # - get_stock_klines("2330", limit=120) -> 回傳 20 筆 (少於 80，觸發 2330 補建)
        # - get_stock_klines("2330", limit=100) -> 載入完整 100 筆
        # - get_stock_klines("TAIEX", limit=100) -> 載入 TAIEX 100 筆
        mock_get_klines.side_effect = [
            [{"stock_code": "TAIEX", "date": "2026-06-10", "open": 16000.0, "high": 16000.0, "low": 16000.0, "close": 16000.0, "volume": 0}] * 10,
            [{"stock_code": "2330", "date": "2026-06-10", "open": 610.0, "high": 610.0, "low": 610.0, "close": 610.0, "volume": 100}] * 20,
            [{"stock_code": "2330", "date": "2026-06-10", "open": 610.0, "high": 610.0, "low": 610.0, "close": 610.0, "volume": 100}] * 100,
            [{"stock_code": "TAIEX", "date": "2026-06-10", "open": 16000.0, "high": 16000.0, "low": 16000.0, "close": 16000.0, "volume": 0}] * 100
        ]
        
        # 5. 執行實盤交易排程
        # 為了避免進入後續真實決策/網路 API 呼叫，Mock trading_agent.generate_portfolio_decisions
        with patch("src.main.get_taiwan_time", return_value=tw_now), \
             patch("src.agents.trading_agent.generate_portfolio_decisions") as mock_decision, \
             patch("src.agents.regime_agent.generate_market_regime") as mock_regime:
             
            mock_regime.return_value = {"regime": "BULLISH", "posture": "LONG", "risk_multiplier": 1.0}
            run_live_trading_job(["2330"])
            
        # 驗證補建：TAIEX 應呼叫 fetch_taiex_klines 共 5 次，2330 應呼叫 fetch_stock_klines 共 5 次 (排除第一次的休市自檢)
        self.assertEqual(mock_fetch_taiex.call_count, 5)
        # 第一個 call 是休市自檢，後面應再呼叫 5 次進行補抓 (共 6 次)
        self.assertEqual(mock_fetch_stock.call_count, 6)
        # 驗證 save_stock_klines 寫入資料庫
        mock_save.assert_called()

if __name__ == "__main__":
    unittest.main()


