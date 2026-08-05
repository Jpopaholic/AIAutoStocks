# Path: src/scratch/tests/test_buy_sell_price_buffer.py
import sys
import math
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parents[3]
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.services.health_check import calculate_buffered_order_price, audit_proposed_order
from src.services.broker_connector import align_to_tw_tick_size


def test_buy_price_buffer_tiers():
    """驗證買進溢價追價階梯 (+1.5%, +1.0%, +0.5%) 與 Tick Size 對齊"""
    print("=== [Test 1] 買進溢價追價階梯測試 ===")
    
    base_price = 200.0  # 200 元股票 (100~500 元階梯，Tick Size = 0.5)
    code = "3034"
    
    # 1. 高信心度 (score >= 85) -> +1.5% -> 200 * 1.015 = 203.0
    order_p, buf_pct, diff = calculate_buffered_order_price(base_price, code, "BUY", total_score=88)
    print(f"High Score (88): base={base_price} -> order={order_p} (buf={buf_pct*100:+.1f}%, diff={diff:+.2f})")
    assert buf_pct == 0.015
    assert order_p == 203.0
    
    # 2. 中信心度 (70 <= score < 85) -> +1.0% -> 200 * 1.01 = 202.0
    order_p, buf_pct, diff = calculate_buffered_order_price(base_price, code, "BUY", total_score=75)
    print(f"Med Score (75):  base={base_price} -> order={order_p} (buf={buf_pct*100:+.1f}%, diff={diff:+.2f})")
    assert buf_pct == 0.010
    assert order_p == 202.0
    
    # 3. 溫和買進 (score < 70) -> +0.5% -> 200 * 1.005 = 201.0
    order_p, buf_pct, diff = calculate_buffered_order_price(base_price, code, "BUY", total_score=65)
    print(f"Low Score (65):  base={base_price} -> order={order_p} (buf={buf_pct*100:+.1f}%, diff={diff:+.2f})")
    assert buf_pct == 0.005
    assert order_p == 201.0
    
    print("✅ Test 1 通過！\n")


def test_sell_price_buffer_tiers():
    """驗證賣出與平倉讓價階梯 (-1.5%, -1.0%) 與 Tick Size 對齊"""
    print("=== [Test 2] 賣出與平倉讓價階梯測試 ===")
    
    base_price = 200.0  # 200 元股票 (100~500 元階梯，Tick Size = 0.5)
    code = "3034"
    
    # 1. 一鍵下車 / 平倉 (is_liquidate=True) -> -1.5% -> 200 * 0.985 = 197.0
    order_p, buf_pct, diff = calculate_buffered_order_price(base_price, code, "SELL", total_score=75, is_liquidate=True)
    print(f"Liquidate Engine: base={base_price} -> order={order_p} (buf={buf_pct*100:+.1f}%, diff={diff:+.2f})")
    assert buf_pct == -0.015
    assert order_p == 197.0
    
    # 2. 緊急風控停損 (score < 50) -> -1.5% -> 200 * 0.985 = 197.0
    order_p, buf_pct, diff = calculate_buffered_order_price(base_price, code, "SELL", total_score=45)
    print(f"Severe Risk (45): base={base_price} -> order={order_p} (buf={buf_pct*100:+.1f}%, diff={diff:+.2f})")
    assert buf_pct == -0.015
    assert order_p == 197.0
    
    # 3. 一般風控賣出 (score = 55) -> -1.0% -> 200 * 0.990 = 198.0
    order_p, buf_pct, diff = calculate_buffered_order_price(base_price, code, "SELL", total_score=55)
    print(f"Standard Sell (55): base={base_price} -> order={order_p} (buf={buf_pct*100:+.1f}%, diff={diff:+.2f})")
    assert buf_pct == -0.010
    assert order_p == 198.0
    
    print("✅ Test 2 通過！\n")


def test_budget_allocation_safety():
    """驗證買進股數試算在套用溢價上限後，預估總花費不超過配置金額"""
    print("=== [Test 3] 買進股數配置安全測試 ===")
    
    base_price = 200.0
    code = "3034"
    allocated_budget = 1000000.0  # 1,000,000 元
    
    order_price, buf_pct, _ = calculate_buffered_order_price(base_price, code, "BUY", total_score=90)  # +1.5% -> 203.0
    qty = math.floor(allocated_budget / order_price)
    cost = qty * order_price
    
    print(f"配置金額: {allocated_budget:,.0f} 元 | 基準單價: {base_price} 元")
    print(f"溢價委託單價: {order_price} 元 | 估算買進股數: {qty} 股 | 估算最高扣款: {cost:,.0f} 元")
    
    assert cost <= allocated_budget
    assert qty == 4926  # 1,000,000 / 203.0 = 4926.10 -> 4926 股
    print("✅ Test 3 通過！\n")


def test_health_check_integration():
    """驗證帶有緩衝溢價/折價的委託單可順利通過下單前安全審查 (health_check)"""
    print("=== [Test 4] Pre-order Health Check 測試 ===")
    
    base_price = 200.0
    code = "3034"
    
    # 買單 (帶 +1.5% 溢價 203.0 元，數量 10 股 = 2,030 元 < 限額)
    buy_order_price, _, _ = calculate_buffered_order_price(base_price, code, "BUY", total_score=90)
    is_valid, reason = audit_proposed_order(
        stock_code=code,
        action="BUY",
        price=buy_order_price,
        quantity=10,
        close_price=base_price
    )
    print(f"BUY Order (price={buy_order_price}): valid={is_valid}, reason={reason}")
    assert is_valid is True
    
    # 賣單 (帶 -1.5% 折價 197.0 元)
    sell_order_price, _, _ = calculate_buffered_order_price(base_price, code, "SELL", total_score=45)
    is_valid, reason = audit_proposed_order(
        stock_code=code,
        action="SELL",
        price=sell_order_price,
        quantity=10,
        close_price=base_price
    )
    print(f"SELL Order (price={sell_order_price}): valid={is_valid}, reason={reason}")
    assert is_valid is True
    
    print("✅ Test 4 通過！\n")


if __name__ == "__main__":
    test_buy_price_buffer_tiers()
    test_sell_price_buffer_tiers()
    test_budget_allocation_safety()
    test_health_check_integration()
    print("🎉 所有價格緩衝單元測試均成功通過！")
