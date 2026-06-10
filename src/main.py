# Path: src/main.py
import sys
import argparse
import time
from datetime import datetime
from typing import List

from src.config import config, get_config_val, resolve_stock_codes
from src.services import supabase_client
from src.time_manager import get_local_taiwan_datetime

def get_taiwan_time() -> datetime:
    """
    獲取目前的台灣時間 (Asia/Taipei, UTC+8)
    """
    return get_local_taiwan_datetime()

def run_live_trading_job(stock_codes: List[str]) -> None:
    """
    執行真實/模擬盤後自動化交易任務 (定時 Cron 觸發)
    """
    if not config.is_auto_trading_active:
        msg = "自動交易已被停用 (AUTO_TRADING_ACTIVE=false)，跳過本次自動交易排程委託。"
        print(f" [排程引擎] {msg}")
        supabase_client.log_system_event("INFO", msg)
        return

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
    from src.services import stock_fetcher, sandbox_simulator, broker_connector, discord_notifier
    from src.agents import trading_agent

    # 0. 優先執行券商對帳同步任務
    try:
        broker_connector.sync_broker_orders()
    except Exception as sync_err:
        print(f" [排程引擎] 警告: 執行對帳同步任務時發生異常: {str(sync_err)}")
        supabase_client.log_system_event("WARN", f"對帳同步任務發生異常: {str(sync_err)}")

    # 1b. 國定假日與臨時休市 (如颱風假) 自檢
    try:
        tsmc_klines = stock_fetcher.fetch_stock_klines("2330")
        if tsmc_klines:
            latest_market_date = tsmc_klines[-1]["date"]
            today_str = tw_now.strftime("%Y-%m-%d")
            if latest_market_date != today_str:
                msg = f"今日 {today_str} 無最新交易數據（最新交易日為 {latest_market_date}），判斷為國定假日或臨時休市（如颱風假），自動跳過今日任務。"
                print(f" [排程引擎] {msg}")
                supabase_client.log_system_event("INFO", msg)
                return
        else:
            print(" [排程引擎] 警告: 無法獲取基準股 (2330) 的 K 線，跳過休市自檢。")
    except Exception as check_err:
        print(f" [排程引擎] 警告: 執行基準股休市自檢時發生異常: {check_err}")

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

    # A-0. 抓取大盤加權指數 (TAIEX) 的最新 K 線歷史數據並儲存
    from datetime import timedelta
    try:
        print(" [排程引擎] 正在獲取大盤加權指數 (TAIEX) 的最新 K 線歷史數據...")
        taiex_klines = stock_fetcher.fetch_taiex_klines()
        # 同步抓取前一個月以避免月份交替時的資料斷層
        prev_date = (tw_now - timedelta(days=30)).strftime("%Y%m%d")
        prev_taiex_klines = stock_fetcher.fetch_taiex_klines(prev_date)
        all_taiex = taiex_klines + prev_taiex_klines
        
        if all_taiex:
            supabase_client.save_stock_klines(all_taiex)
            print(f" [排程引擎] 成功儲存 {len(all_taiex)} 筆大盤 K 線數據至資料庫")
        else:
            print(" [排程引擎] 警告: 未能獲取大盤加權指數的最新 K 線。")
    except Exception as taiex_err:
        print(f" [排程引擎] 警告: 獲取大盤 K 線數據時發生異常: {str(taiex_err)}")

    klines_map = {}
    
    # A. 抓取所有股票的最新日 K 線數據，並自資料庫載入完整的歷史 K 線 (最新 100 筆)
    for stock_code in stock_codes:
        try:
            print(f" [排程引擎] 正在獲取 {stock_code} 的最新 K 線歷史數據...")
            klines = stock_fetcher.fetch_stock_klines(stock_code)
            if klines:
                # 將最新 K 線儲存至 Supabase 作為歷史備份
                supabase_client.save_stock_klines(klines)
            else:
                print(f" [排程引擎] 警告: 未能自 API 獲取 {stock_code} 的 K 線，將嘗試僅從資料庫載入歷史。")

            # 從資料庫載入完整 100 筆 K 線以防月份交替斷層
            db_klines = supabase_client.get_stock_klines(stock_code, limit=100)
            if not db_klines:
                print(f" [排程引擎] 錯誤: 資料庫中亦無 {stock_code} 的 K 線，跳過該股票。")
                continue
                
            formatted = []
            for k in db_klines:
                formatted.append({
                    "stockCode": k["stock_code"],
                    "date": str(k["date"]),
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": int(k["volume"] or 0)
                })
            # 按日期升序排序
            formatted.sort(key=lambda x: x["date"])
            klines_map[stock_code] = formatted
        except Exception as e:
            err_msg = f"處理 {stock_code} 的 K 線數據時發生錯誤: {str(e)}"
            print(f" [排程引擎] {err_msg}")
            supabase_client.log_system_event("ERROR", err_msg)

    # A-idx. 從資料庫讀取大盤加權指數的歷史 K 線 (最新 100 筆)
    try:
        db_taiex = supabase_client.get_stock_klines("TAIEX", limit=100)
        if db_taiex:
            formatted_taiex = []
            for k in db_taiex:
                formatted_taiex.append({
                    "stockCode": k["stock_code"],
                    "date": str(k["date"]),
                    "open": float(k["open"]),
                    "high": float(k["high"]),
                    "low": float(k["low"]),
                    "close": float(k["close"]),
                    "volume": int(k["volume"] or 0)
                })
            formatted_taiex.sort(key=lambda x: x["date"])
            klines_map["TAIEX"] = formatted_taiex
            print(f" [排程引擎] 已成功自資料庫載入 {len(formatted_taiex)} 筆大盤 (TAIEX) 數據並寫入 klines_map")
        else:
            print(" [排程引擎] 警告: 資料庫中無大盤 (TAIEX) 的歷史數據")
    except Exception as taiex_db_err:
        print(f" [排程引擎] 警告: 自資料庫讀取大盤歷史數據失敗: {str(taiex_db_err)}")


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
    # TAIEX 只作為大盤參考數據傳入 klines_map，但不列入決策 stock_codes
    from src.config import get_stock_name
    ai_stock_codes = [c for c in klines_map.keys() if c != "TAIEX"]
    ai_outlook_details = []
    try:
        print(f" [排程引擎] 呼叫 AI 決策代理分析投資組合 {ai_stock_codes}...")
        portfolio_decision = trading_agent.generate_portfolio_decisions(
            stock_codes=ai_stock_codes,
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
        # TAIEX 僅作大盤參考，不應出現在決策輸出中，直接跳過
        if stock_code == "TAIEX":
            continue
        action = d.get("action") if isinstance(d, dict) else d.action
        price = d.get("price") if isinstance(d, dict) else d.price
        quantity = float(d.get("quantity") if isinstance(d, dict) else d.quantity)
        reason = d.get("reason") if isinstance(d, dict) else d.reason
        stock_name = get_stock_name(stock_code)
        display_code = f"{stock_code} {stock_name}" if stock_name else stock_code

        print(f"   - AI 決策 [{display_code}]: {action} | 價格: {price} | 數量: {quantity}")
        print(f"   - 決策理由: {reason}")

        ai_outlook_details.append(
            f"股票 {display_code}: AI 決策為 {action}，"
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

    # E. 彙整今日交易損益與持股，發送每日報告至 Discord Webhook
    ai_outlook_str = "\n\n".join(ai_outlook_details)
    try:
        discord_notifier.send_daily_report(ai_outlook_str)
    except Exception as e:
        print(f" [排程引擎] Discord 報告發送失敗: {str(e)}")

def run_sandbox_simulation(stock_codes: List[str], start_date: str, end_date: str, should_stop=None) -> None:
    """
    執行沙盒演練回測模擬。
    利用 Supabase 中的歷史 K 線重播行情，測試 AI 決策表現並模擬交易帳務與每日報告發送。
    """
    print(f" [排程引擎] 啟動沙盒演練歷史數據模擬。區間: {start_date} 至 {end_date} | 標的: {stock_codes}")
    
    # 延遲載入
    from src.services import sandbox_simulator, broker_connector, discord_notifier
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
    sandbox_scale_str = get_config_val("SANDBOX_TIME_SCALE") or "8640000.0"
    try:
        sandbox_scale = float(sandbox_scale_str)
    except ValueError:
        sandbox_scale = 1.0
    sandbox_simulator.initialize_simulation(start_date, end_date, trading_days, scale=sandbox_scale)

    # 3. 模擬時間軸推進循環
    last_sim_date = None
    while sandbox_simulator.is_simulation_active():
        if should_stop and should_stop():
            msg = "偵測到手動停止指令，安全終止沙盒模擬循環。"
            print(f" [排程引擎] {msg}")
            try:
                supabase_client.log_system_event("INFO", msg)
            except Exception:
                pass
            break

        current_day_idx = sandbox_simulator.get_current_day_index()
        target_day_idx = sandbox_simulator.get_current_target_day_index()
        sim_date = sandbox_simulator.get_current_sim_date()

        if sim_date == last_sim_date:
            if current_day_idx < target_day_idx:
                next_day = sandbox_simulator.advance_simulation_step()
                if next_day:
                    print(f" [模擬器] 時間比例推進至下一個交易日: {next_day}")
                continue

            if sandbox_simulator.has_reached_simulation_end():
                print(" [排程引擎] 已到達模擬結束時間，終止沙盒演練。")
                break

            time.sleep(5)
            continue

        last_sim_date = sim_date
        print(f"\n=================== 模擬交易日: {sim_date} ===================")
        
        # 優先執行模擬對帳同步 (將前一天的 PENDING 模擬單在今天成交)
        try:
            broker_connector.sync_sandbox_orders(sim_date)
        except Exception as sync_err:
            print(f"   [模擬對帳異常]: {str(sync_err)}")
        
        ai_outlook_details = []
        klines_map = {}

        # 取得目前持股，若無持股則自動終止沙盒演練
        try:
            holdings = supabase_client.get_holdings()
        except Exception as e:
            print(f" [排程引擎] 警告: 無法取得持股資料: {str(e)}")
            holdings = []

        if not holdings and not stock_codes:
            msg = "沙盒演練已無任何持股或交易標的，終止模擬。"
            print(f" [排程引擎] {msg}")
            try:
                supabase_client.log_system_event("INFO", msg)
            except Exception:
                pass
            break

        # 獲取各個股票模擬時間軸的 K 線數據
        for stock_code in stock_codes:
            klines = sandbox_simulator.fetch_stock_klines(stock_code)
            if klines:
                klines_map[stock_code] = klines

        # 獲取大盤加權指數 (TAIEX) 的模擬 K 線數據
        taiex_klines = sandbox_simulator.fetch_stock_klines("TAIEX")
        if taiex_klines:
            klines_map["TAIEX"] = taiex_klines

        if not klines_map:
            print(" [排程引擎] 本模擬時間點尚無有效交易日資料，等待下一個模擬時間片...")
            time.sleep(5)
            continue

        # 執行硬體止損防線檢查 (在獲取持股與 AI 決策前)
        try:
            broker_connector.check_and_execute_hard_stop_losses()
        except Exception as stop_err:
            print(f"   [硬體停損防線異常]: {str(stop_err)}")

        # 取得目前的模擬持股
        holdings = supabase_client.get_holdings()

        # 生成交易決策 (TAIEX 只作大盤參考，不列入決策 stock_codes)
        from src.config import get_stock_name as _get_name
        ai_stock_codes = [c for c in klines_map.keys() if c != "TAIEX"]
        try:
            portfolio_decision = trading_agent.generate_portfolio_decisions(
                stock_codes=ai_stock_codes,
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
            # TAIEX 僅大盤參考，跳過
            if stock_code == "TAIEX":
                continue
            action = d.get("action") if isinstance(d, dict) else d.action
            price_val = d.get("price") if isinstance(d, dict) else getattr(d, "price", None)
            quantity_val = d.get("quantity") if isinstance(d, dict) else getattr(d, "quantity", None)
            try:
                price = float(price_val) if price_val is not None else 0.0
            except (ValueError, TypeError):
                price = 0.0
            try:
                quantity = float(quantity_val) if quantity_val is not None else 0.0
            except (ValueError, TypeError):
                quantity = 0.0
            reason = d.get("reason") if isinstance(d, dict) else d.reason
            stock_name = _get_name(stock_code)
            display_code = f"{stock_code} {stock_name}" if stock_name else stock_code

            print(f"  AI 決策 [{display_code}]: {action} | 價格: {price} | 數量: {quantity}")
            print(f"  原因: {reason}")

            ai_outlook_details.append(
                f"股票 {display_code}: AI 決策 {action} (價格 {price}, 股數 {quantity})。\n"
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

        # 該模擬日交易結束，發送模擬結算報告至 Discord Webhook
        ai_outlook_str = "\n\n".join(ai_outlook_details)
        try:
            discord_notifier.send_daily_report(ai_outlook_str)
        except Exception as e:
            print(f"   [模擬 Discord 報告發送失敗]: {str(e)}")

        current_day_idx = sandbox_simulator.get_current_day_index()
        target_day_idx = sandbox_simulator.get_current_target_day_index()
        if current_day_idx < target_day_idx:
            next_day = sandbox_simulator.advance_simulation_step()
            if next_day:
                print(f" [模擬器] 時間比例推進至下一個交易日: {next_day}")
                last_sim_date = None
                continue

        time.sleep(1)

    print("\n [排程引擎] 沙盒演練歷史重播模擬結束。")

def run_liquidate_job() -> None:
    """
    執行「下車」指令：立即賣出（清空）當前模式下的所有持股。
    """
    is_paper = config.limits.is_paper_trading
    mode_name = "沙盒模擬" if is_paper else "真實實盤"
    print(f" [下車引擎] 啟動下車程序（目前模式: {mode_name}）...")
    supabase_client.log_system_event("INFO", f"啟動下車程序，準備清空所有{mode_name}持股")

    # 1. 取得目前的所有持股明細
    try:
        holdings = supabase_client.get_holdings()
    except Exception as e:
        print(f" [下車引擎] 錯誤: 取得持股明細失敗: {e}")
        return

    if not holdings:
        msg = f"目前沒有任何{mode_name}持股，無須進行下車動作。"
        print(f" [下車引擎] {msg}")
        supabase_client.log_system_event("INFO", msg)
        return

    # 2. 載入必要的服務模組
    from src.services import stock_fetcher, broker_connector

    success_count = 0
    fail_count = 0
    liquidated_orders = []

    for h in holdings:
        stock_code = h["stock_code"]
        quantity = float(h["quantity"])
        
        if quantity <= 0:
            continue

        print(f"\n [下車引擎] 處理 {stock_code} | 持股數量: {quantity:.0f} 股")
        
        try:
            # 優先嘗試取得盤中即時報價
            quote = stock_fetcher.fetch_realtime_quote(stock_code)
            latest_price = float(quote.get("price") or 0)
            
            # 若無即時報價（例如盤後非交易時段），則回退使用最新 K 線的收盤價
            if latest_price <= 0:
                klines = stock_fetcher.fetch_stock_klines(stock_code)
                if klines:
                    latest_price = float(klines[-1]["close"])
            
            # 若仍無價格，則回退使用持股之平均買入成本價
            if latest_price <= 0:
                latest_price = float(h.get("average_price") or 0)
                if latest_price > 0:
                    print(f" [下車引擎] 警告: 即時報價與 K 線皆無法獲取，回退使用平均買入成本價: {latest_price} 元")

            if latest_price <= 0:
                raise ValueError("無法獲取即時報價、歷史 K 線收盤價或持股平均成本，無參考成交價")

            print(f" [下車引擎] 獲取最新參考價: {latest_price} 元")
            
            # 送出賣出委託
            order_res = broker_connector.place_order(
                stock_code=stock_code,
                action="SELL",
                price=latest_price,
                quantity=quantity
            )
            if order_res:
                liquidated_orders.append(order_res)
            success_count += 1
            print(f" 成功：已送出 {stock_code} 的賣出下單 (數量: {quantity:.0f} 股，價格: {latest_price} 元)")
        except Exception as e:
            fail_count += 1
            err_msg = f"下車賣出 {stock_code} 失敗: {str(e)}"
            print(f" [下車引擎] ❌ {err_msg}")
            supabase_client.log_system_event("ERROR", err_msg)

    # 3. 總結報告
    summary_msg = f"下車程序執行完畢。成功賣出個股數: {success_count}，失敗個股數: {fail_count}。"
    print(f"\n [下車引擎] {summary_msg}")
    supabase_client.log_system_event("INFO", summary_msg)

    # 4. 發送下車報告至 Discord Webhook
    try:
        from src.services import discord_notifier
        outlook_text = (
            f"【手動下車平倉回報】\n\n"
            f"使用者已手動觸發一鍵下車平倉指令（目前交易模式: {mode_name}）。\n"
            f"下車執行結果：\n"
            f" - 成功賣出平倉個股數: {success_count}\n"
            f" - 失敗個股數: {fail_count}\n\n"
            f"自動交易開關已同步關閉 (AUTO_TRADING_ACTIVE = false)，系統在手動重啟前不會再執行任何自動交易與買入分析。"
        )
        discord_notifier.send_daily_report(outlook_text, override_orders=liquidated_orders)
        print(" [下車引擎] 下車結算報告已成功發送至 Discord。")
    except Exception as em_err:
        print(f" [下車引擎] 警告: 發送下車報告失敗: {str(em_err)}")

def main():
    """
    命令列程式入口
    """
    parser = argparse.ArgumentParser(description="AI 台股自動買賣排程主引擎")
    parser.add_argument(
        "--mode", 
        choices=["live", "sandbox", "liquidate"], 
        default="live",
        help="執行模式。'live': 實時獲取數據並下單; 'sandbox': 進行歷史數據模擬演練; 'liquidate': 立即賣出清空所有持股 ('下車')"
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
        elif args.mode == "liquidate":
            run_liquidate_job()
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
