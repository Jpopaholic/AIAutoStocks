# Path: src/scratch/tests/test_quarterly_review.py
import unittest
from unittest.mock import patch, MagicMock
from datetime import date, datetime
import json

from src.services.quarterly_aggregator import (
    get_quarterly_review_date_range,
    resolve_manual_review_quarter,
    check_quarterly_review_threshold,
    aggregate_quarterly_data
)
from src.agents.quarterly_review_agent import run_quarterly_review
from src.services.discord_notifier import send_quarterly_review_notification, send_periodic_review_notification
from src.config import config


class TestQuarterlyReview(unittest.TestCase):

    def test_quarterly_review_date_ranges(self):
        """測試無縫 3 格週六復盤日跨度計算"""
        # Q1: 涵蓋 1, 2, 3 月
        start_q1, end_q1, months_q1 = get_quarterly_review_date_range(2026, 1)
        self.assertEqual(months_q1, ["2026-01", "2026-02", "2026-03"])
        self.assertEqual(start_q1.weekday(), 5) # 必須為週六
        self.assertEqual(end_q1.weekday(), 5)   # 必須為週六

        # Q2: 涵蓋 4, 5, 6 月，起點為 Q1 終點 (無縫銜接)
        start_q2, end_q2, months_q2 = get_quarterly_review_date_range(2026, 2)
        self.assertEqual(start_q2, end_q1) # 無縫銜接！
        self.assertEqual(months_q2, ["2026-04", "2026-05", "2026-06"])
        self.assertEqual(end_q2.weekday(), 5)

        # Q3: 涵蓋 7, 8, 9 月，起點為 Q2 終點
        start_q3, end_q3, months_q3 = get_quarterly_review_date_range(2026, 3)
        self.assertEqual(start_q3, end_q2) # 無縫銜接！
        self.assertEqual(months_q3, ["2026-07", "2026-08", "2026-09"])

        # Q4: 涵蓋 10, 11, 12 月，起點為 Q3 終點
        start_q4, end_q4, months_q4 = get_quarterly_review_date_range(2026, 4)
        self.assertEqual(start_q4, end_q3) # 無縫銜接！
        self.assertEqual(months_q4, ["2026-10", "2026-11", "2026-12"])

    def test_resolve_manual_review_quarter_parsing(self):
        """測試季度字串解析能力"""
        self.assertEqual(resolve_manual_review_quarter("2026-Q3"), (2026, 3))
        self.assertEqual(resolve_manual_review_quarter("2026-3"), (2026, 3))
        self.assertEqual(resolve_manual_review_quarter("2026Q2"), (2026, 2))
        self.assertEqual(resolve_manual_review_quarter("2025-Q4"), (2025, 4))

    @patch("src.services.quarterly_aggregator.supabase")
    def test_threshold_check_insufficient_months(self, mock_sb):
        """測試門檻規範：3 個月份中若有缺漏，門檻未過且回報缺漏月份"""
        # 模擬 monthly_skills 只有 7 月與 8 月，缺少 9 月
        mock_res = MagicMock()
        mock_res.data = [
            {"review_month": "2026-07", "skills": {}, "daily_analysis_count": 20},
            {"review_month": "2026-08", "skills": {}, "daily_analysis_count": 21}
        ]
        mock_sb.table().select().eq().in_().order().execute.return_value = mock_res

        months = ["2026-07", "2026-08", "2026-09"]
        threshold_met, records, missing = check_quarterly_review_threshold(months, is_paper=False)
        self.assertFalse(threshold_met)
        self.assertEqual(missing, ["2026-09"])
        self.assertEqual(len(records), 2)

    @patch("src.services.quarterly_aggregator.supabase")
    def test_threshold_check_all_months_present(self, mock_sb):
        """測試門檻規範：3 個月份皆具備時通過門檻"""
        mock_res = MagicMock()
        mock_res.data = [
            {"review_month": "2026-07", "skills": {}, "daily_analysis_count": 20},
            {"review_month": "2026-08", "skills": {}, "daily_analysis_count": 21},
            {"review_month": "2026-09", "skills": {}, "daily_analysis_count": 22}
        ]
        mock_sb.table().select().eq().in_().order().execute.return_value = mock_res

        months = ["2026-07", "2026-08", "2026-09"]
        threshold_met, records, missing = check_quarterly_review_threshold(months, is_paper=False)
        self.assertTrue(threshold_met)
        self.assertEqual(missing, [])
        self.assertEqual(len(records), 3)

    @patch("src.agents.quarterly_review_agent.aggregate_quarterly_data")
    def test_run_quarterly_review_skips_when_threshold_not_met(self, mock_agg):
        """測試門檻不足時 run_quarterly_review 安全跳過不呼叫 LLM"""
        mock_agg.return_value = {
            "review_quarter": "2026-Q3",
            "quarter": 3,
            "year": 2026,
            "months_included": ["2026-07", "2026-08", "2026-09"],
            "threshold_met": False,
            "missing_months": ["2026-09"],
            "date_range": {"start_date": "2026-06-27", "end_date": "2026-09-26"},
            "metrics": {},
            "per_stock_data": {}
        }

        mock_llm = MagicMock()
        res = run_quarterly_review(2026, 3, is_paper=False, call_gemini_fn=mock_llm)

        self.assertTrue(res["skipped"])
        self.assertIn("未全數具備月檢討紀錄", res["message"])
        self.assertIn("2026-09", res["message"])
        mock_llm.assert_not_called()

    @patch("src.agents.quarterly_review_agent.aggregate_quarterly_data")
    @patch("src.agents.quarterly_review_agent.supabase")
    def test_run_quarterly_review_success_and_saves_to_quarterly_skills(self, mock_sb, mock_agg):
        """測試季度檢討成功執行，產出月度技能成效回顧，且寫入 quarterly_skills 表（不寫入 monthly_skills）"""
        mock_agg.return_value = {
            "review_quarter": "2026-Q3",
            "quarter": 3,
            "year": 2026,
            "months_included": ["2026-07", "2026-08", "2026-09"],
            "threshold_met": True,
            "missing_months": [],
            "date_range": {"start_date": "2026-06-27", "end_date": "2026-09-26"},
            "daily_analysis_count": 62,
            "metrics": {
                "total_trades": 12,
                "win_rate": 66.7,
                "total_realized_pnl": 58000.0,
                "payoff_ratio": 2.3,
                "profit_factor": 2.1,
                "mean_upside_ratio": 0.058,
                "std_upside_ratio": 0.02,
                "mean_drawdown_ratio": -0.022,
                "std_drawdown_ratio": 0.01,
                "bullish_days_count": 35,
                "defensive_days_count": 12,
                "cancellation_rate_pct": 5.0,
                "mean_slippage_ratio": 0.001,
                "total_cancelled_orders": 2,
                "avg_portfolio_entry_percentile": 42.0,
                "total_chasing_high_trades": 1,
                "total_late_entry_trades": 0,
                "portfolio_mean_atr_pct": 2.4
            },
            "score_calibration": {
                "high_score_count": 45,
                "mid_score_count": 60,
                "low_score_count": 15
            },
            "per_stock_data": {
                "2330": {
                    "stock_code": "2330",
                    "scores": [{"analysis_date": "2026-07-01", "total_score": 75}],
                    "filled_orders": [{"action": "BUY", "price": 950}],
                    "cancelled_orders": [],
                    "price_range_ratio": 0.15,
                    "atr14": 18.5,
                    "atr_pct": 1.9,
                    "volatility_tier": "NORMAL",
                    "expected_upside_str": "+6.2% (±1.5%)",
                    "expected_drawdown_str": "-2.1% (±0.8%)",
                    "actual_pnl_str": "+25000元",
                    "entry_timing_summary": {"avg_entry_percentile": 40.0, "chasing_high_count": 0, "late_entry_count": 0}
                }
            },
            "monthly_skills_trajectory": [
                {
                    "review_month": "2026-07",
                    "daily_analysis_count": 21,
                    "skills": {
                        "execution_skills": {"min_buy_score": 65, "stop_loss_pct": -0.05, "stop_loss_atr_mult": 2.0},
                        "indicator_skills": {"v_shape_reversal_patterns": [{"pattern_rule": "7月V轉", "expected_probability_pct": 80}]}
                    }
                },
                {
                    "review_month": "2026-08",
                    "daily_analysis_count": 21,
                    "skills": {
                        "execution_skills": {"min_buy_score": 70, "stop_loss_pct": -0.05, "stop_loss_atr_mult": 2.2},
                        "indicator_skills": {"v_shape_reversal_patterns": [{"pattern_rule": "8月V轉", "expected_probability_pct": 82}]}
                    }
                },
                {
                    "review_month": "2026-09",
                    "daily_analysis_count": 20,
                    "skills": {
                        "execution_skills": {"min_buy_score": 68, "stop_loss_pct": -0.05, "stop_loss_atr_mult": 2.0},
                        "indicator_skills": {"v_shape_reversal_patterns": [{"pattern_rule": "9月V轉", "expected_probability_pct": 85}]}
                    }
                }
            ]
        }

        # Mock LLM 回傳 JSON
        def mock_llm_call(prompt: str, model_name: str, generation_config: dict) -> str:
            if "技術指標與型態復盤專家" in prompt:
                return json.dumps({
                    "stock_code": "2330",
                    "indicator_retrospective": "2330 季度指標與大盤共振良好。",
                    "anomaly_trait": None
                })
            elif "量化研究總監" in prompt:
                return json.dumps({
                    "indicator_summary": "全季技術指標體系表現優異。",
                    "indicator_skills": {
                        "v_shape_reversal_patterns": [{"pattern_rule": "量能黃金交叉強彈", "expected_probability_pct": 85}],
                        "a_shape_top_warnings": [{"pattern_rule": "高檔背離長黑", "expected_probability_pct": 88}],
                        "stock_specific_rules": [],
                        "score_calibration_rules": [{"calibration_rule": "維持評分標準", "expected_probability_pct": 90}],
                        "regime_indicator_rules": {
                            "BULLISH_TREND": {"focus": "動能選股", "expected_probability_pct": 85},
                            "BEARISH_TREND": {"focus": "底線支撐", "expected_probability_pct": 90}
                        }
                    }
                })
            elif "交易執行與部位風控分析師" in prompt:
                return json.dumps({
                    "stock_code": "2330",
                    "execution_retrospective": "交易執行精準，無追高問題。"
                })
            elif "首席投資官" in prompt:
                # Layer 2 Reduce (CIO 組合執行總評)
                return json.dumps({
                    "cio_summary": "全季組合績效穩健，實現 58000 元獲利，盈虧比 2.3。",
                    "key_learnings": ["嚴守風控紀律", "落實動態停損"],
                    "execution_skills": {
                        "min_buy_score": 68,
                        "max_single_stock_weight": 4,
                        "stop_loss_pct": -0.05,
                        "take_profit_pct": 0.12,
                        "stop_loss_atr_mult": 2.0,
                        "take_profit_atr_mult": 3.5,
                        "chase_buffer_tiers": [
                            {"min_score": 85, "buy_buffer_pct": 0.015, "description": "+1.5% 追價"}
                        ],
                        "sell_discount_tiers": [
                            {"max_score": 49, "sell_discount_pct": -0.015, "description": "-1.5% 求售"}
                        ],
                        "entry_timing_rules": ["避開前 20% 高檔"],
                        "regime_posture": {"BULLISH_TREND": "AGGRESSIVE", "BEARISH_TREND": "DEFENSIVE"},
                        "tactical_rules": ["【規則優先權】鎖利與停損優先於續抱"]
                    }
                })
            elif "量化策略審查委員會" in prompt:
                # Layer 3 專屬：月度技能演化與超參數深度復盤
                return json.dumps({
                    "trajectory_summary": "7月至9月月度技能從65分微調至70分再回穩至68分，成功在大盤回檔期防守並在反彈期掌握行情，未顯現過度擬合問題。",
                    "overfitting_verdict": "未顯現過度擬合 (Overfitting)，各月門檻調整均維持在可控區間內。",
                    "monthly_adjustments_verdict": ["7月調高門檻有效降噪", "8月加強停損乘數成功避開下行"],
                    "rule_conflict_resolutions": ["明確規範鎖利與停損條款優先於個股高分續抱哲學"],
                    "strategic_takeaways": ["跨月調整應以季度趨勢為基準"],
                    "refined_execution_skills": {
                        "min_buy_score": 68,
                        "max_single_stock_weight": 4,
                        "stop_loss_pct": -0.05,
                        "take_profit_pct": 0.12,
                        "stop_loss_atr_mult": 2.0,
                        "take_profit_atr_mult": 3.5,
                        "chase_buffer_tiers": [
                            {"min_score": 85, "buy_buffer_pct": 0.015, "description": "+1.5% 追價"}
                        ],
                        "sell_discount_tiers": [
                            {"max_score": 49, "sell_discount_pct": -0.015, "description": "-1.5% 求售"}
                        ],
                        "entry_timing_rules": ["避開前 20% 高檔"],
                        "regime_posture": {"BULLISH_TREND": "AGGRESSIVE", "BEARISH_TREND": "DEFENSIVE"},
                        "tactical_rules": ["【規則優先權】鎖利與停損優先於續抱"]
                    }
                })
            return "{}"

        result = run_quarterly_review(2026, 3, is_paper=False, call_gemini_fn=mock_llm_call)

        self.assertFalse(result.get("skipped", False))
        self.assertEqual(result["review_quarter"], "2026-Q3")
        self.assertIn("monthly_skills_retrospective", result)
        self.assertIn("未顯現過度擬合", result["monthly_skills_retrospective"]["trajectory_summary"])

        # 驗證資料庫寫入：必須寫入 quarterly_skills，且絕對不寫入 monthly_skills
        table_calls = [c[0][0] for c in mock_sb.table.call_args_list]
        self.assertIn("quarterly_skills", table_calls)
        self.assertNotIn("monthly_skills", table_calls)

    def test_discord_webhook_strictly_enforced_for_quarterly(self):
        """測試使用者指定要求：季度復盤必須強制配置 DISCORD_WEBHOOK_QUARTERLY_REVIEW，未配置時直接報錯 (ValueError)！"""
        fake_quarterly_result = {
            "review_quarter": "2026-Q3",
            "period": "2026-Q3",
            "metrics": {},
            "indicator_summary": "指標診斷",
            "cio_summary": "執行總評",
            "overall_summary": "總結",
            "key_learnings": [],
            "indicator_skills": {},
            "execution_skills": {}
        }

        # 模擬未配置季度 Webhook (使用 dataclasses.replace 替換 frozen dataclass)
        import dataclasses
        mock_discord = dataclasses.replace(config.discord, webhook_quarterly_review="")
        with patch.object(config, "discord", mock_discord):
            with self.assertRaises(ValueError) as ctx:
                send_quarterly_review_notification(fake_quarterly_result)
            self.assertIn("未配置 DISCORD_WEBHOOK_QUARTERLY_REVIEW", str(ctx.exception))
            self.assertIn("強制配置專屬 Webhook", str(ctx.exception))


    def test_skills_merge_quarterly_precedence(self):
        """測試核心規則：當季與月 skills 矛盾衝突時，以季 skills 為主！"""
        from src.services.trading_memory import merge_monthly_and_quarterly_skills, get_active_skills_data, get_active_skills_context

        monthly_sample = {
            "version": "2026-08",
            "indicator_skills": {
                "regime_indicator_rules": {"BULLISH_TREND": {"focus": "月度動能", "expected_probability_pct": 80}},
                "score_calibration_rules": [{"calibration_rule": "月度校正", "expected_probability_pct": 85}],
                "stock_specific_rules": [{"stock_code": "2330", "anomaly_trait": "月度特性"}],
                "v_shape_reversal_patterns": [{"pattern_rule": "月V轉", "expected_probability_pct": 75}]
            },
            "execution_skills": {
                "min_buy_score": 65,
                "max_single_stock_weight": 3,
                "stop_loss_pct": -0.05,
                "take_profit_pct": 0.10,
                "stop_loss_atr_mult": 2.0,
                "take_profit_atr_mult": 3.0,
                "chase_buffer_tiers": [{"min_score": 80, "buy_buffer_pct": 0.01}],
                "sell_discount_tiers": [{"max_score": 50, "sell_discount_pct": -0.01}],
                "regime_posture": {"BULLISH_TREND": "AGGRESSIVE", "BEARISH_TREND": "DEFENSIVE"},
                "tactical_rules": ["【月度】標準分批進場"],
                "entry_timing_rules": ["【月度】避開高檔"]
            }
        }

        quarterly_sample = {
            "version": "2026-Q3",
            "indicator_skills": {
                "regime_indicator_rules": {"BULLISH_TREND": {"focus": "季度宏觀突破", "expected_probability_pct": 88}},
                "score_calibration_rules": [{"calibration_rule": "季度宏觀門檻嚴格化", "expected_probability_pct": 92}],
                "stock_specific_rules": [{"stock_code": "2330", "anomaly_trait": "季度長線拉回年線V轉"}],
                "v_shape_reversal_patterns": [{"pattern_rule": "季V轉爆量長紅", "expected_probability_pct": 88}]
            },
            "execution_skills": {
                "min_buy_score": 72,      # 衝突：月 65 vs 季 72 ➔ 季勝出
                "max_single_stock_weight": 4, # 衝突：月 3 vs 季 4 ➔ 季勝出
                "stop_loss_pct": -0.04,   # 衝突：月 -5% vs 季 -4% ➔ 季勝出
                "take_profit_pct": 0.15,  # 衝突：月 10% vs 季 15% ➔ 季勝出
                "stop_loss_atr_mult": 2.5,# 衝突：月 2.0x vs 季 2.5x ➔ 季勝出
                "take_profit_atr_mult": 4.0, # 衝突：月 3.0x vs 季 4.0x ➔ 季勝出
                "chase_buffer_tiers": [{"min_score": 85, "buy_buffer_pct": 0.015}], # 季覆蓋
                "sell_discount_tiers": [{"max_score": 45, "sell_discount_pct": -0.015}], # 季覆蓋
                "regime_posture": {"BEARISH_TREND": "ULTRA_DEFENSIVE"}, # 衝突：月 DEFENSIVE vs 季 ULTRA ➔ 季覆蓋
                "tactical_rules": ["【季度最高準則】動態鎖利絕對優先於高分續抱"],
                "entry_timing_rules": ["【季度】前20%位階嚴禁追價"]
            }
        }

        merged = merge_monthly_and_quarterly_skills(monthly_sample, quarterly_sample)
        merged_exec = merged["execution_skills"]
        merged_ind = merged["indicator_skills"]

        # 驗證執行參數：季度完全覆蓋衝突值
        self.assertEqual(merged_exec["min_buy_score"], 72)
        self.assertEqual(merged_exec["max_single_stock_weight"], 4)
        self.assertEqual(merged_exec["stop_loss_pct"], -0.04)
        self.assertEqual(merged_exec["take_profit_pct"], 0.15)
        self.assertEqual(merged_exec["stop_loss_atr_mult"], 2.5)
        self.assertEqual(merged_exec["take_profit_atr_mult"], 4.0)
        self.assertEqual(merged_exec["chase_buffer_tiers"], [{"min_score": 85, "buy_buffer_pct": 0.015}])
        self.assertEqual(merged_exec["sell_discount_tiers"], [{"max_score": 45, "sell_discount_pct": -0.015}])

        # 驗證大盤姿態：季覆蓋衝突 key，並保留未衝突 key
        self.assertEqual(merged_exec["regime_posture"]["BEARISH_TREND"], "ULTRA_DEFENSIVE")
        self.assertEqual(merged_exec["regime_posture"]["BULLISH_TREND"], "AGGRESSIVE")

        # 驗證規則列表：季度規則前置為最高優先權
        self.assertEqual(merged_exec["tactical_rules"][0], "【季度最高準則】動態鎖利絕對優先於高分續抱")
        self.assertIn("【月度】標準分批進場", merged_exec["tactical_rules"])

        # 驗證指標技能：同標的以季為準
        stock_traits = {s["stock_code"]: s["anomaly_trait"] for s in merged_ind["stock_specific_rules"]}
        self.assertEqual(stock_traits["2330"], "季度長線拉回年線V轉")
        self.assertEqual(merged_ind["regime_indicator_rules"]["BULLISH_TREND"]["focus"], "季度宏觀突破")

        # 驗證 Context 字串帶有「以季 Skills 為主導」聲明
        with patch("src.services.trading_memory.get_active_skills_data") as mock_active:
            mock_active.return_value = {
                "review_month": "2026-08",
                "review_quarter": "2026-Q3",
                "skills": merged
            }
            ctx = get_active_skills_context(is_paper=False)
            self.assertIn("以「季 Skills」為主導最高準則", ctx)
            self.assertIn("72", ctx)


if __name__ == "__main__":
    unittest.main()
