# Path: src/services/trading_memory.py
from typing import List, Dict, Any
from src.services.supabase_client import get_orders

# 交易記憶與經驗 成敗定義臨界值
SUCCESS_ROI_THRESHOLD = 0.03  # +3% 以上定義為成功經驗
FAILURE_ROI_THRESHOLD = -0.02  # -2% 以下定義為失敗經驗 (警示)

def get_experience_context(limit: int = 3) -> str:
    """
    檢索歷史交易紀錄，並將其分類整理成結構化的經驗上下文 (Few-Shot Prompt) 餵給 AI
    :param limit: 成功與失敗案例各自最多載入的筆數限制 (防止 Context Window 超限)
    :returns: 格式化後的經驗上下文文字
    """
    try:
        # 載入過去 3 個月的所有已平倉交易記錄 (有 realized_pnl 的賣出單)
        # 為了簡化，直接撈取所有訂單，然後在記憶體內篩選有實現損益的賣出單
        orders = get_orders()
    except Exception as e:
        print(f" [交易記憶管理器] 警告: 無法從 Supabase 取得歷史交易以構建記憶: {str(e)}")
        orders = []

    if not orders:
        return (
            "【交易經驗上下文】\n"
            "目前資料庫中尚無歷史交易平倉經驗。請依照現有的市場 K 線指標，進行審慎獨立的交易決策。"
        )

    successful_cases: List[Dict[str, Any]] = []
    failed_cases: List[Dict[str, Any]] = []

    for o in orders:
        # 只處理賣出平倉單且有實現損益的單子
        if o.get("action") == "SELL":
            realized_pnl = float(o.get("realized_pnl") or 0.0)
            total_amount = float(o.get("total_amount") or 0.0)
            
            if total_amount <= 0:
                continue

            # 計算該筆平倉的原始成本與投資報酬率 (ROI)
            # 賣出總額 - 實現損益 = 原始成本
            cost = total_amount - realized_pnl
            roi = realized_pnl / cost if cost > 0 else 0.0

            case_info = {
                "stock_code": o.get("stock_code"),
                "price": float(o.get("price") or 0.0),
                "execution_price": float(o.get("execution_price") or o.get("price") or 0.0),
                "quantity": float(o.get("quantity") or 0.0),
                "realized_pnl": realized_pnl,
                "roi": roi,
                "date": o.get("executed_at", "")[:10]  # 只取 YYYY-MM-DD
            }

            if roi >= SUCCESS_ROI_THRESHOLD:
                successful_cases.append(case_info)
            elif roi <= FAILURE_ROI_THRESHOLD:
                failed_cases.append(case_info)

    # 排序：優先提供損益百分比最大（最成功/最失敗）的案例給 AI 學習
    successful_cases.sort(key=lambda x: x["roi"], reverse=True)
    failed_cases.sort(key=lambda x: x["roi"])  # 由最慘的排在最前

    # 限制載入筆數，防止 token 浪費
    successful_cases = successful_cases[:limit]
    failed_cases = failed_cases[:limit]

    # 組裝 Few-shot 結構化經驗文本
    lines = ["【交易經驗上下文 (學習自過去真實交易成敗)】"]
    
    if successful_cases:
        lines.append("\n◎ 過去成功交易案例 (回報率良好，請參考當時的決策脈絡)：")
        for i, c in enumerate(successful_cases, 1):
            lines.append(
                f"  {i}. 股票: {c['stock_code']} | 賣出日期: {c['date']} | "
                f"委託價: {c['price']:,.2f} | 成交均價: {c['execution_price']:,.2f} | 股數: {c['quantity']:,.0f} | "
                f"平倉損益: +{c['realized_pnl']:,.0f} 元 | 投報率 (ROI): +{c['roi']*100:.2f}%"
            )
    else:
        lines.append("\n◎ 過去成功交易案例：暫無顯著成功案例可供參考。")

    if failed_cases:
        lines.append("\n◎ 過去失敗交易案例 (虧損警示，請分析並避免重複類似錯誤)：")
        for i, c in enumerate(failed_cases, 1):
            lines.append(
                f"  {i}. 股票: {c['stock_code']} | 賣出日期: {c['date']} | "
                f"委託價: {c['price']:,.2f} | 成交均價: {c['execution_price']:,.2f} | 股數: {c['quantity']:,.0f} | "
                f"平倉損益: {c['realized_pnl']:,.0f} 元 | 投報率 (ROI): {c['roi']*100:.2f}%"
            )
    else:
        lines.append("\n◎ 過去失敗交易案例：暫無顯著失敗虧損案例。")

    lines.append("\n請 AI 決策引擎參考上述成功與失敗交易經驗的投報率特徵，在本次分析中避免追高殺低，優化進出場邏輯。")

    return "\n".join(lines)

