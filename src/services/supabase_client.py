# Path: src/services/supabase_client.py
import time
from datetime import datetime
from typing import Callable, Any, List, Dict, Optional
import httpx
from supabase import create_client, Client, ClientOptions
from src.config import config
from src.time_manager import get_utc_now

# 初始化 Supabase 用戶端
_supabase_url = config.supabase.url
_supabase_key = config.supabase.key

# 建立自訂的 httpx Client 以停用 HTTP/2，解決 Supabase API 連線不穩（ConnectionState.CLOSED、PROTOCOL_ERROR 等）的問題
# 並將逾時設定為與 Supabase 預設一致的 120 秒，避免 5 秒預設逾時影響大型查詢。
_custom_http_client = httpx.Client(
    http2=False,
    timeout=120.0
)
_client_options = ClientOptions(
    httpx_client=_custom_http_client
)

# 由於前置驗證已確保 url 與 key 存在，此處可安全初始化
supabase: Client = create_client(_supabase_url, _supabase_key, options=_client_options)

def _get_current_time_iso() -> str:
    """
    獲取目前符合 ISO 8601 格式之時間字串（若為沙盒模式，則使用模擬日期時間的 UTC 格式）
    """
    from src.time_manager import is_sandbox_active, get_effective_datetime
    if is_sandbox_active():
        from datetime import timezone
        eff_dt = get_effective_datetime()
        return eff_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return get_utc_now().isoformat().replace("+00:00", "Z")

def execute_with_retry(query_fn: Callable[[], Any], retries: int = 3, delay: float = 1.0) -> Any:
    """
    通用的資料庫操作重試包裝器（支援指數退避，若資料表不存在則立即中斷重試）
    """
    for attempt in range(1, retries + 1):
        try:
            response = query_fn()
            # supabase-py 回傳的 response 物件包含 .data
            return response.data
        except Exception as error:
            error_str = str(error)
            # 判斷是否為資料表不存在的錯誤 (PGRST205 或 relation does not exist)
            if "Could not find the table" in error_str or "does not exist" in error_str or "PGRST205" in error_str or "42P01" in error_str:
                print(f" [Supabase 錯誤] 偵測到資料表不存在，拒絕重試並立即回退: {error_str}")
                raise error
                
            if attempt == retries:
                print(f" [Supabase 錯誤] 經過 {retries} 次重試後仍失敗: {str(error)}")
                raise error
            print(f" [Supabase 警告] 查詢失敗 (第 {attempt} 次嘗試): {str(error)}，將在 {delay}s 後重試...")
            time.sleep(delay)
            delay *= 2  # 指數退避

# ==========================================================================
# 1. Gemini API 金鑰輪替與冷卻狀態 相關資料庫操作
# ==========================================================================

def get_gemini_keys_state() -> List[Dict[str, Any]]:
    """
    取得所有 Gemini API 金鑰的輪替與冷卻狀態。
    """
    return execute_with_retry(
        lambda: supabase.table("gemini_keys_state")
        .select("key_hash, use_count, rpm_limit, rpd_limit, last_used_at, cooled_until")
        .execute()
    )

def update_gemini_key_state(key_hash: str, updates: Dict[str, Any]) -> Any:
    """
    更新指定 Gemini API 金鑰的狀態（如調用計數、冷卻時間戳）。
    """
    payload = {
        **updates,
        "last_used_at": _get_current_time_iso()
    }
    return execute_with_retry(
        lambda: supabase.table("gemini_keys_state")
        .upsert({ "key_hash": key_hash, **payload }, on_conflict="key_hash")
        .execute()
    )

# ==========================================================================
# 2. 台股 K 線與即時數據 相關資料庫操作
# ==========================================================================

def get_stock_klines(stock_code: str, limit: int = 100) -> List[Dict[str, Any]]:
    """
    取得指定股票代號的歷史 K 線數據。
    """
    return execute_with_retry(
        lambda: supabase.table("stock_klines")
        .select("stock_code, date, open, high, low, close, volume")
        .eq("stock_code", stock_code)
        .order("date", desc=True)
        .limit(limit)
        .execute()
    )

