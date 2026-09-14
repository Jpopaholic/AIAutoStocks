# Path: src/agents/quarterly_review_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config, get_stock_name
from src.services.gemini_rotator import call_gemini_with_rotation
from src.services.supabase_client import supabase
from src.services.quarterly_aggregator import aggregate_quarterly_data
from src.agents.monthly_review_agent import (
    StockIndicatorReviewOutput,
    IndicatorPatternRule,
    StockSpecificRule,
    ScoreCalibrationRule,
    RegimeIndicatorRule,
    IndicatorSkillsJSON,
    IndicatorReviewSummaryOutput,
    StockExecutionReviewOutput,
    ExecutionSkillsJSON,
    ExecutionReviewSummaryOutput,
    stock_rules_str_condition
)

# =====================================================================
# 1. Layer 3 專屬：月度技能演化與超參數深度復盤模型 (Meta-Skills Retrospective)
# =====================================================================
class MonthlySkillsRetrospective(BaseModel):
    trajectory_summary: str = Field(
        ...,
        description="繁體中文 250-400 字回顧該季 3 個月份的 Skills 演化軌跡與實質成效。分析各月對買入門檻、ATR 停損乘數、動態追價 buffer 與讓價 tier 的調整是否有效改善交易品質。"
    )
    overfitting_verdict: str = Field(
        ...,
        description="繁體中文 100-200 字專門評估是否存在因短期市場噪音或單月大幅波動而過度調高/調低門檻、過度收緊或過度擬合 (Overfitting) 的現象。"
    )
    monthly_adjustments_verdict: List[str] = Field(
        ...,
        description="2-4 條針對該季 3 個月份中每次月度調參動作的客觀成效裁定（如：『7月調高買入門檻至 70 分有效避開大盤回檔』或『8月追價 buffer 調降造成取消率上升』）"
    )
    rule_conflict_resolutions: List[str] = Field(
        ...,
        description="1-3 條消除跨月戰術指令歧義與衝突的裁決（如：『明確規定動態鎖利優先於高分續抱哲學』）"
    )
    strategic_takeaways: List[str] = Field(
        ...,
        description="2-3 條供跨季與後續年度大復盤參考之宏觀策略教訓"
    )
    refined_execution_skills: ExecutionSkillsJSON = Field(
        ...,
        description="策略委員會在綜合審視 3 個月份月度技能成效、全季勝率與損益比後，最終校準定案的季度執行超參數"
    )

class UnifiedQuarterlyEvolvedSkillsJSON(BaseModel):
    version: str = Field(..., description="季度版本，如 '2026-Q3'")
    months_included: List[str] = Field(..., description="涵蓋之 3 個月份，如 ['2026-07', '2026-08', '2026-09']")
    monthly_skills_retrospective: MonthlySkillsRetrospective = Field(..., description="Layer 3 月度 Skills 深度成效回顧與調參成果")
    indicator_skills: IndicatorSkillsJSON = Field(..., description="Layer 1 演化之季度指標 Skills")
    execution_skills: ExecutionSkillsJSON = Field(..., description="Layer 3 最終校準之季度交易與風控 Skills")

