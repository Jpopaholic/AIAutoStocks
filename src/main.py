# Path: src/main.py
import sys
import argparse
import pytz
from datetime import datetime
from typing import List

from src.config import config, resolve_stock_codes
from src.services import supabase_client

def get_taiwan_time() -> datetime:
    """
    獲取目前的台灣時間 (Asia/Taipei, UTC+8)
    """
    tz = pytz.timezone(config.timezone)
    return datetime.now(tz)

def run_live_trading_job(stock_codes: List[str]) -> None:
    """
    執行真實/模擬盤後自動化交易任務 (定時 Cron 觸發)
    """
    tw_now = get_taiwan_time()
    
    # 自動清理 7 天前的舊日誌
    try:
        supabase_client.prune_old_db_logs(days=7)
    except Exception as prune_err:
        print(f" [排程引擎] 警告: 自動清理舊日誌失敗: {prune_err}")
        
    # 1. 跳過週末非交易日
    if tw_now.weekday() in (5, 6):
        msg = f"今日 {tw_now.strftime('%Y-%m-%d')} 為週末非交易日，主動跳過排程任務。"
        print(f" [排程引擎] {msg}")
        supabase_client.log_system_event("INFO", msg)
        return

    msg = f"啟動盤後自動化交易流程 (時間: {tw_now.strftime('%Y-%m-%d %H:%M:%S')})"
    print(f" [排程引擎] {msg}")
    supabase_client.log_system_event("INFO", msg)

    # 延遲載入以防循環依賴
    from src.services import stock_fetcher, sandbox_simulator, broker_connector, email_notifier
    from src.agents import trading_agent

    # 確保關閉模擬時間軸模式，使用即時數據窗口
    sandbox_simulator.set_simulation_mode(False)

    # 執行硬體止損防線檢查 (AI 決策前)
    try:
        broker_connector.check_and_execute_hard_stop_losses()
    except Exception as stop_err:
        print(f" [排程引擎] 警告: 執行硬體停損防線檢驗時發生異常: {str(stop_err)}")

    # A-pre. 合併目前的持股標的（確保持有中的個股也會進入 AI 決策範疇，可執行賣出）
    try:
        existing_holdings = supabase_client.get_holdings()
        held_codes = [h["stock_code"] for h in existing_holdings if h.get("stock_code")]
        added_from_holdings = [c for c in held_codes if c not in stock_codes]
        if added_from_holdings:
            stock_codes = stock_codes + added_from_holdings
            supabase_client.log_system_event("INFO", f"已將目前持股合併至分析標的: {added_from_holdings} → 總標的: {stock_codes}")
    except Exception as h_err:
        print(f" [排程引擎] 警告: 獲取目前持股以合併分析標的時發生異常: {str(h_err)}")

    klines_map = {}
    
    # A. 抓取所有股票的最新日 K 線數據
    for stock_code in stock_codes:
        try:
            print(f" [排程引擎] 正在獲取 {stock_code} 的最新 K 線歷史數據...")
            klines = stock_fetcher.fetch_stock_klines(stock_code)
            if not klines:
                print(f" [排程引擎] 警告: 未能獲取 {stock_code} 的 K 線，跳過此股票。")
                continue

            # 將最新 K 線儲存至 Supabase 作為歷史備份
            supabase_client.save_stock_klines(klines)
            klines_map[stock_code] = klines
        except Exception as e:
            err_msg = f"獲取 {stock_code} 的 K 線數據時發生錯誤: {str(e)}"
            print(f" [排程引擎] {err_msg}")
            supabase_client.log_system_event("ERROR", err_msg)

    if not klines_map:
        print(" [排程引擎] 錯誤: 未能獲取任何股票的 K 線數據，結束排程任務。")
        return

    # B. 取得目前的持股明細
    try:
        holdings = supabase_client.get_holdings()
    except Exception as e:
        print(f" [排程引擎] 獲取持股明細失敗: {str(e)}")
        holdings = []

    # C. 呼叫 AI 交易決策代理生成多股聯合配置決策
    ai_outlook_details = []
    try:
        print(f" [排程引擎] 呼叫 AI 決策代理分析投資組合 {list(klines_map.keys())}...")
        portfolio_decision = trading_agent.generate_portfolio_decisions(
            stock_codes=list(klines_map.keys()),
            klines_map=klines_map,
            current_holdings=holdings
        )
        decisions = portfolio_decision.get("decisions", [])
    except Exception as e:
        err_msg = f"呼叫 AI 決策代理時發生異常: {str(e)}"
        print(f" [排程引擎] {err_msg}")
        supabase_client.log_system_event("ERROR", err_msg)
        return

    # D. 執行模擬或真實證券下單 (由 PAPER_TRADING_MODE 決定)
    for d in decisions:
        stock_code = (d.get("stock_code") or d.get("stockCode")) if isinstance(d, dict) else getattr(d, "stock_code", getattr(d, "stockCode", None))
        action = d.get("action") if isinstance(d, dict) else d.action
        price = d.get("price") if isinstance(d, dict) else d.price
        quantity = float(d.get("quantity") if isinstance(d, dict) else d.quantity)
        reason = d.get("reason") if isinstance(d, dict) else d.reason

        print(f"   - AI 決策 [{stock_code}]: {action} | 價格: {price} | 數量: {quantity}")
        print(f"   - 決策理由: {reason}")

        ai_outlook_details.append(
            f"股票代號 {stock_code}: AI 決策為 {action}，"
            f"委託價格 {price} 元，數量 {quantity:.0f} 股。\n"
            f"決策依據: {reason}"
        )

        if action in ("BUY", "SELL") and quantity > 0:
            try:
                broker_connector.place_order(
                    stock_code=stock_code,
                    action=action,
                    price=price,
                    quantity=quantity
                )
            except Exception as e:
                err_msg = f"執行 {stock_code} 的自動化交易下單時發生錯誤: {str(e)}"
                print(f" [排程引擎] {err_msg}")
                supabase_client.log_system_event("ERROR", err_msg)

    # E. 彙整今日交易損益與持股，發送每日 HTML 電子郵件報告信
    ai_outlook_str = "\n\n".join(ai_outlook_details)
    try:
        email_notifier.send_daily_report(ai_outlook_str)
    except Exception as e:
        print(f" [排程引擎] 郵件發送失敗: {str(e)}")

