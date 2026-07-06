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
        
        # 模擬兩次 Gemini 呼叫：第一次回傳分析師評估 (無 price, 無 total_score, 有 confidence)，第二次回傳經理人決策
        mock_call_gemini.side_effect = [
            # 呼叫 1: 分析師評估
            """{
                "scores": [
                    {
                        "stock_code": "2330",
                        "trend_score": 10,
                        "momentum_score": 10,
                        "volume_score": 10,
                        "safety_score": 10,
                        "regime_score": 10,
                        "confidence": 0.9,
                        "reason": "技術指標尚可"
                    },
                    {
                        "stock_code": "2454",
                        "trend_score": 12,
                        "momentum_score": 12,
                        "volume_score": 12,
                        "safety_score": 12,
                        "regime_score": 12,
                        "confidence": 0.85,
                        "reason": "均線呈現多頭形態"
                    }
                ]
            }""",
            # 呼叫 2: 投資組合經理決策
            """{
                "ranking_analysis": "2330較強但智慧平倉中，2454觀望",
                "decisions": [
                    {
                        "stock_code": "2330",
                        "action": "BUY",
                        "pm_reason": "PM建議買入2330（應被智慧平倉安全過濾強制校正）",
                        "allocation_weight": 3
                    },
                    {
                        "stock_code": "2454",
                        "action": "HOLD",
                        "pm_reason": "PM建議觀望2454",
                        "allocation_weight": 0
                    }
                ]
            }"""
        ]
        
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
        
        # 驗證 2330 被強制校正為 HOLD (因為無持股庫存且在等候平倉名單)
        self.assertEqual(decision_2330["action"], "HOLD")
        self.assertEqual(decision_2330["quantity"], 0.0)
        
        # 驗證 2454 保持 HOLD
        self.assertEqual(decision_2454["action"], "HOLD")
        self.assertEqual(decision_2454["quantity"], 0.0)

    @patch("src.agents.trading_agent.get_system_fault_status")
    @patch("src.agents.trading_agent.get_pending_liquidation_stocks")
    @patch("src.agents.trading_agent.call_gemini_with_rotation")
    @patch("src.services.nav_calculator.calculate_nav")
    def test_score_weighted_allocation(self, mock_calc_nav, mock_call_gemini, mock_get_pending, mock_get_fault):
        """
        測試融合量化評分與經理人權重的雙重比例加權分配邏輯。
        """
        from src.services.nav_calculator import clear_limits_cache
        clear_limits_cache()
        
        mock_get_fault.return_value = {"status": "OK", "detail": ""}
        mock_get_pending.return_value = []
        
        # 帳戶狀況：可用資金 20,000 元，NAV 500,000 元。
        # 則單股上限 single_limit = 500,000 * 3% = 15,000 元。
        # 可分配總預算 total_budget = min(20000, 100000) = 20,000 元。
        mock_calc_nav.return_value = (20000.0, 0.0, 500000.0)
        
        mock_call_gemini.side_effect = [
            # 1. 分析師技術分析評估：
            # 2330: 80 分，價格 600 元
            # 2454: 70 分，價格 1000 元
            """{
                "scores": [
                    {
                        "stock_code": "2330",
                        "trend_score": 16,
                        "momentum_score": 16,
                        "volume_score": 16,
                        "safety_score": 16,
                        "regime_score": 16,
                        "confidence": 0.9,
                        "reason": "多頭格局"
                    },
                    {
                        "stock_code": "2454",
                        "trend_score": 14,
                        "momentum_score": 14,
                        "volume_score": 14,
                        "safety_score": 14,
                        "regime_score": 14,
                        "confidence": 0.8,
                        "reason": "區間整理"
                    }
                ]
            }""",
            # 2. 投資組合經理決策：
            # 2330: BUY, allocation_weight = 5. 加權因子 = 80 * 5 = 400
            # 2454: BUY, allocation_weight = 3. 加權因子 = 70 * 3 = 210
            # 總權重因子 = 400 + 210 = 610.
            # 2330 分配比例 = 400 / 610 = 65.57%. 目標預算 = 20,000 * 65.57% = 13,114. Capped by single_limit = 15,000 (無超限)
            # 2454 分配比例 = 210 / 610 = 34.43%. 目標預算 = 20,000 * 34.43% = 6,885.
            # 初步股數：
            # 2330: floor(13,114 / 600) = 21 股。成本 = 12,600 元。
            # 2454: floor(6,885 / 1000) = 6 股。成本 = 6,000 元。
            # 總初步成本 = 18,600 元。
            # 剩餘零星預算 leftover = 20,000 - 18,600 = 1,400 元。
            # 依加權因子高到低（2330 優先於 2454）：
            # 2330 股價 600 元 <= 1400 元，且 12600 + 600 = 13200 <= 15000。
            # 因此 2330 追加 1 股，變為 22 股。剩餘 leftover = 800 元。
            # 2330 再追加 1 股，變為 23 股。剩餘 leftover = 200 元。
            # 2454 股價 1000 元 > 200 元，無法追加。
            # 最終結果：2330 買 23 股，2454 買 6 股。
            """{
                "ranking_analysis": "看好2330並給予高配置，2454中性偏多配置",
                "decisions": [
                    {
                        "stock_code": "2330",
                        "action": "BUY",
                        "pm_reason": "技術評分極高",
                        "allocation_weight": 5
                    },
                    {
                        "stock_code": "2454",
                        "action": "BUY",
                        "pm_reason": "評分中上",
                        "allocation_weight": 3
                    }
                ]
            }"""
        ]
        
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
        
        dec_2330 = next(d for d in decisions if d["stock_code"] == "2330")
        dec_2454 = next(d for d in decisions if d["stock_code"] == "2454")
        
        self.assertEqual(dec_2330["action"], "BUY")
        self.assertEqual(dec_2330["quantity"], 23.0)  # 21股 + leftover 追加 2股
        
        self.assertEqual(dec_2454["action"], "BUY")
        self.assertEqual(dec_2454["quantity"], 6.0)

    @patch("src.services.supabase_client.get_db_config")
    @patch("src.services.supabase_client.set_db_config")
    def test_database_helper_functions(self, mock_set_config, mock_get_config):
        """
        驗證 Supabase client 中新增的平倉股票列表與系統狀態讀寫輔支函數之運作
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