import copy

DEFAULT_TACTICAL_SKILLS = {
    "version": "baseline-v1",
    "indicator_skills": {
        "v_shape_reversal_patterns": [
            {"pattern_rule": "量能突破且 RSI 於 50 以上向上黃金交叉時 V 型反彈機率高", "expected_probability_pct": 80}
        ],
        "a_shape_top_warnings": [
            {"pattern_rule": "高檔乖離率過大且爆大量後無續攻力道時慎防 A 頂誘多", "expected_probability_pct": 85}
        ],
        "stock_specific_rules": [],
        "score_calibration_rules": [
            {"calibration_rule": "維持標準買入門檻，監督分析師打分品質", "expected_probability_pct": 90}
        ],
        "regime_indicator_rules": {
            "BULLISH_TREND": {"focus": "著重動能與量能突破指標", "expected_probability_pct": 85},
            "BEARISH_TREND": {"focus": "要求安全得分 >= 15 且有底線支撐", "expected_probability_pct": 90}
        }
    },
    "execution_skills": {
        "min_buy_score": 60,
        "max_single_stock_weight": 4,
        "stop_loss_pct": -0.05,
        "take_profit_pct": 0.12,
        "stop_loss_atr_mult": 2.0,
        "take_profit_atr_mult": 3.5,
        "chase_buffer_tiers": [
            {"min_score": 85, "buy_buffer_pct": 0.015, "description": "+1.5% 高信心度強勢追價"},
            {"min_score": 70, "buy_buffer_pct": 0.010, "description": "+1.0% 標準追價"},
            {"min_score": 0,  "buy_buffer_pct": 0.005, "description": "+0.5% 溫和追價"}
        ],
        "sell_discount_tiers": [
            {"max_score": 49, "sell_discount_pct": -0.015, "description": "-1.5% 風險停損/急跌果斷求售變現"},
            {"max_score": 69, "sell_discount_pct": -0.010, "description": "-1.0% 轉弱調節標準讓價出清"},
            {"max_score": 100, "sell_discount_pct": -0.005, "description": "-0.5% 高分鎖利惜售防賤賣"}
        ],
        "regime_posture": {
            "BULLISH_TREND": "AGGRESSIVE",
            "BEARISH_TREND": "DEFENSIVE",
            "HIGH_VOLATILITY": "CONSERVATIVE"
        },
        "tactical_rules": [
            "【規則優先權】當個股帳面獲利已觸發動態鎖利門檻 (take_profit_pct 或 take_profit_atr_mult) 或停損門檻時，鎖利/停損條款優先度高於高分續抱哲學，必須執行調節/平倉。",
            "【ATR動態風控】高波動標的 (ATR% >= 3.0%) 應尊重其震盪呼吸空間，依成本 - 2.0*ATR 設防，切忌被正常洗盤雜訊震出場；低波動標的 (ATR% <= 1.5%) 應收緊防線並提早鎖利。",
            "在防禦氣候期間，若個股出現 3% 以上的技術性反彈，應主動執行減碼以鎖定利潤，避免回吐。",
            "嚴格執行離場限價策略，禁止在流動性收縮時使用市價單，以降低成交滑價損失。"
        ]
    }
}

