# Path: src/services/broker_connector.py
import threading
import time
from datetime import datetime, date
from typing import Dict, Any
from src.config import config
from src.services.supabase_client import (
    get_holdings,
    get_orders,
    execute_trade_transaction,
    log_system_event,
    set_system_fault_status,
    add_pending_liquidation_stock,
    update_holding_after_fill,
    get_pending_real_orders,
    update_order_status,
    execute_with_retry,
    supabase
)
from src.services.credential_manager import load_credentials
from src.time_manager import get_local_taiwan_midnight_utc_range, get_effective_date_str

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
        from src.services.sandbox_simulator import is_simulation_active
        if is_simulation_active():
            today_iso = get_effective_date_str()
            today_orders = get_orders(sim_date=today_iso)
        else:
            start_utc, end_utc = get_local_taiwan_midnight_utc_range()
            today_orders = get_orders(start_date=start_utc, end_date=end_utc)
        
        today_buy_sum = sum(
            float(o["total_amount"])
            for o in today_orders
            if o["action"] == "BUY"
        )
        
        if (today_buy_sum + order_amount) > daily_limit:
            raise ValueError(
                f" [防呆攔截] 加上本筆訂單後，今日累計買入金額 {today_buy_sum + order_amount:,.0f} 元將超出每日上限 {daily_limit:,.0f} 元 (目前已用: {today_buy_sum:,.0f} 元)"
            )

_shioaji_api = None
_shioaji_login_time = 0.0
_shioaji_lock = threading.Lock()

