import os
import sys
import math
import unittest
from unittest.mock import patch, MagicMock

# Set mock environment variables before importing config to pass startup validation
os.environ["DISCORD_WEBHOOK_SANDBOX"] = "https://discord.com/api/webhooks/mock_sandbox"
os.environ["DISCORD_WEBHOOK_LIVE"] = "https://discord.com/api/webhooks/mock_live"

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.services.nav_calculator import get_today_remaining_limit
from src.agents.decision_agent import generate_portfolio_decisions

class TestRiskLimitAlignment(unittest.TestCase):

    @patch("src.services.sandbox_simulator.is_simulation_active")
    @patch("src.services.nav_calculator.get_dynamic_limits")
    @patch("src.services.supabase_client.get_orders")
    def test_get_today_remaining_limit_sandbox(self, mock_get_orders, mock_get_dynamic_limits, mock_is_simulation_active):
        """
        Verify remaining daily limit calculation in sandbox simulation mode.
        """
        mock_is_simulation_active.return_value = True
        mock_get_dynamic_limits.return_value = (20000.0, 100000.0) # (single, daily)
        
        # Mock placed BUY orders today totaling 40,000
        mock_get_orders.return_value = [
            {"action": "BUY", "total_amount": 15000.0, "status": "FILLED"},
            {"action": "BUY", "total_amount": 25000.0, "status": "FILLED"},
            {"action": "SELL", "total_amount": 30000.0, "status": "FILLED"},  # Should be ignored (SELL)
            {"action": "BUY", "total_amount": 50000.0, "status": "FAILED"}     # Should be ignored (FAILED)
        ]
        
        remaining = get_today_remaining_limit()
        # 100,000 daily limit - 40,000 today's buy = 60,000 remaining
        self.assertEqual(remaining, 60000.0)

    @patch("src.agents.decision_agent.call_gemini_with_rotation")
    @patch("src.services.nav_calculator.calculate_nav")
    @patch("src.services.nav_calculator.get_dynamic_limits")
    @patch("src.services.nav_calculator.get_today_remaining_limit")
    @patch("src.agents.decision_agent.get_pending_liquidation_stocks")
    def test_decision_agent_risk_multiplier(self, mock_get_pending, mock_get_today_remaining, mock_get_dynamic_limits, mock_calculate_nav, mock_call_gemini):
        """
        Verify that decision_agent respects the risk multiplier when allocating budgets.
        """
        # Mock NAV and cash
        # Cash = 100,000, Equity = 0, NAV = 100,000
        mock_calculate_nav.return_value = (100000.0, 0.0, 100000.0)
        
        # Base limits: single_limit = 20,000, daily_limit = 50,000
        mock_get_dynamic_limits.return_value = (20000.0, 50000.0)
        mock_get_today_remaining.return_value = 50000.0
        mock_get_pending.return_value = []
        
        # Mock Gemini response to recommend BUYing TSMC (2330) with 100% allocation
        mock_call_gemini.return_value = '{"ranking_analysis": "Test PM analysis.", "decisions": [{"stock_code": "2330", "action": "BUY", "pm_reason": "Strong breakout", "allocation_weight": 5}]}'
        
        # Stock price = 1000, total technical score = 80
        analyst_scores = [{
            "stock_code": "2330",
            "trend_score": 20,
            "momentum_score": 20,
            "volume_score": 20,
            "safety_score": 20,
            "regime_score": 0,
            "total_score": 80,
            "price": 1000.0,
            "reason": "Test analyst reason"
        }]
        
        # Regime assessment with risk multiplier = 0.5 (reducing single limit to 10,000)
        regime_assessment_neutral = {
            "regime": "BULLISH_TREND",
            "posture": "AGGRESSIVE",
            "risk_multiplier": 0.5
        }
        
        result = generate_portfolio_decisions(
            stock_codes=["2330"],
            analyst_scores=analyst_scores,
            klines_map={"2330": []},
            current_holdings=[],
            regime_assessment=regime_assessment_neutral,
            call_gemini_fn=mock_call_gemini
        )
        
        # Verify the decision was BUY
        decisions = result.get("decisions", [])
        self.assertEqual(len(decisions), 1)
        tsmc_decision = decisions[0]
        self.assertEqual(tsmc_decision["stock_code"], "2330")
        self.assertEqual(tsmc_decision["action"], "BUY")
        
        # The quantity should be limited to (single_limit * multiplier) / order_price
        # (20,000 * 0.5) / 1010.0 (including +1.0% price buffer for score=80) = 9 shares
        expected_qty = math.floor(10000.0 / tsmc_decision["price"])
        self.assertEqual(tsmc_decision["quantity"], expected_qty)

if __name__ == "__main__":
    unittest.main()
