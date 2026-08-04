import json
import time
from datetime import date, datetime, timezone
from typing import Dict, List, Any, Optional

from src.config import config, get_stock_name, safe_int
from src.services.supabase_client import get_orders, get_holdings, log_system_event, get_unfilled_orders
# 由於要動態判斷是沙盒還是真實環境以獲取報價，我們引用 sandbox_simulator
# 它會自動根據當前系統狀態，透明切換即時報價或歷史模擬報價
from src.services import sandbox_simulator
from src.time_manager import (
    get_local_taiwan_date_str,
    get_local_taiwan_datetime_str,
    get_local_taiwan_midnight_utc_range,
    get_effective_date_str,
)

def _sanitize_payload_embeds(payload: dict) -> dict:
    """
    防禦性淨化 Discord Webhook Payload 中的 embeds 陣列，
    防止空字串、型態錯誤 (如非 dict 物件)、超長欄位導致 Discord 回傳 HTTP 400 ({"embeds": ["0"]})。
    """
    if not isinstance(payload, dict):
        return {}
    
    embeds = payload.get("embeds")
    if not embeds or not isinstance(embeds, list):
        return payload

    sanitized_embeds = []
    for idx, raw_embed in enumerate(embeds):
        if not isinstance(raw_embed, dict):
            raw_embed = {"description": str(raw_embed)}

        embed_copy = dict(raw_embed)
        
        title = str(embed_copy.get("title") or "").strip()
        if title:
            embed_copy["title"] = title[:250]
        else:
            embed_copy.pop("title", None)

        description = embed_copy.get("description")
        if description is not None:
            embed_copy["description"] = _safe_embed_description(str(description), max_len=3900)
        else:
            embed_copy["description"] = "(無內容)"

        footer = embed_copy.get("footer")
        if isinstance(footer, dict) and "text" in footer:
            footer["text"] = str(footer["text"])[:2000]

        fields = embed_copy.get("fields")
        if isinstance(fields, list):
            sanitized_fields = []
            for f in fields[:25]:
                if isinstance(f, dict):
                    f_name = str(f.get("name") or "(項目)").strip()[:250]
                    f_val = str(f.get("value") or "(無內容)").strip()[:1000]
                    sanitized_fields.append({"name": f_name, "value": f_val, "inline": bool(f.get("inline", False))})
            embed_copy["fields"] = sanitized_fields

        sanitized_embeds.append(embed_copy)

    payload["embeds"] = sanitized_embeds
    return payload


def _send_discord_webhook(
    webhook_url: str,
    payload: dict,
    retries: int = 3,
    delay: float = 2.0,
    file_tuple: Optional[Any] = None
) -> bool:
    """
    透過 Discord Webhook 發送 JSON 內容與可選附件 (具備重試機制)
    :param file_tuple: 可選之 (filename, file_content_str, content_type) 元組
    """
    import requests
    payload = _sanitize_payload_embeds(payload)
    for attempt in range(1, retries + 1):
        try:
            if file_tuple:
                filename, content, content_type = file_tuple
                files = {
                    "file": (filename, content.encode("utf-8"), content_type)
                }
                response = requests.post(
                    webhook_url,
                    data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                    files=files,
                    timeout=15
                )
            else:
                response = requests.post(webhook_url, json=payload, timeout=10)

            if response.status_code in (200, 204):
                return True
            elif response.status_code == 429:
                try:
                    res_data = response.json()
                    retry_after = float(res_data.get("retry_after", 1.0))
                except Exception:
                    retry_after = 2.0
                print(f" [Discord通知器] 提示: 觸發 Rate Limit (HTTP 429)，自動暫停 {retry_after:.2f} 秒後重試...")
                time.sleep(retry_after + 0.2)
                continue
            elif response.status_code == 400:
                print(f" [Discord通知器] ❌ 錯誤 (HTTP 400 內容格式遭 Discord 拒絕): {response.text}")
                try:
                    preview = json.dumps(payload, ensure_ascii=False)[:500]
                    print(f"   └─ Payload 內容預覽: {preview}")
                except Exception:
                    pass
                # HTTP 400 代表內容格式無效，重試相同 Payload 無法成功，直接回傳失敗並暴露出詳細原因
                return False
            else:
                print(f" [Discord通知器] 警告: 發送失敗 (HTTP {response.status_code}): {response.text}，將在 {delay}s 後重試...")
        except Exception as e:
            print(f" [Discord通知器] 警告: 連線失敗 (第 {attempt} 次嘗試): {str(e)}，將在 {delay}s 後重試...")
        time.sleep(delay)
        delay *= 2
    return False


def _safe_embed_description(text: str, max_len: int = 3900) -> str:
    """
    將 Discord Embed Description 文字限制在安全字數內 (Discord 限制 4096 字元)，
    防範超長文字引發 HTTP 400 Bad Request ({"embeds": ["0"]}) 錯誤。
    """
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len - 60] + "\n\n...(字數超出限制已自動截斷，完整細節請參閱附件 .md 檔案)..."