def _get_shioaji_api():
    """
    延遲載入並登入永豐證券 Shioaji SDK 實體
    """
    global _shioaji_api, _shioaji_login_time
    with _shioaji_lock:
        if _shioaji_api is not None and (time.time() - _shioaji_login_time > 12 * 3600):
            log_system_event("INFO", "Shioaji 連線已超過 12 小時，主動登出以防 Token 過期...")
            try:
                _shioaji_api.logout()
            except Exception:
                pass
            _shioaji_api = None

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
                _shioaji_login_time = time.time()
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
                    
                    avg_cost = float(matching_holding.get("average_price") or 0.0)
                    # 實現損益 = (賣出價 - 買入均價) * 股數 - 規費
                    realized_pnl = (price - avg_cost) * quantity - fee
                except Exception as e:
                    log_system_event("ERROR", f"模擬平倉帳務計算失敗: {str(e)}")
                    raise
 
            # 取得目前沙盒虛擬日期（如有）
            from src.services.sandbox_simulator import is_simulation_active, get_current_sim_date
            sim_active = is_simulation_active()
            current_sim_date = get_current_sim_date() if sim_active else None

            # 在沙盒演練回測中，我們模擬「盤後預約單」機制：
            # 決定下單時寫入 PENDING，等下一個交易日開盤/收盤對帳時才轉為 FILLED 並正式更新 holdings。
            # 如果是實時的模擬交易 (Live Paper Trading)，則直接 FILLED。
            if sim_active:
                status_to_write = "PENDING"
                exec_price_to_write = None
                order_id_to_write = f"PAPER_PENDING_{int(time.time())}"
            else:
                status_to_write = "FILLED"
                exec_price_to_write = price
                order_id_to_write = f"PAPER_{int(time.time())}"

            order_detail = {
                "stockCode": stock_code,
                "action": action,
                "price": price,
                "quantity": quantity,
                "fee": fee,
                "totalAmount": total_amount,
                "realizedPnl": realized_pnl,
                "status": status_to_write,
                "executionPrice": exec_price_to_write,
                "orderId": order_id_to_write,
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
                        matching_holding = next((h for h in current_holdings if h.get("stock_code") == stock_code), None)
                        if matching_holding:
                            avg_cost = float(matching_holding.get("average_price") or 0.0)
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
                    "realizedPnl": realized_pnl,
                    "status": "PENDING",
                    "executionPrice": None,
                    "orderId": trade.status.id
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
                # 斷線或異常時，清空全域 API 實例，確保下次嘗試時會重新登入
                global _shioaji_api
                try:
                    if _shioaji_api is not None:
                        _shioaji_api.logout()
                except Exception:
                    pass
                _shioaji_api = None
                # 判定是否為系統級故障
                err_msg = str(e).lower()
                systemic_keywords = ["login", "ca_path", "passwd", "cert", "connection", "timeout", "network", "auth", "invalid credential"]
                if any(kw in err_msg for kw in systemic_keywords):
                    set_system_fault_status("FAULT", str(e))
                    try:
                        from src.services.email_notifier import send_emergency_email
                        send_emergency_email(
                            subject="⚠️ AIAutoStocks 系統下單發生系統級故障！",
                            message=f"系統在執行 {action} {stock_code} 時，偵測到無法排除的系統級故障或網路連線失敗，已自動鎖定全局交易！\n\n錯誤詳情：\n{str(e)}"
                        )
                    except Exception as email_err:
                        print(f" [下單連接器] 發送緊急郵件失敗: {str(email_err)}")
                else:
                    # 一般交易性/市場性錯誤（例如限額不足或跌停無法交易）
                    add_pending_liquidation_stock(stock_code)
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
    掃描目前所有持股倉位，若有持股虧損達到或超過 8% (-8%)，
    則不經過 AI 直接觸發硬體強制平倉（SELL），自動全數賣出以控制下檔風險。
    """
    log_system_event("INFO", "啟動持股硬體停損防線掃描...")
    try:
        holdings = get_holdings()
    except Exception as e:
        log_system_event("ERROR", f"[硬體停損防線] 無法取得持股資料進行停損掃描: {str(e)}")
        return

    for h in holdings:
        stock_code = h.get("stock_code")
        if not stock_code:
            continue
        try:
            qty = float(h.get("quantity") or 0.0)
            avg_price = float(h.get("average_price") or 0.0)
        except (ValueError, TypeError):
            continue
        
        if qty <= 0:
            continue
            
        # 取得模擬或真實盤中報價
        from src.services import sandbox_simulator
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        price_val = quote.get("price") or avg_price if quote else avg_price
        try:
            current_price = float(price_val) if price_val is not None else 0.0
        except (ValueError, TypeError):
            current_price = 0.0
        
        roi = (current_price - avg_price) / avg_price if avg_price > 0 else 0.0
        
        # 虧損達 8% 或以上觸發停損
        if roi <= -0.08:
            msg = f"【硬體停損觸發】偵測到 {stock_code} 虧損達 {roi*100:.2f}% (成本: {avg_price} | 現價: {current_price})，執行強制平倉！"
            log_system_event("WARN", msg)
            try:
                # 執行下單賣出全部股數
                place_order(stock_code=stock_code, action="SELL", price=current_price, quantity=qty)
                log_system_event("INFO", f"[硬體停損成功] 已強行賣出 {stock_code} 全數共 {qty:,.0f} 股。")
            except Exception as order_err:
                log_system_event("ERROR", f"[硬體停損失敗] 無法自動賣出 {stock_code}: {str(order_err)}")
                add_pending_liquidation_stock(stock_code)

def sync_broker_orders() -> None:
    """
    與券商對帳同步所有 PENDING 的訂單狀態。
    """
    is_paper = config.limits.is_paper_trading
    if is_paper:
        log_system_event("INFO", "[對帳同步] 模擬交易模式，跳過券商對帳同步。")
        return

    log_system_event("INFO", "[對帳同步] 開始進行券商對帳同步...")
    try:
        # 1. 取得所有真實交易且狀態為 PENDING 的訂單
        pending_orders = get_pending_real_orders()
        if not pending_orders:
            log_system_event("INFO", "[對帳同步] 目前無任何 PENDING 委託訂單，結束同步。")
            return

        # 2. 登入 Shioaji 并更新委託狀態
        api = _get_shioaji_api()
        api.update_status(api.stock_account)
        
        # 獲取今日委託紀錄
        trades = api.list_trades()
        
        # 建立一個 dictionary：order_id -> trade mapping
        trade_map = {}
        for t in trades:
            if t.status and t.status.id:
                trade_map[t.status.id] = t
        
        for order in pending_orders:
            order_db_id = order["id"]
            order_id = order.get("order_id")
            stock_code = order["stock_code"]
            action = order["action"]
            
            if not order_id:
                log_system_event("WARN", f"[對帳同步] 訂單 ID {order_db_id} ({stock_code}) 缺少券商 order_id，跳過")
                continue
                
            if order_id not in trade_map:
                log_system_event("WARN", f"[對帳同步] 找不到券商委託單號 {order_id}，可能尚未送出或非今日委託")
                continue
                
            trade = trade_map[order_id]
            status_val = trade.status.status
            status_name = status_val.name if hasattr(status_val, 'name') else str(status_val)
            
            log_system_event("INFO", f"[對帳同步] 正在同步訂單 {order_id} ({stock_code} {action}) | 券商狀態: {status_name}")
            
            if status_name in ["Filled", "PartFilled"]:
                deals = trade.status.deals
                if not deals:
                    log_system_event("WARN", f"[對帳同步] 訂單 {order_id} 狀態為 {status_name} 但無成交明細，暫不處理")
                    continue
                
                total_deal_qty = sum(float(d.quantity) for d in deals)
                if total_deal_qty == 0:
                    log_system_event("WARN", f"[對帳同步] 訂單 {order_id} 狀態為 {status_name} 但累計成交數量為 0，暫不處理")
                    continue
                    
                avg_exec_price = sum(float(d.price) * float(d.quantity) for d in deals) / total_deal_qty
                
                costs = calculate_fees(action, avg_exec_price, total_deal_qty)
                actual_fee = costs["fee"]
                actual_total_amount = costs["net_amount"]
                
                realized_pnl = 0.0
                if action == "SELL":
                    try:
                        current_holdings = get_holdings()
                        matching_holding = next((h for h in current_holdings if h["stock_code"] == stock_code), None)
                        if matching_holding:
                            avg_cost = float(matching_holding["average_price"])
                            realized_pnl = (avg_exec_price - avg_cost) * total_deal_qty - actual_fee
                    except Exception as he:
                        print(f" [對帳同步] 實盤計算平倉損益失敗: {str(he)}")
                
                updates = {
                    "status": "FILLED",
                    "execution_price": avg_exec_price,
                    "quantity": total_deal_qty,
                    "fee": actual_fee,
                    "total_amount": actual_total_amount,
                    "realized_pnl": realized_pnl
                }
                update_order_status(order_db_id, updates)
                
                update_holding_after_fill(
                    stock_code=stock_code,
                    action=action,
                    price=avg_exec_price,
                    quantity=total_deal_qty,
                    is_paper=False
                )
                
                log_system_event(
                    "INFO",
                    f" [對帳同步成功] 訂單 {order_id} 已成功轉為 FILLED | 實際成交價: {avg_exec_price} | 實際成交股數: {total_deal_qty} | 損益: {realized_pnl:,.0f} 元"
                )
                
            elif status_name in ["Cancelled", "Failed"]:
                updates = {
                    "status": status_name.upper(),
                    "total_amount": 0.0,
                    "fee": 0.0
                }
                update_order_status(order_db_id, updates)
                log_system_event(
                    "INFO",
                    f" [對帳同步] 訂單 {order_id} 已轉為 {status_name.upper()}，已釋放可用資金。"
                )
            else:
                log_system_event(
                    "INFO",
                    f"[對帳同步] 訂單 {order_id} 當前狀態為 {status_name}，保持 PENDING 狀態。"
                )
                
    except Exception as e:
        log_system_event("ERROR", f"[對帳同步] 對帳同步任務執行失敗: {str(e)}")
        # 發生異常時清空快取的 API 實例，確保下次能自動重新登入
        global _shioaji_api
        try:
            if _shioaji_api is not None:
                _shioaji_api.logout()
        except Exception:
            pass
        _shioaji_api = None

def sync_sandbox_orders(sim_date: str) -> None:
    """
    在沙盒模式下，模擬將前一日的 PENDING 訂單在今日 (sim_date) 成交。
    """
    # 1. 取得所有 status = 'PENDING' 且 is_paper = True 的模擬訂單
    pending_orders = execute_with_retry(
        lambda: supabase.table("trade_orders")
        .select("id, stock_code, action, price, quantity, fee, total_amount, executed_at, realized_pnl, status, execution_price, order_id")
        .eq("status", "PENDING")
        .eq("is_paper", True)
        .execute()
    )
    
    if not pending_orders:
        return
        
    log_system_event("INFO", f"[模擬對帳] 發現 {len(pending_orders)} 筆 PENDING 模擬訂單，正在今日 {sim_date} 進行模擬撮合成交...")
    
    from src.services import sandbox_simulator
    
    for order in pending_orders:
        order_db_id = order["id"]
        stock_code = order["stock_code"]
        action = order["action"]
        try:
            qty = float(order.get("quantity") or 0.0)
            limit_price = float(order.get("price") or 0.0)
        except (ValueError, TypeError):
            qty = 0.0
            limit_price = 0.0
        
        # 2. 獲取今日即時報價 (收盤價)
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        if not quote:
            log_system_event("WARN", f"[模擬對帳] 警告: 無法獲取 {stock_code} 在 {sim_date} 的模擬報價，跳過")
            continue
            
        price_val = quote.get("price") or limit_price if quote else limit_price
        try:
            exec_price = float(price_val) if price_val is not None else 0.0
        except (ValueError, TypeError):
            exec_price = 0.0
        
        # 3. 重新計算費用與實現損益 (買入以實際成交價，賣出計算與持股均價的損益)
        costs = calculate_fees(action, exec_price, qty)
        actual_fee = costs["fee"]
        actual_total_amount = costs["net_amount"]
        
        realized_pnl = 0.0
        if action == "SELL":
            try:
                # 取得持股庫存均價
                holdings = get_holdings()
                matching_holding = next((h for h in holdings if h.get("stock_code") == stock_code), None)
                if matching_holding:
                    avg_cost = float(matching_holding.get("average_price") or 0.0)
                    realized_pnl = (exec_price - avg_cost) * qty - actual_fee
            except Exception as e:
                print(f" [模擬對帳] 計算模擬平倉損益失敗: {e}")
                
        # 4. 更新訂單為 FILLED，且將 executed_at 更新為今日時間戳，以便出現在今天的報告中
        updates = {
            "status": "FILLED",
            "execution_price": exec_price,
            "fee": actual_fee,
            "total_amount": actual_total_amount,
            "realized_pnl": realized_pnl,
            "executed_at": f"{sim_date}T13:30:00Z"
        }
        
        execute_with_retry(
            lambda: supabase.table("trade_orders")
            .update(updates)
            .eq("id", order_db_id)
            .execute()
        )
        
        # 5. 更新持股表
        update_holding_after_fill(
            stock_code=stock_code,
            action=action,
            price=exec_price,
            quantity=qty,
            is_paper=True
        )
        
        log_system_event(
            "INFO",
            f" [模擬對帳成功] 訂單 ID {order_db_id} ({stock_code}) 於 {sim_date} 成交 | 成交價: {exec_price} | 股數: {qty}"
        )