DEFAULT_ACTIVE_SKILLS_DATA = {
    "review_month": "預設基準",
    "skills": DEFAULT_TACTICAL_SKILLS
}

def merge_monthly_and_quarterly_skills(
    monthly_skills: Dict[str, Any],
    quarterly_skills: Dict[str, Any]
) -> Dict[str, Any]:
    """
    彙整月度與季度動態戰術 Skills：
    核心原則：【當季與月 Skills 矛盾衝突時，以季 Skills 為主導最高準則】。
    1. 執行參數 (Execution Skills)：
       - 門檻得分、最重權重、停損停利百分比、ATR 停損停利乘數：季優先覆蓋月。
       - 買進追價 (chase_buffer_tiers) 與 賣出讓價 (sell_discount_tiers)：季階梯優先覆蓋月階梯。
       - 大盤姿態 (regime_posture)：季氣候設定覆蓋月氣候設定。
       - 戰術與風控規則 (tactical_rules & entry_timing_rules)：季度戰術規則置於最前列（最高優先權），月度非衝突規則隨後補齊。
    2. 指標技能 (Indicator Skills)：
       - 氣候指標側重 (regime_indicator_rules)：季覆蓋月。
       - 評分校正規則 (score_calibration_rules)：季優先。
       - 個股特殊特徵 (stock_specific_rules)：同個股以季優先。
       - V 轉與 A 頂型態特徵：季度規則優先列於前。
    """
    merged = copy.deepcopy(monthly_skills) if monthly_skills else copy.deepcopy(DEFAULT_TACTICAL_SKILLS)
    if not quarterly_skills:
        return merged

    q_copy = copy.deepcopy(quarterly_skills)

    # 1. 處理 Execution Skills
    m_exec = merged.setdefault("execution_skills", {})
    q_exec = q_copy.get("execution_skills", {})

    scalar_keys = [
        "min_buy_score", "max_single_stock_weight", "stop_loss_pct",
        "take_profit_pct", "stop_loss_atr_mult", "take_profit_atr_mult"
    ]
    for k in scalar_keys:
        if k in q_exec and q_exec[k] is not None:
            m_exec[k] = q_exec[k]

    if "chase_buffer_tiers" in q_exec and q_exec["chase_buffer_tiers"]:
        m_exec["chase_buffer_tiers"] = q_exec["chase_buffer_tiers"]

    if "sell_discount_tiers" in q_exec and q_exec["sell_discount_tiers"]:
        m_exec["sell_discount_tiers"] = q_exec["sell_discount_tiers"]

    # 大盤姿態合併：季覆蓋月
    m_regime = m_exec.setdefault("regime_posture", {})
    q_regime = q_exec.get("regime_posture", {})
    if isinstance(m_regime, dict) and isinstance(q_regime, dict):
        m_regime.update(q_regime)

    # 戰術規則列表：季前置優先
    q_tactical = q_exec.get("tactical_rules", [])
    m_tactical = m_exec.get("tactical_rules", [])
    combined_tactical = list(q_tactical)
    for r in m_tactical:
        if r not in combined_tactical:
            combined_tactical.append(r)
    m_exec["tactical_rules"] = combined_tactical

    # 進場 Timing 規則：季前置優先
    q_timing = q_exec.get("entry_timing_rules", [])
    m_timing = m_exec.get("entry_timing_rules", [])
    combined_timing = list(q_timing)
    for r in m_timing:
        if r not in combined_timing:
            combined_timing.append(r)
    m_exec["entry_timing_rules"] = combined_timing

    # 2. 處理 Indicator Skills
    m_ind = merged.setdefault("indicator_skills", {})
    q_ind = q_copy.get("indicator_skills", {})

    if "regime_indicator_rules" in q_ind and isinstance(q_ind["regime_indicator_rules"], dict):
        m_ind_regime = m_ind.setdefault("regime_indicator_rules", {})
        if isinstance(m_ind_regime, dict):
            m_ind_regime.update(q_ind["regime_indicator_rules"])

    for rule_key in ["v_shape_reversal_patterns", "a_shape_top_warnings", "score_calibration_rules"]:
        q_rules = q_ind.get(rule_key, [])
        m_rules = m_ind.get(rule_key, [])
        combined = list(q_rules)
        for mr in m_rules:
            if mr not in combined:
                combined.append(mr)
        m_ind[rule_key] = combined

    # 個股特殊特徵規則：季覆蓋同股代碼
    q_stock_rules = {r.get("stock_code"): r for r in q_ind.get("stock_specific_rules", []) if isinstance(r, dict) and r.get("stock_code")}
    m_stock_rules = {r.get("stock_code"): r for r in m_ind.get("stock_specific_rules", []) if isinstance(r, dict) and r.get("stock_code")}
    m_stock_rules.update(q_stock_rules)
    m_ind["stock_specific_rules"] = list(m_stock_rules.values())

    merged["resolution_principle"] = "當季與月 Skills 矛盾衝突時，以季 Skills 為主導最高準則"
    return merged


