# Path: src/scratch/tests/test_score_based_pricing_review.py
import unittest
from unittest.mock import patch, MagicMock

from src.services.health_check import calculate_buffered_order_price
from src.agents.monthly_review_agent import run_monthly_review
from src.services.trading_memory import get_active_skills_data, DEFAULT_TACTICAL_SKILLS, DEFAULT_ACTIVE_SKILLS_DATA


class TestScoreBasedPricingAndReview(unittest.TestCase):
    def test_default_skills_sell_discount_tiers(self):
        """驗證預設 Skills 具備三層依分數區別之讓價/防賤賣階梯"""
        sell_tiers = DEFAULT_TACTICAL_SKILLS["execution_skills"]["sell_discount_tiers"]
        self.assertIsInstance(sell_tiers, list)
        self.assertEqual(len(sell_tiers), 3)
        self.assertEqual(sell_tiers[0]["max_score"], 49)
        self.assertEqual(sell_tiers[0]["sell_discount_pct"], -0.015)
        self.assertEqual(sell_tiers[2]["max_score"], 100)
        self.assertEqual(sell_tiers[2]["sell_discount_pct"], -0.005)

    @patch("src.services.trading_memory.get_active_skills_data", return_value=DEFAULT_ACTIVE_SKILLS_DATA)
    def test_buy_chase_and_sell_anti_bargain_pricing(self, mock_skills):
        """驗證買進依信心追價 (+1.5%, +1.0%, +0.5%) 與賣出依風險防賤賣 (-1.5%, -1.0%, -0.5%)"""
        base_price = 200.0
        code = "2330"

        # 買進追價
        _, buf_high_buy, _ = calculate_buffered_order_price(base_price, code, "BUY", total_score=88)
        self.assertEqual(buf_high_buy, 0.015)

        _, buf_med_buy, _ = calculate_buffered_order_price(base_price, code, "BUY", total_score=75)
        self.assertEqual(buf_med_buy, 0.010)

        _, buf_low_buy, _ = calculate_buffered_order_price(base_price, code, "BUY", total_score=60)
        self.assertEqual(buf_low_buy, 0.005)

        # 賣出防賤賣
        _, buf_panic_sell, _ = calculate_buffered_order_price(base_price, code, "SELL", total_score=45)
        self.assertEqual(buf_panic_sell, -0.015)

        _, buf_std_sell, _ = calculate_buffered_order_price(base_price, code, "SELL", total_score=55)
        self.assertEqual(buf_std_sell, -0.010)

        _, buf_profit_sell, _ = calculate_buffered_order_price(base_price, code, "SELL", total_score=75)
        self.assertEqual(buf_profit_sell, -0.005)

        # 一鍵下車 / 緊急平倉 (最高優先級讓價變現)
        _, buf_liquidate, _ = calculate_buffered_order_price(base_price, code, "SELL", total_score=75, is_liquidate=True)
        self.assertEqual(buf_liquidate, -0.015)

    @patch("src.agents.monthly_review_agent.aggregate_monthly_data")
    @patch("src.agents.monthly_review_agent.supabase")
    def test_monthly_review_summary_contains_pricing_tiers(self, mock_sb, mock_agg):
        """驗證月度檢討 overall_summary 與報告中包含買進追價與賣出防賤賣階梯"""
        mock_agg.return_value = {
            "review_month": "2026-07",
            "daily_analysis_ids": [f"id-{i}" for i in range(12)],
            "daily_analysis_count": 12,
            "metrics": {
                "total_trades": 10,
                "win_rate": 70.0,
                "total_realized_pnl": 50000.0,
                "payoff_ratio": 2.5,
                "profit_factor": 2.1,
                "mean_upside_ratio": 0.08,
                "mean_drawdown_ratio": -0.03,
                "std_upside_ratio": 0.02,
                "std_drawdown_ratio": 0.01,
                "mean_slippage_ratio": 0.001,
                "std_slippage_ratio": 0.0005,
                "total_cancelled_orders": 1,
                "cancellation_rate_pct": 5.0,
                "avg_portfolio_entry_percentile": 42.5,
                "total_chasing_high_trades": 1,
                "total_late_entry_trades": 0
            },
            "per_stock_data": {
                "2330": {
                    "scores": [{"analysis_date": "2026-07-05", "total_score": 85}],
                    "price_range_ratio": 0.10,
                    "filled_orders": [],
                    "cancelled_orders": [],
                    "entry_timing_summary": {"avg_entry_percentile": 42.5, "chasing_high_count": 1, "late_entry_count": 0}
                }
            },
            "score_calibration": {"high_score_count": 5, "mid_score_count": 5, "low_score_count": 2}
        }
        mock_sb.table.return_value.insert.return_value.execute.return_value = MagicMock()

        # 模擬 Gemini 呼叫回傳合法 JSON
        def mock_call_gemini(prompt, **kwargs):
            if "量化研究總監" in prompt:
                return '{"indicator_summary": "指標正常", "indicator_skills": {"v_shape_reversal_patterns": [], "a_shape_top_warnings": [], "stock_specific_rules": [], "score_calibration_rules": [], "regime_indicator_rules": {}}}'
            elif "首席投資官" in prompt:
                return '{"cio_summary": "交易執行良好", "key_learnings": ["執行正常"], "execution_skills": {"min_buy_score": 65, "max_single_stock_weight": 4, "stop_loss_pct": -0.05, "take_profit_pct": 0.12, "chase_buffer_tiers": [{"min_score": 85, "buy_buffer_pct": 0.015, "description": "+1.5% 強勢追價"}, {"min_score": 70, "buy_buffer_pct": 0.010, "description": "+1.0% 標準追價"}, {"min_score": 0, "buy_buffer_pct": 0.005, "description": "+0.5% 溫和追價"}], "sell_discount_tiers": [{"max_score": 49, "sell_discount_pct": -0.015, "description": "-1.5% 斷尾求售"}, {"max_score": 69, "sell_discount_pct": -0.010, "description": "-1.0% 標準讓價"}, {"max_score": 100, "sell_discount_pct": -0.005, "description": "-0.5% 惜售防賤賣"}], "entry_timing_rules": ["防追高"], "regime_posture": {}, "tactical_rules": ["【規則優先權】鎖利優先"]}}'
            elif "技術指標與型態復盤專家" in prompt:
                return '{"stock_code": "2330", "indicator_retrospective": "指標捕捉良好", "anomaly_trait": null}'
            else:
                return '{"stock_code": "2330", "execution_retrospective": "執行良好"}'

        result = run_monthly_review(2026, 7, is_paper=False, call_gemini_fn=mock_call_gemini)
        overall_summary = result["overall_summary"]

        self.assertIn("動態定價階梯", overall_summary)
        self.assertIn("買進依信心追價", overall_summary)
        self.assertIn("賣出依風險防賤賣", overall_summary)
        self.assertIn(">=85分: +1.5%", overall_summary)
        self.assertIn("<=100分: -0.5%", overall_summary)


if __name__ == "__main__":
    unittest.main()
