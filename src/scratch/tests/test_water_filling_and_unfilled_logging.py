import unittest
from unittest.mock import patch, MagicMock
import json

from src.agents.decision_agent import generate_portfolio_decisions
from src.services.discord_notifier import send_daily_report

class TestWaterFillingAndUnfilledLogging(unittest.TestCase):
    @patch("src.agents.decision_agent.call_gemini_with_rotation")
    @patch("src.agents.decision_agent.get_pending_liquidation_stocks")
    @patch("src.agents.decision_agent.get_orders")
    @patch("src.services.nav_calculator.calculate_nav")
    @patch("src.services.nav_calculator.get_dynamic_limits")
    @patch("src.services.nav_calculator.get_today_remaining_limit")
    def test_water_filling_never_exceeds_cash(
        self,
        mock_get_today_remaining,
        mock_get_dynamic,
        mock_calc_nav,
        mock_get_orders,
        mock_get_pending,
        mock_call_gemini
    ):
        """
        驗證水箱分配演算法在可用現金僅有 6273.3 元時，
        產出的所有買單總金額 (包含手續費) 絕不會超過可用現金。
        """
        mock_calc_nav.return_value = (6273.3, 29488.5, 35761.8)
        mock_get_dynamic.return_value = (9387.47, 28609.44)
        mock_get_today_remaining.return_value = 28609.44
        mock_get_pending.return_value = []
        mock_get_orders.return_value = []

        # 模擬 LLM 經理人對 2379, 00947, 2891 皆給予買入建議
        llm_response = {
            "ranking_analysis": "市場多頭整理，重點佈局優質個股。",
            "decisions": [
                {"stock_code": "2379", "action": "BUY", "allocation_weight": 5, "reason": "看好動能與均線多頭"},
                {"stock_code": "00947", "action": "BUY", "allocation_weight": 3, "reason": "反彈修復且位置安全"},
                {"stock_code": "2891", "action": "BUY", "allocation_weight": 2, "reason": "金融股防禦兼具"}
            ]
        }
        mock_call_gemini.return_value = json.dumps(llm_response)

        analyst_scores = [
            {"stock_code": "2379", "total_score": 76, "trend_score": 18, "momentum_score": 18, "volume_score": 7, "safety_score": 15, "regime_score": 18, "price": 772.0, "reason": "強勢"},
            {"stock_code": "00947", "total_score": 75, "trend_score": 16, "momentum_score": 18, "volume_score": 10, "safety_score": 14, "regime_score": 17, "price": 36.71, "reason": "穩健"},
            {"stock_code": "2891", "total_score": 76, "trend_score": 16, "momentum_score": 17, "volume_score": 12, "safety_score": 14, "regime_score": 17, "price": 66.8, "reason": "防禦"}
        ]
        regime_assessment = {
            "regime": "REBOUND_BULL",
            "posture": "MODERATE_ATTACK",
            "risk_multiplier": 0.75
        }

        res = generate_portfolio_decisions(
            stock_codes=["2379", "00947", "2891"],
            analyst_scores=analyst_scores,
            klines_map={},
            current_holdings=[],
            regime_assessment=regime_assessment,
            call_gemini_fn=mock_call_gemini
        )

        decisions = res.get("decisions", [])
        buy_decisions = [d for d in decisions if d.get("action") == "BUY"]
        
        total_proposed_cost = 0.0
        for b in buy_decisions:
            price = b["price"]
            qty = b["quantity"]
            cost = price * qty
            total_proposed_cost += cost

        # 斷言：買單股票金額總和必不大於 6273.3 元
        self.assertLessEqual(total_proposed_cost, 6273.3)

    @patch("src.services.discord_notifier.get_orders")
    @patch("src.services.discord_notifier.get_unfilled_orders")
    @patch("src.services.nav_calculator.calculate_nav")
    @patch("src.services.discord_notifier._send_discord_webhook")
    def test_unfilled_custom_reason_formatting(
        self,
        mock_send_webhook,
        mock_calc_nav,
        mock_get_unfilled,
        mock_get_orders
    ):
        """
        驗證當 unfilled_orders 存在自訂攔截與失敗原因時， send_daily_report 能正確顯示詳細原因
        """
        mock_calc_nav.return_value = (6273.3, 29488.5, 35761.8)
        mock_get_orders.return_value = []
        mock_get_unfilled.return_value = [
            {
                "stock_code": "00947",
                "action": "BUY",
                "price": 37.08,
                "quantity": 170.0,
                "reason": "下單前安全審查攔截: 委託總額超出單筆限額"
            }
        ]
        mock_send_webhook.return_value = True

        send_daily_report(ai_outlook="測試報告內容")

        # 檢視發送至 webhook 的 payload 中是否有傳入詳細攔截原因
        self.assertTrue(mock_send_webhook.called)
        first_call_args = mock_send_webhook.call_args_list[0]
        payload = first_call_args[0][1]
        fields = payload["embeds"][0]["fields"]

        unfilled_field = next((f for f in fields if any(k in f["name"] for k in ("未成交", "攔截"))), None)
        self.assertIsNotNone(unfilled_field)
        self.assertIn("00947", unfilled_field["value"])
        self.assertIn("下單前安全審查攔截: 委託總額超出單筆限額", unfilled_field["value"])

    @patch("src.agents.decision_agent.call_gemini_with_rotation")
    @patch("src.agents.decision_agent.get_pending_liquidation_stocks")
    @patch("src.agents.decision_agent.get_orders")
    @patch("src.services.nav_calculator.calculate_nav")
    @patch("src.services.nav_calculator.get_dynamic_limits")
    @patch("src.services.nav_calculator.get_today_remaining_limit")
    def test_fee_buffer_prevents_negative_cash(
        self,
        mock_get_today_remaining,
        mock_get_dynamic,
        mock_calc_nav,
        mock_get_orders,
        mock_get_pending,
        mock_call_gemini
    ):
        """
        重現今日案例：可用現金為 12,628.13 元，委託價 143.5 元。
        舊邏輯會買 floor(12628.13 / 143.5) = 88 股 (12,628 元)，加上手續費 20 元變成 12,648 元導致現金變成 -19.87 元。
        新邏輯必須預留手續費緩衝，買入 87 股，總支出 (含手續費) 絕不超過 12,628.13 元。
        """
        cash = 12628.13
        mock_calc_nav.return_value = (cash, 20000.0, cash + 20000.0)
        mock_get_dynamic.return_value = (50000.0, 50000.0)
        mock_get_today_remaining.return_value = 50000.0
        mock_get_pending.return_value = []
        mock_get_orders.return_value = []

        llm_response = {
            "ranking_analysis": "買入 2303",
            "decisions": [
                {"stock_code": "2303", "action": "BUY", "allocation_weight": 5, "reason": "突破多頭"}
            ]
        }
        mock_call_gemini.return_value = json.dumps(llm_response)

        analyst_scores = [
            {"stock_code": "2303", "total_score": 85, "trend_score": 18, "momentum_score": 18, "volume_score": 15, "safety_score": 16, "regime_score": 18, "price": 143.5, "reason": "突破"}
        ]
        regime_assessment = {"regime": "REBOUND_BULL", "posture": "MODERATE_ATTACK", "risk_multiplier": 1.0}

        res = generate_portfolio_decisions(
            stock_codes=["2303"],
            analyst_scores=analyst_scores,
            klines_map={},
            current_holdings=[],
            regime_assessment=regime_assessment,
            call_gemini_fn=mock_call_gemini
        )

        decisions = res.get("decisions", [])
        buy_2303 = next((d for d in decisions if d["stock_code"] == "2303" and d["action"] == "BUY"), None)
        self.assertIsNotNone(buy_2303)
        
        # 88 股純股價 12,628 元 + 手續費 20 元 = 12,648 元 > 12,628.13 元
        # 正確防違約股數必須是 <= 87 股
        self.assertLessEqual(buy_2303["quantity"], 87.0)
        
        # 總支出（股價 + 估算手續費）必須 <= 12628.13
        stock_cost = buy_2303["price"] * buy_2303["quantity"]
        fee = max(20.0, round(stock_cost * 0.001425 * 0.6))
        total_cost = stock_cost + fee
        self.assertLessEqual(total_cost, cash)

    @patch("src.agents.decision_agent.call_gemini_with_rotation")
    @patch("src.agents.decision_agent.get_pending_liquidation_stocks")
    @patch("src.agents.decision_agent.get_orders")
    @patch("src.services.nav_calculator.calculate_nav")
    @patch("src.services.nav_calculator.get_dynamic_limits")
    @patch("src.services.nav_calculator.get_today_remaining_limit")
    def test_pre_water_filling_insufficient_cash_blocks_immediately(
        self,
        mock_get_today_remaining,
        mock_get_dynamic,
        mock_calc_nav,
        mock_get_orders,
        mock_get_pending,
        mock_call_gemini
    ):
        """
        驗證在執行水桶演算法前，若可用現金 (例如 100 元) 扣除手續費後連最便宜的 1 股 (例如 150+20=170 元) 都買不起，
        系統會在進入水桶演算法前直接提前認知並安全阻斷，產出明確的防違約交割觀望理由。
        """
        cash = 100.0  # 僅剩 100 元
        mock_calc_nav.return_value = (cash, 20000.0, cash + 20000.0)
        mock_get_dynamic.return_value = (50000.0, 50000.0)
        mock_get_today_remaining.return_value = 50000.0
        mock_get_pending.return_value = []
        mock_get_orders.return_value = []

        llm_response = {
            "ranking_analysis": "買入 2303 與 2330",
            "decisions": [
                {"stock_code": "2303", "action": "BUY", "allocation_weight": 5, "reason": "看好"},
                {"stock_code": "2330", "action": "BUY", "allocation_weight": 5, "reason": "龍頭"}
            ]
        }
        mock_call_gemini.return_value = json.dumps(llm_response)

        analyst_scores = [
            {"stock_code": "2303", "total_score": 85, "trend_score": 18, "momentum_score": 18, "volume_score": 15, "safety_score": 16, "regime_score": 18, "price": 143.5, "reason": "看好"},
            {"stock_code": "2330", "total_score": 90, "trend_score": 18, "momentum_score": 18, "volume_score": 15, "safety_score": 16, "regime_score": 18, "price": 950.0, "reason": "龍頭"}
        ]
        regime_assessment = {"regime": "REBOUND_BULL", "posture": "MODERATE_ATTACK", "risk_multiplier": 1.0}

        res = generate_portfolio_decisions(
            stock_codes=["2303", "2330"],
            analyst_scores=analyst_scores,
            klines_map={},
            current_holdings=[],
            regime_assessment=regime_assessment,
            call_gemini_fn=mock_call_gemini
        )

        decisions = res.get("decisions", [])
        # 所有決策必須全部轉為 HOLD，買入數量為 0
        for d in decisions:
            self.assertEqual(d["action"], "HOLD")
            self.assertEqual(d["quantity"], 0.0)
            self.assertIn("風控防違約交割觀望", d["reason"])
            self.assertIn("不足以買入 1 股", d["reason"])

if __name__ == "__main__":
    unittest.main()
