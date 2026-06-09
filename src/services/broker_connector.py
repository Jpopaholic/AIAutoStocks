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
    檢查單筆交易金額、今日累計交易總額與帳戶現金餘額是否超出限制或不足
    """
    order_amount = price * quantity
    
    # 1. 檢查可用現金餘額是否充足 (僅在買入時限制)
    if action == "BUY":
        from src.services.nav_calculator import calculate_nav
        try:
            cash_balance, _, _ = calculate_nav()
        except Exception:
            cash_balance = config.limits.initial_cash
            
        if order_amount > cash_balance:
            raise ValueError(
                f"可用現金餘額不足！欲委託買入金額為 {order_amount:,.0f} 元，而當前帳戶可用現金僅剩 {cash_balance:,.0f} 元。"
            )
            
    # 2. 檢查單筆限額 (僅在買入時限制，賣出平倉時不限制以確保能順利停損停利)
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

_shioaji_api = None
_shioaji_lock = threading.Lock()

def _get_shioaji_api():
    """
    延遲載入並登入永豐證券 Shioaji SDK 實體
    """
    global _shioaji_api
    with _shioaji_lock:
        if _shioaji_api is None:
            log_system_event("INFO", "正在初始化永豐證券 Shioaji SDK...")
            try:
                import shioaji as sj
            except ImportError as err:
                log_system_event("ERROR", "安裝依賴失敗，缺少 shioaji 套件")
                raise RuntimeError("請確保已在環境中安裝 shioaji") from err
            except Exception as import_err:
                log_system_event("ERROR", f"載入 shioaji 模組時發生未知錯誤: {str(import_err)}")
                raise

            try:
                import os
                credentials = load_credentials()
                broker_creds = credentials.get("brokerCredentials", {})
                
                api_key = broker_creds.get("apiId")
                secret_key = broker_creds.get("apiSecret")
                password = broker_creds.get("password")
                cert_path = broker_creds.get("certificatePath")
                person_id = broker_creds.get("personId")
                
                if not api_key or not secret_key:
                    raise ValueError("安全憑證中缺少 apiId 或 apiSecret，無法登入永豐證券")
                
                # 初始化 API (實盤下單模式使用 simulation=False)
                api = sj.Shioaji(simulation=False)
                api.login(api_key=api_key, secret_key=secret_key)
                log_system_event("INFO", "永豐證券 API 帳號登入成功。")
                
                # 啟用 CA 憑證
                if cert_path and password and person_id:
                    if os.path.exists(cert_path):
                        api.activate_ca(
                            ca_path=cert_path,
                            ca_passwd=password,
                            person_id=person_id
                        )
                        log_system_event("INFO", "CA 下單安全憑證啟用成功，實盤交易功能已解鎖。")
                    else:
                        log_system_event("WARN", f"CA 憑證檔案不存在 ({cert_path})，可能只能進行查詢而無法下單")
                else:
                    log_system_event("WARN", "憑證路徑 (certificatePath)、密碼或身分證字號不足，下單委託可能會被拒絕")
                
                _shioaji_api = api
            except Exception as e:
                log_system_event("ERROR", f"初始化 Shioaji API 發生異常: {str(e)}")
                raise
        return _shioaji_api

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
 
        # 取得目前沙盒虛擬日期（如有）
        from src.services.sandbox_simulator import is_simulation_active, get_current_sim_date
        current_sim_date = get_current_sim_date() if is_simulation_active() else None

        order_detail = {
            "stockCode": stock_code,
            "action": action,
            "price": price,
            "quantity": quantity,
            "fee": fee,
            "totalAmount": total_amount,
            "realizedPnl": realized_pnl,
            "simDate": current_sim_date  # 沙盒模式為虛擬日期，真實操盤為 None
        }

        try:
            # 寫入模擬平倉帳務與交易訂單
            db_result = execute_trade_transaction(order_detail)
            
            log_system_event(
                "INFO", 
                f"【模擬交易】已成功執行: {action} {stock_code} | 成交價: {price} | 股數: {quantity} | 損益: {realized_pnl:,.0f} 元",
                sim_date=current_sim_date
            )
            
            # 詳細記錄 Raw Log 供日後審計
            _write_raw_order_log(order_detail, {"status": "SUCCESS", "mode": "PAPER", "db_id": db_result.get("id")})
            
            return db_result
        except Exception as e:
            log_system_event("ERROR", f"模擬交易資料庫寫入異常: {str(e)}", sim_date=current_sim_date)
            raise
        else:
            # ================= 真實交易模式 (Real Trading) =================
            try:
                # 1. 取得登入的 Shioaji API 實例
                api = _get_shioaji_api()
                
                account = api.stock_account
                if not account:
                    if api.stock_accounts:
                        account = api.stock_accounts[0]
                    else:
                        raise RuntimeError("Shioaji 登入成功，但未找到任何可用證券帳戶")
                
                # 動態導入 Shioaji 常數，確保模擬交易模式在不安裝 Shioaji 時亦能順暢加載運行
                from shioaji.constant import Action as SjAction, StockPriceType, OrderType, StockOrderLot
                
                # 2. 獲取股票合約 (Contract)
                contract = api.Contracts.Stocks[stock_code]
                if not contract:
                    raise ValueError(f"無法在 Shioaji 中找到股票代號 {stock_code} 的合約資訊，委託終止")
                
                # 3. 判斷交易單位與數量換算
                # 買賣類別 (Action)
                sj_action = SjAction.Buy if action == "BUY" else SjAction.Sell
                
                # 台灣市場：整股為 1000 股的整數倍，不足 1000 股需走盤中零股 (IntradayOdd)
                if quantity % 1000 == 0 and quantity >= 1000:
                    order_lot = StockOrderLot.Common
                    order_qty = int(quantity / 1000)  # Common 委託數量為張數 (張)
                else:
                    order_lot = StockOrderLot.IntradayOdd
                    order_qty = int(quantity)          # 零股委託數量為股數 (股)
                
                # 4. 建立委託物件 (預設使用限價 LMT 與當日有效單 ROD)
                order = api.Order(
                    action=sj_action,
                    price=price,
                    quantity=order_qty,
                    price_type=StockPriceType.LMT,
                    order_type=OrderType.ROD,
                    order_lot=order_lot,
                    account=account
                )
                
                # 5. 送出委託下單
                log_system_event(
                    "INFO",
                    f"【真實交易】向永豐發送委託: {action} {stock_code} | 價格: {price} | 數量: {order_qty} {order_lot.name}"
                )
                
                trade = api.place_order(contract, order)
                
                # 6. 處理與記錄成交資訊
                realized_pnl = 0.0
                if action == "SELL":
                    try:
                        current_holdings = get_holdings()
                        matching_holding = next((h for h in current_holdings if h["stock_code"] == stock_code), None)
                        if matching_holding:
                            avg_cost = float(matching_holding["average_price"])
                            # 實現損益 = (賣出價 - 買入均價) * 股數 - 規費
                            realized_pnl = (price - avg_cost) * quantity - fee
                    except Exception as he:
                        print(f" [下單連接器] 實盤計算平倉損益失敗: {str(he)}")
                
                order_detail = {
                    "stockCode": stock_code,
                    "action": action,
                    "price": price,
                    "quantity": quantity,
                    "fee": fee,
                    "totalAmount": total_amount,
                    "realizedPnl": realized_pnl
                }
                
                # 寫入 Supabase 訂單與持股明細
                db_result = execute_trade_transaction(order_detail)
                
                log_system_event(
                    "INFO",
                    f"【真實交易成功】永豐委託建立完成！單號: {trade.status.id} | 狀態: {trade.status.status}"
                )
                
                # 記錄 Raw Log
                _write_raw_order_log(order_detail, {
                    "status": "SUCCESS", 
                    "mode": "REAL", 
                    "db_id": db_result.get("id"),
                    "trade_id": trade.status.id,
                    "trade_status": str(trade.status.status),
                    "api_id": account.person_id
                })
                
                return db_result
            except Exception as e:
                log_system_event("ERROR", f"真實下單委託異常失敗: {str(e)}")
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
