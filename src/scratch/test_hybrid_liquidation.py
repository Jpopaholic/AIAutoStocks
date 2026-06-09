import os
import sys
import unittest
from unittest.mock import patch, MagicMock

# 載入專案路徑
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.agents.trading_agent import generate_portfolio_decisions
from src.services.supabase_client import (
    get_pending_liquidation_stocks,
    add_pending_liquidation_stock,
    remove_pending_liquidation_stock,
    get_system_fault_status,
    set_system_fault_status
)

class TestHybridLiquidation(unittest.TestCase):

    @patch("src.agents.trading_agent.get_system_fault_status")
    def test_generate_portfolio_decisions_fault_fallback(self, mock_get_fault):
        """
        測試當系統檢測到 FAULT 狀態時，決策生成是否直接阻斷並返回 HOLD 觀望決策
        """
        mock_get_fault.return_value = {"status": "FAULT", "detail": "Test connection timeout"}
        
        stock_codes = ["2330", "2454"]
        klines_map = {
            "2330": [{"date": "2026-06-09", "open": 600.0, "high": 600.0, "low": 600.0, "close": 600.0, "volume": 100.0}],
            "2454": [{"date": "2026-06-09", "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "volume": 100.0}]
        }
        current_holdings = []
        
        result = generate_portfolio_decisions(stock_codes, klines_map, current_holdings)
        
        self.assertIn("decisions", result)
        decisions = result["decisions"]
        self.assertEqual(len(decisions), 2)
        
        for d in decisions:
            self.assertEqual(d["action"], "HOLD")
            self.assertEqual(d["quantity"], 0.0)
            self.assertIn("SYSTEM FAULT", d["reason"])
            self.assertIn("Test connection timeout", d["reason"])

    @patch("src.agents.trading_agent.get_system_fault_status")
    @patch("src.agents.trading_agent.get_pending_liquidation_stocks")
    @patch("src.agents.trading_agent.call_gemini_with_rotation")
    def test_generate_portfolio_decisions_pending_stocks_override(self, mock_call_gemini, mock_get_pending, mock_get_fault):
        """
        測試當個股處於等候平倉名單時，若 AI 不慎產生 BUY 決策，是否能被強制校正為 HOLD 並且股數為 0
        """
        mock_get_fault.return_value = {"status": "OK", "detail": ""}
        mock_get_pending.return_value = ["2330"]  # 2330 處於智慧等候平倉排隊中
        
        # 模擬 Gemini 回傳了 2330 的 BUY 決策，2454 的 HOLD 決策
        mock_gemini_json = """{
            "decisions": [
                {
                    "stock_code": "2330",
                    "action": "BUY",
                    "price": 610.0,
                    "quantity": 100.0,
                    "confidence": 0.8,
                    "reason": "AI 決定買入"
                },
                {
                    "stock_code": "2454",
                    "action": "HOLD",
                    "price": 1000.0,
                    "quantity": 0.0,
                    "confidence": 0.5,
                    "reason": "AI 觀望"
                }
            ]
        }"""
        mock_call_gemini.return_value = mock_gemini_json
        
        stock_codes = ["2330", "2454"]
        klines_map = {
            "2330": [{"date": "2026-06-09", "open": 600.0, "high": 600.0, "low": 600.0, "close": 600.0, "volume": 100.0}],
            "2454": [{"date": "2026-06-09", "open": 1000.0, "high": 1000.0, "low": 1000.0, "close": 1000.0, "volume": 100.0}],
            "TAIEX": [{"date": "2026-06-09", "open": 16000.0, "high": 16000.0, "low": 16000.0, "close": 16000.0, "volume": 100.0}]
        }
        current_holdings = []
        
        result = generate_portfolio_decisions(stock_codes, klines_map, current_holdings)
        
        self.assertIn("decisions", result)
        decisions = result["decisions"]
        
        # 找到 2330 的決策
        decision_2330 = next(d for d in decisions if d["stock_code"] == "2330")
        decision_2454 = next(d for d in decisions if d["stock_code"] == "2454")
        
        # 驗證 2330 被強制校正為 HOLD
        self.assertEqual(decision_2330["action"], "HOLD")
        self.assertEqual(decision_2330["quantity"], 0.0)
        
        # 驗證 2454 保持 HOLD
        self.assertEqual(decision_2454["action"], "HOLD")
        self.assertEqual(decision_2454["quantity"], 0.0)

    @patch("src.services.supabase_client.get_db_config")
    @patch("src.services.supabase_client.set_db_config")
    def test_database_helper_functions(self, mock_set_config, mock_get_config):
        """
        驗證 Supabase client 中新增的平倉股票列表與系統狀態讀寫輔助函數之運作
        """
        # 模擬 get_db_config 回傳
        mock_get_config.return_value = {
            "PENDING_LIQUIDATION_STOCKS": "2330,2454",
            "SYSTEM_FAULT_STATUS": "FAULT:Login timeout error"
        }
        
        # 1. 測試讀取等候平倉列表
        stocks = get_pending_liquidation_stocks()
        self.assertEqual(stocks, ["2330", "2454"])
        
        # 2. 測試讀取系統故障狀態
        fault_state = get_system_fault_status()
        self.assertEqual(fault_state["status"], "FAULT")
        self.assertEqual(fault_state["detail"], "Login timeout error")
        
        # 3. 測試加入/移除等候平倉列表與設定故障狀態的調用
        # 此處僅驗證是否呼叫 set_db_config
        add_pending_liquidation_stock("2317")
        # 原列表有 2330,2454，再加入 2317
        mock_set_config.assert_any_call("PENDING_LIQUIDATION_STOCKS", "2330,2454,2317")
        
        set_system_fault_status("OK")
        mock_set_config.assert_any_call("SYSTEM_FAULT_STATUS", "OK")

if __name__ == "__main__":
    unittest.main()
