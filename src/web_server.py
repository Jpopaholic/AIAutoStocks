# Path: src/web_server.py
import os
import sys
import time
import threading
from datetime import datetime
from typing import Dict, Any, List
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add workspace directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import config, get_stock_name
from src.services.supabase_client import (
    get_db_watchlist,
    add_to_db_watchlist,
    delete_from_db_watchlist,
    get_db_config,
    set_db_config,
    get_holdings,
    get_orders,
    log_system_event,
    get_pending_liquidation_stocks,
    remove_pending_liquidation_stock,
    get_system_fault_status,
    set_system_fault_status,
    execute_with_retry,
    supabase
)
from src.services.nav_calculator import calculate_nav

app = FastAPI(title="AIAutoStocks Dashboard API")

# Enable CORS for local testing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Background runner state
is_running = False
is_running_lock = threading.Lock()
is_liquidating = False
is_liquidating_lock = threading.Lock()
stop_requested = False
last_run_status = "尚未執行過手動交易"
last_run_time = "無"

@app.on_event("startup")
def on_startup():
    try:
        from src.services.supabase_client import prune_old_db_logs
        # 自動清理 7 天前的舊日誌
        prune_old_db_logs(days=7)
    except Exception as e:
        print(f" [啟動任務] 警告: 自動清理舊日誌失敗: {e}")

    # 啟動自檢：如果自動交易開關在啟動前為開啟，且是實時交易模式，則在啟動時自動拉起定時排程引擎 (永動機)
    try:
        from src.config import config
        if config.is_auto_trading_active and not config.limits.is_paper_trading:
            global is_running
            with is_running_lock:
                if not is_running:
                    is_running = True
                    t = threading.Thread(target=run_trading_job_in_background, daemon=True)
                    t.start()
                    print(" [啟動任務] 已成功在背景自動重新啟動實時交易定時排程引擎 (永動機)。")
    except Exception as auto_err:
        print(f" [啟動任務] 自動拉起定時排程引擎失敗: {auto_err}")

from fastapi import Request
from fastapi.responses import JSONResponse
from src.services.totp_service import verify_session_token, verify_totp, get_totp_url, get_totp_secret, create_session_token

class VerifyOTPRequest(BaseModel):
    code: str

class SetupOTPRequest(BaseModel):
    master_key: str

class StockCodeRequest(BaseModel):
    stock_code: str

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in ["/api/auth/verify", "/api/auth/setup"]:
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            
        if not token or not verify_session_token(token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized: Invalid or expired session token"}
            )
            
    response = await call_next(request)
    return response

@app.post("/api/auth/setup")
def api_setup_otp(payload: SetupOTPRequest):
    config_master = config.credentials.master_key or "default_master_key"
    if payload.master_key != config_master:
        raise HTTPException(status_code=401, detail="驗證失敗：Master Key 不正確")
    return {
        "status": "ok",
        "secret": get_totp_secret(),
        "url": get_totp_url()
    }

@app.post("/api/auth/verify")
def api_verify_otp(payload: VerifyOTPRequest):
    if verify_totp(payload.code):
        token = create_session_token()
        return {
            "status": "ok",
            "token": token
        }
    raise HTTPException(status_code=401, detail="驗證碼無效或已過期")


class WatchlistItem(BaseModel):
    stock_code: str

class ConfigUpdate(BaseModel):
    settings: Dict[str, Any]