def save_stock_klines(klines: List[Dict[str, Any]]) -> Any:
    """
    批次儲存或更新 K 線數據。
    """
    if not klines:
        return []

    records = []
    for k in klines:
        records.append({
            "stock_code": k["stockCode"],
            "date": k["date"],
            "open": k["open"],
            "high": k["high"],
            "low": k["low"],
            "close": k["close"],
            "volume": k["volume"],
            "updated_at": _get_current_time_iso()
        })

    return execute_with_retry(
        lambda: supabase.table("stock_klines")
        .upsert(records, on_conflict="stock_code,date")
        .execute()
    )

# ==========================================================================
# 3. 交易訂單與持股明細 相關資料庫操作
# ==========================================================================

def get_holdings() -> List[Dict[str, Any]]:
    """
    取得目前的持股明細。
    """
    is_paper = config.limits.is_paper_trading
    return execute_with_retry(
        lambda: supabase.table("holdings")
        .select("stock_code, quantity, average_price, updated_at")
        .eq("is_paper", is_paper)
        .gt("quantity", 0)
        .execute()
    )

def get_orders(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    sim_date: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    取得特定時間區間內的交易訂單。
    :param start_date: 依真實 executed_at 篩選起始時間（真實操盤模式用）
    :param end_date: 依真實 executed_at 篩選結束時間
    :param sim_date: 依沙盒虛擬日期精準篩選（如 '2026-05-05'，沙盒模式用）
    """
    is_paper = config.limits.is_paper_trading
    query = supabase.table("trade_orders").select(
        "id, stock_code, action, price, quantity, fee, total_amount, executed_at, realized_pnl, status, execution_price, order_id"
    ).eq("is_paper", is_paper)

    if sim_date:
        # 沙盒模式：若無 sim_date 欄位，利用 executed_at 進行台灣當天時間範圍過濾
        from src.time_manager import get_local_taiwan_midnight_utc_range
        utc_start, utc_end = get_local_taiwan_midnight_utc_range(sim_date)
        query = query.gte("executed_at", utc_start).lte("executed_at", utc_end)
    else:
        # 真實操盤：用真實 UTC 時間戳範圍過濾
        if start_date:
            query = query.gte("executed_at", start_date)
        if end_date:
            query = query.lte("executed_at", end_date)

    return execute_with_retry(
        lambda: query.order("executed_at", desc=True).execute()
    )

def execute_trade_transaction(order_detail: Dict[str, Any]) -> Dict[str, Any]:
    """
    寫入一筆新的交易訂單，並自動更新或刪除持股明細（封裝交易與帳務計算）。
    order_detail 可含 'simDate' 欄位（沙盒模式下傳入虛擬日期字串，如 '2026-05-05'）。
    """
    is_paper = config.limits.is_paper_trading

    # 1. 寫入交易訂單
    order_record = {
        "stock_code": order_detail["stockCode"],
        "action": order_detail["action"],  # 'BUY' 或 'SELL'
        "price": order_detail["price"],
        "quantity": order_detail["quantity"],
        "fee": order_detail.get("fee", 0.0),
        "total_amount": order_detail["totalAmount"],
        "realized_pnl": order_detail.get("realizedPnl", 0.0),
        "is_paper": is_paper,
        "executed_at": _get_current_time_iso(),
        "status": order_detail.get("status", "PENDING"),
        "execution_price": order_detail.get("executionPrice", None),
        "order_id": order_detail.get("orderId", None)
    }

    inserted_order = execute_with_retry(
        lambda: supabase.table("trade_orders").insert(order_record).execute()
    )

    # 僅在訂單狀態為 FILLED (status = 'FILLED') 時才執行更新/刪除持股表
    status = order_record["status"]
    if status == "FILLED":
        # 2. 獲取當前該個股持股
        holdings_res = execute_with_retry(
            lambda: supabase.table("holdings")
            .select("id, quantity, average_price")
            .eq("stock_code", order_detail["stockCode"])
            .eq("is_paper", is_paper)
            .execute()
        )

        existing_holding = holdings_res[0] if holdings_res else None

        new_quantity = 0.0
        new_avg_price = 0.0

        if order_detail["action"] == "BUY":
            if existing_holding:
                cur_qty = float(existing_holding["quantity"])
                cur_avg = float(existing_holding["average_price"])
                buy_qty = float(order_detail["quantity"])
                buy_price = float(order_detail["price"])

                new_quantity = cur_qty + buy_qty
                new_avg_price = ((cur_qty * cur_avg) + (buy_qty * buy_price)) / new_quantity
            else:
                new_quantity = float(order_detail["quantity"])
                new_avg_price = float(order_detail["price"])
        elif order_detail["action"] == "SELL":
            if existing_holding:
                cur_qty = float(existing_holding["quantity"])
                sell_qty = float(order_detail["quantity"])

                new_quantity = max(0.0, cur_qty - sell_qty)
                new_avg_price = float(existing_holding["average_price"]) if new_quantity > 0 else 0.0
            else:
                raise ValueError(f"無法執行賣出訂單：帳戶中無 {order_detail['stockCode']} 的持股")

        # 3. 更新持股表
        if new_quantity > 0:
            holding_record = {
                "stock_code": order_detail["stockCode"],
                "quantity": new_quantity,
                "average_price": new_avg_price,
                "is_paper": is_paper,
                "updated_at": _get_current_time_iso()
            }
            execute_with_retry(
                lambda: supabase.table("holdings")
                .upsert(holding_record, on_conflict="stock_code,is_paper")
                .execute()
            )
        else:
            # 若持股降為 0，則刪除該持股紀錄
            if existing_holding:
                execute_with_retry(
                    lambda: supabase.table("holdings")
                    .delete()
                    .eq("id", existing_holding["id"])
                    .execute()
                )

    return inserted_order[0] if inserted_order else {}

def update_holding_after_fill(stock_code: str, action: str, price: float, quantity: float, is_paper: bool = False) -> None:
    """
    對帳同步成交後，更新資料庫的持股明細庫存。
    """
    # 1. 獲取當前該個股持股
    holdings_res = execute_with_retry(
        lambda: supabase.table("holdings")
        .select("id, quantity, average_price")
        .eq("stock_code", stock_code)
        .eq("is_paper", is_paper)
        .execute()
    )

    existing_holding = holdings_res[0] if holdings_res else None

    new_quantity = 0.0
    new_avg_price = 0.0

    if action == "BUY":
        if existing_holding:
            cur_qty = float(existing_holding["quantity"])
            cur_avg = float(existing_holding["average_price"])
            buy_qty = float(quantity)
            buy_price = float(price)

            new_quantity = cur_qty + buy_qty
            new_avg_price = ((cur_qty * cur_avg) + (buy_qty * buy_price)) / new_quantity
        else:
            new_quantity = float(quantity)
            new_avg_price = float(price)
    elif action == "SELL":
        if existing_holding:
            cur_qty = float(existing_holding["quantity"])
            sell_qty = float(quantity)

            new_quantity = max(0.0, cur_qty - sell_qty)
            new_avg_price = float(existing_holding["average_price"]) if new_quantity > 0 else 0.0
        else:
            print(f" [Supabase] 警告: 嘗試平倉賣出但持股表中無 {stock_code} 的任何持股")
            return

    # 3. 更新持股表
    if new_quantity > 0:
        holding_record = {
            "stock_code": stock_code,
            "quantity": new_quantity,
            "average_price": new_avg_price,
            "is_paper": is_paper,
            "updated_at": _get_current_time_iso()
        }
        execute_with_retry(
            lambda: supabase.table("holdings")
            .upsert(holding_record, on_conflict="stock_code,is_paper")
            .execute()
        )
    else:
        # 若持股降為 0，則刪除該持股紀錄
        if existing_holding:
            execute_with_retry(
                lambda: supabase.table("holdings")
                .delete()
                .eq("id", existing_holding["id"])
                .execute()
            )

def get_pending_real_orders() -> List[Dict[str, Any]]:
    """
    取得所有狀態為 PENDING 且為真實交易的訂單
    """
    return execute_with_retry(
        lambda: supabase.table("trade_orders")
        .select("id, stock_code, action, price, quantity, fee, total_amount, executed_at, realized_pnl, status, execution_price, order_id")
        .eq("status", "PENDING")
        .eq("is_paper", False)
        .execute()
    )

def update_order_status(order_id_db: int, updates: Dict[str, Any]) -> Any:
    """
    更新指定 id 的訂單狀態及相關欄位
    """
    return execute_with_retry(
        lambda: supabase.table("trade_orders")
        .update(updates)
        .eq("id", order_id_db)
        .execute()
    )

# ==========================================================================
# 4. 系統日誌 相關資料庫操作
# ==========================================================================

def log_system_event(
    level: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    sim_date: Optional[str] = None
) -> None:
    """
    寫入中文或英文系統運行日誌。同時輸出至標準主機控制台。
    :param sim_date: 沙盒模擬虛擬日期（如 '2026-05-05'），真實操盤時不傳（預設 None）
    """
    if details is None:
        details = {}

    timestamp = datetime.utcnow().isoformat() + "Z"
    log_prefix = f"[{timestamp}] [{level}]"
    
    if level == "ERROR":
        print(f"{log_prefix} {message} Details: {details}")
    elif level == "WARN":
        print(f"{log_prefix} {message} Details: {details}")
    else:
        print(f"{log_prefix} {message}")

    try:
        supabase.table("system_logs").insert({
            "level": level,
            "message": message,
            "details": details,
            "created_at": timestamp
            # sim_date 欄位已從資料庫中移除，日誌統一使用實際時間 created_at，模擬訊息已寫在 message 中
        }).execute()
    except Exception as err:
        print(f"[Supabase Log Error] 無法寫入日誌到資料庫: {str(err)}")

# ==========================================================================
# 5. 自選股與動態配置 相關資料庫操作 (Web 控制台專用)
# ==========================================================================

def get_db_watchlist() -> List[str]:
    """
    獲取 Supabase 資料表中已儲存的自選股代號列表。
    若資料表不存在則拋出例外，以便調用端執行本機回退。
    """
    res = execute_with_retry(
        lambda: supabase.table("watchlist")
        .select("stock_code")
        .execute()
    )
    return [r["stock_code"] for r in res]

def add_to_db_watchlist(stock_code: str) -> None:
    """
    新增股票代號至資料庫自選股列表。
    """
    execute_with_retry(
        lambda: supabase.table("watchlist")
        .upsert({"stock_code": stock_code}, on_conflict="stock_code")
        .execute()
    )

def delete_from_db_watchlist(stock_code: str) -> None:
    """
    從資料庫自選股列表中刪除股票代號。
    """
    execute_with_retry(
        lambda: supabase.table("watchlist")
        .delete()
        .eq("stock_code", stock_code)
        .execute()
    )

def get_db_config() -> Dict[str, str]:
    """
    獲取 Supabase 資料表中已儲存的動態系統設定字典。
    若資料表不存在則拋出例外，以便調用端執行本機回退。
    """
    res = execute_with_retry(
        lambda: supabase.table("system_config")
        .select("key, value")
        .execute()
    )
    return {r["key"]: r["value"] for r in res}

def set_db_config(key: str, value: str) -> None:
    """
    新增或更新資料庫中的系統設定參數。
    """
    execute_with_retry(
        lambda: supabase.table("system_config")
        .upsert({"key": key, "value": str(value)}, on_conflict="key")
        .execute()
    )

def clear_db_sandbox_data() -> None:
    """
    清除資料庫中的沙盒模擬交易資料（包含 holdings 與 trade_orders 中的 is_paper=True 紀錄）。
    """
    execute_with_retry(
        lambda: supabase.table("holdings")
        .delete()
        .eq("is_paper", True)
        .execute()
    )
    execute_with_retry(
        lambda: supabase.table("trade_orders")
        .delete()
        .eq("is_paper", True)
        .execute()
    )
    log_system_event("INFO", "已手動清除所有沙盒模擬交易與持股紀錄")

def prune_old_db_logs(days: int = 7) -> None:
    """
    自動清理過於久遠的系統日誌，防止資料庫容量爆滿。
    預設只保留最近 7 天的日誌。
    """
    from datetime import datetime, timedelta
    cutoff_time = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    try:
        execute_with_retry(
            lambda: supabase.table("system_logs")
            .delete()
            .lt("created_at", cutoff_time)
            .execute()
        )
        print(f" [日誌管理器] 已成功清理 {cutoff_time} 之前的舊系統日誌（保留最近 {days} 天）。")
    except Exception as err:
        print(f" [日誌管理器] 警告: 清理舊日誌失敗: {str(err)}")

# ==========================================================================
# 6. 等候平倉列表與全局系統故障 相關資料庫操作
# ==========================================================================

def get_pending_liquidation_stocks() -> List[str]:
    """
    獲取目前處於等候平倉（智慧排隊）狀態的股票代號列表。
    """
    try:
        config_dict = get_db_config()
        val = config_dict.get("PENDING_LIQUIDATION_STOCKS", "")
        if not val:
            return []
        return [c.strip() for c in val.split(",") if c.strip()]
    except Exception as e:
        print(f" [Supabase] 讀取等候平倉股票失敗: {str(e)}")
        return []

def add_pending_liquidation_stock(stock_code: str) -> None:
    """
    將股票代號加入等候平倉列表。
    """
    try:
        stocks = get_pending_liquidation_stocks()
        if stock_code not in stocks:
            stocks.append(stock_code)
            set_db_config("PENDING_LIQUIDATION_STOCKS", ",".join(stocks))
            log_system_event("INFO", f"股票 {stock_code} 已加入等候平倉(智慧排隊)列表")
    except Exception as e:
        print(f" [Supabase] 加入等候平倉列表失敗: {str(e)}")

def remove_pending_liquidation_stock(stock_code: str) -> None:
    """
    從等候平倉列表中移除指定股票代號。
    """
    try:
        stocks = get_pending_liquidation_stocks()
        if stock_code in stocks:
            stocks.remove(stock_code)
            set_db_config("PENDING_LIQUIDATION_STOCKS", ",".join(stocks))
            log_system_event("INFO", f"股票 {stock_code} 已從等候平倉列表移除(解鎖)")
    except Exception as e:
        print(f" [Supabase] 移除等候平倉列表失敗: {str(e)}")

def get_system_fault_status() -> Dict[str, Any]:
    """
    獲取全局系統故障狀態，回傳格式例如: {"status": "OK", "detail": ""} 或 {"status": "FAULT", "detail": "error message"}
    """
    try:
        config_dict = get_db_config()
        val = config_dict.get("SYSTEM_FAULT_STATUS", "OK")
        if val.startswith("FAULT:"):
            return {"status": "FAULT", "detail": val[6:]}
        return {"status": "OK", "detail": ""}
    except Exception as e:
        print(f" [Supabase] 獲取系統故障狀態失敗: {str(e)}")
        return {"status": "OK", "detail": ""}

def set_system_fault_status(status: str, detail: str = "") -> None:
    """
    設定全局系統故障狀態。
    """
    try:
        val = "OK" if status == "OK" else f"FAULT:{detail}"
        set_db_config("SYSTEM_FAULT_STATUS", val)
        log_system_event("WARN" if status == "FAULT" else "INFO", f"系統故障狀態已設為 {status} (詳情: {detail})")
    except Exception as e:
        print(f" [Supabase] 設定系統故障狀態失敗: {str(e)}")



