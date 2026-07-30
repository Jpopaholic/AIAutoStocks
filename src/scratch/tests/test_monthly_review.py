# Path: src/scratch/tests/test_monthly_review.py
import unittest
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

if __name__ == "__main__":
    unittest.main()

