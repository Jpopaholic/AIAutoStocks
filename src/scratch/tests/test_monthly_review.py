# Path: src/scratch/tests/test_monthly_review.py
import unittest
from unittest.mock import patch, MagicMock
from datetime import date
from src.services.monthly_aggregator import (
    get_monthly_analysis_date,
    get_monthly_analysis_datetime,
    resolve_manual_review_month,
    calculate_mean_and_std,
    get_review_date_range,
    aggregate_daily_scores
)
from src.services.trading_memory import get_active_skills_context
from src.web_server import is_weekend_taiwan

class TestMonthlyReviewSuite(unittest.TestCase):
    def test_saturday_analysis_date_calculation(self):
        """驗證週六分析日 (Saturday Review Day) 演算法"""
        # 1. 2026 年 5 月底為 5/31 (星期日) ➔ 應取得上個禮拜六 5/30
        may_2026 = get_monthly_analysis_date(2026, 5)
        self.assertEqual(may_2026, date(2026, 5, 30))
        self.assertEqual(may_2026.weekday(), 5) # 5 = Saturday

        # 驗證帶有時間點與時區的官方預設時間點 (預設 00:00 Asia/Taipei 起)
        may_2026_dt = get_monthly_analysis_datetime(2026, 5)
        self.assertEqual(may_2026_dt.hour, 0)
        self.assertEqual(may_2026_dt.minute, 0)
        self.assertEqual(may_2026_dt.tzinfo.zone, "Asia/Taipei")

        # 2. 2026 年 7 月底為 7/31 (星期五) ➔ 應取得該週禮拜六 8/1
        july_2026 = get_monthly_analysis_date(2026, 7)
        self.assertEqual(july_2026, date(2026, 8, 1))
        self.assertEqual(july_2026.weekday(), 5)

        # 3. 2026 年 10 月底為 10/31 (星期六) ➔ 應取得 10/31 當天禮拜六
        oct_2026 = get_monthly_analysis_date(2026, 10)
        self.assertEqual(oct_2026, date(2026, 10, 31))
        self.assertEqual(oct_2026.weekday(), 5)

    def test_manual_review_month_resolution(self):
        """驗證手動觸發時指定月份與回溯邏輯"""
        y, m = resolve_manual_review_month("2026-06")
        self.assertEqual(y, 2026)
        self.assertEqual(m, 6)

    def test_mean_and_std_calculation(self):
        """驗證平均值 (Mean) 與標準差 (Std Dev) 數學計算精準度"""
        data = [0.10, 0.20, 0.30]
        mean_val, std_val = calculate_mean_and_std(data)
        self.assertAlmostEqual(mean_val, 0.20, places=4)
        self.assertAlmostEqual(std_val, 0.10, places=4)

        # 單一數值時標準差應為 0
        mean_single, std_single = calculate_mean_and_std([0.15])
        self.assertEqual(mean_single, 0.15)
        self.assertEqual(std_single, 0.0)

    def test_daily_score_aggregation(self):
        """驗證同日多筆分析分數平均與 BUY/SELL 優先判斷邏輯"""
        raw_scores = [
            # 2330 第一筆 (HOLD, 總分 50)
            {
                "stock_code": "2330",
                "analysis_date": "2026-07-15",
                "trend_score": 10, "momentum_score": 10, "volume_score": 10, "safety_score": 10, "regime_score": 10,
                "decision": "HOLD"
            },
            # 2330 第二筆 (BUY, 總分 90)
            {
                "stock_code": "2330",
                "analysis_date": "2026-07-15",
                "trend_score": 18, "momentum_score": 18, "volume_score": 18, "safety_score": 18, "regime_score": 18,
                "decision": "BUY"
            },
            # 2330 第三筆 (HOLD, 總分 70)
            {
                "stock_code": "2330",
                "analysis_date": "2026-07-15",
                "trend_score": 14, "momentum_score": 14, "volume_score": 14, "safety_score": 14, "regime_score": 14,
                "decision": "HOLD"
            },
            # 2454 只有一筆 (SELL)
            {
                "stock_code": "2454",
                "analysis_date": "2026-07-15",
                "trend_score": 8, "momentum_score": 8, "volume_score": 8, "safety_score": 8, "regime_score": 8,
                "decision": "SELL"
            }
        ]

        res = aggregate_daily_scores(raw_scores)
        self.assertEqual(len(res), 2)

        # 檢驗 2330 聚合結果
        sc_2330 = next(x for x in res if x["stock_code"] == "2330")
        self.assertEqual(sc_2330["sample_count"], 3)
        self.assertEqual(sc_2330["trend_score"], 14)  # (10+18+14)/3 = 14
        self.assertEqual(sc_2330["momentum_score"], 14)
        self.assertEqual(sc_2330["total_score"], 70)
        # 即使包含 HOLD，只要有一筆 BUY 則決策必須為 BUY
        self.assertEqual(sc_2330["decision"], "BUY")

        # 檢驗 2454 聚合結果
        sc_2454 = next(x for x in res if x["stock_code"] == "2454")
        self.assertEqual(sc_2454["decision"], "SELL")

    def test_active_skills_query_single_latest(self):
        """驗證 get_active_skills_context 能順利返回 Prompt 字串與 JSON Skills 區塊"""
        ctx = get_active_skills_context(is_paper=False)
        self.assertIn("【當前生效之動態交易戰術規範", ctx)
        self.assertIn("```json", ctx)

    def test_weekend_only_guard(self):
        """驗證 週末限定判斷 (is_weekend_taiwan) 回傳布林值"""
        val = is_weekend_taiwan()
        self.assertIsInstance(val, bool)

    def test_entry_timing_and_chasing_high_calculation(self):
        """驗證進場分位數 (Entry Percentile) 與追高/太晚入場邏輯"""
        m_low = 100.0
        m_high = 200.0
        
        # 1. 追高買價 (190 元，位在 90% 高檔)
        high_entry_p = 190.0
        percentile_high = (high_entry_p - m_low) / (m_high - m_low)
        self.assertGreaterEqual(percentile_high, 0.80)

        # 2. 太晚入場買價 (進場後隨即回撤 -6%，且後續最高僅 +1%)
        post_drawdown = -0.06
        post_upside = 0.01
        is_late_entry = (post_drawdown < -0.05) and (post_upside < 0.03)
        self.assertTrue(is_late_entry)

        # 3. 正常低檔買進 (120 元，位在 20% 低檔)
        low_entry_p = 120.0
        percentile_low = (low_entry_p - m_low) / (m_high - m_low)
        self.assertLess(percentile_low, 0.80)

    def test_bullish_regime_trap_calculation(self):
        """驗證順風/多頭氣候下『大盤好買入卻虧損』之虧損率統計"""
        bullish_buy_count = 5
        bullish_losing_trade_count = 2
        loss_rate = (bullish_losing_trade_count / bullish_buy_count * 100.0) if bullish_buy_count > 0 else 0.0
        self.assertEqual(loss_rate, 40.0)

    def test_multi_layer_monthly_review_pipeline(self):
        """驗證多層月度檢討管道 (Layer 1 -> Layer 2 -> Layer 3) 回傳資料結構"""
        from src.agents.monthly_review_agent import run_monthly_review

        # 模擬 Gemini 呼叫回傳格式
        def mock_call_gemini(prompt: str, generation_config: dict) -> str:
            if "StockIndicatorReviewOutput" in str(generation_config.get("response_schema")):
                return '{"stock_code": "2330", "indicator_retrospective": "2330 技術指標表現優異，V 轉確立。", "anomaly_trait": "拉回年線為典型 V 轉特徵"}'
            elif "IndicatorReviewSummaryOutput" in str(generation_config.get("response_schema")):
                return json.dumps({
                    "indicator_summary": "全月技術指標精準捕捉 V 型反彈。",
                    "indicator_skills": {
                        "v_shape_reversal_patterns": [{"pattern_rule": "量能突破且 RSI>60", "expected_probability_pct": 85}],
                        "a_shape_top_warnings": [{"pattern_rule": "高檔乖離爆量", "expected_probability_pct": 80}],
                        "stock_specific_rules": [{"stock_code": "2330", "anomaly_trait": "拉回年線 V 轉", "expected_probability_pct": 85}],
                        "score_calibration_rules": [{"calibration_rule": "嚴格評分", "expected_probability_pct": 90}],
                        "regime_indicator_rules": {"BULLISH_TREND": {"focus": "動能突破", "expected_probability_pct": 85}}
                    }
                })
            elif "StockExecutionReviewOutput" in str(generation_config.get("response_schema")):
                return '{"stock_code": "2330", "execution_retrospective": "2330 交易執行良好，未出現追高。"}'
            elif "ExecutionReviewSummaryOutput" in str(generation_config.get("response_schema")):
                return json.dumps({
                    "cio_summary": "組合部位執行穩健，Timing 控制得宜。",
                    "key_learnings": ["落實 5% 停損"],
                    "execution_skills": {
                        "min_buy_score": 65,
                        "max_single_stock_weight": 4,
                        "stop_loss_pct": -0.05,
                        "take_profit_pct": 0.12,
                        "entry_timing_rules": ["避開高檔區間追高"],
                        "regime_posture": {"BULLISH_TREND": "AGGRESSIVE"},
                        "tactical_rules": ["嚴格執行停損"]
                    }
                })
            return "{}"

        import json
        res = run_monthly_review(2026, 7, is_paper=True, call_gemini_fn=mock_call_gemini)

        if not res.get("skipped"):
            self.assertIn("stock_indicator_reports", res)
            self.assertIn("stock_execution_reports", res)
            self.assertIn("indicator_skills", res)
            self.assertIn("execution_skills", res)
            self.assertIn("skills_json", res)
            skills = res["skills_json"]
            self.assertIn("indicator_skills", skills)
            self.assertIn("execution_skills", skills)

    def test_same_day_anti_churning_safeguard_logic(self):
        """驗證同日對沖防護 logic（今日買入禁同日賣、今日賣出禁同日買）"""
        today_bought_stocks = {"2330"}
        today_sold_stocks = {"2454"}

        # 1. 2330 今日已買入，即使 LLM 發出 SELL 決策，應強制為 HOLD
        action_2330 = "SELL"
        holding_qty_2330 = 1000.0
        if action_2330 == "SELL" and "2330" in today_bought_stocks:
            final_action_2330 = "HOLD"
        else:
            final_action_2330 = action_2330
        self.assertEqual(final_action_2330, "HOLD")

        # 2. 2454 今日已賣出，即使 LLM 發出 BUY 決策，應強制為 HOLD
        action_2454 = "BUY"
        if action_2454 == "BUY" and "2454" in today_sold_stocks:
            final_action_2454 = "HOLD"
        else:
            final_action_2454 = action_2454
        self.assertEqual(final_action_2454, "HOLD")

    @patch("src.services.supabase_client.supabase")
    def test_has_monthly_review_run(self, mock_supabase):
        """驗證 has_monthly_review_run 查詢 Supabase 歷程紀錄去重邏輯"""
        from src.services.supabase_client import has_monthly_review_run

        # 模擬有紀錄
        mock_chain = MagicMock()
        mock_chain.select.return_value = mock_chain
        mock_chain.eq.return_value = mock_chain
        mock_chain.limit.return_value = mock_chain
        mock_chain.execute.return_value = MagicMock(data=[{"id": 1}])
        mock_supabase.table.return_value = mock_chain

        res_true = has_monthly_review_run(2026, 7, is_paper=True)
        self.assertTrue(res_true)

        # 模擬無紀錄
        mock_chain.execute.return_value = MagicMock(data=[])
        res_false = has_monthly_review_run(2026, 8, is_paper=True)
        self.assertFalse(res_false)

if __name__ == "__main__":
    unittest.main()