def run_sandbox_simulation(stock_codes: List[str], start_date: str, end_date: str, should_stop=None) -> None:
    """
    執行沙盒演練回測模擬。
    利用 Supabase 中的歷史 K 線重播行情，測試 AI 決策表現並模擬交易帳務與每日報告發送。
    """
    print(f" [排程引擎] 啟動沙盒演練歷史數據模擬。區間: {start_date} 至 {end_date} | 標的: {stock_codes}")
    
    # 延遲載入
    from src.services import sandbox_simulator, broker_connector, email_notifier
    from src.agents import trading_agent

    # 0. 合併目前的持股標的（確保持有中的個股也會進入沙盒 AI 決策範疇，可執行賣出）
    try:
        existing_holdings = supabase_client.get_holdings()
        held_codes = [h["stock_code"] for h in existing_holdings if h.get("stock_code")]
        added_from_holdings = [c for c in held_codes if c not in stock_codes]
        if added_from_holdings:
            stock_codes = stock_codes + added_from_holdings
            print(f" [排程引擎] 已將目前持股合併至沙盒分析標的: {added_from_holdings} → 總標的: {stock_codes}")
    except Exception as h_err:
        print(f" [排程引擎] 警告: 獲取目前持股以合併沙盒分析標的時發生異常: {str(h_err)}")

    # 1. 從 Supabase 中獲取基礎股票的交易日作為模擬時間軸基準
    db_klines = supabase_client.get_stock_klines(stock_codes[0], limit=500)
    if not db_klines:
        print(" [排程引擎] 錯誤: Supabase 資料庫中無歷史 K 線數據，請先在 live 模式下執行資料擷取持久化。")
        return

    # 篩選在 [start_date, end_date] 範圍內的交易日期
    trading_days = sorted(list(set([
        k["date"] for k in db_klines 
        if start_date <= k["date"] <= end_date
    ])))

    if not trading_days:
        print(f" [排程引擎] 錯誤: 在該區間 {start_date} 至 {end_date} 內未找到任何交易日。")
        return

    # 2. 初始化沙盒演練狀態
    sandbox_simulator.initialize_simulation(start_date, end_date, trading_days)

    # 3. 模擬時間軸推進循環
    while True:
        if should_stop and should_stop():
            msg = "偵測到手動停止指令，安全終止沙盒模擬循環。"
            print(f" [排程引擎] {msg}")
            try:
                supabase_client.log_system_event("INFO", msg)
            except Exception:
                pass
            break
        sim_date = sandbox_simulator.get_current_sim_date()
        print(f"\n=================== 模擬交易日: {sim_date} ===================")
        
        ai_outlook_details = []
        klines_map = {}

        # 獲取各個股票模擬時間軸的 K 線數據
        for stock_code in stock_codes:
            klines = sandbox_simulator.fetch_stock_klines(stock_code)
            if klines:
                klines_map[stock_code] = klines

        if not klines_map:
            # 時間軸推進
            next_day = sandbox_simulator.advance_simulation_step()
            if not next_day:
                break
            continue

        # 執行硬體止損防線檢查 (在獲取持股與 AI 決策前)
        try:
            broker_connector.check_and_execute_hard_stop_losses()
        except Exception as stop_err:
            print(f"   [硬體停損防線異常]: {str(stop_err)}")

        # 取得目前的模擬持股
        holdings = supabase_client.get_holdings()

        # 生成交易決策
        try:
            portfolio_decision = trading_agent.generate_portfolio_decisions(
                stock_codes=list(klines_map.keys()),
                klines_map=klines_map,
                current_holdings=holdings
            )
            decisions = portfolio_decision.get("decisions", [])
        except Exception as e:
            print(f"   [沙盒決策失敗]: {str(e)}")
            decisions = []

        # 執行模擬下單
        for d in decisions:
            stock_code = (d.get("stock_code") or d.get("stockCode")) if isinstance(d, dict) else getattr(d, "stock_code", getattr(d, "stockCode", None))
            action = d.get("action") if isinstance(d, dict) else d.action
            price = d.get("price") if isinstance(d, dict) else d.price
            quantity = float(d.get("quantity") if isinstance(d, dict) else d.quantity)
            reason = d.get("reason") if isinstance(d, dict) else d.reason

            print(f"  AI 決策 [{stock_code}]: {action} | 價格: {price} | 數量: {quantity}")
            print(f"  原因: {reason}")

            ai_outlook_details.append(
                f"股票 {stock_code}: AI 決策 {action} (價格 {price}, 股數 {quantity})。\n"
                f"理由: {reason}"
            )

            if action in ("BUY", "SELL") and quantity > 0:
                try:
                    broker_connector.place_order(
                        stock_code=stock_code,
                        action=action,
                        price=price,
                        quantity=quantity
                    )
                except Exception as e:
                    print(f"   [模擬下單失敗]: {str(e)}")

        # 該模擬日交易結束，發送模擬結算報告信
        ai_outlook_str = "\n\n".join(ai_outlook_details)
        try:
            email_notifier.send_daily_report(ai_outlook_str)
        except Exception as e:
            print(f"   [模擬郵件發送失敗]: {str(e)}")

        # 時間軸推進
        next_day = sandbox_simulator.advance_simulation_step()
        if not next_day:
            break

    print("\n [排程引擎] 沙盒演練歷史重播模擬結束。")

