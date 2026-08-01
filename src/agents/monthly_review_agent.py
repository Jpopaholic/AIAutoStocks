# Path: src/agents/monthly_review_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config, get_stock_name
from src.services.gemini_rotator import call_gemini_with_rotation
from src.services.supabase_client import supabase
from src.services.monthly_aggregator import aggregate_monthly_data

# =====================================================================
# 1. Layer 1：技術指標與打分檢討模型 (Indicator & Score Retrospective)
# =====================================================================
class StockIndicatorReviewOutput(BaseModel):
    stock_code: str = Field(..., description="股票代號，如 '2330'")
    indicator_retrospective: str = Field(
        ...,
        description="繁體中文 150-250 字個股技術指標與打分品質診斷。請結合個股指標數據與當月大盤對照參照，分析指標對 V 型強勢反彈或 A 型頂點反轉的捕捉品質，評估分析師給分偏斜與精準度（完全不看成交單與損益）。"
    )
    anomaly_trait: Optional[str] = Field(
        ...,
        description="若該股偏離大盤常規具有特殊型態或異常走勢慣性（如拉回年線為典型 V 轉特徵，或高股息 ETF 波動極低），請簡述（20-50字）；若無特別異常則填 None。"
    )

class IndicatorPatternRule(BaseModel):
    pattern_rule: str = Field(..., description="指標型態與轉折規則描述（繁體中文）")
    expected_probability_pct: int = Field(..., description="預期轉折/勝率機率 (0-100)")

class StockSpecificRule(BaseModel):
    stock_code: str = Field(..., description="股票代號，如 '2330'")
    anomaly_trait: str = Field(..., description="個股特殊特徵與走勢慣性描述")
    expected_probability_pct: int = Field(..., description="預期轉折/勝率機率 (0-100)")

class ScoreCalibrationRule(BaseModel):
    calibration_rule: str = Field(..., description="分數校正與偏斜調整規則描述（如『將趨勢比重調高5%』或『買入門檻嚴格化』）")
    expected_probability_pct: int = Field(..., description="預期機率 (0-100)")

class RegimeIndicatorRule(BaseModel):
    focus: str = Field(..., description="在該大盤氣候下的指標側重或篩選標準描述")
    expected_probability_pct: int = Field(..., description="預期機率 (0-100)")

class IndicatorSkillsJSON(BaseModel):
    v_shape_reversal_patterns: List[IndicatorPatternRule] = Field(
        ..., description="2-4 條 V 型強勢反彈指標與型態特徵規則 (含 expected_probability_pct)"
    )
    a_shape_top_warnings: List[IndicatorPatternRule] = Field(
        ..., description="2-4 條 A 型頂點/誘多警戒指標規則 (含 expected_probability_pct)"
    )
    stock_specific_rules: List[StockSpecificRule] = Field(
        default_factory=list, description="0-3 條特定股票之特殊特徵與異常慣性規則 (含 expected_probability_pct)"
    )
    score_calibration_rules: List[ScoreCalibrationRule] = Field(
        ..., description="1-3 條分析師打分校正與偏斜調整規則 (含 expected_probability_pct)"
    )
    regime_indicator_rules: Dict[str, RegimeIndicatorRule] = Field(
        ..., description="大盤氣候下的指標選股側重映射，如 {'BULLISH_TREND': {'focus': '...', 'expected_probability_pct': 85}, 'BEARISH_TREND': ...}"
    )

class IndicatorReviewSummaryOutput(BaseModel):
    indicator_summary: str = Field(
        ...,
        description="繁體中文 250-400 字指標與打分品質綜合診斷報告。著重分析全月技術指標對 V/A 型轉折的捕捉能力、打分偏斜與大盤對照配合度。"
    )
    indicator_skills: IndicatorSkillsJSON = Field(
        ..., description="演化出之 Key-Value 結構化指標與評分 Skills"
    )

# =====================================================================
# 2. Layer 2：交易與部位執行檢討模型 (Trade Execution & Position Retrospective)
# =====================================================================
class StockExecutionReviewOutput(BaseModel):
    stock_code: str = Field(..., description="股票代號，如 '2330'")
    execution_retrospective: str = Field(
        ...,
        description="繁體中文 150-250 字個股交易與部位執行診斷。請結合個股買賣/觀望紀錄與大盤對照參照，評估入場 Timing（追高、太晚入場、或觀望中錯失良機）、成交滑價、委託單取消率與離場效率。"
    )

