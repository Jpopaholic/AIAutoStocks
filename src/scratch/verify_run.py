import os
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from src.agents.trading_agent import generate_portfolio_decisions

stock_codes = ["2303", "2618", "2324", "2883", "3481", "2353"]
klines_map = {}
# 填寫基本的日 K 線與技術指標，以利分析師評分
for code in stock_codes + ["TAIEX"]:
    klines_map[code] = [
        {
            "date": "2026-07-06",
            "open": 50.0,
            "high": 51.0,
            "low": 49.5,
            "close": 50.5,
            "volume": 100000.0,
            "ma5": 50.2,
            "ma20": 49.8,
            "ma60": 49.0,
            "rsi14": 55.0,
            "vol_ma5": 90000.0,
            "vol_ma20": 85000.0,
            "macd": 0.5,
            "macd_signal": 0.3,
            "macd_hist": 0.2,
            "plus_di": 22.0,
            "minus_di": 18.0,
            "adx": 26.0
        }
    ]

# 模擬帳戶庫存：持有 2883
current_holdings = [
    {"stock_code": "2883", "quantity": 1000.0, "average_price": 12.0}
]

regime_assessment = {
    "regime": "BULLISH_TREND",
    "posture": "AGGRESSIVE",
    "risk_multiplier": 1.0,
    "reason": "大盤維持在季線與月線之上，成交量回升"
}

print("=== 執行真實 Gemini API 雙層架構決策驗證 ===")
try:
    res = generate_portfolio_decisions(
        stock_codes=stock_codes,
        klines_map=klines_map,
        current_holdings=current_holdings,
        regime_assessment=regime_assessment
    )
    print("\n--- 投資組合經理排序分析 (Ranking Analysis) ---")
    print(res.get("ranking_analysis"))
    
    print("\n--- 個股決策明細 (Decisions) ---")
    for d in res.get("decisions", []):
        print(f"個股: {d['stock_code']} | 決策: {d['action']} | 價格: {d['price']} | 數量: {d['quantity']} | 總分: {d['total_score']}")
        print(f"詳細理由: {d['reason']}")
        print("-" * 50)
except Exception as e:
    import traceback
    traceback.print_exc()
