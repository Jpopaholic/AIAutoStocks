# Path: src/services/email_notifier.py
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date, datetime
from typing import Dict, List, Any, Optional

from src.config import config, get_stock_name
from src.services.supabase_client import get_orders, get_holdings, log_system_event
# 由於要動態判斷是沙盒還是真實環境以獲取報價，我們引用 sandbox_simulator
# 它會自動根據當前系統狀態，透明切換即時報價或歷史模擬報價
from src.services import sandbox_simulator

def _send_email_via_gmail(subject: str, html_content: str, retries: int = 3, delay: float = 2.0) -> bool:
    """
    透過 Gmail SMTP 伺服器發送 HTML 電子郵件 (具備重試機制)
    """
    gmail_user = config.gmail.user
    gmail_pass = config.gmail.app_password
    to_addr = config.gmail.to_addr

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = f"AI 台股自動交易系統 <{gmail_user}>"
    msg['To'] = to_addr
    
    # 附加 HTML 內容並指定 utf-8 編碼
    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    for attempt in range(1, retries + 1):
        try:
            # 使用 587 連接 TLS 加密
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, to_addr, msg.as_string())
            server.close()
            return True
        except Exception as e:
            if attempt == retries:
                log_system_event("ERROR", f"每日報告郵件發送失敗 (已重試 {retries} 次): {str(e)}")
                raise e
            print(f" [郵件通知器] 警告: 郵件發送失敗 (第 {attempt} 次嘗試): {str(e)}，將在 {delay}s 後重試...")
            time.sleep(delay)
            delay *= 2
    return False

