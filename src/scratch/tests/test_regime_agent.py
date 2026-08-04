# Path: src/scratch/test_regime_agent.py
import unittest
from unittest.mock import patch

from src.agents.regime_agent import generate_market_regime

class TestRegimeAgent(unittest.TestCase):
    def test_generate_market_regime_empty(self):
        """
        測試大盤 K 線空值時，回退到預設正常狀態
        """
        res = generate_market_regime([])
        self.assertEqual(res["regime"], "CALM_RANGE")
        self.assertEqual(res["posture"], "CHOPPY_TACTICAL")
        self.assertEqual(res["risk_multiplier"], 0.7)
        self.assertEqual(res["target_cash_ratio"], 0.35)
        self.assertIn("PULLBACK", res["allowed_buy_styles"])
        self.assertIn("無可用的大盤 K 線數據", res["reason"])

    @patch("src.agents.regime_agent.call_gemini_with_rotation")
    def test_generate_market_regime_mocked(self, mock_call):
        """
        測試模擬 Gemini 成功回傳結果的情境
        """
        mock_response = (
            '{"regime": "STRONG_BULL", "posture": "STRONG_ATTACK", '
            '"risk_multiplier": 0.95, "target_cash_ratio": 0.10, '
            '"allowed_buy_styles": ["BREAKOUT", "PULLBACK"], '
            '"tactical_directive": "積極強攻，放大倉位。", '
            '"reason": "大盤收盤價站在 MA20 之上，多頭強勢。"}'
        )
        mock_call.return_value = mock_response

        dummy_klines = [
            {"date": "2026-06-01", "open": 20000.0, "high": 20100.0, "low": 19900.0, "close": 20050.0, "volume": 300000000}
        ]
        
        res = generate_market_regime(dummy_klines)
        self.assertEqual(res["regime"], "STRONG_BULL")
        self.assertEqual(res["posture"], "STRONG_ATTACK")
        self.assertEqual(res["risk_multiplier"], 0.95)
        self.assertEqual(res["target_cash_ratio"], 0.10)
        self.assertIn("BREAKOUT", res["allowed_buy_styles"])
        self.assertEqual(res["tactical_directive"], "積極強攻，放大倉位。")
        self.assertEqual(res["reason"], "大盤收盤價站在 MA20 之上，多頭強勢。")

