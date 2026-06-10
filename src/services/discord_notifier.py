# Path: src/services/discord_notifier.py
import time
from datetime import date
from typing import Dict, List, Any, Optional

from src.config import config, get_stock_name
from src.services.supabase_client import get_orders, get_holdings, log_system_event
# 由於要動態判斷是沙盒還是真實環境以獲取報價，我們引用 sandbox_simulator
# 它會自動根據當前系統狀態，透明切換即時報價或歷史模擬報價
from src.services import sandbox_simulator
from src.time_manager import (
    get_local_taiwan_date_str,
    get_local_taiwan_datetime_str,
    get_local_taiwan_midnight_utc_range,
    get_effective_date_str,
)

def _send_discord_webhook(webhook_url: str, payload: dict, retries: int = 3, delay: float = 2.0) -> bool:
    """
    透過 Discord Webhook 發送 JSON 內容 (具備重試機制)
    """
    import requests
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(webhook_url, json=payload, timeout=10)
            if response.status_code in (200, 204):
                return True
            else:
                print(f" [Discord通知器] 警告: 發送失敗 (HTTP {response.status_code}): {response.text}，將在 {delay}s 後重試...")
        except Exception as e:
            print(f" [Discord通知器] 警告: 連線失敗 (第 {attempt} 次嘗試): {str(e)}，將在 {delay}s 後重試...")
        time.sleep(delay)
        delay *= 2
    return False