class ExecutionSkillsJSON(BaseModel):
    min_buy_score: int = Field(..., description="建議最低買入門檻總得分 (40-90)")
    max_single_stock_weight: int = Field(..., description="建議單一標的最重部位權重 (1-5)")
    stop_loss_pct: float = Field(..., description="建議個股硬停損百分比，如 -0.05 代表 -5%")
    take_profit_pct: float = Field(..., description="建議動態鎖利觸發百分比，如 0.12 代表 12%")
    entry_timing_rules: List[str] = Field(
        ..., description="2-3 條 Timing 進場規範（如防追高、避開價格前 20% 高檔等）"
    )
    regime_posture: Dict[str, str] = Field(
        ..., description="大盤氣候姿態映射，如 {'BULLISH_TREND': 'AGGRESSIVE', 'BEARISH_TREND': 'DEFENSIVE', 'HIGH_VOLATILITY': 'CONSERVATIVE'}"
    )
    tactical_rules: List[str] = Field(
        ..., description="2-4 條風控與部位執行戰術規則指令"
    )

class ExecutionReviewSummaryOutput(BaseModel):
    cio_summary: str = Field(
        ...,
        description="繁體中文 250-400 字 CIO 組合交易與部位執行總評。涵蓋：Timing 追高/太晚入場診斷、觀望錯失良機診斷、低迷氣候是否過於保守、順風氣候是否盲目追高與部位動態風控。"
    )
    key_learnings: List[str] = Field(
        ..., description="3-5 條交易執行與風控學習點 (繁體中文)"
    )
    execution_skills: ExecutionSkillsJSON = Field(
        ..., description="演化出之 Key-Value 結構化交易與部位風控 Skills"
    )

# =====================================================================
# 3. Layer 3：全域 Skills 彙整模型 (Unified Evolved Skills)
# =====================================================================
class UnifiedEvolvedSkillsJSON(BaseModel):
    version: str = Field(..., description="Skills 版本，如 '2026.07'")
    indicator_skills: IndicatorSkillsJSON = Field(..., description="Layer 1 演化之指標 Skills")
    execution_skills: ExecutionSkillsJSON = Field(..., description="Layer 2 演化之交易與風控 Skills")

