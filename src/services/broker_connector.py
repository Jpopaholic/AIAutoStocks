# Path: src/services/broker_connector.py
import threading
from datetime import datetime, date
from typing import Dict, Any
from src.config import config
from src.services.supabase_client import get_holdings, get_orders, execute_trade_transaction, log_system_event
from src.services.credential_manager import load_credentials

# 全局排他鎖，防止多線程重複下單
_order_mutex = threading.Lock()

def calculate_fees(action: str, price: float, quantity: float) -> Dict[str, float]:
    """
    計算台股交易手續費與證券交易稅
    - 手續費費率: 0.1425% (預設打 6 折)
    - 證交稅費率: 0.3% (僅在賣出時收取)
    """
    amount = price * quantity
    # 手續費 (打 6 折，最低 20 元)
    fee = max(20.0, round(amount * 0.001425 * 0.6))
    
    tax = 0.0
    if action == "SELL":
        tax = round(amount * 0.003)
        
    total_fee = fee + tax
    return {
        "fee": total_fee,
        "tax": tax,
        "raw_amount": amount,
        "net_amount": (amount + total_fee) if action == "BUY" else (amount - total_fee)
    }

def _validate_trading_limits(action: str, price: float, quantity: float) -> None:
    """
    檢查單筆交易金額及今日累計交易總額是否超出動態計算的防呆限額
    """
    order_amount = price * quantity
    
    # 1. 檢查單筆限額 (僅在買入時限制，賣出平倉時不限制以確保能順利停損停利)
    if action == "BUY":
        from src.services.nav_calculator import get_dynamic_limits
        single_limit, daily_limit = get_dynamic_limits()
        
        if order_amount > single_limit:
            raise ValueError(
                f" [防呆攔截] 單筆交易金額 {order_amount:,.0f} 元超出上限 {single_limit:,.0f} 元"
            )
        
    # 2. 檢查每日累計交易限額 (僅統計買入金額以防範資金超限風險)
    if action == "BUY":
        from src.services.nav_calculator import get_dynamic_limits
        single_limit, daily_limit = get_dynamic_limits()
        
        # 判斷是否為沙盒模擬模式，若是則使用模擬日期
        from src.services.sandbox_simulator import is_simulation_active, get_current_sim_date
        if is_simulation_active():
            today_iso = get_current_sim_date()
        else:
            today_iso = date.today().isoformat()
        
        # 撈取今日所有的委託訂單
        try:
            today_orders = get_orders(start_date=f"{today_iso}T00:00:00Z")
        except Exception as e:
            print(f" [下單連接器] 警告: 無法獲取今日訂單進行限額檢查: {str(e)}")
            today_orders = []
            
        today_buy_sum = sum(
            float(o["total_amount"]) 
            for o in today_orders 
            if o["action"] == "BUY" and o["executed_at"].startswith(today_iso)
        )
        
        if (today_buy_sum + order_amount) > daily_limit:
            raise ValueError(
                f" [防呆攔截] 加上本筆訂單後，今日累計買入金額 {today_buy_sum + order_amount:,.0f} 元將超出每日上限 {daily_limit:,.0f} 元 (目前已用: {today_buy_sum:,.0f} 元)"
            )