def _split_text_by_length(text: str, max_len: int = 1000) -> List[str]:
    """
    將一段長文字切割成多個符合 Discord 欄位限制 (1024字) 的字串區塊。
    儘量保留行與段落完整性。
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
        
    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_len = 0
    
    for line in lines:
        if len(line) > max_len:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i+max_len])
            continue
            
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = len(line)
        else:
            current_chunk.append(line)
            current_len += len(line) + 1
            
    if current_chunk:
        chunks.append("\n".join(current_chunk))
        
    return chunks


def _split_into_fields(title_prefix: str, content: str, syntax: Optional[str] = None, max_len: int = 900) -> List[Dict[str, Any]]:
    """
    將內容分割成多個 Discord Embed Fields，符合 1024 限制。
    如果指定了 syntax，則每一個分段內容都會被獨立包裹在 ```代碼框中。
    """
    if not content:
        val = "無"
        if syntax:
            val = f"```{syntax}\n{val}```"
        return [{
            "name": title_prefix,
            "value": val,
            "inline": False
        }]
        
    chunks = _split_text_by_length(content, max_len=max_len)
    fields = []
    for idx, chunk in enumerate(chunks):
        title = title_prefix if idx == 0 else f"{title_prefix} (續 {idx+1})"
        value_str = f"```{syntax}\n{chunk}```" if syntax else chunk
        fields.append({
            "name": title,
            "value": value_str,
            "inline": False
        })
    return fields


def send_daily_report(
    ai_outlook: str,
    override_orders: Optional[List[Dict[str, Any]]] = None,
    regime_assessment: Optional[Dict[str, Any]] = None,
    analyst_scores: Optional[List[Dict[str, Any]]] = None,
    portfolio_decision: Optional[Dict[str, Any]] = None,
    is_manual: bool = False
) -> None:
    """
    彙整今日交易、大盤氣候、評分降序排名、今日停損警告與 AI 第三層決策原因，產出精簡的單一 Discord Embed 卡片報告。
    """
    sim_active = sandbox_simulator.is_simulation_active()
    is_paper = config.limits.is_paper_trading
    shioaji_sim = config.shioaji_simulation
    is_sandbox_mode = sim_active or is_paper or shioaji_sim

    # ── 1. 時間與模式標籤 ───────────────────────────────────────────
    if override_orders is not None:
        mode_label = "下車平倉"
        current_date_label = get_local_taiwan_datetime_str()
    elif sim_active:
        mode_label = "沙盒演練"
        current_date_label = get_effective_date_str()
    elif is_manual:
        mode_label = "手動分析"
        current_date_label = get_local_taiwan_date_str()
    elif is_paper:
        mode_label = "模擬交易"
        current_date_label = get_local_taiwan_date_str()
    elif shioaji_sim:
        mode_label = "永豐沙盒"
        current_date_label = get_local_taiwan_date_str()
    else:
        mode_label = "實際操盤"
        current_date_label = get_local_taiwan_date_str()

    # 判斷 AI 決策引擎模型標籤
    ai_provider = config.ai_provider
    if ai_provider == "openai" or (ai_provider == "auto" and config.openai_api_key):
        model_label = f"OpenAI ({config.openai_model or 'gpt-4o-mini'})"
    else:
        model_label = f"Gemini ({config.gemini_model or 'gemini-3.6-flash'})"

    # ── 2. 獲取今日交易委託與成交狀態 ──────────────────────────────────
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

    # ── 2.5 獲取今日未成交/滑價取消訂單 ────────────────────────────────
    if override_orders is not None:
        today_unfilled = []
    else:
        try:
            if sim_active:
                today_unfilled = get_unfilled_orders(sim_date=get_effective_date_str())
            else:
                start_utc, end_utc = get_local_taiwan_midnight_utc_range()
                today_unfilled = get_unfilled_orders(start_date=start_utc, end_date=end_utc)
        except Exception as e:
            print(f" [Discord通知器] 無法取得今日未成交紀錄: {str(e)}")
            today_unfilled = []

    # 計算實現損益
    today_realized_pnl = 0.0
    for o in today_orders:
        status = o.get("status", "FILLED")
        if status != "PENDING":
            realized_pnl = float(o.get("realized_pnl") or 0.0)
            today_realized_pnl += realized_pnl

    # 帳戶總覽 (NAV)
    from src.services.nav_calculator import calculate_nav
    cash_balance, _, net_asset_value = calculate_nav()
    initial_cash = config.limits.initial_cash
    net_asset_roi = ((net_asset_value - initial_cash) / initial_cash * 100)

    # ── 3. 欄位 1: 大盤氣候與本日交易 ─────────────────────────────────
    regime_display = "UNKNOWN"
    posture_display = "UNKNOWN"
    risk_mult = 1.0
    posture = "UNKNOWN"
    climate_reason = ""
    if regime_assessment:
        regime = regime_assessment.get("regime", "UNKNOWN")
        posture = regime_assessment.get("posture", "UNKNOWN")
        risk_mult = regime_assessment.get("risk_multiplier", 1.0)
        climate_reason = regime_assessment.get("reason", "")
        
        regime_emoji_map = {
            "STRONG_BULL": "🐂 強勢多頭",
            "REBOUND_BULL": "📈 驚驚漲/震盪偏多",
            "BULLISH_TREND": "🐂 多頭趨勢",
            "CALM_RANGE": "🦀 低波動橫盤",
            "VOLATILE_RANGE": "🌪️ 高波動震盪",
            "CORRECTION_BEAR": "📉 震盪修正",
            "PANIC_BEAR": "🐻 恐慌空頭",
            "BEARISH_TREND": "🐻 空頭趨勢"
        }
        posture_emoji_map = {
            "STRONG_ATTACK": "⚔️ 強攻攻勢",
            "MODERATE_ATTACK": "🛡️ 穩健進攻",
            "CHOPPY_TACTICAL": "🔄 震盪靈活",
            "DEFENSIVE_ACCUMULATION": "🧱 防禦承接",
            "STRICT_DEFENSE": "🏰 現金為王",
            "AGGRESSIVE": "⚔️ 積極進攻",
            "NORMAL": "🛡️ 正常操作",
            "DEFENSIVE": "🏰 防禦保守"
        }
        regime_display = regime_emoji_map.get(regime, regime)
        posture_display = posture_emoji_map.get(posture, posture)
        target_cash_ratio = regime_assessment.get("target_cash_ratio")
        cash_ratio_str = f" | **目標現金比例**: `{float(target_cash_ratio)*100:.0f}%`" if target_cash_ratio is not None else ""

    climate_header = (
        f"• **市場狀態**: `{regime_display}` | **交易姿態**: `{posture_display}`\n"
        f"• **風險乘數**: `{float(risk_mult):.2f}`{cash_ratio_str}\n"
    )


    account_header = (
        f"• **帳戶淨值 (NAV)**: **`{net_asset_value:,.0f}`** 元 (`{net_asset_roi:+.2f}%`)\n"
        f"• **現金餘額**: `{cash_balance:,.0f}` 元\n"
        f"• **今日實現損益**: **`{today_realized_pnl:+,.0f}`** 元\n"
    )

    # 交易列表 (使用 diff 美化)
    trades_lines = []
    for o in today_orders:
        status = o.get("status", "FILLED")
        action_label = "買" if o["action"] == "BUY" else "賣"
        stock_name = get_stock_name(o['stock_code'])
        name_display = f"({stock_name})" if stock_name else ""
        qty = float(o.get("quantity") or 0.0)
        limit_price = float(o.get("price") or 0.0)
        exec_price = float(o.get("execution_price") or 0.0) if status == "FILLED" else limit_price
        
        prefix = "+" if o["action"] == "BUY" else "-"
        status_str = "已成交" if status == "FILLED" else ("已取消" if status == "CANCELLED" else "委託中")
        
        line = f"{prefix} {action_label} {o['stock_code']}{name_display} | {qty:,.0f}股 | 均價:{exec_price:,.2f} | {status_str}"
        if o["action"] == "SELL" and status == "FILLED":
            realized = float(o.get("realized_pnl") or 0.0)
            line += f" (已實現損益: {realized:+,.0f})"
        trades_lines.append(line)

    trades_text = "\n".join(trades_lines) if trades_lines else "今日無任何交易委託成交。"

    # 未成交/滑價取消列表
    unfilled_lines = []
    for o in today_unfilled:
        action_label = "買" if o["action"] == "BUY" else "賣"
        stock_name = get_stock_name(o['stock_code'])
        name_display = f"({stock_name})" if stock_name else ""
        qty = float(o.get("quantity") or 0.0)
        limit_price = float(o.get("price") or 0.0)
        reason_val = o.get("reason") or "CANCELLED"
        reason_str = "券商取消(滑價)" if reason_val == "CANCELLED" else "未找到委託(過期)"
        
        prefix = "!"
        line = f"{prefix} {action_label} {o['stock_code']}{name_display} | {qty:,.0f}股 | 委託價:{limit_price:,.2f} | 原因:{reason_str}"
        unfilled_lines.append(line)

    unfilled_text = "\n".join(unfilled_lines) if unfilled_lines else "今日無任何未成交/滑價取消委託。"

    # ── 4. 欄位 2: 評分與相對排名 (第二層) ────────────────────────────────
    scores_text = "暫無分析師評分資料。"
    target_scores = analyst_scores
    if not target_scores and portfolio_decision and "decisions" in portfolio_decision:
        target_scores = portfolio_decision["decisions"]
        
    pattern_lines = []
    if target_scores:
        try:
            from src.services.trading_memory import get_active_skills_data
            ind_info = get_active_skills_data(is_paper=is_sandbox_mode)
            ind_rev_m = ind_info.get("review_month", "最新")
            ind_sk = ind_info.get("skills", {}).get("indicator_skills", {})
            v_patterns = ind_sk.get("v_shape_reversal_patterns", [])
            a_warnings = ind_sk.get("a_shape_top_warnings", [])
            v_rule_str = v_patterns[0].get("pattern_rule") if v_patterns and isinstance(v_patterns[0], dict) else ""
            a_rule_str = a_warnings[0].get("pattern_rule") if a_warnings and isinstance(a_warnings[0], dict) else ""
            
            pattern_lines.append(f"📌 **當前生效之第 2.5 層指標與型態 Skills (版本: {ind_rev_m})**:")
            if v_rule_str:
                pattern_lines.append(f"• **V轉勝率特徵**: {v_rule_str}")
            if a_rule_str:
                pattern_lines.append(f"• **A頂警示特徵**: {a_rule_str}")
            pattern_lines.append("")
        except Exception:
            pass

        # 按總分降序排列
        sorted_scores = sorted(target_scores, key=lambda x: x.get("total_score", 0), reverse=True)
        score_lines = []
        for idx, s in enumerate(sorted_scores):
            stock_code = s["stock_code"]
            stock_name = get_stock_name(stock_code)
            name_display = f" {stock_name}" if stock_name else ""
            reg_score_val = safe_int(s.get('regime_score'), default=10, min_val=0, max_val=20)
            score_lines.append(
                f"{idx+1}. {stock_code}{name_display} | 總分: **{s['total_score']}** "
                f"(趨勢:{s['trend_score']} 動能:{s['momentum_score']} 成交量:{s['volume_score']} 安全:{s['safety_score']} 大盤:{reg_score_val})  "
            )
            
        scores_text = "\n".join(score_lines)
    
    section2_value = scores_text

    # ── 5. 欄位 3: 今日停損警告清單 ──────────────────────────────────────
    stop_loss_text = "🟢 今日無任何股票觸發停損。"
    try:
        from src.services.supabase_client import get_stop_loss_stocks_today
        stop_loss_list = get_stop_loss_stocks_today()
        if stop_loss_list:
            stop_loss_details = []
            for code in stop_loss_list:
                name = get_stock_name(code)
                name_display = f" ({name})" if name else ""
                stop_loss_details.append(f"● {code}{name_display}")
            stop_loss_text = "🚨 **今日停損排除清單 (後續免除 AI 分析)**:\n" + ", ".join(stop_loss_details)
    except Exception as e:
        print(f" [Discord通知器] 警告: 載入停損清單失敗: {e}")
    section3_value = stop_loss_text

# ── 6. 欄位 4: 第三層買賣決策與原因 ──────────────────────────────────
    decisions_text = "今日無 AI 配置決策。"
    if portfolio_decision:
        try:
            from src.services.trading_memory import get_active_skills_data
            skills_info = get_active_skills_data(is_paper=is_sandbox_mode)
            rev_m = skills_info.get("review_month", "最新")
            exec_sk = skills_info.get("skills", {}).get("execution_skills", {})
            m_score = exec_sk.get("min_buy_score", 60)
            m_weight = exec_sk.get("max_single_stock_weight", 4)
            s_loss = exec_sk.get("stop_loss_pct", -0.05)
            t_profit = exec_sk.get("take_profit_pct", 0.12)
            t_rules = exec_sk.get("tactical_rules", [])
            t_rules_str = "；".join(t_rules) if t_rules else "維持標準紀律"
            
            active_skills_summary_header = (
                f"📌 **當前生效之第三層月度戰術 Skills (版本: {rev_m})**:\n"
                f"• **下月風控門檻**: 最低買入門檻 **{m_score} 分** | 單檔最重權重 **{m_weight} 級** | 個股停損 **{s_loss*100:.1f}%** | 動態鎖利 **{t_profit*100:.1f}%**\n"
                f"• **戰術執行重點**: {t_rules_str}\n\n"
            )
        except Exception as e:
            active_skills_summary_header = ""

        ranking_analysis = str(portfolio_decision.get("ranking_analysis", "橫向對比分析中。")).replace("\\n", "\n").replace("\r\n", "\n").strip()
        decision_lines = [
            f"{active_skills_summary_header}"
            f"**經理人橫向配置說明**:\n{ranking_analysis}\n"
        ]
        
        raw_decs = portfolio_decision.get("decisions", [])
        for d in raw_decs:
            code = d.get("stock_code")
            action = d.get("action", "HOLD")
            qty = float(d.get("quantity") or 0.0)
            raw_reason = d.get("reason", "維持觀望。")
            reason = str(raw_reason).replace("\\n", "\n").replace("\r\n", "\n").strip()
            
            applied_skills = d.get("applied_skills", [])
            skills_line = ""
            if isinstance(applied_skills, list) and applied_skills:
                skills_str = "；".join([str(s).strip() for s in applied_skills if str(s).strip()])
                if skills_str:
                    skills_line = f"└ *📜 引用戰術 Skills*: {skills_str}\n"
            
            action_emoji = "🟢 BUY" if action == "BUY" else ("🔴 SELL" if action == "SELL" else "⚪ HOLD")
            stock_name = get_stock_name(code)
            name_display = f" ({stock_name})" if stock_name else ""
            
            qty_str = f" | 數量: {qty:,.0f} 股" if action != "HOLD" else ""
            decision_lines.append(
                f"**{action_emoji}** {code}{name_display}{qty_str}  \n"
                f"{skills_line}"
                f"└ *原因*: {reason}\n"
            )
        decisions_text = "\n".join(decision_lines)
    elif ai_outlook:
        # 相容舊模式 (未傳入結構化變數時)
        decisions_text = str(ai_outlook).replace("\\n", "\n").replace("\r\n", "\n").strip()
        
    section4_value = decisions_text

    # ── 7. 送出報告至 Discord Webhook ──────────────────────────────────
    webhook_url = config.discord.webhook_sandbox if is_sandbox_mode else config.discord.webhook_live
    
    if override_orders is not None:
        subject = f"【AI下車平倉報告】{current_date_label} ({mode_label})"
    elif is_manual:
        subject = f"【AI手動交易報告】{current_date_label} ({mode_label})"
    else:
        subject = f"【AI交易報告】{current_date_label} ({mode_label})"
        
    if not webhook_url:
        err_msg = f"未配置 Discord Webhook 網址 (is_sandbox_mode={is_sandbox_mode})，無法發送每日報告。"
        log_system_event("ERROR", err_msg)
        raise ValueError(err_msg)
        
    try:
        from datetime import datetime, timezone
        color = 16744448 if override_orders is not None else (3447003 if is_sandbox_mode else 3066993)
        
        # 動態分割長文字為多個 Embed Fields，以符合 Discord 的 1024 字元限制並防止截斷
        fields = []
        fields.extend(_split_into_fields("🌦️ 1a. 大盤氣候狀態", climate_header, max_len=950))
        # 氣候分析理由單獨作為一個可自動拆分的欄位，避免超過 1024 字元限制
        if climate_reason:
            fields.extend(_split_into_fields("📋 1b. 大盤氣候分析理由", climate_reason, max_len=950))
        fields.extend(_split_into_fields("💰 1c. 帳戶資金狀態", account_header, max_len=950))
        fields.extend(_split_into_fields("💸 1d. 本日交易明細", trades_text, syntax="diff", max_len=950))
        fields.extend(_split_into_fields("⚠️ 1e. 本日未成交/滑價取消明細", unfilled_text, syntax="diff", max_len=950))
        fields.extend(_split_into_fields("📈 2. 評分與相對排名 (第二層)", section2_value, max_len=950))
        fields.extend(_split_into_fields("🚨 3. 今日停損警告清單", section3_value, max_len=950))
        fields.extend(_split_into_fields("🧠 4. 經理人交易配置與理由 (第三層)", section4_value, max_len=950))

        # ── Discord Embed 6000 字元限制：拆分多個 Embed 分批發送 ──
        # 💡 將安全防線降低，留足夠的空間給 Discord 後端緩衝，避免 500 錯誤
        MAX_EMBED_CHARS = 4000  # 從 5500 調降至 4000，極致安全線
        MAX_FIELDS_PER_EMBED = 10  # 單個卡片不要塞太多欄位，改為最多 10 個

        base_chars = len(subject) + len(mode_label) + 100  # 固定寬鬆基底

        embeds = []
        current_fields = []
        current_chars = base_chars

        for field in fields:
            field_chars = len(field.get("name", "")) + len(field.get("value", ""))
            
            # 💡 防禦性檢查：如果單個 field 本身就超過單卡上限（理論上不應該，因為前面限制 950）
            if field_chars + base_chars > MAX_EMBED_CHARS:
                # 強制截斷這個極端欄位以保護系統不崩潰
                field["value"] = field["value"][:(MAX_EMBED_CHARS - base_chars - 50)] + "\n...(因防禦限制截斷)..."
                field_chars = len(field.get("name", "")) + len(field.get("value", ""))

            # 若加入此 field 後會超過單張卡片上限，就先將目前的打包，並另開一張新卡
            if current_fields and (current_chars + field_chars > MAX_EMBED_CHARS or len(current_fields) >= MAX_FIELDS_PER_EMBED):
                embeds.append(list(current_fields))
                current_fields = []
                current_chars = base_chars
                
            current_fields.append(field)
            current_chars += field_chars

        if current_fields:
            embeds.append(current_fields)

        ts = datetime.now(timezone.utc).isoformat()
        all_success = True

        # ── 建立完整 Plaintext Markdown 報告檔案 (供方案 A 附件一鍵下載複製) ──
        full_report_md = (
            f"# {subject}\n"
            f"**環境/模式**: `{mode_label}` | **AI模型引擎**: `{model_label}` | **日期**: `{current_date_label}`\n\n"
            f"---\n\n"
            f"### 🌦️ 大盤氣候與帳戶狀態\n"
            f"{climate_header}\n"
            f"**氣候判定理由**:\n{climate_reason}\n\n"
            f"{account_header}\n\n"
            f"---\n\n"
            f"### 💸 本日交易與成交明細\n"
            f"```diff\n{trades_text}\n```\n\n"
            f"### ⚠️ 本日未成交/滑價取消明細\n"
            f"```diff\n{unfilled_text}\n```\n\n"
            f"---\n\n"
            f"### 📈 分析師評分與相對排名 (第二層)\n"
            f"{section2_value}\n\n"
            f"---\n\n"
            f"### 🚨 今日停損警告清單\n"
            f"{section3_value}\n\n"
            f"---\n\n"
            f"### 🧠 經理人交易配置與理由 (第三層)\n"
            f"{section4_value}\n"
        )
        report_filename = f"{current_date_label}_Daily_Report.md"

        for embed_idx, embed_fields in enumerate(embeds):
            is_first = (embed_idx == 0)
            is_last = (embed_idx == len(embeds) - 1)
            
            # 💡 建立乾淨標準的 Embed 結構
            embed_obj = {
                "color": color,
                "fields": embed_fields,
            }
            if is_first:
                embed_obj["title"] = subject
                embed_obj["description"] = f"**環境/模式**: `{mode_label}` | **AI引擎**: `{model_label}`\n📎 *完整報告 Markdown 檔案已作為附件發送 (可點擊下載一次複製全文)*"
            else:
                embed_obj["title"] = f"{subject} (第 {embed_idx + 1}/{len(embeds)} 部分)"
                embed_obj["description"] = "*(續前文)*"
                
            if is_last:
                embed_obj["footer"] = {"text": "此報告由 AI 三層決策系統自動發送。"}
                embed_obj["timestamp"] = ts

            payload = {
                "username": "AI 台股自動交易報告",
                "embeds": [embed_obj]
            }

            file_to_attach = (report_filename, full_report_md, "text/markdown") if is_first else None
            success = _send_discord_webhook(webhook_url, payload, file_tuple=file_to_attach)
            if not success:
                all_success = False
                err_msg = f"發送每日整合報告 (第 {embed_idx+1}/{len(embeds)} 部分) 至 Discord Webhook 失敗 (網址: {webhook_url})。"
                log_system_event("ERROR", err_msg)
                raise RuntimeError(err_msg)
                
            # 若還有後續 embed，延長等待時間至 1.0s，防止打爆 Discord API Rate Limit
            if not is_last:
                time.sleep(1.0)

        if all_success:
            log_system_event("INFO", f"已成功發送 {current_date_label} 每日整合報告至 Discord Webhook ({mode_label})，共 {len(embeds)} 個 Embed 訊息。")
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
                    "description": _safe_embed_description(
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

def send_periodic_review_notification(review_result: Dict[str, Any], review_type: Optional[str] = None) -> None:
    """
    發送多層週期性 (月 / 季 / 年) AI 復盤與 Skills 演化報告至專屬的 DISCORD_WEBHOOK_REVIEW 頻道。
    依據 Layer 1 (指標診斷) -> Layer 2 (交易執行診斷) -> Layer 3 (整體策略總結) 有序推播並附帶全文 .md 檔案。
    """
    webhook_url = config.discord.webhook_monthly_review or config.discord.webhook_live
    if not webhook_url:
        print(" [Discord通知器] 警告: 未配置 DISCORD_WEBHOOK_MONTHLY_REVIEW 網址，跳過推播。")
        return

    review_month = str(review_result.get("review_month") or review_result.get("period", "未知期間"))
    
    # 自動推算復盤週期標籤 (月度 / 季度 / 年度)
    if review_type:
        period_label = review_type
    elif "Q" in review_month.upper():
        period_label = "季度"
    elif len(review_month) == 4 and review_month.isdigit():
        period_label = "年度"
    else:
        period_label = "月度"

    metrics = review_result.get("metrics", {})
    stock_ind_reports = review_result.get("stock_indicator_reports", [])
    stock_exe_reports = review_result.get("stock_execution_reports", [])
    stock_reports = review_result.get("stock_reports", [])
    
    indicator_summary = review_result.get("indicator_summary", "")
    cio_summary = review_result.get("cio_summary", "")
    overall_summary = review_result.get("overall_summary", "")
    key_learnings = review_result.get("key_learnings", [])
    
    ind_skills = review_result.get("indicator_skills") or {}
    exe_skills = review_result.get("execution_skills") or {}

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()

    # =====================================================================
    # 1. 發送 Layer 1：技術指標與打分診斷卡片
    # =====================================================================
    target_ind_reports = stock_ind_reports if stock_ind_reports else stock_reports
    for s_rep in target_ind_reports:
        sc = s_rep.get("stock_code", "")
        retro = s_rep.get("indicator_retrospective") or s_rep.get("stock_retrospective", "")
        anomaly = s_rep.get("anomaly_trait")
        anomaly_text = f"\n\n**【個股特殊特徵與走勢慣性】**\n{anomaly}" if anomaly else ""

        stock_payload = {
            "username": f"AI 檢討 AI - Layer 1 個股指標診斷 ({period_label})",
            "embeds": [
                {
                    "title": f"📈 個股指標復盤: {sc} (期間: {review_month})",
                    "description": _safe_embed_description(f"{retro}{anomaly_text}"),
                    "color": 3447003, # 藍色
                    "footer": {"text": f"AIAutoStocks Layer 1 指標診斷 · {sc}"},
                    "timestamp": ts
                }
            ]
        }
        _send_discord_webhook(webhook_url, stock_payload)
        time.sleep(1.0)

    v_rules = ind_skills.get("v_shape_reversal_patterns", [])
    a_rules = ind_skills.get("a_shape_top_warnings", [])
    v_text = "\n".join([f"• {item.get('pattern_rule')} (預期機率: {item.get('expected_probability_pct', 0)}%)" for item in v_rules if isinstance(item, dict)]) if v_rules else "無"
    a_text = "\n".join([f"• {item.get('pattern_rule')} (預期機率: {item.get('expected_probability_pct', 0)}%)" for item in a_rules if isinstance(item, dict)]) if a_rules else "無"

    l1_summary_payload = {
        "username": f"AI 檢討 AI - Layer 1 指標總診斷 ({period_label})",
        "embeds": [
            {
                "title": f"📊 Layer 1 技術指標與打分品質總診斷 (期間: {review_month})",
                "description": _safe_embed_description(
                    f"**【指標與打分品質總評】**\n{indicator_summary}\n\n"
                    f"**【V 型強勢反彈特徵 Key Learnings】**\n{v_text}\n\n"
                    f"**【A 型頂點/誘多警戒 Key Warnings】**\n{a_text}"
                ),
                "color": 3447003, # 藍色
                "footer": {"text": f"AIAutoStocks Layer 1 綜合診斷卡片 ({period_label})"},
                "timestamp": ts
            }
        ]
    }
    _send_discord_webhook(webhook_url, l1_summary_payload)
    time.sleep(1.0)

    # =====================================================================
    # 2. 發送 Layer 2：交易與部位執行診斷卡片
    # =====================================================================
    target_exe_reports = stock_exe_reports if stock_exe_reports else stock_reports
    for s_rep in target_exe_reports:
        sc = s_rep.get("stock_code", "")
        retro = s_rep.get("execution_retrospective") or s_rep.get("stock_retrospective", "")

        stock_payload = {
            "username": f"AI 檢討 AI - Layer 2 個股執行診斷 ({period_label})",
            "embeds": [
                {
                    "title": f"⚔️ 個股交易執行復盤: {sc} (期間: {review_month})",
                    "description": _safe_embed_description(retro),
                    "color": 15844367, # 金黃色
                    "footer": {"text": f"AIAutoStocks Layer 2 執行診斷 · {sc}"},
                    "timestamp": ts
                }
            ]
        }
        _send_discord_webhook(webhook_url, stock_payload)
        time.sleep(1.0)

    learnings_text = "\n".join([f"• {item}" for item in key_learnings]) if key_learnings else "無"

    l2_summary_payload = {
        "username": f"AI 檢討 AI - Layer 2 CIO 執行總評 ({period_label})",
        "embeds": [
            {
                "title": f"🛡️ Layer 2 交易執行與部位風控 CIO 總評 (期間: {review_month})",
                "description": _safe_embed_description(
                    f"**【CIO 組合執行與 Timing 總評】**\n{cio_summary}\n\n"
                    f"**【交易執行核心學習點】**\n{learnings_text}"
                ),
                "color": 15844367, # 金黃色
                "footer": {"text": f"AIAutoStocks Layer 2 CIO 執行卡片 ({period_label})"},
                "timestamp": ts
            }
        ]
    }
    _send_discord_webhook(webhook_url, l2_summary_payload)
    time.sleep(1.0)

    # 組合各標的個股 Layer 1 與 Layer 2 診斷區塊 (包含標的名稱與獨立詳細診斷)
    ind_md_blocks = []
    for s_rep in target_ind_reports:
        sc = s_rep.get("stock_code", "")
        sn = get_stock_name(sc)
        name_str = f" ({sn})" if sn else ""
        retro = s_rep.get("indicator_retrospective") or s_rep.get("stock_retrospective", "無")
        anomaly = s_rep.get("anomaly_trait")
        anomaly_str = f"\n> 💡 **個股走勢慣性 (Anomaly Trait)**: {anomaly}" if anomaly else ""
        ind_md_blocks.append(f"### 📈 標的 `{sc}`{name_str}\n{retro}{anomaly_str}")
    ind_reports_md = "\n\n".join(ind_md_blocks) if ind_md_blocks else "無個股指標診斷數據。"

    exe_md_blocks = []
    for s_rep in target_exe_reports:
        sc = s_rep.get("stock_code", "")
        sn = get_stock_name(sc)
        name_str = f" ({sn})" if sn else ""
        retro = s_rep.get("execution_retrospective") or s_rep.get("stock_retrospective", "無")
        exe_md_blocks.append(f"### 🛡️ 標的 `{sc}`{name_str}\n{retro}")
    exe_reports_md = "\n\n".join(exe_md_blocks) if exe_md_blocks else "無個股執行診斷數據。"

    # =====================================================================
    # 3. 發送 Layer 3：整體復盤與下期戰術策略總結卡片 (附帶 .md 全文附件)
    # =====================================================================
    tactical_rules = exe_skills.get("tactical_rules", [])
    tactical_text = "\n".join([f"• {r}" for r in tactical_rules]) if tactical_rules else "• 維持穩健分批進場紀律"

    periodic_report_md = (
        f"# 🏆 {period_label} AI 復盤與戰術演化報告 (期間: {review_month})\n\n"
        f"## 📊 當期實盤硬指標統計\n"
        f"• 平倉總筆數: **{metrics.get('total_trades', 0)}** 筆 | 勝率: **{metrics.get('win_rate', 0)}%**\n"
        f"• 實現總損益: **{metrics.get('total_realized_pnl', 0):,}** 元 | 盈虧比: **{metrics.get('payoff_ratio', 0)}** | 獲利因子: **{metrics.get('profit_factor', 0)}**\n"
        f"• 期望潛在漲幅 Mean: **+{metrics.get('mean_upside_ratio', 0)*100:.2f}%** (Std: {metrics.get('std_upside_ratio', 0)})\n"
        f"• 期望潛在回撤 Mean: **{metrics.get('mean_drawdown_ratio', 0)*100:.2f}%** (Std: {metrics.get('std_drawdown_ratio', 0)})\n"
        f"• 成交平均滑價 Mean: **{metrics.get('mean_slippage_ratio', 0)*100:.2f}%** (Std: {metrics.get('std_slippage_ratio', 0)})\n"
        f"• 未成交取消單: **{metrics.get('total_cancelled_orders', 0)}** 筆 (取消率: **{metrics.get('cancellation_rate_pct', 0)}%**)\n\n"
        f"---\n\n"
        f"## 📈 Layer 1 技術指標與打分品質總診斷\n{indicator_summary}\n\n"
        f"### 🔍 各標的 Layer 1 個股指標與打分診斷報告\n{ind_reports_md}\n\n"
        f"---\n\n"
        f"## 🛡️ Layer 2 CIO 交易執行與部位風控總評\n{cio_summary}\n\n"
        f"**【交易執行核心學習點】**\n{learnings_text}\n\n"
        f"### ⚔️ 各標的 Layer 2 個股交易與執行風控診斷報告\n{exe_reports_md}\n\n"
        f"---\n\n"
        f"## 🏆 Layer 3 {period_label}整體策略總結\n{overall_summary}\n\n"
        f"**【下期關鍵戰術執行守則】**\n{tactical_text}\n\n"
        f"---\n\n"
        f"## ⚙️ 演化下期動態戰術 Skills (JSON)\n```json\n{json.dumps(review_result.get('skills', {}), ensure_ascii=False, indent=2)}\n```\n"
    )
    report_filename = f"{review_month}_{period_label}_Review_Report.md"

    overall_payload = {
        "username": f"AI 檢討 AI - Layer 3 策略總結 ({period_label})",
        "embeds": [
            {
                "title": f"🏆 {period_label}整體復盤與下期戰術策略總結 (期間: {review_month})",
                "description": _safe_embed_description(
                    f"**【當期實盤硬指標統計】**\n"
                    f"• 平倉總筆數: **{metrics.get('total_trades', 0)}** 筆 | 勝率: **{metrics.get('win_rate', 0)}%**\n"
                    f"• 實現總損益: **{metrics.get('total_realized_pnl', 0):,}** 元 | 盈虧比: **{metrics.get('payoff_ratio', 0)}** | 獲利因子: **{metrics.get('profit_factor', 0)}**\n"
                    f"• 期望潛在漲幅 Mean: **+{metrics.get('mean_upside_ratio', 0)*100:.2f}%** (Std: {metrics.get('std_upside_ratio', 0)})\n"
                    f"• 期望潛在回撤 Mean: **{metrics.get('mean_drawdown_ratio', 0)*100:.2f}%** (Std: {metrics.get('std_drawdown_ratio', 0)})\n"
                    f"• 成交平均滑價 Mean: **{metrics.get('mean_slippage_ratio', 0)*100:.2f}%** (Std: {metrics.get('std_slippage_ratio', 0)})\n"
                    f"• 未成交取消單: **{metrics.get('total_cancelled_orders', 0)}** 筆 (取消率: **{metrics.get('cancellation_rate_pct', 0)}%**)\n\n"
                    f"{overall_summary}\n\n"
                    f"**【下期關鍵戰術執行守則】**\n{tactical_text}\n\n"
                    f"📎 *完整{period_label}復盤與演化 Skills Markdown 檔案已作為附件發送*"
                ),
                "color": 10181046, # 紫色
                "footer": {"text": f"此報告由 {period_label} Review Agent 檢討引擎自動產出與發送。"},
                "timestamp": ts
            }
        ]
    }
    _send_discord_webhook(webhook_url, overall_payload, file_tuple=(report_filename, periodic_report_md, "text/markdown"))
    log_system_event("INFO", f"已成功發送 {review_month} {period_label}復盤與演化 Skills 至 Discord Webhook。")

# 別名相容：月度復盤直接對應至通用週期復盤
send_monthly_review_notification = send_periodic_review_notification


def send_test_notification(webhook_type: str = "monthly_review", test_mode: str = "simple") -> Dict[str, Any]:
    """
    發送測試訊息至指定的 Discord Webhook 頻道（用於測試與驗證通知通道是否正常運作）。
    :param test_mode: 'simple' (發送簡單測試卡片) 或 'full_review' (發送完整模擬復盤的多 Embed 與 .md 報告)
    """
    webhook_type = str(webhook_type).lower().strip()
    test_mode = str(test_mode).lower().strip()

    if webhook_type in ("sandbox", "paper"):
        webhook_url = config.discord.webhook_sandbox
        channel_name = "沙盒/模擬交易頻道 (DISCORD_WEBHOOK_SANDBOX)"
    elif webhook_type in ("live", "real"):
        webhook_url = config.discord.webhook_live
        channel_name = "實盤交易頻道 (DISCORD_WEBHOOK_LIVE)"
    elif webhook_type in ("monthly", "monthly_review"):
        webhook_url = config.discord.webhook_monthly_review or config.discord.webhook_live
        channel_name = "月度 AI 復盤頻道 (DISCORD_WEBHOOK_MONTHLY_REVIEW)"
    elif webhook_type in ("quarterly", "quarterly_review"):
        webhook_url = config.discord.webhook_quarterly_review or config.discord.webhook_monthly_review or config.discord.webhook_live
        channel_name = "季度 AI 復盤頻道 (DISCORD_WEBHOOK_QUARTERLY_REVIEW)"
    elif webhook_type in ("yearly", "yearly_review"):
        webhook_url = config.discord.webhook_yearly_review or config.discord.webhook_monthly_review or config.discord.webhook_live
        channel_name = "年度 AI 復盤頻道 (DISCORD_WEBHOOK_YEARLY_REVIEW)"
    else:
        webhook_url = config.discord.webhook_monthly_review or config.discord.webhook_live
        channel_name = f"自訂頻道 ({webhook_type})"

    if not webhook_url:
        return {"success": False, "message": f"未配置 {channel_name} 的 Discord Webhook 網址。"}

    if test_mode in ("full_review", "review", "full"):
        # 測試完整月度復盤推播 (模擬 Layer 1 -> Layer 2 -> Layer 3 多卡片與 .md 全文附件)
        mock_review_result = {
            "review_month": "2026-07 (測試演練)",
            "metrics": {
                "total_trades": 8,
                "win_rate": 62.5,
                "total_realized_pnl": 35600,
                "payoff_ratio": 2.1,
                "profit_factor": 1.85,
                "mean_upside_ratio": 0.052,
                "std_upside_ratio": 0.018,
                "mean_drawdown_ratio": -0.021,
                "std_drawdown_ratio": 0.009,
                "mean_slippage_ratio": 0.0012,
                "std_slippage_ratio": 0.0004,
                "total_cancelled_orders": 1,
                "cancellation_rate_pct": 11.1
            },
            "stock_indicator_reports": [
                {
                    "stock_code": "2330",
                    "indicator_retrospective": "台積電 (2330) 在 7 月突破關鍵抗力位，均線向上多頭排列。分析師評分無通膨傾斜。",
                    "anomaly_trait": "大盤拉回時常展現強勢抗跌慣性"
                },
                {
                    "stock_code": "0051",
                    "indicator_retrospective": "元大中型100 (0051) 振幅溫和，指標與評分準確捕捉底波段反彈。",
                    "anomaly_trait": "流動性良好，指標貼合成分股大盤特徵"
                }
            ],
            "stock_execution_reports": [
                {
                    "stock_code": "2330",
                    "execution_retrospective": "買單分位數 22%，成功佈局於波段相對低點，無追高現象。"
                },
                {
                    "stock_code": "0051",
                    "execution_retrospective": "委託單滑價控制極佳 (< 0.1%)，移動停損機制運作良好。"
                }
            ],
            "indicator_summary": "【測試】Layer 1 技術指標與評分總診斷運作良好，V 型反彈與 A 頂預警機制正常。",
            "cio_summary": "【測試】Layer 2 CIO 風控與執行層表現優異，入場 Timing 與部位控管符合作戰紀律。",
            "overall_summary": "【測試】Layer 3 整體策略運作健全，此為完整的 Discord 推播卡片與 Markdown 附件格式測試。",
            "key_learnings": ["均線金叉配合量能突破進場勝率最高", "嚴格防範高檔背離與追高風險"],
            "skills": {
                "version": "2026-07-test",
                "indicator_skills": {
                    "v_shape_reversal_patterns": [{"pattern_rule": "量能突破且 RSI > 50", "expected_probability_pct": 80}]
                },
                "execution_skills": {
                    "tactical_rules": ["分批建倉降低滑價", "移動停損鎖住獲利"]
                }
            }
        }
        try:
            send_periodic_review_notification(mock_review_result, review_type="月度 (測試演練)")
            log_system_event("INFO", f"已成功測試發送完整復盤多 Embed 報告與 .md 附件至 {channel_name}。")
            return {"success": True, "message": f"已成功測試發送『完整復盤多卡片與 Markdown 附件』至 {channel_name}！"}
        except Exception as e:
            return {"success": False, "message": f"發送完整復盤測試失敗: {str(e)}"}
    else:
        # 簡單連線測試卡片
        ts = datetime.now(timezone.utc).isoformat()
        test_payload = {
            "username": "AIAutoStocks 系統測試員",
            "embeds": [
                {
                    "title": "🧪 Discord Webhook 通道連線測試成功",
                    "description": _safe_embed_description(
                        f"**測試時間**: `{get_local_taiwan_datetime_str()}`\n"
                        f"**測試頻道類型**: `{channel_name}`\n"
                        f"**系統狀態**: `OK` (連線與 Embed 格式防護驗證通過)\n\n"
                        f"如果您能在 Discord 看見此卡片，代表 AIAutoStocks 通知通道運作正常！"
                    ),
                    "color": 3066993, # 翠綠色
                    "footer": {"text": "AIAutoStocks 通訊測試助手"},
                    "timestamp": ts
                }
            ]
        }
        success = _send_discord_webhook(webhook_url, test_payload)
        if success:
            log_system_event("INFO", f"已成功連線發送 Discord Webhook 測試訊息至 {channel_name}。")
            return {"success": True, "message": f"已成功發送測試訊息至 {channel_name}！"}
        else:
            return {"success": False, "message": f"發送測試訊息至 {channel_name} 失敗，請檢查 Webhook 網址或伺服器連線。"}