def run_trading_job_in_background():
    global is_running, last_run_status, last_run_time, stop_requested
    try:
        from src.services.supabase_client import log_system_event, prune_old_db_logs
        # 交易執行前先清理 7 天前的舊日誌
        try:
            prune_old_db_logs(days=7)
        except Exception as prune_err:
            print(f"[日誌清理] 警告: 自動清理舊日誌失敗: {prune_err}")
            
        log_system_event("INFO", "=== 開始執行網頁端手動觸發交易任務 ===")
        stop_requested = False  # 每次啟動前重置停止旗標
        start_mode_is_paper = config.limits.is_paper_trading
        
        # 1. 獲取當前自選股（優先 Supabase，回退本機 watchlist.json）
        stock_codes = []
        try:
            stock_codes = get_db_watchlist()
            if stock_codes:
                log_system_event("INFO", f"已成功自 Supabase 載入自選股: {stock_codes}")
        except Exception as err:
            log_system_event("WARN", f"無法自 Supabase 載入自選股: {str(err)}，嘗試讀取本機 watchlist.json")

        if not stock_codes:
            # 回退讀本機 watchlist.json（不再使用寫死的預設股票）
            import json as _json
            _wl_path = os.path.join(os.getcwd(), "watchlist.json")
            if os.path.exists(_wl_path):
                try:
                    with open(_wl_path, "r", encoding="utf-8") as _f:
                        _local = _json.load(_f)
                        if isinstance(_local, list) and _local:
                            stock_codes = _local
                            log_system_event("INFO", f"已自本機 watchlist.json 載入自選股: {stock_codes}")
                except Exception as _fe:
                    log_system_event("WARN", f"讀取本機 watchlist.json 失敗: {str(_fe)}")

        # 1b. 強制合併目前的持股標的（持倉股永遠進入 AI 決策範疇，確保能執行賣出）
        try:
            current_holdings = get_holdings()
            held_codes = [h["stock_code"] for h in current_holdings if h.get("stock_code")]
            added_from_holdings = [c for c in held_codes if c not in stock_codes]
            if added_from_holdings:
                stock_codes = stock_codes + added_from_holdings
                log_system_event("INFO", f"已將目前持股強制合併至分析標的: {added_from_holdings} → 總標的: {stock_codes}")
            if not stock_codes and held_codes:
                stock_codes = held_codes
                log_system_event("INFO", f"自選股為空，以目前持股作為分析標的: {stock_codes}")
        except Exception as h_err:
            log_system_event("WARN", f"獲取目前持股失敗，僅以自選股為準: {str(h_err)}")

        if not stock_codes:
            log_system_event("WARN", "自選股與持股均為空，本輪交易任務取消。")
            last_run_status = "取消：無自選股或持股"
            return
            
        last_run_status = "執行中..."
        from src.time_manager import get_local_taiwan_datetime_str
        last_run_time = get_local_taiwan_datetime_str()
        
        # 2. 呼叫主交易流程
        if config.limits.is_paper_trading:
            from src.main import run_sandbox_simulation
            run_sandbox_simulation(
                stock_codes,
                start_date=config.sandbox_start_date,
                end_date=config.sandbox_end_date,
                should_stop=lambda: stop_requested
            )
        else:
            from src.main import run_live_trading_job
            from src.time_manager import get_local_taiwan_datetime
            
            # 第一步：手動觸發時，立即執行第一輪實盤交易任務
            log_system_event("INFO", "[永動機] 手動觸發啟動：立即執行第一輪實盤交易任務")
            run_live_trading_job(stock_codes)
            
            last_run_date = get_local_taiwan_datetime().date()
            log_system_event("INFO", f"[永動機] 已完成初始交易輪次。進入每日定時自動交易循環 (目標時間: 每日 14:00 - 14:15 台灣時間，排除週末)")
            
            # 進入永動機定時排程循環
            while not stop_requested:
                # 靈敏偵測：每隔 30 秒分段 Sleep 1 秒，快速響應 stop_requested
                for _ in range(30):
                    if stop_requested:
                        break
                    time.sleep(1)
                
                if stop_requested:
                    break
                
                # 運行中防竄改交易模式自檢鎖
                current_mode_is_paper = config.limits.is_paper_trading
                if current_mode_is_paper != start_mode_is_paper:
                    msg = f"[永動機] 偵測到交易模式 (PAPER_TRADING_MODE) 被修改（啟動時為 {'模擬' if start_mode_is_paper else '實盤'}, 目前為 {'模擬' if current_mode_is_paper else '實盤'}），為防誤傷與安全起見，已自動強制終止背景定時循環！請重新啟動交易任務。"
                    log_system_event("ERROR", msg)
                    last_run_status = "錯誤：運行中交易模式被修改，已強制停止"
                    break
                
                tw_now = get_local_taiwan_datetime()
                current_date = tw_now.date()
                
                # 若跨天
                if current_date > last_run_date:
                    # 目標觸發時段：每日 14:00 - 14:15 之間
                    if tw_now.hour == 14 and 0 <= tw_now.minute < 15:
                        # 排除週末 (週六是 5, 週日是 6)
                        if tw_now.weekday() not in (5, 6):
                            log_system_event("INFO", f"[永動機] 跨天偵測觸發：開始執行當日 ({current_date}) 實盤自動交易...")
                            # 更新最後執行狀態與時間
                            from src.time_manager import get_local_taiwan_datetime_str
                            last_run_time = get_local_taiwan_datetime_str()
                            last_run_status = "定時任務執行中..."
                            try:
                                run_live_trading_job(stock_codes)
                                last_run_status = "定時任務成功完成"
                                log_system_event("INFO", f"[永動機] 當日 ({current_date}) 實盤自動交易順利完成。")
                            except Exception as ex:
                                last_run_status = f"定時任務失敗: {str(ex)}"
                                log_system_event("ERROR", f"[永動機] 執行定時自動交易出錯: {str(ex)}")
                        else:
                            log_system_event("INFO", f"[永動機] 今日 ({current_date}) 為週末非交易日，跳過當日定時交易排程。")
                        
                        # 不論成敗或週末，皆標記為此日已處理，避免在該時段內重複觸發
                        last_run_date = current_date
        
        if stop_requested:
            last_run_status = "已被使用者手動停止"
            log_system_event("INFO", "=== 網頁端交易任務已被使用者手動停止 ===")
        elif last_run_status.startswith("錯誤："):
            pass
        else:
            last_run_status = "成功完成"
            log_system_event("INFO", "=== 網頁端手動觸發交易任務順利完成 ===")

    except Exception as e:
        last_run_status = f"失敗: {str(e)}"
        from src.services.supabase_client import log_system_event
        log_system_event("ERROR", f"網頁端手動觸發交易任務時發生異常: {str(e)}")
    finally:
        with is_running_lock:
            is_running = False