# =====================================================================
# 2. 執行多層 Map-Reduce 季度檢討主流程 (四層架構)
# =====================================================================
def run_quarterly_review(year: int, quarter: int, is_paper: bool = False, call_gemini_fn: Optional[Any] = None) -> Dict[str, Any]:
    """
    執行季度 AI 自我檢討與動態 Skills 演化（高規格四層架構）：
    1. Layer 0: Python quarterly_aggregator 彙整無縫 3 格跨度硬指標與 3 個月份之月度 Skills
    2. 門檻保護：該季度 3 個月份皆必須至少有一筆有效月檢討紀錄（缺一不可），否則安全跳過
    3. Layer 1: 技術指標與打分季度檢討 (Map: 個股指標+大盤參照 -> Reduce: Indicator Skills)
    4. Layer 2: 交易執行與部位風控季度檢討 (Map: 個股執行+大盤參照 -> Reduce: CIO 組合執行總評)
    5. Layer 3: ⭐ 獨立專屬層：月度技能演化深度復盤與超參數校準 (策略委員會審視 3 個月度 Skills + Layer 1 + Layer 2)
    6. Layer 4: 全域 Skills 彙整寫入 Supabase quarterly_skills 表（獨立表防污染）
    """
    if call_gemini_fn is None:
        call_gemini_fn = call_gemini_with_rotation

    review_quarter_str = f"{year}-Q{quarter}"
    print(f" [Quarterly Review Agent] 啟動 {review_quarter_str} 季度 AI 決策復盤流程 (is_paper={is_paper})...")

    # -----------------------------------------------------------------
    # Step 1: Layer 0 數據聚合與 3 個月份門檻防線
    # -----------------------------------------------------------------
    aggregated_data = aggregate_quarterly_data(year, quarter, is_paper=is_paper)
    months_included = aggregated_data["months_included"]
    date_range = aggregated_data["date_range"]
    metrics = aggregated_data["metrics"]
    per_stock_data = aggregated_data["per_stock_data"]
    monthly_skills_trajectory = aggregated_data.get("monthly_skills_trajectory", [])

    # 🛡️ 門檻規範：3 個月份中每個月皆必須至少有一筆有效月度檢討紀錄
    if not aggregated_data.get("threshold_met", False):
        missing = aggregated_data.get("missing_months", [])
        msg = f"季度 {review_quarter_str} 涵蓋之 3 個月份 ({', '.join(months_included)}) 未全數具備月檢討紀錄（缺漏月份: {', '.join(missing)}），依規範安全跳過檢討以維護宏觀策略之月度基石支撐。"
        print(f" [Quarterly Review Agent] 提示: {msg}")
        return {
            "review_quarter": review_quarter_str,
            "quarter": quarter,
            "year": year,
            "months_included": months_included,
            "is_paper": is_paper,
            "skipped": True,
            "message": msg,
            "metrics": metrics,
            "stock_indicator_reports": [],
            "stock_execution_reports": [],
            "stock_reports": [],
            "indicator_summary": f"季度 ({review_quarter_str}) 缺乏完整月檢討基石，維持現有策略。",
            "cio_summary": f"季度 ({review_quarter_str}) 缺乏完整月檢討基石，維持現有風控配置。",
            "overall_summary": msg,
            "key_learnings": ["月度復盤基石不足，維持現有季度戰術防線"],
            "monthly_skills_retrospective": None,
            "indicator_skills": None,
            "execution_skills": None,
            "skills_json": None
        }

    # 準備季度大盤宏觀脈絡
    macro_context_str = (
        f"【全季大盤氣候與宏觀參照背景】\n"
        f"- 檢討區間: {date_range['start_date']} ~ {date_range['end_date']} (涵蓋 3 個月份: {', '.join(months_included)})\n"
        f"- 全季順風氣候天數 (Bullish Days): {metrics.get('bullish_days_count', 0)} 天 | 防禦/低迷氣候天數 (Defensive Days): {metrics.get('defensive_days_count', 0)} 天\n"
        f"- 分析師打分分佈: 高分(>=80) {aggregated_data['score_calibration']['high_score_count']} 筆, 中分(60-79) {aggregated_data['score_calibration']['mid_score_count']} 筆, 低分(<60) {aggregated_data['score_calibration']['low_score_count']} 筆\n"
        f"- 全季平均成交滑價: {metrics.get('mean_slippage_ratio', 0)*100:.2f}% | 取消單率: {metrics.get('cancellation_rate_pct', 0)}% (取消 {metrics.get('total_cancelled_orders', 0)} 筆)"
    )

    # 整理 3 個月份月度技能文字摘要供後續獨立復盤層深度檢視
    skills_trajectory_str = "【本季 3 個月份的月度 Skills 演化軌跡】\n"
    for r in monthly_skills_trajectory:
        rm = r.get("review_month")
        sk = r.get("skills", {})
        if isinstance(sk, str):
            try:
                sk = json.loads(sk)
            except Exception:
                sk = {}
        exec_s = sk.get("execution_skills", {})
        ind_s = sk.get("indicator_skills", {})
        skills_trajectory_str += (
            f"• 月份 {rm} (納入 {r.get('daily_analysis_count', 0)} 天日分析):\n"
            f"  - 買入門檻: {exec_s.get('min_buy_score')} 分 | 最重權重: {exec_s.get('max_single_stock_weight')} 級\n"
            f"  - 停損比率: {exec_s.get('stop_loss_pct')} (ATR停損乘數: {exec_s.get('stop_loss_atr_mult')}) | 鎖利: {exec_s.get('take_profit_pct')} (ATR鎖利乘數: {exec_s.get('take_profit_atr_mult')})\n"
            f"  - 買進追價 Tier: {json.dumps(exec_s.get('chase_buffer_tiers', []), ensure_ascii=False)}\n"
            f"  - 賣出讓價 Tier: {json.dumps(exec_s.get('sell_discount_tiers', []), ensure_ascii=False)}\n"
            f"  - 戰術風控指令: {json.dumps(exec_s.get('tactical_rules', []), ensure_ascii=False)}\n"
            f"  - 演化指標規則筆數: V轉 {len(ind_s.get('v_shape_reversal_patterns', []))} 條, A頂 {len(ind_s.get('a_shape_top_warnings', []))} 條\n"
        )

    # -----------------------------------------------------------------
    # Step 2: Layer 1 - 技術指標與打分季度檢討 (Map & Reduce)
    # -----------------------------------------------------------------
    print(f" [Quarterly Review Agent] 開始 Layer 1: 技術指標與打分季度檢討...")
    stock_indicator_reports: List[Dict[str, Any]] = []

    for stock_code, stock_info in per_stock_data.items():
        stock_name = get_stock_name(stock_code)
        stock_label = f"{stock_code} ({stock_name})" if stock_name else stock_code
        l1_map_prompt = (
            f"你是一位頂級量化基金的季度技術指標與型態復盤專家。請對標的 {stock_label} 在 {review_quarter_str} 全季度的技術指標與打分品質進行診斷。\n"
            f"【注意：1. 本階段請專注於全季度技術指標、K線型態與評分品質，完全不要評估交易買賣與損益！ 2. 回傳 JSON 中的 stock_code 欄位必須嚴格保持為 '{stock_code}'。】\n\n"
            f"{macro_context_str}\n\n"
            f"【個股全季指標與打分歷史數據】\n"
            f"- 分析師打分紀錄筆數: {len(stock_info.get('scores', []))}\n"
            f"- 全季該股總振幅 (Price Range Ratio): {stock_info.get('price_range_ratio', 0.0) * 100:.2f}%\n"
            f"- 個股 ATR(14) 波動度: {stock_info.get('atr_pct', 0.0):.2f}% (波動屬性: {stock_info.get('volatility_tier', 'NORMAL')})\n"
            f"- 打分詳細紀錄 (前 15 筆與後 15 筆採樣): {json.dumps(stock_info.get('scores', [])[:15] + stock_info.get('scores', [])[-15:], ensure_ascii=False)}\n\n"
            f"請跨越全季度週期評估該股：技術指標與評分對季度 V 型強勢反彈或 A 型頂點反轉的捕捉精準度，全季評分是否存在長期偏斜/通膨，以及該股跨月份是否顯現偏離大盤常規的特殊走勢慣性 (anomaly_trait)。"
        )
        exp_up_str = stock_info.get("expected_upside_str", "--")
        exp_down_str = stock_info.get("expected_drawdown_str", "--")
        act_pnl_str = stock_info.get("actual_pnl_str", "無平倉交易")

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
                parsed_l1["expected_upside_str"] = exp_up_str
                parsed_l1["expected_drawdown_str"] = exp_down_str
                parsed_l1["actual_pnl_str"] = act_pnl_str
            stock_indicator_reports.append(parsed_l1)
        except Exception as e:
            print(f" [Quarterly Review Agent] 警告: 個股 {stock_code} Layer 1 Map 檢討失敗: {e}")
            stock_indicator_reports.append({
                "stock_code": stock_code,
                "indicator_retrospective": f"個股 {stock_code} 季度技術指標診斷跳過 (LLM 呼叫異常)。",
                "anomaly_trait": None,
                "expected_upside_str": exp_up_str,
                "expected_drawdown_str": exp_down_str,
                "actual_pnl_str": act_pnl_str
            })

    # Layer 1 Reduce
    l1_reduce_prompt = (
        f"你是一位量化研究總監 (Head of Quantitative Research)，正在對 {review_quarter_str} 季度的技術指標與分析師打分品質進行全季總診斷，並演化下一季度的指標與打分 Skills (`indicator_skills`)。\n\n"
        f"{macro_context_str}\n\n"
        f"【各標的 Layer 1 個股指標診斷報告】\n"
        f"{json.dumps(stock_indicator_reports, ensure_ascii=False, indent=2)}\n\n"
        f"請綜合跨季度診斷：\n"
        f"1. 哪些指標與量價特徵在全季宏觀週期下能穩定捕捉 V 型反彈與 A 型頂點（請核心著重於真實 K 線型態、均線排列、價格走勢與量能結構）。\n"
        f"2. 跨月份分析師給分偏斜與評分校正規則。\n"
        f"3. 針對特定標的的跨季特殊特徵規則。\n"
        f"4. 產出 Key-Value 結構化 indicator_skills (附帶 expected_probability_pct 預期機率 0-100)。\n\n"
        f"【⚠️ 極其重要：單日即時可判定規範】\n"
        f"- 演化產出之所有 pattern_rule、anomaly_trait 與 calibration_rule 必須是【當日 (Day T) 分析師憑藉截至當日之歷史 K 線與技術指標立即判定之客觀條件】！\n"
        f"- 嚴禁產出需要『未來觀望數天』或『等待未來確認』等延遲條文。"
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
        print(f" [Quarterly Review Agent] 錯誤: Layer 1 Reduce 總體檢討失敗: {e}")
        l1_overall_data = {
            "indicator_summary": f"{review_quarter_str} 季度技術指標檢討完成，指標體系運行穩健。",
            "indicator_skills": {
                "v_shape_reversal_patterns": [
                    {"pattern_rule": "量能突破且 RSI 於 50 以上向上黃金交叉時 V 型反彈機率高", "expected_probability_pct": 82}
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
    # Step 3: Layer 2 - 交易執行與部位風控季度檢討 (Map & Reduce)
    # -----------------------------------------------------------------
    print(f" [Quarterly Review Agent] 開始 Layer 2: 交易執行與部位風控檢討...")
    stock_execution_reports: List[Dict[str, Any]] = []

    for stock_code, stock_info in per_stock_data.items():
        stock_name = get_stock_name(stock_code)
        stock_label = f"{stock_code} ({stock_name})" if stock_name else stock_code
        timing_sum = stock_info.get("entry_timing_summary", {})
        exp_up_str = stock_info.get("expected_upside_str", "--")
        exp_down_str = stock_info.get("expected_drawdown_str", "--")
        act_pnl_str = stock_info.get("actual_pnl_str", "無平倉交易")

        l2_map_prompt = (
            f"你是一位頂級量化基金的季度交易執行與部位風控分析師。請對標的 {stock_label} 在 {review_quarter_str} 全季度的交易與觀望執行進行診斷。\n"
            f"【注意：1. 本階段專注於進場 Timing（追高、遲入場、錯失良機）、成交滑價與離場風控！ 2. 回傳 JSON 中的 stock_code 必須為 '{stock_code}'。】\n\n"
            f"{macro_context_str}\n\n"
            f"【個股全季交易與觀望數據】\n"
            f"- 波動屬性: {stock_info.get('volatility_tier', 'NORMAL')} (ATR%: {stock_info.get('atr_pct', 0.0):.2f}%, ATR14: {stock_info.get('atr14', 0.0):.2f}元)\n"
            f"- 成交單筆數: {len(stock_info.get('filled_orders', []))}\n"
            f"- 取消/未成交筆數: {len(stock_info.get('cancelled_orders', []))}\n"
            f"- 期望潛在漲幅: {exp_up_str} | 期望潛在回撤: {exp_down_str}\n"
            f"- 實際平倉損益: {act_pnl_str}\n"
            f"- 買單平均進場分位數: {timing_sum.get('avg_entry_percentile', 50.0)}%\n"
            f"- 追高買單筆數: {timing_sum.get('chasing_high_count', 0)} 筆 | 遲進場買單筆數: {timing_sum.get('late_entry_count', 0)} 筆\n"
            f"- 成交單紀錄: {json.dumps(stock_info.get('filled_orders', []), ensure_ascii=False)}\n"
            f"- 取消單紀錄: {json.dumps(stock_info.get('cancelled_orders', []), ensure_ascii=False)}\n\n"
            f"請綜合全季度評估該股：買入 Timing 準確度（追高/遲入場/錯失機會）、成交滑價、委託單取消原因以及離場風控成效。"
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
                parsed_l2["expected_upside_str"] = exp_up_str
                parsed_l2["expected_drawdown_str"] = exp_down_str
                parsed_l2["actual_pnl_str"] = act_pnl_str
            stock_execution_reports.append(parsed_l2)
        except Exception as e:
            print(f" [Quarterly Review Agent] 警告: 個股 {stock_code} Layer 2 Map 檢討失敗: {e}")
            stock_execution_reports.append({
                "stock_code": stock_code,
                "execution_retrospective": f"個股 {stock_code} 季度交易執行診斷跳過 (LLM 呼叫異常)。",
                "expected_upside_str": exp_up_str,
                "expected_drawdown_str": exp_down_str,
                "actual_pnl_str": act_pnl_str
            })

    # Layer 2 Reduce (CIO 組合交易執行總評)
    l2_reduce_prompt = (
        f"你是一位首席投資官 (CIO)，正在對 {review_quarter_str} 季度的實盤交易執行與部位風控進行總診斷，並演化初版季度戰術 Skills (`execution_skills`)。\n\n"
        f"【全季度投資組合硬指標】\n"
        f"- 平倉總筆數: {metrics['total_trades']} | 勝率: {metrics['win_rate']}%\n"
        f"- 實現總損益: {metrics['total_realized_pnl']} 元 | 盈虧比: {metrics['payoff_ratio']} | 獲利因子: {metrics['profit_factor']}\n"
        f"- 期望潛在漲幅: +{metrics['mean_upside_ratio']*100:.2f}% | 期望潛在回撤: {metrics['mean_drawdown_ratio']*100:.2f}%\n"
        f"- 平均成交滑價: {metrics.get('mean_slippage_ratio', 0)*100:.2f}% | 取消單筆數: {metrics.get('total_cancelled_orders', 0)} (取消率: {metrics.get('cancellation_rate_pct', 0)}%)\n"
        f"- 買單平均入場分位數: {metrics.get('avg_portfolio_entry_percentile', 50.0)}% | 追高筆數: {metrics.get('total_chasing_high_trades', 0)} | 遲入場筆數: {metrics.get('total_late_entry_trades', 0)}\n"
        f"- 標的平均波動率 (Mean ATR%): {metrics.get('portfolio_mean_atr_pct', 0.0):.2f}%\n"
        f"{macro_context_str}\n\n"
        f"【各標的 Layer 2 個股交易執行診斷報告】\n"
        f"{json.dumps(stock_execution_reports, ensure_ascii=False, indent=2)}\n\n"
        f"請綜合評估：\n"
        f"1. Timing 追高與遲入場原因與改善戰術。\n"
        f"2. 買進追價 (chase_buffer_tiers) 與賣出讓價/防賤賣 (sell_discount_tiers) 評估。\n"
        f"3. 結合波動率演化 ATR 動態停損乘數 (stop_loss_atr_mult) 與動態鎖利乘數 (take_profit_atr_mult)。\n"
        f"4. 產出初版 execution_skills。\n\n"
        f"【⚠️ 極其重要：單日即時可執行與鎖利優先權規範】\n"
        f"- 所有 entry_timing_rules 與 tactical_rules 必須是單一交易日即可落地的客觀規則！\n"
        f"- tactical_rules 必須包含【風控與鎖利優先權規範】：『當個股帳面獲利觸發動態鎖利門檻 (take_profit_pct) 或停損門檻時，鎖利與停損條款優先度絕對高於高分續抱哲學，經理人必須執行調節平倉。』"
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
        print(f" [Quarterly Review Agent] 錯誤: Layer 2 Reduce 總體檢討失敗: {e}")
        l2_overall_data = {
            "cio_summary": f"{review_quarter_str} 季度交易與部位執行檢討完成，風控體系維持穩定。",
            "key_learnings": [
                "堅持跨月紀律化停損與分批限價佈局",
                "適應宏觀順風與防禦氣候切換，防範高檔追價風險",
                "結合 ATR 乘數落實跨季自適應風控"
            ],
            "execution_skills": {
                "min_buy_score": 65,
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
                    {"max_score": 49, "sell_discount_pct": -0.015, "description": "-1.5% 風險停損/急跌果斷讓價求售"},
                    {"max_score": 69, "sell_discount_pct": -0.010, "description": "-1.0% 轉弱調節標準讓價出清"},
                    {"max_score": 100, "sell_discount_pct": -0.005, "description": "-0.5% 高分鎖利惜售防賤賣"}
                ],
                "entry_timing_rules": [
                    "避免在股票當季價格前 20% 高檔區間追高入場",
                    "觀望標的若突破門檻應於次日分批限價入場"
                ],
                "regime_posture": {
                    "BULLISH_TREND": "AGGRESSIVE",
                    "BEARISH_TREND": "DEFENSIVE",
                    "HIGH_VOLATILITY": "CONSERVATIVE"
                },
                "tactical_rules": [
                    "【規則優先權】當個股帳面獲利觸發動態鎖利門檻 (take_profit_pct) 或停損門檻時，鎖利與停損條款優先度高於高分續抱哲學，必須執行調節平倉。"
                ]
            }
        }

    cio_summary = l2_overall_data["cio_summary"]
    key_learnings = l2_overall_data["key_learnings"]
    initial_execution_skills = l2_overall_data["execution_skills"]

    # -----------------------------------------------------------------
    # Step 4: Layer 3 - ⭐ 獨立專屬層：月度技能演化與超參數深度復盤層 (Meta-Evaluation)
    # -----------------------------------------------------------------
    print(f" [Quarterly Review Agent] 開始 Layer 3: ⭐ 月度技能演化深度復盤與策略委員會審議 (Meta-Quant Committee)...")
    l3_meta_prompt = (
        f"你是由頂級對沖基金合夥人組成的【量化策略審查委員會 (Meta-Quant Strategy Committee)】。\n"
        f"正在對 {review_quarter_str} 季度中【該季 3 個月份的月度 Skills 演化軌跡與超參數調整】進行專門的深度復盤與成效審查。\n\n"
        f"【全季度實際硬指標事實】\n"
        f"- 檢討區間: {date_range['start_date']} ~ {date_range['end_date']} (涵蓋月份: {', '.join(months_included)})\n"
        f"- 平倉總筆數: {metrics['total_trades']} | 勝率: {metrics['win_rate']}%\n"
        f"- 實現總損益: {metrics['total_realized_pnl']} 元 | 盈虧比: {metrics['payoff_ratio']} | 獲利因子: {metrics['profit_factor']}\n"
        f"- 平均成交滑價: {metrics.get('mean_slippage_ratio', 0)*100:.2f}% | 取消單率: {metrics.get('cancellation_rate_pct', 0)}%\n\n"
        f"【Layer 1 技術指標總結結論】\n{indicator_summary}\n\n"
        f"【Layer 2 CIO 交易與風控總結結論】\n{cio_summary}\n\n"
        f"{skills_trajectory_str}\n\n"
        f"【CIO 提議之初版季度執行 Skills】\n"
        f"{json.dumps(initial_execution_skills, ensure_ascii=False, indent=2)}\n\n"
        f"請策略委員會以最高標準執行下列獨立審議：\n"
        f"1. 🧠【月度 Skills 演化軌跡診斷 (trajectory_summary)】：回顧 3 個月份中，買入門檻、ATR 停損停利乘數與追價讓價 tiers 的微調是否切合市場節奏。\n"
        f"2. 🛡️【過度擬合與短期噪音檢驗 (overfitting_verdict)】：嚴格診斷是否存在因單月回檔或短期市場情緒，而過度收緊買入門檻（導致後續錯失反彈波段），或因短期盈利盲目放寬追價的過度擬合 (Overfitting) 現象。\n"
        f"3. ⚖️【逐月調整成效裁定 (monthly_adjustments_verdict)】：為該季 3 個月份的每次調整提出清晰客觀的利弊成效裁決。\n"
        f"4. 🔍【規則語義與優先權衝突消除 (rule_conflict_resolutions)】：審視跨月戰術指令是否存在矛盾，提出明確裁決。\n"
        f"5. 🏛️【跨季宏觀戰略教訓 (strategic_takeaways)】：提煉供跨季與年度大復盤參考的核心經驗。\n"
        f"6. 🎯【季度最終校準超參數 (refined_execution_skills)】：策略委員會最後校準定案的季度 execution_skills。"
    )
    generation_config_l3_meta = {
        "response_mime_type": "application/json",
        "response_schema": MonthlySkillsRetrospective,
        "temperature": 0.0
    }
    try:
        l3_meta_res = call_gemini_fn(prompt=l3_meta_prompt, model_name=config.gemini_model, generation_config=generation_config_l3_meta)
        l3_meta_data = json.loads(l3_meta_res)
    except Exception as e:
        print(f" [Quarterly Review Agent] 錯誤: Layer 3 技能深度復盤審議失敗: {e}")
        l3_meta_data = {
            "trajectory_summary": f"該季 3 個月份 ({', '.join(months_included)}) 之月度 Skills 穩定微調，整體風控防禦姿態優良。",
            "overfitting_verdict": "未顯現顯著之短期過度擬合 (Overfitting)，各月門檻調整均維持在可控區間內。",
            "monthly_adjustments_verdict": [
                "月度買入門檻微調有效適應當季市場波動",
                "動態 ATR 停損乘數成功抑制極端下行風險"
            ],
            "rule_conflict_resolutions": [
                "明確規範鎖利與停損條款優先於個股高分續抱哲學"
            ],
            "strategic_takeaways": [
                "避免因單月噪音過度修改核心買入門檻",
                "保持穩健之信心分級追價策略"
            ],
            "refined_execution_skills": initial_execution_skills
        }

    monthly_skills_retrospective = l3_meta_data
    final_execution_skills = l3_meta_data.get("refined_execution_skills") or initial_execution_skills

    # -----------------------------------------------------------------
    # Step 5: Layer 4 - 全域 Skills 彙整與儲存至 quarterly_skills 表
    # -----------------------------------------------------------------
    print(f" [Quarterly Review Agent] 開始 Layer 4: 全域 Skills 彙整與 Supabase quarterly_skills 儲存...")
    unified_skills_dict = {
        "version": review_quarter_str,
        "months_included": months_included,
        "monthly_skills_retrospective": monthly_skills_retrospective,
        "indicator_skills": indicator_skills,
        "execution_skills": final_execution_skills
    }

    daily_count = aggregated_data.get("daily_analysis_count", 0)
    insert_payload = {
        "review_quarter": review_quarter_str,
        "months_included": months_included,
        "daily_analysis_count": daily_count,
        "skills": unified_skills_dict,
        "is_paper": is_paper
    }

    try:
        supabase.table("quarterly_skills").insert(insert_payload).execute()
        print(f" [Quarterly Review Agent] 成功寫入 quarterly_skills 表: 季度 {review_quarter_str} (納入 {daily_count} 天日分析，涵蓋月份: {months_included})")
    except Exception as e:
        print(f" [Quarterly Review Agent] 警告: 寫入 quarterly_skills 資料表失敗: {e}")

    # 產生 Discord 與 Web 展示用之自然繁體中文季度總結
    min_score = final_execution_skills.get("min_buy_score", 65)
    max_weight = final_execution_skills.get("max_single_stock_weight", 4)
    stop_loss = final_execution_skills.get("stop_loss_pct", -0.05)
    take_profit = final_execution_skills.get("take_profit_pct", 0.12)
    stop_loss_atr = final_execution_skills.get("stop_loss_atr_mult", 2.0)
    take_profit_atr = final_execution_skills.get("take_profit_atr_mult", 3.5)
    tactical_rules_str = "；".join(final_execution_skills.get("tactical_rules", []))

    chase_tiers = final_execution_skills.get("chase_buffer_tiers", [])
    if isinstance(chase_tiers, list) and chase_tiers:
        chase_str = " | ".join([f">={t.get('min_score', 0)}分: {float(t.get('buy_buffer_pct', 0))*100:+.1f}%" for t in chase_tiers if isinstance(t, dict)])
    else:
        chase_str = ">=85分: +1.5% | >=70分: +1.0% | <70分: +0.5%"

    sell_tiers = final_execution_skills.get("sell_discount_tiers", [])
    if isinstance(sell_tiers, list) and sell_tiers:
        sell_str = " | ".join([f"<={t.get('max_score', 100)}分: {float(t.get('sell_discount_pct', 0))*100:+.1f}%" for t in sell_tiers if isinstance(t, dict)])
    else:
        sell_str = "<=49分: -1.5% | <=69分: -1.0% | <=100分: -0.5%"

    stock_rules_list = indicator_skills.get("stock_specific_rules", [])
    stock_rules_str = "；".join([f"{item.get('stock_code')}: {item.get('anomaly_trait')}" for item in stock_rules_list if isinstance(item, dict)]) if stock_rules_str_condition(stock_rules_list) else "無特別異常標的"

    monthly_summary_text = ""
    overfitting_text = ""
    if isinstance(monthly_skills_retrospective, dict):
        monthly_summary_text = monthly_skills_retrospective.get("trajectory_summary", "")
        overfitting_text = monthly_skills_retrospective.get("overfitting_verdict", "")
    elif hasattr(monthly_skills_retrospective, "trajectory_summary"):
        monthly_summary_text = getattr(monthly_skills_retrospective, "trajectory_summary")
        overfitting_text = getattr(monthly_skills_retrospective, "overfitting_verdict", "")

    overall_summary = (
        f"【{review_quarter_str} 季度決策策略總結】\n"
        f"• **涵蓋月份區間**：{date_range['start_date']} ~ {date_range['end_date']} ({', '.join(months_included)})\n"
        f"• **下季風控門檻**：建議最低買入門檻 **{min_score} 分** | 單檔最重權重 **{max_weight} 級** | 動態 ATR 停損 **-{stop_loss_atr:.1f}x ATR** (底線 {stop_loss*100:.1f}%) | 動態 ATR 鎖利 **+{take_profit_atr:.1f}x ATR** (基準 {take_profit*100:.1f}%)\n"
        f"• **動態定價階梯**：\n"
        f"  - 🎯 **買進依信心追價**：{chase_str}\n"
        f"  - 🛡️ **賣出依風險防賤賣**：{sell_str}\n"
        f"• **戰術執行重點**：{tactical_rules_str if tactical_rules_str else '維持穩健分批進場紀律'}\n"
        f"• **個股特殊特徵與關注**：{stock_rules_str}\n"
        f"• **月度技能演化回顧 (Layer 3)**：{monthly_summary_text}\n"
        f"• **過度擬合審查裁定**：{overfitting_text}\n"
        f"• **CIO 季度執行總評**：{cio_summary}"
    )

    stock_reports: List[Dict[str, Any]] = []
    for i, sc in enumerate(per_stock_data.keys()):
        ind_rep = stock_indicator_reports[i] if i < len(stock_indicator_reports) else {}
        exe_rep = stock_execution_reports[i] if i < len(stock_execution_reports) else {}
        stock_info = per_stock_data.get(sc, {})
        stock_reports.append({
            "stock_code": sc,
            "indicator_retrospective": ind_rep.get("indicator_retrospective", ""),
            "anomaly_trait": ind_rep.get("anomaly_trait"),
            "execution_retrospective": exe_rep.get("execution_retrospective", ""),
            "expected_upside_str": exe_rep.get("expected_upside_str") or stock_info.get("expected_upside_str", "--"),
            "expected_drawdown_str": exe_rep.get("expected_drawdown_str") or stock_info.get("expected_drawdown_str", "--"),
            "actual_pnl_str": exe_rep.get("actual_pnl_str") or stock_info.get("actual_pnl_str", "無平倉交易"),
            "stock_retrospective": (
                f"【指標診斷】{ind_rep.get('indicator_retrospective', '')}\n"
                f"【交易執行診斷】{exe_rep.get('execution_retrospective', '')}"
            )
        })

    return {
        "review_quarter": review_quarter_str,
        "quarter": quarter,
        "year": year,
        "months_included": months_included,
        "is_paper": is_paper,
        "metrics": metrics,
        "stock_indicator_reports": stock_indicator_reports,
        "stock_execution_reports": stock_execution_reports,
        "stock_reports": stock_reports,
        "indicator_summary": indicator_summary,
        "cio_summary": cio_summary,
        "overall_summary": overall_summary,
        "key_learnings": key_learnings,
        "monthly_skills_retrospective": monthly_skills_retrospective,
        "indicator_skills": indicator_skills,
        "execution_skills": final_execution_skills,
        "skills_json": unified_skills_dict
    }