def get_active_skills_data(is_paper: bool = False) -> Dict[str, Any]:
    """
    從 Supabase 撈取最新 monthly_skills 與 quarterly_skills 並進行戰術彙整。
    核心原則：【當季與月 Skills 矛盾衝突時，以季 Skills 為主導最高準則】。
    """
    from src.services.supabase_client import supabase
    import json

    default_skills = copy.deepcopy(DEFAULT_TACTICAL_SKILLS)
    monthly_skills = None
    quarterly_skills = None
    rev_month = None
    rev_quarter = None

    # 1. 撈取最新 monthly_skills
    try:
        m_res = supabase.table("monthly_skills") \
            .select("skills, review_month, created_at") \
            .eq("is_paper", is_paper) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        m_data = m_res.data or []
        if m_data and "skills" in m_data[0]:
            raw = m_data[0]["skills"]
            monthly_skills = raw if isinstance(raw, dict) else json.loads(raw)
            rev_month = m_data[0].get("review_month")
    except Exception as e:
        print(f" [交易記憶管理器] 警告: 撈取 monthly_skills 失敗: {e}")

    # 2. 撈取最新 quarterly_skills
    try:
        q_res = supabase.table("quarterly_skills") \
            .select("skills, review_quarter, created_at") \
            .eq("is_paper", is_paper) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        q_data = q_res.data or []
        if q_data and "skills" in q_data[0]:
            raw_q = q_data[0]["skills"]
            quarterly_skills = raw_q if isinstance(raw_q, dict) else json.loads(raw_q)
            rev_quarter = q_data[0].get("review_quarter")
    except Exception as e:
        print(f" [交易記憶管理器] 警告: 撈取 quarterly_skills 失敗: {e}")

    # 3. 執行彙整：衝突以季為主
    if monthly_skills and quarterly_skills:
        final_skills = merge_monthly_and_quarterly_skills(monthly_skills, quarterly_skills)
    elif quarterly_skills:
        final_skills = quarterly_skills
    elif monthly_skills:
        final_skills = monthly_skills
    else:
        final_skills = default_skills

    # 確保階梯與乘數向後相容
    exec_s = final_skills.setdefault("execution_skills", {})
    if "chase_buffer_tiers" not in exec_s:
        exec_s["chase_buffer_tiers"] = default_skills["execution_skills"]["chase_buffer_tiers"]
    if "sell_discount_tiers" not in exec_s:
        exec_s["sell_discount_tiers"] = default_skills["execution_skills"]["sell_discount_tiers"]
    if "stop_loss_atr_mult" not in exec_s:
        exec_s["stop_loss_atr_mult"] = default_skills["execution_skills"].get("stop_loss_atr_mult", 2.0)
    if "take_profit_atr_mult" not in exec_s:
        exec_s["take_profit_atr_mult"] = default_skills["execution_skills"].get("take_profit_atr_mult", 3.5)

    return {
        "review_month": rev_month or "預設基準",
        "review_quarter": rev_quarter,
        "skills": final_skills
    }