@app.get("/", response_class=HTMLResponse)
def read_index():
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(
        content="<h1>index.html 尚未建立。請在 src/static/ 下建立 index.html。</h1>",
        status_code=404
    )

@app.get("/api/status")
def get_status():
    global is_running, last_run_status, last_run_time, stop_requested
    try:
        cash_balance, holdings_value, net_asset_value = calculate_nav()
        
        # 取得持股並添加現價與盈虧資訊
        enhanced_holdings = []
        try:
            holdings = get_holdings()
            from src.services import sandbox_simulator
            for h in holdings:
                stock_code = h["stock_code"]
                qty = float(h["quantity"])
                avg_price = float(h["average_price"])
                quote = sandbox_simulator.fetch_realtime_quote(stock_code)
                current_price = float(quote.get("price") or avg_price)
                market_value = qty * current_price
                cost = qty * avg_price
                pnl = market_value - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0.0
                
                enhanced_holdings.append({
                    "stock_code": stock_code,
                    "stock_name": get_stock_name(stock_code),
                    "quantity": qty,
                    "average_price": avg_price,
                    "current_price": current_price,
                    "market_value": market_value,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct
                })
        except Exception as e:
            print(f"[Web API] 載入持股失敗: {e}")
            
        try:
            orders = get_orders()
            orders = orders[:15] # 僅回傳前15筆
            # 為訂單添加股票名稱
            enhanced_orders = []
            for o in orders:
                o_dict = dict(o)
                o_dict["stock_name"] = get_stock_name(o_dict["stock_code"])
                enhanced_orders.append(o_dict)
            orders = enhanced_orders
        except Exception as e:
            orders = []
            print(f"[Web API] 載入交易訂單失敗: {e}")
            
        return {
            "cash": cash_balance,
            "holdings_value": holdings_value,
            "nav": net_asset_value,
            "holdings": enhanced_holdings,
            "orders": orders,
            "is_paper": config.limits.is_paper_trading,
            "is_running": is_running,
            "is_liquidating": is_liquidating,
            "stop_requested": stop_requested,
            "last_run_status": last_run_status,
            "last_run_time": last_run_time
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"載入儀表板狀態失敗: {str(e)}")

