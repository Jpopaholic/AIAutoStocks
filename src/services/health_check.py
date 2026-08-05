# Path: src/services/health_check.py
import os
import sys
from typing import Tuple, Dict, Any, Optional

from src.config import config
from src.services import supabase_client
from src.services.broker_connector import align_to_tw_tick_size

def calculate_buffered_order_price(
    base_price: float,
    stock_code: str,
    action: str,
    total_score: float = 75.0,
    is_liquidate: bool = False
) -> Tuple[float, float, float]:
    """
    依據 AI 評定分數/信心度與交易動作，計算包含溢價追價 (BUY) 或折價讓價 (SELL) 的最終限價委託價。
    
    :param base_price: 參考基準價 (如前一交易日收盤價或最新即時報價)
    :param stock_code: 股票代號
    :param action: "BUY" 或 "SELL"
    :param total_score: AI 量化綜合評分 (0 ~ 100)
    :param is_liquidate: 是否為一鍵下車/緊急平倉
    :return: (order_price, buffer_pct, price_diff)
             - order_price: 符合台股 Tick Size 的最終委託限價
             - buffer_pct: 緩衝幅度比率 (例如 BUY +0.015, SELL -0.015)
             - price_diff: 委託限價與基準價之差額 (order_price - base_price)
    """
    if base_price <= 0:
        return 0.0, 0.0, 0.0

    action_upper = str(action).upper()

    if action_upper == "BUY":
        # 買進溢價追價緩衝 (Premium Buffer)
        if total_score >= 85:
            buffer_pct = 0.015  # +1.5% 高信心度強勢追價
        elif total_score >= 70:
            buffer_pct = 0.010  # +1.0% 標準追價
        else:
            buffer_pct = 0.005  # +0.5% 溫和追價
        
        raw_price = base_price * (1.0 + buffer_pct)
    elif action_upper == "SELL":
        # 賣出/平倉折價讓價緩衝 (Discount Buffer)
        if is_liquidate or total_score < 50:
            buffer_pct = -0.015  # -1.5% 一鍵下車或高風險停損，讓價優先變現
        else:
            buffer_pct = -0.010  # -1.0% 標準風控賣出讓價
        
        raw_price = max(base_price * (1.0 + buffer_pct), 0.01)
    else:
        buffer_pct = 0.0
        raw_price = base_price

    order_price = align_to_tw_tick_size(raw_price, stock_code)
    price_diff = order_price - base_price
    return order_price, buffer_pct, price_diff


def run_preflight_checks() -> Tuple[bool, Dict[str, Any]]:
    """
    執行系統運行前診斷 (Pre-flight System Diagnostics)
    回傳: (is_healthy, check_details)
    """
    details = {
        "supabase": False,
        "gemini": False,
        "broker": False,
        "errors": []
    }

    # 1. 檢查 Supabase 連線
    try:
        supabase_client.get_db_config()
        details["supabase"] = True
    except Exception as e:
        details["errors"].append(f"Supabase 連線失敗: {str(e)}")

    # 2. 檢查 Gemini API 連線
    try:
        from src.services.gemini_rotator import call_gemini_with_rotation
        res = call_gemini_with_rotation(
            prompt="Ping, reply ONLY with pong.",
            system_instruction="You are a ping responder.",
            model_name=config.gemini_model
        )
        if res and "pong" in res.lower():
            details["gemini"] = True
        else:
            details["errors"].append("Gemini 回應不符合預期")
    except Exception as e:
        details["errors"].append(f"Gemini API 連線失敗: {str(e)}")

    # 3. 檢查券商連線 (僅在實盤下單模式下檢查)
    is_paper = config.limits.is_paper_trading
    if is_paper:
        details["broker"] = True  # 模擬交易模式下免檢券商登入
    else:
        try:
            from src.services.broker_connector import _get_shioaji_api
            api = _get_shioaji_api()
            if api and api.stock_account:
                details["broker"] = True
            else:
                details["errors"].append("Shioaji 登入成功但找不到可用帳戶")
        except Exception as e:
            details["errors"].append(f"永豐金 Shioaji 登入失敗: {str(e)}")

    # 4. 綜合判定
    is_healthy = details["supabase"] and details["gemini"] and details["broker"]
    return is_healthy, details

def audit_proposed_order(
    stock_code: str,
    action: str,
    price: float,
    quantity: float,
    regime_assessment: Optional[Dict[str, Any]] = None,
    close_price: Optional[float] = None
) -> Tuple[bool, str]:
    """
    下單前安全審查 (Pre-order Safety Audit)
    驗證價格是否偏離市價過大、數量是否有效、是否超出風控限額等。
    回傳: (is_valid, reason_or_error)
    """
    if action not in ("BUY", "SELL"):
        return False, f"無效的交易動作: {action}"

    if quantity <= 0:
        return False, f"委託數量必須大於 0: {quantity}"

    if price <= 0:
        return False, f"委託價格必須大於 0: {price}"

    # 1. 取得該股最新收盤價 (以傳入價格或資料庫最後一筆日 K 線為基準)
    if close_price is None:
        try:
            db_klines = supabase_client.get_stock_klines(stock_code, limit=1)
            if not db_klines:
                return False, f"資料庫中無此股 {stock_code} 的 K 線數據，無法審查價格合理性"
            close_price = float(db_klines[0]["close"])
        except Exception as e:
            return False, f"取得最新收盤價時發生異常: {str(e)}"

    # 2. 檢查價格是否偏離收盤價達 10% (漲跌停限制)
    price_deviation = abs(price - close_price) / close_price
    if price_deviation > 0.10:
        return False, f"委託價格 {price} 偏離最新收盤價 {close_price} 達 {price_deviation*100:.2f}%，超出台股 ±10% 漲跌幅限制"

    # 3. 檢查價格是否符合台股升降單位 (Tick Size)
    aligned_price = align_to_tw_tick_size(price, stock_code)
    # 浮點數比對加上極小容許誤差
    if abs(price - aligned_price) > 0.0001:
        return False, f"委託價格 {price} 不符合台股升降單位規範，應調整為 {aligned_price}"

    # 4. 檢查是否超出單筆交易限額
    try:
        from src.services.nav_calculator import get_dynamic_limits
        single_limit, _ = get_dynamic_limits()
        
        # 配合大盤氣候風險乘數
        if regime_assessment:
            multiplier = max(float(regime_assessment.get("risk_multiplier", 1.0)), 0.15)
            single_limit *= multiplier
            
        order_amount = price * quantity
        if action == "BUY" and order_amount > single_limit:
            return False, f"委託總額 {order_amount:,.0f} 元超出單筆交易限額 {single_limit:,.0f} 元"
    except Exception as limit_err:
        return False, f"限額安全檢查執行失敗: {str(limit_err)}"

    return True, "通過下單前安全審查"
