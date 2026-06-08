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

from src.config import config
from src.services.supabase_client import (
    get_db_watchlist,
    add_to_db_watchlist,
    delete_from_db_watchlist,
    get_db_config,
    set_db_config,
    get_holdings,
    get_orders
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

@app.on_event("startup")
def on_startup():
    try:
        from src.services.supabase_client import prune_old_db_logs
        # 自動清理 7 天前的舊日誌
        prune_old_db_logs(days=7)
    except Exception as e:
        print(f" [啟動任務] 警告: 自動清理舊日誌失敗: {e}")

# Background runner state
is_running = False
is_running_lock = threading.Lock()
stop_requested = False
last_run_status = "尚未執行過手動交易"
last_run_time = "無"

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
        last_run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
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
            run_live_trading_job(stock_codes)
        
        if stop_requested:
            last_run_status = "已被使用者手動停止"
            log_system_event("INFO", "=== 網頁端交易任務已被使用者手動停止 ===")
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
            "stop_requested": stop_requested,
            "last_run_status": last_run_status,
            "last_run_time": last_run_time
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"載入儀表板狀態失敗: {str(e)}")

@app.get("/api/watchlist")
def api_get_watchlist():
    try:
        watchlist = get_db_watchlist()
        return {"watchlist": watchlist, "fallback": False}
    except Exception as e:
        import json
        watchlist_path = os.path.join(os.getcwd(), "watchlist.json")
        if os.path.exists(watchlist_path):
            try:
                with open(watchlist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return {"watchlist": data, "fallback": True}
            except Exception:
                pass
        from src.config import resolve_stock_codes
        return {"watchlist": resolve_stock_codes("2330,2454"), "fallback": True, "error": str(e)}

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
                elif k in ["TRADING_LIMIT_SINGLE_STOCK_PCT", "TRADING_LIMIT_DAILY_TOTAL_PCT"]:
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
            
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(existing_config, f, indent=2, ensure_ascii=False)
                
            return {"status": "ok", "message": "資料表未建立，已自動將設定寫入本機 config.json 檔案！"}
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

@app.post("/api/trigger")
def api_trigger_run(background_tasks: BackgroundTasks):
    global is_running
    with is_running_lock:
        if is_running:
            return {"status": "error", "message": "交易任務正在執行中，請勿重複觸發。"}
        is_running = True
        
    background_tasks.add_task(run_trading_job_in_background)
    return {"status": "ok", "message": "交易排程已成功在背景中啟動！"}

@app.post("/api/stop")
def api_stop_job():
    global stop_requested
    if not is_running:
        return {"status": "error", "message": "目前沒有正在執行的交易任務。"}
    stop_requested = True
    return {"status": "ok", "message": "已發送停止訊號，任務將在完成當前模擬交易日後安全停止。"}

# Serve static files directory
os.makedirs(os.path.join(os.path.dirname(__file__), "static"), exist_ok=True)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.web_server:app", host="0.0.0.0", port=3000, reload=True)