def main():
    """
    命令列程式入口
    """
    parser = argparse.ArgumentParser(description="AI 台股自動買賣排程主引擎")
    parser.add_argument(
        "--mode", 
        choices=["live", "sandbox"], 
        default="live",
        help="執行模式。'live': 實時獲取數據並下單; 'sandbox': 進行歷史數據模擬演練"
    )
    parser.add_argument(
        "--stocks", 
        default="2330,2454", 
        help="股票代號列表，以逗號分隔 (例如: 2330,2454)"
    )
    parser.add_argument(
        "--start-date", 
        default="2026-05-01", 
        help="沙盒演練起始日期 (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date", 
        default="2026-06-08", 
        help="沙盒演練結束日期 (YYYY-MM-DD)"
    )

    args = parser.parse_args()
    
    # 檢查使用者是否在命令列中明確傳入 --stocks
    stocks_arg_passed = any(arg.startswith("--stocks") for arg in sys.argv)
    stock_codes = []

    if not stocks_arg_passed:
        try:
            from src.services.supabase_client import get_db_watchlist
            db_watchlist = get_db_watchlist()
            if db_watchlist:
                stock_codes = db_watchlist
                print(f" [排程引擎] 自 Supabase 載入動態自選股: {stock_codes}")
        except Exception as e:
            # 回退嘗試自本機 watchlist.json 讀取
            import os
            import json
            watchlist_path = os.path.join(os.getcwd(), "watchlist.json")
            if os.path.exists(watchlist_path):
                try:
                    with open(watchlist_path, "r", encoding="utf-8") as f:
                        local_list = json.load(f)
                        if isinstance(local_list, list) and local_list:
                            stock_codes = local_list
                            print(f" [排程引擎] 自本機 watchlist.json 載入動態自選股: {stock_codes}")
                except Exception:
                    pass
            if not stock_codes:
                print(f" [排程引擎] 嘗試載入 Supabase 自選股失敗 (將使用預設/參數值): {str(e)}")

    if not stock_codes:
        stock_codes = resolve_stock_codes(args.stocks)
        print(f" [排程引擎] 使用參數解析自選股: {stock_codes}")

    # 捕捉全局未處理的例外，防止 Fly.io 容器無端崩潰
    try:
        if args.mode == "live":
            run_live_trading_job(stock_codes)
        elif args.mode == "sandbox":
            run_sandbox_simulation(
                stock_codes=stock_codes,
                start_date=args.start_date,
                end_date=args.end_date
            )
    except Exception as e:
        import traceback
        err_msg = f"排程引擎遭遇致命異常崩潰: {str(e)}"
        print(f" [排程引擎] {err_msg}")
        traceback.print_exc(file=sys.stderr)
        try:
            supabase_client.log_system_event("ERROR", err_msg, {"traceback": traceback.format_exc()})
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
