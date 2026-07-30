# Path: src/scratch/tests/test_numeric_overflow_safeguards.py
import math
import unittest
from unittest.mock import patch, MagicMock

from src.config import safe_int, safe_float
from src.agents.analyst_agent import generate_analyst_assessments
from src.agents.decision_agent import generate_portfolio_decisions
from src.agents.regime_agent import generate_market_regime

class TestNumericOverflowSafeguards(unittest.TestCase):
    """
    測試數字溢位護欄，確保當 LLM 或 API 回傳巨大整數 (例如 10**350)、NaN 或無效數據時，
    不會觸發 'int too large to convert to float' 異常。
    """

    def test_safe_int_and_safe_float_basics(self):
        huge_int = 10**350
        
        # safe_int 測試
        self.assertEqual(safe_int(huge_int, default=10, min_val=0, max_val=20), 20)
        self.assertEqual(safe_int("not_a_number", default=5), 5)
        self.assertEqual(safe_int(None, default=7), 7)
        self.assertEqual(safe_int(-50, default=0, min_val=0, max_val=20), 0)

        # safe_float 測試
        self.assertEqual(safe_float(huge_int, default=0.5, min_val=0.0, max_val=1.0), 0.5)
        self.assertEqual(safe_float(float("nan"), default=1.0), 1.0)
        self.assertEqual(safe_float(float("inf"), default=1.0), 1.0)
        self.assertEqual(safe_float("123.45", default=0.0), 123.45)
        self.assertEqual(safe_float(None, default=10.0), 10.0)

    def test_analyst_agent_overflow_resilience(self):
        """測試分析師代理面對 LLM 回傳巨大整數時的抗性"""
        huge_int = 10**350
        mock_raw_response = f"""{{
            "stock_code": "2330",
            "trend_score": {huge_int},
            "momentum_score": {huge_int},
            "volume_score": 15,
            "safety_score": 15,
            "regime_score": 15,
            "confidence": {huge_int},
            "reason": "評分測試"
        }}"""

        klines_map = {
            "2330": [{"date": "2026-07-24", "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000}]
        }

        mock_call_gemini = MagicMock(return_value=mock_raw_response)

        # 執行評估，確保不會丟出 OverflowError: int too large to convert to float
        scores = generate_analyst_assessments(
            stock_codes=["2330"],
            klines_map=klines_map,
            call_gemini_fn=mock_call_gemini
        )

        self.assertEqual(len(scores), 1)
        res = scores[0]
        self.assertEqual(res["stock_code"], "2330")
        self.assertLessEqual(res["trend_score"], 20)
        self.assertLessEqual(res["momentum_score"], 20)
        self.assertLessEqual(res["total_score"], 100)
        self.assertLessEqual(res["confidence"], 1.0)

    def test_4500_digits_integer_conversion(self):
        """測試 4500 位數以上巨大整數字串 (如 LLM 幻覺 4365 位數) 不會觸發 Exceeds the limit (4300 digits)"""
        extreme_digits_str = "9" * 4500
        mock_raw_response = f"""{{
            "stock_code": "2449",
            "trend_score": {extreme_digits_str},
            "momentum_score": 15,
            "volume_score": 15,
            "safety_score": 15,
            "regime_score": 15,
            "confidence": 0.9,
            "reason": "4500位數測試"
        }}"""
        klines_map = {
            "2449": [{"date": "2026-07-24", "open": 100, "high": 105, "low": 99, "close": 104, "volume": 1000}]
        }
        mock_call_gemini = MagicMock(return_value=mock_raw_response)
        scores = generate_analyst_assessments(
            stock_codes=["2449"],
            klines_map=klines_map,
            call_gemini_fn=mock_call_gemini
        )
        self.assertEqual(len(scores), 1)
        self.assertEqual(scores[0]["stock_code"], "2449")
        self.assertEqual(scores[0]["trend_score"], 20)

    def test_decision_agent_overflow_resilience(self):
        """測試決策代理面對 LLM 回傳巨大整數與溢位權重時的抗性"""
        huge_int = 10**350
        mock_pm_response = f"""{{
            "ranking_analysis": "巨大數值測試",
            "decisions": [
                {{
                    "stock_code": "2330",
                    "action": "BUY",
                    "pm_reason": "買入測試",
                    "allocation_weight": {huge_int}
                }}
            ]
        }}"""

        analyst_scores = [
            {
                "stock_code": "2330",
                "trend_score": 18,
                "momentum_score": 18,
                "volume_score": 18,
                "safety_score": 18,
                "regime_score": 18,
                "total_score": 90,
                "price": 100.0,
                "reason": "強勢"
            }
        ]

        klines_map = {
            "2330": [{"date": "2026-07-24", "open": 100, "high": 105, "low": 99, "close": 100, "volume": 1000}]
        }

        mock_call_gemini = MagicMock(return_value=mock_pm_response)

        with patch("src.services.nav_calculator.calculate_nav", return_value=(500000.0, 0.0, 500000.0)), \
             patch("src.services.nav_calculator.get_dynamic_limits", return_value=(100000.0, 500000.0)), \
             patch("src.services.supabase_client.get_pending_liquidation_stocks", return_value=[]), \
             patch("src.services.supabase_client.get_orders", return_value=[]):
            
            result = generate_portfolio_decisions(
                stock_codes=["2330"],
                analyst_scores=analyst_scores,
                klines_map=klines_map,
                current_holdings=[],
                call_gemini_fn=mock_call_gemini
            )

            self.assertIn("decisions", result)
            self.assertEqual(len(result["decisions"]), 1)
            dec = result["decisions"][0]
            self.assertEqual(dec["stock_code"], "2330")
            self.assertEqual(dec["action"], "BUY")
            self.assertIsInstance(dec["confidence"], float)
            self.assertLessEqual(dec["confidence"], 1.0)

    def test_regime_agent_overflow_resilience(self):
        """測試大盤氣候診斷代理面對 LLM 回傳巨大數值時的抗性"""
        huge_int = 10**350
        mock_response = f"""{{
            "regime": "BULLISH_TREND",
            "posture": "AGGRESSIVE",
            "risk_multiplier": {huge_int},
            "reason": "多頭強勁"
        }}"""

        mock_call_gemini = MagicMock(return_value=mock_response)

        with patch("src.agents.regime_agent.call_gemini_with_rotation", mock_call_gemini):
            taiex_klines = [{"date": "2026-07-24", "open": 20000, "high": 20100, "low": 19900, "close": 20050, "volume": 100000}]
            res = generate_market_regime(taiex_klines)
            self.assertEqual(res["regime"], "BULLISH_TREND")
            self.assertLessEqual(res["risk_multiplier"], 1.0)
            self.assertGreaterEqual(res["risk_multiplier"], 0.15)

if __name__ == "__main__":
    unittest.main()