@app.post("/api/sync")
def api_sync_orders():
    """
    手動對帳同步：調用券商與資料庫進行未成交訂單狀態同步
    """
    try:
        from src.services.broker_connector import sync_broker_orders
        sync_broker_orders()
        return {"status": "ok", "message": "對帳同步已執行完成"}
    except Exception as e:
        log_system_event("ERROR", f"網頁端手動觸發對帳同步時發生異常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"對帳同步失敗: {str(e)}")

@app.get("/api/watchlist")
def api_get_watchlist():
    try:
        watchlist = get_db_watchlist()
        watchlist_details = [{"code": code, "name": get_stock_name(code)} for code in watchlist]
        return {"watchlist": watchlist, "watchlist_details": watchlist_details, "fallback": False}
    except Exception as e:
        import json
        watchlist_path = os.path.join(os.getcwd(), "watchlist.json")
        fallback_wl = []
        if os.path.exists(watchlist_path):
            try:
                with open(watchlist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        fallback_wl = data
            except Exception:
                pass
        if not fallback_wl:
            from src.config import resolve_stock_codes
            fallback_wl = resolve_stock_codes("2330,2454")
        
        watchlist_details = [{"code": code, "name": get_stock_name(code)} for code in fallback_wl]
        return {"watchlist": fallback_wl, "watchlist_details": watchlist_details, "fallback": True, "error": str(e)}

@app.post("/api/watchlist")
def api_add_watchlist(item: WatchlistItem):
    try:
        from src.config import resolve_stock_codes
        resolved_codes = resolve_stock_codes(item.stock_code)
        if not resolved_codes:
            raise HTTPException(status_code=400, detail="無效的股票代號")
            
        try:
            # 嘗試寫入資料庫
            for code in resolved_codes:
                add_to_db_watchlist(code)
            return {"status": "ok", "message": f"成功新增自選股: {', '.join(resolved_codes)}"}
        except Exception as db_err:
            print(f"[Web API] 寫入 Supabase 自選股失敗 ({str(db_err)})，將回退寫入本機 watchlist.json...")
            import json
            watchlist_path = os.path.join(os.getcwd(), "watchlist.json")
            watchlist = []
            if os.path.exists(watchlist_path):
                try:
                    with open(watchlist_path, "r", encoding="utf-8") as f:
                        watchlist = json.load(f)
                        if not isinstance(watchlist, list):
                            watchlist = []
                except Exception:
                    pass
            
            added_any = False
            for code in resolved_codes:
                if code not in watchlist:
                    watchlist.append(code)
                    added_any = True
                    
            if added_any:
                with open(watchlist_path, "w", encoding="utf-8") as f:
                    json.dump(watchlist, f, indent=2, ensure_ascii=False)
                    
            return {"status": "ok", "message": f"資料表未建立，已將自選股 {', '.join(resolved_codes)} 加入本機 watchlist.json！"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"無法寫入自選股: {str(e)}")

@app.delete("/api/watchlist/{stock_code}")
def api_delete_watchlist(stock_code: str):
    try:
        delete_from_db_watchlist(stock_code)
        return {"status": "ok", "message": f"成功自自選股刪除 {stock_code}"}
    except Exception as db_err:
        print(f"[Web API] 自 Supabase 刪除自選股失敗 ({str(db_err)})，將回退修改本機 watchlist.json...")
        try:
            import json
            watchlist_path = os.path.join(os.getcwd(), "watchlist.json")
            if os.path.exists(watchlist_path):
                with open(watchlist_path, "r", encoding="utf-8") as f:
                    watchlist = json.load(f)
                if isinstance(watchlist, list) and stock_code in watchlist:
                    watchlist.remove(stock_code)
                    with open(watchlist_path, "w", encoding="utf-8") as f:
                        json.dump(watchlist, f, indent=2, ensure_ascii=False)
                return {"status": "ok", "message": "資料表未建立，已自本機 watchlist.json 移除！"}
            return {"status": "ok", "message": "本機無自選股檔案。"}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"無法自本機自選股刪除: {str(e)}")

@app.get("/api/analysis-scope")
def api_get_analysis_scope():
    """回傳本輪 AI 將實際分析的股票範疇 (watchlist ∪ 目前持股)"""
    try:
        watchlist = []
        try:
            watchlist = get_db_watchlist()
        except Exception:
            import json as _json
            _wl_path = os.path.join(os.getcwd(), "watchlist.json")
            if os.path.exists(_wl_path):
                try:
                    with open(_wl_path, "r", encoding="utf-8") as _f:
                        _local = _json.load(_f)
                        if isinstance(_local, list):
                            watchlist = _local
                except Exception:
                    pass

        held_codes = []
        try:
            holdings = get_holdings()
            held_codes = [h["stock_code"] for h in holdings if h.get("stock_code")]
        except Exception:
            pass

        scope = list(watchlist)  # watchlist first
        for c in held_codes:
            if c not in scope:
                scope.append(c)

        return {
            "scope": scope,
            "watchlist": watchlist,
            "from_holdings": [c for c in held_codes if c not in watchlist],
            "total": len(scope)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"獲取分析範疇失敗: {str(e)}")

@app.get("/api/presets")
def api_get_presets():
    try:
        from src.config import STOCK_PRESETS, STOCK_PRESETS_INFO
        result = {}
        for key, codes in STOCK_PRESETS.items():
            info = STOCK_PRESETS_INFO.get(key, {"name": key, "desc": ""})
            result[key] = {
                "name": info["name"],
                "desc": info["desc"],
                "codes": codes
            }
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"獲取預設股群失敗: {str(e)}")