def get_active_skills_context(is_paper: bool = False) -> str:
    """
    從 Supabase 撈取彙整後之動態 JSON 戰術 Skills，
    組裝為 System Prompt 文字傳給 decision_agent。
    明確標註：當季與月 Skills 矛盾衝突時，以「季 Skills」為主導最高準則！
    """
    import json
    data_info = get_active_skills_data(is_paper=is_paper)
    rev_month = data_info["review_month"]
    rev_quarter = data_info.get("review_quarter")
    skills_json = data_info["skills"]
    skills_pretty = json.dumps(skills_json, ensure_ascii=False, indent=2)

    version_str = f"月度: {rev_month} | 季度: {rev_quarter}" if rev_quarter else f"月度: {rev_month}"

    return (
        f"【當前生效之動態交易戰術規範 (Active Dynamic JSON Skills - {version_str})】\n"
        f"【⚠️ 優先權架構規範】：此規範彙整自月度檢討與季度檢討；當季與月 Skills 矛盾衝突時，以「季 Skills」為主導最高準則！\n"
        f"```json\n{skills_pretty}\n```\n"
        f"請投資組合經理 AI 嚴格遵守上述最新買入門檻得分、部位權重與風控停損比率。"
    )


def get_indicator_skills_context(is_paper: bool = False) -> str:
    """
    撈取彙整後之 indicator_skills Context，供 analyst_agent 打分前置參考。
    若季與月衝突，以季指標 Skills 為主。
    """
    import json
    data_info = get_active_skills_data(is_paper=is_paper)
    rev_month = data_info["review_month"]
    rev_quarter = data_info.get("review_quarter")
    skills_json = data_info["skills"]
    ind_skills = skills_json.get("indicator_skills", {})
    if ind_skills:
        ind_pretty = json.dumps(ind_skills, ensure_ascii=False, indent=2)
        version_str = f"季度: {rev_quarter} + 月度: {rev_month}" if rev_quarter else f"月度: {rev_month}"
        return (
            f"【最新技術指標與評分 Skills 規範 ({version_str}，衝突以季為主)】:\n"
            f"```json\n{ind_pretty}\n```"
        )
    return ""
    """
    從 Supabase quarterly_skills 表中，精準撈取最新單一筆 (ORDER BY created_at DESC LIMIT 1) 之 JSON 戰術 Skills 字典與季度。
    """
    from src.services.supabase_client import supabase
    import json

    default_skills = copy.deepcopy(DEFAULT_TACTICAL_SKILLS)

    try:
        res = supabase.table("quarterly_skills") \
            .select("skills, review_quarter, created_at") \
            .eq("is_paper", is_paper) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        data = res.data or []
        if data and "skills" in data[0]:
            raw_skills = data[0]["skills"]
            if isinstance(raw_skills, dict):
                skills_json = raw_skills
            elif isinstance(raw_skills, str):
                skills_json = json.loads(raw_skills)
            else:
                skills_json = default_skills
            rev_quarter = data[0].get("review_quarter", "最新季度")
        else:
            skills_json = default_skills
            rev_quarter = "預設基準"
    except Exception as e:
        print(f" [交易記憶管理器] 警告: 撈取最新 quarterly_skills 失敗: {e}")
        skills_json = default_skills
        rev_quarter = "預設基準"

    return {
        "review_quarter": rev_quarter,
        "skills": skills_json
    }


def get_active_quarterly_skills_context(is_paper: bool = False) -> str:
    """
    從 Supabase quarterly_skills 表中撈取最新單一筆 JSON 戰術 Skills 組裝 Context。
    """
    import json
    data_info = get_active_quarterly_skills_data(is_paper=is_paper)
    rev_quarter = data_info["review_quarter"]
    skills_json = data_info["skills"]
    skills_pretty = json.dumps(skills_json, ensure_ascii=False, indent=2)

    return (
        f"【當前生效之季度動態戰術規範 (Active Quarterly Dynamic JSON Skills - 季度: {rev_quarter})】\n"
        f"```json\n{skills_pretty}\n```"
    )