def send_daily_report(ai_outlook: str, override_orders: Optional[List[Dict[str, Any]]] = None) -> None:
    """
    彙整今日交易、持股現況、資產淨值與 AI 分析，產出 HTML 每日交易與狀態報告信並發送
    :param ai_outlook: AI 針對今日交易的反思或明日台股的分析預測
    :param override_orders: 手動指定交易訂單列表（主要用於下車平倉報告，防止時區或沙盒時間軸不對而漏載）
    """
    from datetime import timezone

    is_liquidation = override_orders is not None
    sim_active = sandbox_simulator.is_simulation_active()

    # ── 日期標籤：Email 標題顯示用 ──────────────────────────────────────────────
    if is_liquidation:
        # 下車清倉：顯示真實台北時間（含時分秒）
        current_date_label = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elif sim_active:
        # 沙盒演練：顯示模擬（虛擬）日期，讓收件者知道這是哪一天的回測結果
        current_date_label = sandbox_simulator.get_current_sim_date()
    else:
        # 真實操盤：顯示真實台北日期
        current_date_label = datetime.now().strftime("%Y-%m-%d")

    # ── 訂單查詢：永遠用真實 UTC 日期過濾，因為 Supabase executed_at 存的是 UTC ─
    # 不論是沙盒還是真實模式，寫入 DB 的 executed_at 都是真實 UTC 時間戳
    utc_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. 取得今日交易訂單
    if override_orders is not None:
        today_orders = override_orders
    else:
        try:
            if sim_active:
                # 【沙盒模式】：取今天（真實 UTC）在本次模擬中下的所有訂單
                orders = get_orders(start_date=f"{utc_today}T00:00:00Z")
                today_orders = [o for o in orders if o["executed_at"].startswith(utc_today)]
            else:
                # 【真實操盤】：取今天（真實 UTC）的所有真實下單紀錄
                orders = get_orders(start_date=f"{utc_today}T00:00:00Z")
                today_orders = [o for o in orders if o["executed_at"].startswith(utc_today)]
        except Exception as e:
            print(f" [郵件通知器] 無法取得今日交易紀錄: {str(e)}")
            today_orders = []

    # 2. 取得目前持股明細，並動態查詢現價以估算市值
    try:
        holdings = get_holdings()
    except Exception as e:
        print(f" [郵件通知器] 無法取得持股明細: {str(e)}")
        holdings = []

    # 計算持股市值與未實現損益
    holdings_value = 0.0
    total_cost = 0.0
    holdings_rows_html = []
    
    for h in holdings:
        stock_code = h["stock_code"]
        qty = float(h["quantity"])
        avg_price = float(h["average_price"])
        
        # 依據目前是沙盒還是真實模式，動態讀取即時報價
        quote = sandbox_simulator.fetch_realtime_quote(stock_code)
        current_price = float(quote.get("price") or avg_price)
        
        market_value = qty * current_price
        cost = qty * avg_price
        unrealized_pnl = market_value - cost
        unrealized_roi = (unrealized_pnl / cost * 100) if cost > 0 else 0.0
        
        holdings_value += market_value
        total_cost += cost

        # PnL HSL 著色
        pnl_color = "#22c55e" if unrealized_pnl >= 0 else "#ef4444"
        pnl_prefix = "+" if unrealized_pnl >= 0 else ""

        stock_name = get_stock_name(stock_code)
        name_display = f" ({stock_name})" if stock_name else ""
        holdings_rows_html.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px; font-weight: bold; color: #1e293b;">{stock_code}{name_display}</td>
                <td style="padding: 10px; text-align: right;">{qty:,.0f}</td>
                <td style="padding: 10px; text-align: right;">{avg_price:,.2f}</td>
                <td style="padding: 10px; text-align: right;">{current_price:,.2f}</td>
                <td style="padding: 10px; text-align: right; font-weight: bold; color: {pnl_color};">{pnl_prefix}{unrealized_pnl:,.0f} 元 ({pnl_prefix}{unrealized_roi:.2f}%)</td>
            </tr>
        """)

    # 3. 透過中央計算器計算帳戶資產淨值 (NAV)
    from src.services.nav_calculator import calculate_nav
    cash_balance, _, net_asset_value = calculate_nav()
    initial_cash = config.limits.initial_cash
    net_asset_roi = ((net_asset_value - initial_cash) / initial_cash * 100)

    # 4. 建立交易記錄表格 HTML
    orders_rows_html = []
    today_realized_pnl = 0.0
    
    for o in today_orders:
        action_label = "買入" if o["action"] == "BUY" else "賣出"
        action_bg = "#dcfce7" if o["action"] == "BUY" else "#fee2e2"
        action_color = "#15803d" if o["action"] == "BUY" else "#b91c1c"
        
        realized_pnl = float(o.get("realized_pnl") or 0.0)
        today_realized_pnl += realized_pnl
        pnl_text = f"{realized_pnl:+,.0f} 元" if o["action"] == "SELL" else "-"
        pnl_color = "#22c55e" if realized_pnl > 0 else ("#ef4444" if realized_pnl < 0 else "#64748b")

        stock_name = get_stock_name(o['stock_code'])
        name_display = f" ({stock_name})" if stock_name else ""
        orders_rows_html.append(f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px; color: #1e293b; font-weight: bold;">{o['stock_code']}{name_display}</td>
                <td style="padding: 10px; text-align: center;"><span style="background-color: {action_bg}; color: {action_color}; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;">{action_label}</span></td>
                <td style="padding: 10px; text-align: right;">{float(o['price']):,.2f}</td>
                <td style="padding: 10px; text-align: right;">{float(o['quantity']):,.0f}</td>
                <td style="padding: 10px; text-align: right;">{float(o['fee']):,.0f} 元</td>
                <td style="padding: 10px; text-align: right; font-weight: bold; color: {pnl_color};">{pnl_text}</td>
            </tr>
        """)

    if not orders_rows_html:
        orders_table_body = """<tr><td colspan="6" style="padding: 20px; text-align: center; color: #64748b;">今日無交易委託紀錄</td></tr>"""
    else:
        orders_table_body = "\n".join(orders_rows_html)

    if not holdings_rows_html:
        holdings_table_body = """<tr><td colspan="5" style="padding: 20px; text-align: center; color: #64748b;">目前帳戶無持股倉位</td></tr>"""
    else:
        holdings_table_body = "\n".join(holdings_rows_html)

    # 5. 組合現代響應式 HTML 郵件內容 (Inline CSS)
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>每日交易與狀態報告信</title>
    </head>
    <body style="margin: 0; padding: 0; background-color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width: 600px; background-color: #ffffff; border-radius: 8px; overflow: hidden; margin: 20px auto; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
            <!-- 標題欄 (漸層美學) -->
            <tr>
                <td style="background: linear-gradient(135deg, #1e3a8a, #3b82f6); padding: 30px 20px; text-align: center; color: #ffffff;">
                    <h1 style="margin: 0; font-size: 24px; font-weight: 800; letter-spacing: 1px;">AI 台股自動交易報告</h1>
                    <p style="margin: 5px 0 0 0; opacity: 0.9; font-size: 14px;">報告日期: {current_date_label} {"(沙盒演練)" if sim_active else ""}</p>
                </td>
            </tr>

            <!-- 資產總覽卡片 -->
            <tr>
                <td style="padding: 20px;">
                    <div style="background-color: #f1f5f9; border-radius: 6px; padding: 15px; border-left: 4px solid #3b82f6;">
                        <h3 style="margin: 0 0 10px 0; color: #1e293b; font-size: 16px;">投資組合帳戶總覽</h3>
                        <table width="100%" cellspacing="0" cellpadding="0" style="font-size: 14px; color: #475569;">
                            <tr>
                                <td style="padding: 4px 0;">可用現金餘額:</td>
                                <td style="text-align: right; font-weight: bold; color: #1e293b;">{cash_balance:,.0f} 元</td>
                            </tr>
                            <tr>
                                <td style="padding: 4px 0;">持股總市值:</td>
                                <td style="text-align: right; font-weight: bold; color: #1e293b;">{holdings_value:,.0f} 元</td>
                            </tr>
                            <tr style="border-top: 1px solid #cbd5e1;">
                                <td style="padding: 8px 0 4px 0; font-weight: bold; color: #0f172a; font-size: 15px;">資產淨總值 (NAV):</td>
                                <td style="text-align: right; font-weight: bold; color: #1e3a8a; font-size: 16px;">{net_asset_value:,.0f} 元 ({"+" if net_asset_roi >= 0 else ""}{net_asset_roi:.2f}%)</td>
                            </tr>
                            <tr>
                                <td style="padding: 4px 0;">今日已實現損益:</td>
                                <td style="text-align: right; font-weight: bold; color: {'#22c55e' if today_realized_pnl >= 0 else '#ef4444'};">
                                    {"+" if today_realized_pnl >= 0 else ""}{today_realized_pnl:,.0f} 元
                                </td>
                            </tr>
                        </table>
                    </div>
                </td>
            </tr>

            <!-- 今日交易明細 -->
            <tr>
                <td style="padding: 0 20px;">
                    <h3 style="margin: 10px 0; color: #1e293b; border-bottom: 2px solid #e2e8f0; padding-bottom: 5px; font-size: 16px;">今日交易明細</h3>
                    <table width="100%" cellspacing="0" cellpadding="0" style="font-size: 13px; border-collapse: collapse;">
                        <thead>
                            <tr style="background-color: #f8fafc; color: #64748b; font-weight: bold; border-bottom: 2px solid #cbd5e1;">
                                <th style="padding: 8px; text-align: left;">股票</th>
                                <th style="padding: 8px; text-align: center;">動作</th>
                                <th style="padding: 8px; text-align: right;">成交價</th>
                                <th style="padding: 8px; text-align: right;">股數</th>
                                <th style="padding: 8px; text-align: right;">規費</th>
                                <th style="padding: 8px; text-align: right;">實現損益</th>
                            </tr>
                        </thead>
                        <tbody>
                            {orders_table_body}
                        </tbody>
                    </table>
                </td>
            </tr>

            <!-- 目前持股明細 -->
            <tr>
                <td style="padding: 20px;">
                    <h3 style="margin: 10px 0; color: #1e293b; border-bottom: 2px solid #e2e8f0; padding-bottom: 5px; font-size: 16px;">目前持股現況</h3>
                    <table width="100%" cellspacing="0" cellpadding="0" style="font-size: 13px; border-collapse: collapse;">
                        <thead>
                            <tr style="background-color: #f8fafc; color: #64748b; font-weight: bold; border-bottom: 2px solid #cbd5e1;">
                                <th style="padding: 8px; text-align: left;">股票</th>
                                <th style="padding: 8px; text-align: right;">股數</th>
                                <th style="padding: 8px; text-align: right;">買入均價</th>
                                <th style="padding: 8px; text-align: right;">目前市價</th>
                                <th style="padding: 8px; text-align: right;">未實現損益</th>
                            </tr>
                        </thead>
                        <tbody>
                            {holdings_table_body}
                        </tbody>
                    </table>
                </td>
            </tr>

            <!-- AI 決策與行情反思 -->
            <tr>
                <td style="padding: 0 20px 20px 20px;">
                    <div style="background-color: #faf5ff; border: 1px solid #f3e8ff; border-radius: 6px; padding: 15px;">
                        <h3 style="margin: 0 0 10px 0; color: #6b21a8; font-size: 16px;">AI 明日分析預測與反思</h3>
                        <p style="margin: 0; font-size: 14px; color: #581c87; line-height: 1.6; white-space: pre-wrap;">{ai_outlook}</p>
                    </div>
                </td>
            </tr>

            <!-- 頁尾 -->
            <tr>
                <td style="background-color: #f1f5f9; padding: 20px; text-align: center; color: #94a3b8; font-size: 11px;">
                    <p style="margin: 0;">此報告由 AI 自動化台股交易系統產生，並透過 Gmail 伺服器安全傳送。</p>
                    <p style="margin: 5px 0 0 0;">© 2026 AIAutoStocks. All rights reserved.</p>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

    # 6. 送出郵件
    if is_liquidation:
        subject = f"【AI下車平倉報告】{current_date_label} 結算回報"
    else:
        subject = f"【AI交易報告】{current_date_label} 台股結算回報 {'[沙盒]' if sim_active else ''}"
    try:
        success = _send_email_via_gmail(subject, html_content)
        if success:
            log_system_event("INFO", f"已成功發送 {current_date_label} 每日報告電子郵件至 {config.gmail.to_addr}")
    except Exception as e:
        log_system_event("ERROR", f"每日報告郵件發送中斷: {str(e)}")
