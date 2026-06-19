# Path: src/scratch/test_technical_indicators.py
import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import unittest
from src.services.technical_indicators import (
    calculate_sma,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_dmi,
    compute_all_indicators
)

class TestTechnicalIndicators(unittest.TestCase):

    def test_calculate_sma(self):
        prices = [10.0, 11.0, 12.0, 13.0, 14.0]
        sma = calculate_sma(prices, 3)
        self.assertEqual(sma[:2], [None, None])
        self.assertAlmostEqual(sma[2], 11.0)
        self.assertAlmostEqual(sma[3], 12.0)
        self.assertAlmostEqual(sma[4], 13.0)

    def test_calculate_ema(self):
        prices = [10.0, 11.0, 12.0, 13.0, 14.0]
        ema = calculate_ema(prices, 3)
        self.assertEqual(ema[:2], [None, None])
        # first ema (at index 2) is SMA(3) of first 3 elements = 11.0
        self.assertAlmostEqual(ema[2], 11.0)
        # alpha = 2 / 4 = 0.5
        # ema[3] = 13.0 * 0.5 + 11.0 * 0.5 = 12.0
        self.assertAlmostEqual(ema[3], 12.0)
        # ema[4] = 14.0 * 0.5 + 12.0 * 0.5 = 13.0
        self.assertAlmostEqual(ema[4], 13.0)

    def test_calculate_rsi(self):
        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 20.0]
        rsi = calculate_rsi(prices, 14)
        self.assertEqual(len(rsi), len(prices))
        self.assertEqual(rsi[:14], [None] * 14)
        self.assertIsNotNone(rsi[14])
        self.assertIsNotNone(rsi[15])
        # rsi[14] is 100% since there were only gains (10 to 24)
        self.assertAlmostEqual(rsi[14], 100.0)
        # rsi[15] drops because of the price drop from 24 to 20
        self.assertTrue(rsi[15] < 100.0)

    def test_calculate_macd(self):
        prices = [float(i) for i in range(1, 40)]
        macd_line, signal_line, hist = calculate_macd(prices, 12, 26, 9)
        self.assertEqual(len(macd_line), len(prices))
        self.assertEqual(len(signal_line), len(prices))
        self.assertEqual(len(hist), len(prices))
        self.assertEqual(macd_line[:25], [None] * 25)
        self.assertIsNotNone(macd_line[25])
        self.assertIsNotNone(signal_line[33])
        self.assertIsNotNone(hist[33])

    def test_calculate_dmi(self):
        highs = [10.0 + i * 0.5 for i in range(35)]
        lows = [9.0 + i * 0.5 for i in range(35)]
        closes = [9.5 + i * 0.5 for i in range(35)]
        plus_di, minus_di, adx = calculate_dmi(highs, lows, closes, 14)
        self.assertEqual(len(plus_di), len(closes))
        self.assertEqual(len(minus_di), len(closes))
        self.assertEqual(len(adx), len(closes))
        self.assertEqual(plus_di[:14], [None] * 14)
        self.assertIsNotNone(plus_di[14])
        self.assertEqual(adx[:27], [None] * 27)
        self.assertIsNotNone(adx[27])

    def test_compute_all_indicators(self):
        klines = []
        for i in range(35):
            klines.append({
                "date": f"2026-06-{i+1:02d}",
                "open": 10.0 + i,
                "high": 11.5 + i,
                "low": 9.5 + i,
                "close": 11.0 + i,
                "volume": 1000 + i * 10
            })
        res = compute_all_indicators(klines)
        self.assertEqual(len(res), 35)
        for k in res:
            self.assertIn("ma5", k)
            self.assertIn("ma20", k)
            self.assertIn("vol_ma5", k)
            self.assertIn("vol_ma20", k)
            self.assertIn("rsi14", k)
            self.assertIn("macd", k)
            self.assertIn("macd_signal", k)
            self.assertIn("macd_hist", k)
            self.assertIn("plus_di", k)
            self.assertIn("minus_di", k)
            self.assertIn("adx", k)

if __name__ == "__main__":
    unittest.main()