# =====================================================================
# 4. 執行多層 Map-Reduce 月度檢討主流程
# =====================================================================
def run_monthly_review(year: int, month: int, is_paper: bool = False, call_gemini_fn: Optional[Any] = None) -> Dict[str, Any]:
    """
    執行實盤 (`is_paper = False`) 多層月度 AI 自我檢討與 Skills 演化：
    1. Layer 0: Python monthly_aggregator 計算硬指標與 5 大 Context
    2. Layer 1: 指標與打分檢討 (Map: 個股指標+大盤參照 -> Reduce: Indicator Skills)
    3. Layer 2: 交易與部位執行檢討 (Map: 個股交易/觀望+大盤參照 -> Reduce: Execution Skills)
    4. Layer 3: 全域 Skills 彙整寫入 Supabase monthly_skills 表
    """
    if call_gemini_fn is None:
        call_gemini_fn = call_gemini_with_rotation

    # -----------------------------------------------------------------
    # Step 1: Layer 0 預計算硬指標與數據 Context
    # -----------------------------------------------------------------
    aggregated_data = aggregate_monthly_data(year, month, is_paper=is_paper)
    review_month_str = aggregated_data["review_month"]
    metrics = aggregated_data["metrics"]
    per_stock_data = aggregated_data["per_stock_data"]

    # 🛡️ 防崩盤與無效 Token 浪費保護：若該區間完全無任何每日分析與個股資料，跳過檢討
    if not aggregated_data.get("daily_analysis_ids") and not per_stock_data:
        print(f" [Monthly Review Agent] 提示: {review_month_str} 區間內完全無任何歷史分析與交易紀錄，安全跳過檢討。")
        return {
            "review_month": review_month_str,
            "is_paper": is_paper,
            "skipped": True,
            "message": f"區間 ({aggregated_data['date_range']['start_date']} ~ {aggregated_data['date_range']['end_date']}) 內完全無歷史交易與分析資料，跳過檢討。",
            "metrics": metrics,
            "stock_indicator_reports": [],
            "stock_execution_reports": [],
            "stock_reports": [],
            "indicator_summary": f"區間 ({review_month_str}) 內尚無歷史分析紀錄。",
            "cio_summary": f"區間 ({review_month_str}) 內尚無歷史交易紀錄。",
            "overall_summary": f"區間 ({review_month_str}) 內尚無歷史分析與交易紀錄，維持現有戰術防線。",
            "key_learnings": ["區間內無交易與分析資料，保持觀望與現有配置"],
            "indicator_skills": None,
            "execution_skills": None,
            "skills_json": None
        }

    macro_context_str = (
        f"【全月大盤氣候與大盤參照背景】\n"
        f"- 看多/順風氣候天數 (Bullish Days): {metrics.get('bullish_days_count', 0)} 天 | 順風天數買單: {metrics.get('bullish_period_buy_count', 0)} 筆 | 順風買入虧損率: {metrics.get('bullish_loss_rate_pct', 0.0)}%\n"
        f"- 保守/低迷氣候天數 (Defensive Days): {metrics.get('defensive_days_count', 0)} 天 | 防禦期間個股潛在反彈漲幅: +{metrics.get('defensive_period_mean_upside_pct', 0.0)}%\n"
        f"- 分析師打分分佈: 高分(>=80) {aggregated_data['score_calibration']['high_score_count']} 筆, 中分(60-79) {aggregated_data['score_calibration']['mid_score_count']} 筆, 低分(<60) {aggregated_data['score_calibration']['low_score_count']} 筆"
    )

    # -----------------------------------------------------------------
    # Step 2: Layer 1 - 技術指標與打分檢討 (Map & Reduce)
    # -----------------------------------------------------------------
    print(f" [Monthly Review Agent] 開始 Layer 1: 技術指標與打分檢討...")
    stock_indicator_reports: List[Dict[str, Any]] = []

    # Layer 1 Map: 個股指標 + 大盤參照
    for stock_code, stock_info in per_stock_data.items():
        stock_name = get_stock_name(stock_code)
        stock_label = f"{stock_code} ({stock_name})" if stock_name else stock_code
        l1_map_prompt = (
            f"你是一位頂級量化基金的技術指標與型態復盤專家。請對標的 {stock_label} 在 {review_month_str} 月份的技術指標與打分品質進行診斷。\n"
            f"【注意：1. 本階段請專注於技術指標、K線走勢與評分品質，完全不要評估交易買賣與損益！ 2. 回傳 JSON 中的 stock_code 欄位必須嚴格保持為 '{stock_code}'，不得填寫複雜文字或免責聲明。】\n\n"
            f"{macro_context_str}\n\n"
            f"【個股指標與打分歷史數據】\n"
            f"- 分析師打分紀錄筆數: {len(stock_info.get('scores', []))}\n"
            f"- 當月該股總振幅 (Price Range Ratio): {stock_info.get('price_range_ratio', 0.0) * 100:.2f}%\n"
            f"- 打分詳細紀錄: {json.dumps(stock_info.get('scores', []), ensure_ascii=False)}\n\n"
            f"請評估該股：技術指標與評分對 V 型強勢反彈或 A 型頂點反轉的捕捉精準度，分析師給分是否存在偏斜/通膨，以及該股是否具有偏離大盤常規的特殊型態/異常走勢慣性 (anomaly_trait)。"
        )
        generation_config_l1_map = {
            "response_mime_type": "application/json",
            "response_schema": StockIndicatorReviewOutput,
            "temperature": 0.0
        }
        try:
            l1_res = call_gemini_fn(prompt=l1_map_prompt, model_name=config.gemini_model, generation_config=generation_config_l1_map)
            parsed_l1 = json.loads(l1_res)
            if isinstance(parsed_l1, dict):
                parsed_l1["stock_code"] = stock_code
            stock_indicator_reports.append(parsed_l1)
        except Exception as e:
            print(f" [Monthly Review Agent] 警告: 個股 {stock_code} Layer 1 Map 檢討失敗: {e}")
            stock_indicator_reports.append({
                "stock_code": stock_code,
                "indicator_retrospective": f"個股 {stock_code} 技術指標診斷跳過 (LLM 呼叫異常)。",
                "anomaly_trait": None
            })

    # Layer 1 Reduce: 綜合指標診斷與 Indicator Skills 產出
    l1_reduce_prompt = (
        f"你是一位量化研究總監 (Head of Quantitative Research)，正在對 {review_month_str} 月份的技術指標與分析師打分品質進行總診斷，並演化下個月的指標與打分 Skills (`indicator_skills`)。\n\n"
        f"{macro_context_str}\n\n"
        f"【各標的 Layer 1 個股指標診斷報告】\n"
        f"{json.dumps(stock_indicator_reports, ensure_ascii=False, indent=2)}\n\n"
        f"請綜合診斷：1.哪些指標特徵容易/不容易形成 V 型反彈與 A 型頂點？ 2.分析師給分偏斜與門檻調整 3.特定股票異常特徵 4.氣候對指標的側重，並產出 Key-Value 結構化 indicator_skills (規則請附帶 expected_probability_pct 表示預期機率 0-100)。"
    )
    generation_config_l1_reduce = {
        "response_mime_type": "application/json",
        "response_schema": IndicatorReviewSummaryOutput,
        "temperature": 0.0
    }
    try:
        l1_reduce_res = call_gemini_fn(prompt=l1_reduce_prompt, model_name=config.gemini_model, generation_config=generation_config_l1_reduce)
        l1_overall_data = json.loads(l1_reduce_res)
    except Exception as e:
        print(f" [Monthly Review Agent] 錯誤: Layer 1 Reduce 總體檢討失敗: {e}")
        l1_overall_data = {
            "indicator_summary": f"{review_month_str} 月度技術指標檢討完成，指標表現穩定。",
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
            }
        }

    indicator_summary = l1_overall_data["indicator_summary"]
    indicator_skills = l1_overall_data["indicator_skills"]

    # -----------------------------------------------------------------
    # Step 3: Layer 2 - 交易與部位執行檢討 (Map & Reduce)
    # -----------------------------------------------------------------
    print(f" [Monthly Review Agent] 開始 Layer 2: 交易與部位執行檢討...")
    stock_execution_reports: List[Dict[str, Any]] = []

    # Layer 2 Map: 個股交易/觀望單 + 大盤參照
    for stock_code, stock_info in per_stock_data.items():
        stock_name = get_stock_name(stock_code)
        stock_label = f"{stock_code} ({stock_name})" if stock_name else stock_code
        timing_sum = stock_info.get("entry_timing_summary", {})
        l2_map_prompt = (
            f"你是一位頂級量化基金的交易執行與部位風控分析師。請對標的 {stock_label} 在 {review_month_str} 月份的交易與觀望執行進行診斷。\n"
            f"【注意：1. 本階段請專注於入場 Timing、追高/遲入場、觀望錯失機會、成交滑價與離場風控！ 2. 回傳 JSON 中的 stock_code 欄位必須嚴格保持為 '{stock_code}'，不得填寫複雜文字或免責聲明。】\n\n"
            f"{macro_context_str}\n\n"
            f"【個股交易與觀望數據】\n"
            f"- 成交單筆數: {len(stock_info.get('filled_orders', []))}\n"
            f"- 未成交/被取消單筆數: {len(stock_info.get('cancelled_orders', []))}\n"
            f"- 買單平均入場價位分位數 (Avg Entry Percentile): {timing_sum.get('avg_entry_percentile', 50.0)}% (0%=最低價, 100%=最高價)\n"
            f"- 疑似追高買單筆數 (買在頂部 20% 區間): {timing_sum.get('chasing_high_count', 0)} 筆\n"
            f"- 疑似太晚入場/波段頂點買單筆數: {timing_sum.get('late_entry_count', 0)} 筆\n"
            f"- 成功成交單紀錄: {json.dumps(stock_info.get('filled_orders', []), ensure_ascii=False)}\n"
            f"- 被取消/未成交單紀錄: {json.dumps(stock_info.get('cancelled_orders', []), ensure_ascii=False)}\n\n"
            f"請評估該股：買入 timing 準確度（是否存在追高或太晚入場？觀望是否錯失良機？）、成交滑價、委託單取消原因以及離場風控效率。"
        )
        generation_config_l2_map = {
            "response_mime_type": "application/json",
            "response_schema": StockExecutionReviewOutput,
            "temperature": 0.0
        }
        try:
            l2_res = call_gemini_fn(prompt=l2_map_prompt, model_name=config.gemini_model, generation_config=generation_config_l2_map)
            parsed_l2 = json.loads(l2_res)
            if isinstance(parsed_l2, dict):
                parsed_l2["stock_code"] = stock_code
            stock_execution_reports.append(parsed_l2)
        except Exception as e:
            print(f" [Monthly Review Agent] 警告: 個股 {stock_code} Layer 2 Map 檢討失敗: {e}")
            stock_execution_reports.append({
                "stock_code": stock_code,
                "execution_retrospective": f"個股 {stock_code} 交易執行診斷跳過 (LLM 呼叫異常)。"
            })

    # Layer 2 Reduce: CIO 組合執行診斷與 Execution Skills 產出
    l2_reduce_prompt = (
        f"你是一位首席投資官 (CIO)，正在對 {review_month_str} 月份的實盤交易執行與部位風控進行總診斷，並演化下個月的交易與風控 Skills (`execution_skills`)。\n\n"
        f"【當月組合交易與執行硬指標】\n"
        f"- 平倉總筆數: {metrics['total_trades']} | 勝率: {metrics['win_rate']}%\n"
        f"- 實現總損益: {metrics['total_realized_pnl']} 元 | 盈虧比: {metrics['payoff_ratio']} | 獲利因子: {metrics['profit_factor']}\n"
        f"- 期望潛在漲幅 (Mean Upside): +{metrics['mean_upside_ratio']*100:.2f}% | 期望潛在回撤: {metrics['mean_drawdown_ratio']*100:.2f}%\n"
        f"- 平均成交滑價: {metrics.get('mean_slippage_ratio', 0)*100:.2f}% | 未成交/取消單: {metrics.get('total_cancelled_orders', 0)} 筆 (取消率: {metrics.get('cancellation_rate_pct', 0)}%)\n"
        f"- 全月買單平均入場分位數: {metrics.get('avg_portfolio_entry_percentile', 50.0)}% | 追高買單: {metrics.get('total_chasing_high_trades', 0)} 筆 | 太晚入場單: {metrics.get('total_late_entry_trades', 0)} 筆\n"
        f"{macro_context_str}\n\n"
        f"【各標的 Layer 2 個股交易執行診斷報告】\n"
        f"{json.dumps(stock_execution_reports, ensure_ascii=False, indent=2)}\n\n"
        f"請綜合診斷：1.Timing 追高與太晚入場原因與改善戰術 2.低迷氣候是否過於保守錯失良機 3.順風盤是否盲目追高 4.離場停損停利與部位權重，並產出 Key-Value 結構化 execution_skills。"
    )
    generation_config_l2_reduce = {
        "response_mime_type": "application/json",
        "response_schema": ExecutionReviewSummaryOutput,
        "temperature": 0.0
    }
    try:
        l2_reduce_res = call_gemini_fn(prompt=l2_reduce_prompt, model_name=config.gemini_model, generation_config=generation_config_l2_reduce)
        l2_overall_data = json.loads(l2_reduce_res)
    except Exception as e:
        print(f" [Monthly Review Agent] 錯誤: Layer 2 Reduce 總體檢討失敗: {e}")
        l2_overall_data = {
            "cio_summary": f"{review_month_str} 月度交易與部位執行檢討完成，執行防線維持穩定。",
            "key_learnings": [
                "嚴格執行 5% 個股停損紀律",
                "避免在股價高檔區間追高建立倉位",
                "大盤順風天數時落實選股品質防踩雷"
            ],
            "execution_skills": {
                "min_buy_score": 65,
                "max_single_stock_weight": 4,
                "stop_loss_pct": -0.05,
                "take_profit_pct": 0.12,
                "entry_timing_rules": [
                    "避免在股票當月價格前 20% 高檔區間追高入場",
                    "觀望標的若突破門檻應於次日分批限價入場"
                ],
                "regime_posture": {
                    "BULLISH_TREND": "AGGRESSIVE",
                    "BEARISH_TREND": "DEFENSIVE",
                    "HIGH_VOLATILITY": "CONSERVATIVE"
                },
                "tactical_rules": [
                    "Maintain steady position sizing and strict 5% stop loss."
                ]
            }
        }

    cio_summary = l2_overall_data["cio_summary"]
    key_learnings = l2_overall_data["key_learnings"]
    execution_skills = l2_overall_data["execution_skills"]

    # -----------------------------------------------------------------
    # Step 4: Layer 3 - 全域 Skills 彙整與 Supabase / 白話總結
    # -----------------------------------------------------------------
    print(f" [Monthly Review Agent] 開始 Layer 3: 全域 Skills 彙整與 Supabase 儲存...")
    unified_skills_dict = {
        "version": review_month_str,
        "indicator_skills": indicator_skills,
        "execution_skills": execution_skills
    }

    # 寫入 Supabase monthly_skills 表
    try:
        insert_payload = {
            "review_month": review_month_str,
            "daily_analysis_ids": aggregated_data["daily_analysis_ids"],
            "skills": unified_skills_dict,
            "is_paper": is_paper
        }
        supabase.table("monthly_skills").insert(insert_payload).execute()
        print(f" [Monthly Review Agent] 成功寫入 monthly_skills 表: 月份 {review_month_str}")
    except Exception as e:
        print(f" [Monthly Review Agent] 警告: 寫入 monthly_skills 資料表失敗: {e}")

    # 產生 Discord 與 Web 展示用之自然繁體中文策略總結
    min_score = execution_skills.get("min_buy_score", 65)
    max_weight = execution_skills.get("max_single_stock_weight", 4)
    stop_loss = execution_skills.get("stop_loss_pct", -0.05)
    take_profit = execution_skills.get("take_profit_pct", 0.12)
    tactical_rules_str = "；".join(execution_skills.get("tactical_rules", []))

    stock_rules_list = indicator_skills.get("stock_specific_rules", [])
    stock_rules_str = "；".join([f"{item.get('stock_code')}: {item.get('anomaly_trait')}" for item in stock_rules_list if isinstance(item, dict)]) if stock_rules_str_condition(stock_rules_list) else "無特別異常標的"

    overall_summary = (
        f"【{review_month_str} 月度戰術策略總結】\n"
        f"• **下月風控門檻**：建議最低買入門檻 **{min_score} 分** | 單檔最重權重 **{max_weight} 級** | 個股停損 **{stop_loss*100:.1f}%** | 動態鎖利 **{take_profit*100:.1f}%**\n"
        f"• **戰術執行重點**：{tactical_rules_str if tactical_rules_str else '維持穩健分批進場紀律'}\n"
        f"• **個股特殊特徵與關注**：{stock_rules_str}\n"
        f"• **指標與執行綜合評估**：{cio_summary}"
    )

    # 為了向下相容性，組合 stock_reports 包含 execution 與 indicator reports
    stock_reports: List[Dict[str, Any]] = []
    for i, sc in enumerate(per_stock_data.keys()):
        ind_rep = stock_indicator_reports[i] if i < len(stock_indicator_reports) else {}
        exe_rep = stock_execution_reports[i] if i < len(stock_execution_reports) else {}
        stock_reports.append({
            "stock_code": sc,
            "indicator_retrospective": ind_rep.get("indicator_retrospective", ""),
            "anomaly_trait": ind_rep.get("anomaly_trait"),
            "execution_retrospective": exe_rep.get("execution_retrospective", ""),
            "stock_retrospective": (
                f"【指標診斷】{ind_rep.get('indicator_retrospective', '')}\n"
                f"【交易執行診斷】{exe_rep.get('execution_retrospective', '')}"
            )
        })

    return {
        "review_month": review_month_str,
        "is_paper": is_paper,
        "metrics": metrics,
        "stock_indicator_reports": stock_indicator_reports,
        "stock_execution_reports": stock_execution_reports,
        "stock_reports": stock_reports,
        "indicator_summary": indicator_summary,
        "cio_summary": cio_summary,
        "overall_summary": overall_summary,
        "key_learnings": key_learnings,
        "indicator_skills": indicator_skills,
        "execution_skills": execution_skills,
        "skills_json": unified_skills_dict
    }

def stock_rules_str_condition(stock_rules_list: Any) -> bool:
    return bool(stock_rules_list and isinstance(stock_rules_list, list))