@app.get("/api/config")
def api_get_config():
    try:
        db_cfg = {}
        try:
            db_cfg = get_db_config()
        except Exception:
            pass
            
        return {
            "TRADING_LIMIT_SINGLE_STOCK_PCT": db_cfg.get("TRADING_LIMIT_SINGLE_STOCK_PCT", str(config.limits.single_stock_pct or "")),
            "TRADING_LIMIT_DAILY_TOTAL_PCT": db_cfg.get("TRADING_LIMIT_DAILY_TOTAL_PCT", str(config.limits.daily_total_pct or "")),
            "HARD_STOP_LOSS_PCT": db_cfg.get("HARD_STOP_LOSS_PCT", str(config.limits.hard_stop_loss_pct or "")),
            "INITIAL_CASH": db_cfg.get("INITIAL_CASH", str(config.limits.initial_cash)),
            "PAPER_TRADING_MODE": db_cfg.get("PAPER_TRADING_MODE", str(config.limits.is_paper_trading).lower()),
            "TAIWAN_STOCK_TIMEZONE": db_cfg.get("TAIWAN_STOCK_TIMEZONE", config.timezone),
            "GEMINI_MODEL": db_cfg.get("GEMINI_MODEL", config.gemini_model),
            "SANDBOX_START_DATE": db_cfg.get("SANDBOX_START_DATE", config.sandbox_start_date),
            "SANDBOX_END_DATE": db_cfg.get("SANDBOX_END_DATE", config.sandbox_end_date)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"獲取動態配置失敗: {str(e)}")

