# Path: src/scratch/tests/test_dynamic_atr_risk.py
import unittest
from unittest.mock import patch, MagicMock
from src.services.technical_indicators import calculate_atr, compute_all_indicators
from src.agents.monthly_review_agent import ExecutionSkillsJSON
from src.services.trading_memory import DEFAULT_TACTICAL_SKILLS

class TestDynamicATRRisk(unittest.TestCase):
    def test_calculate_atr_standard(self):
        """驗證 calculate_atr 於 14 根以上 K 線之標準計算"""
        highs = [10.0 + i for i in range(20)]
        lows = [8.0 + i for i in range(20)]
        closes = [9.0 + i for i in range(20)]

        atr_series = calculate_atr(highs, lows, closes, period=14)
        self.assertEqual(len(atr_series), 20)
        # 前 13 根應為 None
        for i in range(13):
            self.assertIsNone(atr_series[i])
        # 第 14 根 (index 13) 及其後應有數值
        for i in range(13, 20):
            self.assertIsNotNone(atr_series[i])
            self.assertGreater(atr_series[i], 0.0)

    def test_calculate_atr_short_series(self):
        """驗證短於 14 根之 calculate_atr 回傳 [None]*n"""
        highs = [10.0, 11.0, 12.0]
        lows = [8.0, 9.0, 10.0]
        closes = [9.0, 10.0, 11.0]
        atr_series = calculate_atr(highs, lows, closes, period=14)
        self.assertEqual(atr_series, [None, None, None])

    def test_compute_all_indicators_contains_atr(self):
        """驗證 compute_all_indicators 包含 atr14 與 atr_pct"""
        klines = [
            {
                "date": f"2026-07-{i+1:02d}",
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 95.0 + i,
                "close": 102.0 + i,
                "volume": 1000 + i * 10
            }
            for i in range(25)
        ]
        res = compute_all_indicators(klines)
        self.assertEqual(len(res), 25)
        latest = res[-1]
        self.assertIn("atr14", latest)
        self.assertIn("atr_pct", latest)
        self.assertGreater(latest["atr14"], 0.0)
        self.assertGreater(latest["atr_pct"], 0.0)

    def test_execution_skills_json_atr_defaults(self):
        """驗證 ExecutionSkillsJSON 包含 stop_loss_atr_mult 與 take_profit_atr_mult"""
        data = {
            "min_buy_score": 65,
            "max_single_stock_weight": 4,
            "stop_loss_pct": -0.05,
            "take_profit_pct": 0.12,
            "stop_loss_atr_mult": 2.0,
            "take_profit_atr_mult": 3.5,
            "entry_timing_rules": ["防追高"],
            "regime_posture": {"BULLISH_TREND": "AGGRESSIVE"},
            "tactical_rules": ["停損優先"]
        }
        model = ExecutionSkillsJSON(**data)
        self.assertEqual(model.stop_loss_atr_mult, 2.0)
        self.assertEqual(model.take_profit_atr_mult, 3.5)

    def test_default_tactical_skills_atr_values(self):
        """驗證 DEFAULT_TACTICAL_SKILLS 包含 stop_loss_atr_mult 與 take_profit_atr_mult"""
        exec_skills = DEFAULT_TACTICAL_SKILLS["execution_skills"]
        self.assertIn("stop_loss_atr_mult", exec_skills)
        self.assertIn("take_profit_atr_mult", exec_skills)
        self.assertEqual(exec_skills["stop_loss_atr_mult"], 2.0)
        self.assertEqual(exec_skills["take_profit_atr_mult"], 3.5)

    def test_dynamic_atr_stop_calculations(self):
        """驗證個股 ATR 動態停損與動態停利點位數學計算"""
        avg_price = 1000.0  # 台積電
        atr_val = 25.0      # ATR 25 元 (日波幅 2.5%)
        sl_mult = 2.0
        tp_mult = 3.5

        dyn_sl_price = max(avg_price - sl_mult * atr_val, 0.0)
        dyn_sl_pct = ((dyn_sl_price - avg_price) / avg_price) * 100.0

        dyn_tp_price = avg_price + tp_mult * atr_val
        dyn_tp_pct = ((dyn_tp_price - avg_price) / avg_price) * 100.0

        # sl: 1000 - 50 = 950 (-5.0%)
        self.assertEqual(dyn_sl_price, 950.0)
        self.assertEqual(dyn_sl_pct, -5.0)

        # tp: 1000 + 87.5 = 1087.5 (+8.75%)
        self.assertEqual(dyn_tp_price, 1087.5)
        self.assertEqual(dyn_tp_pct, 8.75)

    def test_high_volatility_vs_low_volatility_tier(self):
        """驗證高波動標的 vs 低波動標的自適應風控差異"""
        # 高波動股 (如正2或高動能飆股)，股價 100，ATR 5.0 (ATR% = 5.0% >= 3.5% -> HIGH)
        high_vol_price = 100.0
        high_vol_atr = 5.0
        high_sl = high_vol_price - 2.0 * high_vol_atr # 90 (-10%)
        high_tp = high_vol_price + 3.5 * high_vol_atr # 117.5 (+17.5%)
        self.assertEqual(high_sl, 90.0)
        self.assertEqual(high_tp, 117.5)

        # 低波動股 (如牛皮防守股)，股價 50，ATR 0.6 (ATR% = 1.2% < 2.0% -> LOW)
        low_vol_price = 50.0
        low_vol_atr = 0.6
        low_sl = low_vol_price - 2.0 * low_vol_atr # 48.8 (-2.4%)
        low_tp = low_vol_price + 3.5 * low_vol_atr # 52.1 (+4.2%)
        self.assertAlmostEqual(low_sl, 48.8, places=2)
        self.assertAlmostEqual(low_tp, 52.1, places=2)

    def test_discord_notifier_atr_formatting(self):
        """驗證 Discord 通知中週期復盤報告包含 ATR 波動度與動態風控資訊"""
        from src.services.discord_notifier import send_periodic_review_notification

        review_data = {
            "review_month": "2026-07",
            "metrics": {
                "total_trades": 3, "win_rate": 66.7, "total_realized_pnl": 25000.0,
                "payoff_ratio": 2.1, "profit_factor": 1.8, "mean_upside_ratio": 0.08,
                "std_upside_ratio": 0.03, "mean_drawdown_ratio": -0.03, "std_drawdown_ratio": 0.01,
                "portfolio_mean_atr_pct": 2.8,
                "high_volatility_stock_count": 1,
                "normal_volatility_stock_count": 2,
                "low_volatility_stock_count": 0
            },
            "per_stock_data": {
                "2330": {
                    "atr14": 25.0,
                    "atr_pct": 2.6,
                    "volatility_tier": "NORMAL"
                }
            },
            "stock_execution_reports": [
                {
                    "stock_code": "2330",
                    "execution_retrospective": "2330 執行流暢",
                    "expected_upside_str": "+9.00% (±1.80%)",
                    "expected_drawdown_str": "-2.50% (±0.80%)",
                    "actual_pnl_str": "+20,000 元 (+4.50%) [1筆平倉]"
                }
            ],
            "stock_indicator_reports": [
                {
                    "stock_code": "2330",
                    "indicator_retrospective": "指標穩定"
                }
            ],
            "indicator_summary": "指標診斷良好",
            "cio_summary": "執行總評良好",
            "overall_summary": "【2026-07 月度戰術策略總結】\n• **下月風控門檻**：建議最低買入門檻 **65 分** | 動態 ATR 停損 **-2.0x ATR** | 動態 ATR 鎖利 **+3.5x ATR**",
            "key_learnings": ["嚴格執行 ATR 動態停損紀律"],
            "execution_skills": {
                "min_buy_score": 65,
                "max_single_stock_weight": 4,
                "stop_loss_pct": -0.05,
                "take_profit_pct": 0.12,
                "stop_loss_atr_mult": 2.0,
                "take_profit_atr_mult": 3.5,
                "tactical_rules": ["停損優先於續抱"]
            }
        }

        with patch("src.services.discord_notifier._send_discord_webhook") as mock_webhook:
            send_periodic_review_notification(review_data, review_type="月度")
            self.assertGreaterEqual(mock_webhook.call_count, 3)

            # 檢查 Layer 2 卡片有 ATR 波動度
            l2_call_payload = mock_webhook.call_args_list[2].args[1]
            l2_desc = l2_call_payload["embeds"][0]["description"]
            self.assertIn("ATR(14) 波動度", l2_desc)
            self.assertIn("25.0", l2_desc)

            # 檢查 Layer 3 附件 Markdown 內容
            file_args = None
            for call in mock_webhook.call_args_list:
                if "file_tuple" in call.kwargs and call.kwargs["file_tuple"]:
                    file_args = call.kwargs["file_tuple"]
            self.assertIsNotNone(file_args)
            md_content = file_args[1]
            self.assertIn("標的平均波動度 (ATR%)", md_content)
            self.assertIn("2.80%", md_content)
            self.assertIn("ATR 波動度", md_content)
            self.assertIn("2.6%", md_content)
            self.assertIn("-2.0x ATR", md_content)
            self.assertIn("+3.5x ATR", md_content)

if __name__ == "__main__":
    unittest.main()