def place_order(stock_code: str, action: str, price: float, quantity: float) -> Dict[str, Any]:
    """
    執行證券交易下單委託，並返回成交狀態與訂單明細。
    :param stock_code: 股票代號 (如 "2330")
    :param action: 'BUY' (買入) 或 'SELL' (賣出)
    :param price: 委託成交價
    :param quantity: 股數 (台股 1 張 = 1000 股)
    :returns: 訂單回報資訊
    """
    # 取得排他鎖，防止並行/重複下單
    with _order_mutex:
        log_system_event("INFO", f"收到下單委託要求: {action} {stock_code} | 價格: {price} | 股數: {quantity}")
        
        # 1. 執行防呆限額檢查
        try:
            _validate_trading_limits(action, price, quantity)
        except ValueError as limit_err:
            log_system_event("WARN", f"下單限額驗證攔截: {str(limit_err)}")
            raise

        # 2. 計算相關交易規費
        costs = calculate_fees(action, price, quantity)
        fee = costs["fee"]
        total_amount = costs["net_amount"]

        # 3. 判斷交易模式並執行
        is_paper = config.limits.is_paper_trading
        
        if is_paper:
            # ================= 模擬交易模式 (Paper Trading) =================
            realized_pnl = 0.0
            if action == "SELL":
                # 模擬平倉：從 Supabase 取得目前的持股平均成本以計算實現損益 (Realized PnL)
                try:
                    current_holdings = get_holdings()
                    matching_holding = next((h for h in current_holdings if h["stock_code"] == stock_code), None)
                    if not matching_holding:
                        raise ValueError(f"模擬平倉失敗：帳戶中並無 {stock_code} 的任何持股")
                    
                    avg_cost = float(matching_holding["average_price"])
                    # 實現損益 = (賣出價 - 買入均價) * 股數 - 規費
                    realized_pnl = (price - avg_cost) * quantity - fee
                except Exception as e:
                    log_system_event("ERROR", f"模擬平倉帳務計算失敗: {str(e)}")
                    raise

            order_detail = {
                "stockCode": stock_code,
                "action": action,
                "price": price,
                "quantity": quantity,
                "fee": fee,
                "totalAmount": total_amount,
                "realizedPnl": realized_pnl
            }

            try:
                # 寫入模擬平倉帳務與交易訂單
                db_result = execute_trade_transaction(order_detail)
                
                log_system_event(
                    "INFO", 
                    f"【模擬交易】已成功執行: {action} {stock_code} | 成交價: {price} | 股數: {quantity} | 損益: {realized_pnl:,.0f} 元"
                )
                
                # 詳細記錄 Raw Log 供日後審計
                _write_raw_order_log(order_detail, {"status": "SUCCESS", "mode": "PAPER", "db_id": db_result.get("id")})
                
                return db_result
            except Exception as e:
                log_system_event("ERROR", f"模擬交易資料庫寫入異常: {str(e)}")
                raise
        else:
            # ================= 真實交易模式 (Real Trading) =================
            # 1. 載入安全憑證
            try:
                credentials = load_credentials()
                broker_creds = credentials.get("brokerCredentials", {})
            except Exception as e:
                log_system_event("ERROR", f"真實交易啟動失敗，載入安全憑證異常: {str(e)}")
                raise RuntimeError("真實交易環境下必須提供外部解密金鑰與真實交易錢包帳密")

            # 2. 模擬/串接 Shioaji SDK 委託邏輯
            # 此處為證券商 SDK 的實作插槽
            api_id = broker_creds.get("apiId")
            password = broker_creds.get("password")
            
            log_system_event(
                "INFO", 
                f"【真實下單】使用金鑰 [{api_id[:4]}...] 發送委託至真實證券商中..."
            )

            # 實際部署時引入 Shioaji：
            # import shioaji as sj
            # api = sj.Shioaji()
            # api.login(api_id, password)
            # contract = api.Contracts.Stocks[stock_code]
            # order = api.Order(action=sj.constant.Action.Buy if action == 'BUY' else sj.constant.Action.Sell, ...)
            # trade = api.place_order(contract, order)
            
            # 以下為真實下單之模擬回報 Stub
            realized_pnl = 0.0
            order_detail = {
                "stockCode": stock_code,
                "action": action,
                "price": price,
                "quantity": quantity,
                "fee": fee,
                "totalAmount": total_amount,
                "realizedPnl": realized_pnl
            }

            try:
                # 仍須同步寫入 Supabase 資料庫以追蹤實際損益
                db_result = execute_trade_transaction(order_detail)
                
                log_system_event(
                    "INFO", 
                    f"【真實交易成功】已成功送出委託: {action} {stock_code} | 成交價: {price} | 股數: {quantity}"
                )
                
                _write_raw_order_log(order_detail, {"status": "SUCCESS", "mode": "REAL", "db_id": db_result.get("id"), "api_id": api_id})
                
                return db_result
            except Exception as e:
                log_system_event("ERROR", f"真實交易同步本地資料庫時發生異常: {str(e)}")
                raise

def _write_raw_order_log(order_detail: Dict[str, Any], response: Dict[str, Any]) -> None:
    """
    寫入 Raw Log 檔案供日後安全查核
    """
    timestamp = datetime.now().isoformat()
    log_line = f"[{timestamp}] [ORDER_AUDIT] Detail: {order_detail} | Response: {response}\n"
    
    # 嚴禁以明文寫入帳號密碼與憑證，僅記錄訂單業務與狀態
    try:
        with open("order_execution.log", "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as e:
        print(f" [下單連接器] 警告: 無法寫入本地 Raw Audit Log: {str(e)}")

def check_and_execute_hard_stop_losses() -> None:
    """
    掃描目前所有持股倉位，若有持股虧損達到或超過 5% (-5%)，
    則不經過 AI 直接觸發硬體強制平倉（SELL），自動全數賣出以控制下檔風險。
    """
    log_system_event("INFO", "啟動持股硬體停損防線掃描...")
    try:
        holdings = get_holdings()
    except Exception as e:
        log_system_event("ERROR", f"[硬體停損防線] 無法取得持股資料進行停損掃描: {str(e)}")
        return

    for h in holdings:
        stock_code = h["stock_code"]
        qty = float(h["quantity"])
        avg_price = float(h["average_price"])
        
        if qty <= 0:
            continue
            
        # 取得模擬或真實盤中報價
        from src.services import sandbox_simulator
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        current_price = float(quote.get("price") or avg_price)
        
        roi = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
        
        # 虧損達 5% 或以上觸發停損
        if roi <= -0.05:
            msg = f"【硬體停損觸發】偵測到 {stock_code} 虧損達 {roi*100:.2f}% (成本: {avg_price} | 現價: {current_price})，執行強制平倉！"
            log_system_event("WARN", msg)
            try:
                # 執行下單賣出全部股數
                place_order(stock_code=stock_code, action="SELL", price=current_price, quantity=qty)
                log_system_event("INFO", f"[硬體停損成功] 已強行賣出 {stock_code} 全數共 {qty:,.0f} 股。")
            except Exception as order_err:
                log_system_event("ERROR", f"[硬體停損失敗] 無法自動賣出 {stock_code}: {str(order_err)}")