@app.post("/api/config")
def api_update_config(payload: ConfigUpdate):
    global is_running
    if is_running:
        new_paper_mode_val = payload.settings.get("PAPER_TRADING_MODE")
        if new_paper_mode_val is not None:
            new_paper_mode = str(new_paper_mode_val).lower() == "true"
            if new_paper_mode != config.limits.is_paper_trading:
                raise HTTPException(
                    status_code=400,
                    detail="交易任務正在執行中，禁止修改交易模式 (PAPER_TRADING_MODE)。請先點選「停止」交易任務。"
                )
    try:
        # 1. 嘗試寫入 Supabase 資料庫
        try:
            for k, v in payload.settings.items():
                set_db_config(k, str(v))
            return {"status": "ok", "message": "已成功將設定更新至 Supabase 資料庫！"}
        except Exception as db_err:
            # 2. 資料表不存在或資料庫連線失敗，回退寫入本機 config.json
            print(f"[Web API] 寫入 Supabase 失敗 ({str(db_err)})，將回退寫入本機 config.json...")
            
            import json
            config_path = os.path.join(os.getcwd(), "config.json")
            
            existing_config = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        existing_config = json.load(f)
                except Exception:
                    pass
            
            for k, v in payload.settings.items():
                if k in ["INITIAL_CASH", "TRADING_LIMIT_SINGLE_STOCK", "TRADING_LIMIT_DAILY_TOTAL"]:
                    try:
                        existing_config[k] = float(v)
                    except ValueError:
                        existing_config[k] = str(v)
                elif k in ["TRADING_LIMIT_SINGLE_STOCK_PCT", "TRADING_LIMIT_DAILY_TOTAL_PCT", "HARD_STOP_LOSS_PCT"]:
                    if v is None or str(v).strip() == "":
                        existing_config[k] = None
                    else:
                        try:
                            existing_config[k] = float(v)
                        except ValueError:
                            existing_config[k] = str(v)
                elif k == "PAPER_TRADING_MODE":
                    existing_config[k] = str(v).lower() == "true"
                else:
                    existing_config[k] = str(v)
            
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(existing_config, f, indent=2, ensure_ascii=False)
                return {"status": "ok", "message": "資料表未建立，已自動將設定寫入本機 config.json 檔案！"}
            except Exception as io_err:
                print(f"[Web API] 警告: 無法寫入 config.json (可能為唯讀檔案系統): {str(io_err)}")
                return {"status": "ok", "message": "已成功更新記憶體設定（但本機唯讀檔案系統無法寫入 config.json）"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新動態配置失敗: {str(e)}")

@app.get("/api/logs")
def api_get_logs():
    try:
        from src.services.supabase_client import supabase
        res = supabase.table("system_logs").select("*").order("created_at", desc=True).limit(40).execute()
        return res.data
    except Exception as e:
        return [{
            "message": f"無法載入系統日誌 (尚未建立 system_logs 表或連線異常): {str(e)}",
            "level": "WARN",
            "created_at": datetime.utcnow().isoformat() + "Z"
        }]

@app.post("/api/sandbox/clear")
def api_clear_sandbox():
    if not config.limits.is_paper_trading:
        raise HTTPException(
            status_code=400,
            detail="目前處於【實盤交易模式】，禁止清除交易資料！"
        )
    try:
        from src.services.supabase_client import clear_db_sandbox_data
        clear_db_sandbox_data()
        return {"status": "ok", "message": "已成功清除所有模擬交易與持股紀錄！"}
    except Exception as e:
        err_msg = str(e)
        if "42P01" in err_msg or "PGRST205" in err_msg or "relation" in err_msg:
            return {"status": "ok", "message": "資料表未建立或無模擬交易資料，無須清除。"}
        raise HTTPException(
            status_code=500,
            detail=f"清除模擬交易資料失敗: {err_msg}"
        )

def run_liquidate_in_background():
    global is_liquidating
    with is_liquidating_lock:
        is_liquidating = True
    try:
        from src.main import run_liquidate_job
        run_liquidate_job()
    finally:
        with is_liquidating_lock:
            is_liquidating = False

@app.post("/api/liquidate")
def api_liquidate(background_tasks: BackgroundTasks):
    global stop_requested, is_running
    try:
        # 1. 停止目前正在執行的背景交易任務
        if is_running:
            stop_requested = True
            log_system_event("WARN", "已手動發送停止自動交易任務訊號（準備執行下車清倉）")
            
        # 2. 自動關閉自動交易開關 (AUTO_TRADING_ACTIVE = false)
        try:
            set_db_config("AUTO_TRADING_ACTIVE", "false")
        except Exception as db_err:
            # 回退寫入 config.json
            import json
            config_path = os.path.join(os.getcwd(), "config.json")
            existing_config = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        existing_config = json.load(f)
                except Exception:
                    pass
            existing_config["AUTO_TRADING_ACTIVE"] = "false"
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(existing_config, f, indent=2, ensure_ascii=False)
            except Exception as io_err:
                print(f"[Web API] 警告: 無法寫入 config.json (可能為唯讀檔案系統): {str(io_err)}")
        
        # 3. 在背景非同步執行下車平倉任務
        background_tasks.add_task(run_liquidate_in_background)
        
        return {"status": "ok", "message": "已成功停止自動交易並關閉開關，且已於背景啟動下車平倉流程！"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下車平倉失敗: {str(e)}")

@app.post("/api/trigger")
def api_trigger_run(background_tasks: BackgroundTasks):
    global is_running
    with is_running_lock:
        if is_running:
            return {"status": "error", "message": "交易任務正在執行中，請勿重複觸發。"}
        is_running = True
        
    # 動態重啟自動交易開關 (AUTO_TRADING_ACTIVE = true)
    try:
        set_db_config("AUTO_TRADING_ACTIVE", "true")
    except Exception as db_err:
        # 回退寫入 config.json
        import json
        config_path = os.path.join(os.getcwd(), "config.json")
        existing_config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    existing_config = json.load(f)
            except Exception:
                pass
        existing_config["AUTO_TRADING_ACTIVE"] = "true"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(existing_config, f, indent=2, ensure_ascii=False)
        except Exception as io_err:
            print(f"[Web API] 警告: 無法寫入 config.json (可能為唯讀檔案系統): {str(io_err)}")

    t = threading.Thread(target=run_trading_job_in_background, daemon=True)
    t.start()
    return {"status": "ok", "message": "自動交易排程已成功在背景啟動，且已重啟自動交易開關！"}


@app.post("/api/stop")
def api_stop_job():
    global stop_requested
    if not is_running:
        return {"status": "error", "message": "目前沒有正在執行的交易任務。"}
    
    stop_requested = True
    
    # 同步將自動交易開關設為 false，避免重啟後自動拉起
    try:
        set_db_config("AUTO_TRADING_ACTIVE", "false")
    except Exception as db_err:
        # 回退寫入 config.json
        import json
        config_path = os.path.join(os.getcwd(), "config.json")
        existing_config = {}
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    existing_config = json.load(f)
            except Exception:
                pass
        existing_config["AUTO_TRADING_ACTIVE"] = "false"
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(existing_config, f, indent=2, ensure_ascii=False)
        except Exception as io_err:
            print(f"[Web API] 警告: 無法寫入 config.json (可能為唯讀檔案系統): {str(io_err)}")
            
    return {"status": "ok", "message": "已發送停止訊號，任務將在完成當前個股/交易日後安全停止，且已關閉自動交易開關。"}

@app.post("/api/liquidate")
def api_liquidate(background_tasks: BackgroundTasks):
    global stop_requested, is_running
    try:
        # 1. 停止目前正在執行的背景交易任務
        if is_running:
            stop_requested = True
            log_system_event("WARN", "已手動發送停止自動交易任務訊號（準備執行下車清倉）")
            
        # 2. 自動關閉自動交易開關 (AUTO_TRADING_ACTIVE = false)
        try:
            set_db_config("AUTO_TRADING_ACTIVE", "false")
        except Exception as db_err:
            # 回退寫入 config.json
            import json
            config_path = os.path.join(os.getcwd(), "config.json")
            existing_config = {}
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        existing_config = json.load(f)
                except Exception:
                    pass
            existing_config["AUTO_TRADING_ACTIVE"] = "false"
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(existing_config, f, indent=2, ensure_ascii=False)
            except Exception as io_err:
                print(f"[Web API] 警告: 無法寫入 config.json (可能為唯讀檔案系統): {str(io_err)}")
        
        # 3. 在背景非同步執行下車平倉任務
        from src.main import run_liquidate_job
        background_tasks.add_task(run_liquidate_job)
        
        return {"status": "ok", "message": "已成功停止自動交易並關閉開關，且已於背景啟動下車平倉流程！"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"下車平倉失敗: {str(e)}")

@app.get("/api/auth/liquidation-status")
def api_get_liquidation_status():
    try:
        stocks = get_pending_liquidation_stocks()
        fault = get_system_fault_status()
        return {
            "pending_liquidation_stocks": stocks,
            "system_fault_status": fault
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"獲取等候平倉狀態與系統故障狀態失敗: {str(e)}")

@app.post("/api/auth/liquidation/clear")
def api_clear_liquidation(payload: StockCodeRequest):
    try:
        remove_pending_liquidation_stock(payload.stock_code)
        return {"status": "ok", "message": f"股票 {payload.stock_code} 已手動解鎖，從等候平倉清單中移除"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"手動解鎖股票失敗: {str(e)}")

@app.post("/api/auth/liquidation/sync-sell")
def api_sync_sell_liquidation(payload: StockCodeRequest):
    try:
        is_paper = config.limits.is_paper_trading
        execute_with_retry(
            lambda: supabase.table("holdings")
            .delete()
            .eq("stock_code", payload.stock_code)
            .eq("is_paper", is_paper)
            .execute()
        )
        remove_pending_liquidation_stock(payload.stock_code)
        log_system_event("INFO", f"已成功手動同步庫存：刪除 {payload.stock_code} 的庫存，並將其自等候平倉清單解鎖")
        return {"status": "ok", "message": f"已成功將 {payload.stock_code} 的庫存清除並解除平倉鎖定"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"手動同步庫存失敗: {str(e)}")

@app.post("/api/auth/system-fault/clear")
def api_clear_system_fault():
    try:
        set_system_fault_status("OK")
        return {"status": "ok", "message": "已成功手動解除全局系統故障鎖，恢復自動交易"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清除全局系統故障鎖失敗: {str(e)}")

# Serve static files directory
os.makedirs(os.path.join(os.path.dirname(__file__), "static"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    is_fly = "FLY_APP_NAME" in os.environ
    uvicorn.run(
        "src.web_server:app",
        host="0.0.0.0",
        port=port,
        reload=not is_fly,
        timeout_keep_alive=65 if is_fly else 5
    )

