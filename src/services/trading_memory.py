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

def get_active_skills_context(is_paper: bool = False) -> str:
    """
    從 Supabase monthly_skills 表中，精準撈取最新單一筆 (ORDER BY created_at DESC LIMIT 1) 之 JSON 戰術 Skills，
    組裝為 System Prompt 文字傳給 decision_agent。
    """
    from src.services.supabase_client import supabase
    import json

    default_skills = {
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
            "regime_posture": {
                "BULLISH_TREND": "AGGRESSIVE",
                "BEARISH_TREND": "DEFENSIVE",
                "HIGH_VOLATILITY": "CONSERVATIVE"
            },
            "tactical_rules": [
                "Maintain strict risk management and follow analyst scores."
            ]
        }
    }

    try:
        res = supabase.table("monthly_skills") \
            .select("skills, review_month, created_at") \
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
            rev_month = data[0].get("review_month", "最新")
        else:
            skills_json = default_skills
            rev_month = "預設基準"
    except Exception as e:
        print(f" [交易記憶管理器] 警告: 撈取最新 monthly_skills 失敗，使用預設 Skills: {e}")
        skills_json = default_skills
        rev_month = "預設基準"

    skills_pretty = json.dumps(skills_json, ensure_ascii=False, indent=2)

    return (
        f"【當前生效之動態交易戰術規範 (Active Dynamic JSON Skills - 月份: {rev_month})】\n"
        f"(此規範由「月度檢討 Agent」根據實盤損益全權演化產出，徹底替代傳統硬編碼程式覆寫)：\n"
        f"```json\n{skills_pretty}\n```\n"
        f"請投資組合經理 AI 嚴格遵守上述演化出的最新買入門檻得分、部位權重與風控停損比率。"
    )

def get_indicator_skills_context(is_paper: bool = False) -> str:
    """
    特別撈取 Layer 1 產出之 indicator_skills Context，供 analyst_agent 打分前置參考。
    """
    from src.services.supabase_client import supabase
    import json

    try:
        res = supabase.table("monthly_skills") \
            .select("skills, review_month") \
            .eq("is_paper", is_paper) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        data = res.data or []
        if data and "skills" in data[0]:
            skills_json = data[0]["skills"]
            if isinstance(skills_json, str):
                skills_json = json.loads(skills_json)
            ind_skills = skills_json.get("indicator_skills", {})
            if ind_skills:
                ind_pretty = json.dumps(ind_skills, ensure_ascii=False, indent=2)
                return f"【最新技術指標與評分 Skills 規範】:\n```json\n{ind_pretty}\n```"
    except Exception as e:
        print(f" [交易記憶管理器] 警告: 撈取 indicator_skills 失敗: {e}")
    return ""