def send_daily_report(ai_outlook: str, override_orders: Optional[List[Dict[str, Any]]] = None) -> None:
    """
    彙整今日交易、持股現況、資產淨值與 AI 分析，產出 Discord Rich Embed 每日交易與狀態報告並發送
    :param ai_outlook: AI 針對今日交易的反思或明日台股的分析預測
    :param override_orders: 手動指定交易訂單列表（主要用於下車平倉報告，防止時區或沙盒時間軸不對而漏載）
    """

    is_liquidation = override_orders is not None
    sim_active = sandbox_simulator.is_simulation_active()
    is_paper = config.limits.is_paper_trading
    shioaji_sim = config.shioaji_simulation
    
    is_sandbox_mode = sim_active or is_paper or shioaji_sim

    # ── 日期標籤與模式名稱：顯示與過濾用 ──────────────────────────────────────────────
    if is_liquidation:
        mode_label = "下車平倉"
        current_date_label = get_local_taiwan_datetime_str()
    elif sim_active:
        mode_label = "沙盒演練"
        current_date_label = get_effective_date_str()
    elif is_paper:
        mode_label = "模擬交易"
        current_date_label = get_local_taiwan_date_str()
    elif shioaji_sim:
        mode_label = "永豐沙盒"
        current_date_label = get_local_taiwan_date_str()
    else:
        mode_label = "實際操盤"
        current_date_label = get_local_taiwan_date_str()

    # 1. 取得今日交易訂單
    if override_orders is not None:
        today_orders = override_orders
    else:
        try:
            if sim_active:
                today_orders = get_orders(sim_date=get_effective_date_str())
            else:
                start_utc, end_utc = get_local_taiwan_midnight_utc_range()
                today_orders = get_orders(start_date=start_utc, end_date=end_utc)
        except Exception as e:
            print(f" [Discord通知器] 無法取得今日交易紀錄: {str(e)}")
            today_orders = []

    # 2. 取得目前持股明細，並動態查詢現價以估算市值
    try:
        holdings = get_holdings()
    except Exception as e:
        print(f" [Discord通知器] 無法取得持股明細: {str(e)}")
        holdings = []

    # 計算持股市值與未實現損益
    holdings_value = 0.0
    total_cost = 0.0
    
    for h in holdings:
        stock_code = h["stock_code"]
        qty = float(h["quantity"])
        avg_price = float(h["average_price"])
        
        # 依據目前是沙盒還是真實模式，動態讀取即時報價
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        current_price = float(quote.get("price") or avg_price)
        
        market_value = qty * current_price
        cost = qty * avg_price
        
        holdings_value += market_value
        total_cost += cost

    # 3. 透過中央計算器計算帳戶資產淨值 (NAV)
    from src.services.nav_calculator import calculate_nav
    cash_balance, _, net_asset_value = calculate_nav()
    initial_cash = config.limits.initial_cash
    net_asset_roi = ((net_asset_value - initial_cash) / initial_cash * 100)

    # 4. 計算今日交易已實現損益
    today_realized_pnl = 0.0
    for o in today_orders:
        status = o.get("status", "FILLED")
        if status != "PENDING":
            realized_pnl = float(o.get("realized_pnl") or 0.0)
            today_realized_pnl += realized_pnl

    # 5. 送出報告至 Discord Webhook
    webhook_url = config.discord.webhook_sandbox if is_sandbox_mode else config.discord.webhook_live
    
    if is_liquidation:
        subject = f"【AI下車平倉報告】{current_date_label} 結算回報 ({mode_label})"
    else:
        subject = f"【AI交易報告】{current_date_label} 台股結算回報 ({mode_label})"
        
    if not webhook_url:
        err_msg = f"未配置 Discord Webhook 網址 (is_sandbox_mode={is_sandbox_mode})，無法發送每日報告。"
        log_system_event("ERROR", err_msg)
        raise ValueError(err_msg)
        
    try:
        from datetime import datetime, timezone
        
        # 建立已完成交易文字
        completed_trades_text = ""
        for o in today_orders:
            status = o.get("status", "FILLED")
            if status != "PENDING":
                action_label = "買入" if o["action"] == "BUY" else "賣出"
                stock_name = get_stock_name(o['stock_code'])
                name_display = f" {stock_name}" if stock_name else ""
                fee_val = float(o.get("fee") or 0.0)
                qty = float(o.get("quantity") or 0.0)
                
                limit_price_val = o.get("price")
                limit_price_display = f"{float(limit_price_val):,.2f}" if limit_price_val is not None else "--"
                exec_price_val = o.get("execution_price")
                exec_price_display = f"{float(exec_price_val):,.2f}" if status == "FILLED" and exec_price_val is not None else "--"
                
                realized_pnl = float(o.get("realized_pnl") or 0.0)
                pnl_display = f" | 實現損益: {realized_pnl:+,.0f} 元" if o["action"] == "SELL" and status == "FILLED" else ""
                
                prefix = "+" if o["action"] == "BUY" else "-"
                status_str = "已成交" if status == "FILLED" else ("已取消" if status == "CANCELLED" else "已失敗")
                
                completed_trades_text += f"{prefix} {action_label} {o['stock_code']}{name_display} | {qty:,.0f} 股 | 委託: {limit_price_display} | 成交: {exec_price_display} (規費: {fee_val:,.0f} 元){pnl_display} - {status_str}\n"

        if not completed_trades_text:
            completed_trades_text = "今日無已完成交易成交紀錄"
        else:
            completed_trades_text = f"```diff\n{completed_trades_text}```"

        # 建立未完成委託文字
        pending_trades_text = ""
        for o in today_orders:
            status = o.get("status", "FILLED")
            if status == "PENDING":
                action_label = "買入" if o["action"] == "BUY" else "賣出"
                stock_name = get_stock_name(o['stock_code'])
                name_display = f" {stock_name}" if stock_name else ""
                fee_val = float(o.get("fee") or 0.0)
                qty = float(o.get("quantity") or 0.0)
                limit_price_val = o.get("price")
                limit_price_display = f"{float(limit_price_val):,.2f}" if limit_price_val is not None else "--"
                
                pending_trades_text += f"[{action_label}] {o['stock_code']}{name_display} | 數量: {qty:,.0f} 股 | 委託價: {limit_price_display} 元 (預估規費: {fee_val:,.0f} 元)\n"

        if not pending_trades_text:
            pending_trades_text = "今日無新委託預約單紀錄"
        else:
            pending_trades_text = f"```ini\n{pending_trades_text}```"

        # 建立目前持股現況文字
        holdings_text = ""
        for h in holdings:
            stock_code = h["stock_code"]
            qty = float(h["quantity"])
            avg_price = float(h["average_price"])
            
            quote = sandbox_simulator.fetch_realtime_quote(stock_code)
            current_price = float(quote.get("price") or avg_price)
            
            market_value = qty * current_price
            cost = qty * avg_price
            unrealized_pnl = market_value - cost
            unrealized_roi = (unrealized_pnl / cost * 100) if cost > 0 else 0.0
            
            stock_name = get_stock_name(stock_code)
            name_display = f" {stock_name}" if stock_name else ""
            
            prefix = "+" if unrealized_pnl >= 0 else "-"
            pnl_prefix = "+" if unrealized_pnl >= 0 else ""
            
            holdings_text += f"{prefix} {stock_code}{name_display} | {qty:,.0f} 股 | 均價: {avg_price:,.2f} | 現價: {current_price:,.2f} | 損益: {pnl_prefix}{unrealized_pnl:,.0f} 元 ({pnl_prefix}{unrealized_roi:.2f}%)\n"

        if not holdings_text:
            holdings_text = "目前帳戶無持股倉位"
        else:
            holdings_text = f"```diff\n{holdings_text}```"

        color = 15679812 if is_liquidation else (3899902 if is_sandbox_mode else 2278750)
        ai_outlook_display = ai_outlook[:1000] + "..." if len(ai_outlook) > 1000 else ai_outlook

        discord_payload = {
            "username": "AI 台股自動交易報告",
            "embeds": [
                {
                    "title": f"📊 {subject}",
                    "description": f"**環境/模式**: `{mode_label}`",
                    "color": color,
                    "fields": [
                        {
                            "name": "💰 投資組合帳戶總覽",
                            "value": (
                                f"可用現金餘額: `{cash_balance:,.0f}` 元\n"
                                f"持股總市值: `{holdings_value:,.0f}` 元\n"
                                f"資產淨總值 (NAV): **`{net_asset_value:,.0f}`** 元 (`{'+' if net_asset_roi >= 0 else ''}{net_asset_roi:.2f}%`)\n"
                                f"今日已實現損益: **`{today_realized_pnl:+,.0f}`** 元"
                            ),
                            "inline": False
                        },
                        {
                            "name": "🟢 今日已完成交易 (實際成交回報)",
                            "value": completed_trades_text,
                            "inline": False
                        },
                        {
                            "name": "⏳ 今日盤後 AI 新增委託 (預約明日交易)",
                            "value": pending_trades_text,
                            "inline": False
                        },
                        {
                            "name": "📈 目前持股現況",
                            "value": holdings_text,
                            "inline": False
                        },
                        {
                            "name": "🧠 AI 明日分析預測與反思",
                            "value": ai_outlook_display or "無 AI 預測數據",
                            "inline": False
                        }
                    ],
                    "footer": {
                        "text": f"此報告由 AI 自動化交易系統發送。"
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            ]
        }
        success = _send_discord_webhook(webhook_url, discord_payload)
        if success:
            log_system_event("INFO", f"已成功發送 {current_date_label} 每日報告至 Discord Webhook ({mode_label})")
        else:
            err_msg = f"發送每日報告至 Discord Webhook 失敗 (網址: {webhook_url})。"
            log_system_event("ERROR", err_msg)
            raise RuntimeError(err_msg)
    except Exception as e:
        log_system_event("ERROR", f"發送每日報告至 Discord Webhook 發生異常: {str(e)}")
        raise

def send_emergency_alert(subject: str, message: str) -> None:
    """
    發送緊急警報 (使用醒目的紅色警報樣式至 Discord Webhook)
    :param subject: 警報主旨
    :param message: 警報詳細訊息
    """
    sim_active = sandbox_simulator.is_simulation_active()
    is_paper = config.limits.is_paper_trading
    shioaji_sim = config.shioaji_simulation
    is_sandbox_mode = sim_active or is_paper or shioaji_sim
    
    webhook_url = config.discord.webhook_sandbox if is_sandbox_mode else config.discord.webhook_live
    
    if not webhook_url:
        err_msg = f"未配置 Discord Webhook 網址 (is_sandbox_mode={is_sandbox_mode})，無法發送緊急安全警報。"
        log_system_event("ERROR", err_msg)
        raise ValueError(err_msg)
        
    try:
        from datetime import datetime, timezone
        discord_payload = {
            "username": "AI 台股自動交易系統 - 緊急安全警報",
            "embeds": [
                {
                    "title": f"🚨 {subject}",
                    "description": (
                        f"**發送時間**: {get_local_taiwan_datetime_str()}\n\n"
                        f"**異常事件摘要**:\n```\n{message}\n```\n\n"
                        f"📌 **後續建議處置**:\n"
                        f"1. 請立即登入 **AIAutoStocks Web 控制台** 查看詳細系統日誌。\n"
                        f"2. 若為券商 API 連線失敗，請檢查 Fly.io 部署環境及憑證與密碼設定。\n"
                        f"3. 若為個股交易失敗或跌停鎖死，請登入您個人的證券商官方 App 進行人工部位檢查與手動交易。\n"
                        f"4. 手動處置完畢後，請於控制台進行「解鎖」或「手動同步庫存」以恢復自動交易流程。"
                    ),
                    "color": 15679812,  # 紅色
                    "footer": {
                        "text": "此警報由緊急防禦模組自動發送。"
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
            ]
        }
        success = _send_discord_webhook(webhook_url, discord_payload)
        if success:
            log_system_event("INFO", f"已成功發送緊急警報至 Discord Webhook: {subject}")
        else:
            err_msg = f"發送緊急警報至 Discord Webhook 失敗 (網址: {webhook_url})。"
            log_system_event("ERROR", err_msg)
            raise RuntimeError(err_msg)
    except Exception as e:
        log_system_event("ERROR", f"發送緊急警報至 Discord Webhook 發生異常: {str(e)}")
        raise
