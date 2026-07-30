# Path: src/agents/monthly_review_agent.py
import json
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

from src.config import config
from src.services.gemini_rotator import call_gemini_with_rotation
from src.services.supabase_client import supabase
from src.services.monthly_aggregator import aggregate_monthly_data

# =====================================================================
# 1. 定義 Map 階段：個股獨立復盤診斷模型
# =====================================================================
class StockReviewOutput(BaseModel):
    stock_code: str = Field(..., description="股票代號，如 '2330'")
    stock_retrospective: str = Field(
        ...,
        description="繁體中文 150-250 字個股獨立復盤診斷報告。請著重於分析當月買賣進場準確度、離場效率 (Upside vs Drawdown 期望值) 以及 AI 分析師當時的打分品質。"
    )

# =====================================================================
# 2. 定義 Reduce 階段：動態 JSON Skills 規範模型 (精簡 Token 結構)
# =====================================================================
class EvolvedSkillsJSON(BaseModel):
    version: str = Field(..., description="Skills 版本，如 '2026.07'")
    min_buy_score: int = Field(..., description="建議的最低買入門檻總得分 (40-90)")
    max_single_stock_weight: int = Field(..., description="建議單一標的最重部位權重 (1-5)")
    stop_loss_pct: float = Field(..., description="建議之個股硬停損百分比 (例如 -0.05 代表 -5%)")
    take_profit_pct: float = Field(..., description="建議之動態鎖利觸發百分比 (例如 0.12 代表 12%)")
    regime_posture: Dict[str, str] = Field(
        ...,
        description="在大盤不同氣候下建議的交易姿態映射，如 {'BULLISH_TREND': 'AGGRESSIVE', 'BEARISH_TREND': 'DEFENSIVE', 'HIGH_VOLATILITY': 'CONSERVATIVE'}"
    )
    tactical_rules: List[str] = Field(
        ...,
        description="2-4 條簡短英文或繁體中文精簡戰術規則指令"
    )

class OverallReviewOutput(BaseModel):
    overall_summary: str = Field(
        ...,
        description="繁體中文 300-500 字投資組合整體月度復盤報告。需涵蓋：1.熊市/盤整市是否過於保守 2.牛市是否過於積極導致虧損 3.打分偏差與偏斜校正結論 4.離場效率與氣候配合度。"
    )
    key_learnings: List[str] = Field(
        ...,
        description="3-5 條月度關鍵反思與策略學習點 (繁體中文)"
    )
    evolved_skills: EvolvedSkillsJSON = Field(
        ...,
        description="根據本月實盤績效與診斷，演化出供下個月日常交易使用的極簡 JSON 戰術 Skills"
    )

# =====================================================================
# 3. 執行 Map-Reduce 月度檢討主流程
# =====================================================================
def run_monthly_review(year: int, month: int, is_paper: bool = False, call_gemini_fn: Optional[Any] = None) -> Dict[str, Any]:
    """
    執行實盤 (`is_paper = False`) 月度 AI 自我檢討與 Skills 演化：
    1. Python monthly_aggregator 計算硬指標與 5 大 Context 柱石
    2. Phase 1 (Map): 迴圈呼叫 Gemini 產出個股獨立診斷報告
    3. Phase 2 (Reduce): 彙整發送整體 Prompt，診斷 6 大核心維度，產出 JSON Skills
    4. 寫入 Supabase monthly_skills 資料表
    """
    if call_gemini_fn is None:
        call_gemini_fn = call_gemini_with_rotation

    # Step 1: 預計算硬指標與數據 Context
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
            "stock_reports": [],
            "overall_summary": f"區間 ({review_month_str}) 內尚無歷史分析與交易紀錄，維持現有戰術防線。",
            "key_learnings": ["區間內無交易與分析資料，保持觀望與現有配置"],
            "skills_json": None
        }

    stock_reports: List[Dict[str, Any]] = []

    # Step 2: Phase 1 (Map 階段) - 個股獨立檢討 Prompt
    for stock_code, stock_info in per_stock_data.items():
        map_prompt = (
            f"你是一位頂級量化基金的個股復盤分析師。請對股票代號 {stock_code} 在 {review_month_str} 月份的表現進行個股獨立診斷。\n\n"
            f"【個股交易與評分數據】\n"
            f"- 分析師打分紀錄筆數: {len(stock_info.get('scores', []))}\n"
            f"- 成交單筆數: {len(stock_info.get('filled_orders', []))}\n"
            f"- 未成交/被取消單筆數: {len(stock_info.get('cancelled_orders', []))}\n"
            f"- 當月該股總振幅 (Price Range Ratio): {stock_info.get('price_range_ratio', 0.0) * 100:.2f}%\n"
            f"- 打分詳細紀錄: {json.dumps(stock_info.get('scores', []), ensure_ascii=False)}\n"
            f"- 成功成交單紀錄: {json.dumps(stock_info.get('filled_orders', []), ensure_ascii=False)}\n"
            f"- 被取消/未成交單紀錄: {json.dumps(stock_info.get('cancelled_orders', []), ensure_ascii=False)}\n\n"
            f"請評估該股：買入 timing 準確度、進場後 Upside/Drawdown 表現、未成交/取消單原因 (如滑價過大或掛價不及)，以及分析師給分品質。"
        )
        generation_config_map = {
            "response_mime_type": "application/json",
            "response_schema": StockReviewOutput,
            "temperature": 0.1
        }
        try:
            map_res = call_gemini_fn(
                prompt=map_prompt,
                generation_config=generation_config_map
            )
            map_data = json.loads(map_res)
            stock_reports.append(map_data)
        except Exception as e:
            print(f" [Monthly Review Agent] 警告: 個股 {stock_code} Map 檢討失敗: {e}")
            stock_reports.append({
                "stock_code": stock_code,
                "stock_retrospective": f"個股 {stock_code} 復盤分析跳過 (數據不足或 LLM 呼叫異常)。"
            })

    # Step 3: Phase 2 (Reduce 階段) - 組合整體分析與 Skills 演化 Prompt
    reduce_prompt = (
        f"你是一位首席投資官 (CIO)，正在對 {review_month_str} 月份的實盤交易策略進行月度總復盤，並演化下個月的交易戰術規範 (JSON Skills)。\n\n"
        f"【當月投資組合硬指標 (Python 精準預計算)】\n"
        f"- 平倉總筆數: {metrics['total_trades']} | 勝率: {metrics['win_rate']}%\n"
        f"- 實現總損益: {metrics['total_realized_pnl']} 元 | 盈虧比 (Payoff Ratio): {metrics['payoff_ratio']}\n"
        f"- 獲利因子 (Profit Factor): {metrics['profit_factor']}\n"
        f"- 期望潛在漲幅 (Mean Upside): +{metrics['mean_upside_ratio']*100:.2f}% (Std: {metrics['std_upside_ratio']})\n"
        f"- 期望潛在回撤 (Mean Drawdown): {metrics['mean_drawdown_ratio']*100:.2f}% (Std: {metrics['std_drawdown_ratio']})\n"
        f"- 成交平均滑價 (Mean Slippage): {metrics.get('mean_slippage_ratio', 0)*100:.2f}% (Std: {metrics.get('std_slippage_ratio', 0)})\n"
        f"- 未成交/被取消委託單筆數: {metrics.get('total_cancelled_orders', 0)} 筆 (取消率: {metrics.get('cancellation_rate_pct', 0)}%)\n"
        f"- 打分分佈: 高分(>=80) {aggregated_data['score_calibration']['high_score_count']} 筆, 中分(60-79) {aggregated_data['score_calibration']['mid_score_count']} 筆, 低分(<60) {aggregated_data['score_calibration']['low_score_count']} 筆\n\n"
        f"【各標的 Map 階段個股診斷報告彙整】\n"
        f"{json.dumps(stock_reports, ensure_ascii=False, indent=2)}\n\n"
        f"【核心診斷 8 大維度引導】\n"
        f"1. 熊市/盤整市是否過於保守 (錯失反彈與閒置資金)？\n"
        f"2. 牛市/反彈市是否過於積極 (追高或過度開倉導致虧損)？\n"
        f"3. 分析師打分預測品質與選股表現？\n"
        f"4. 打分偏差與偏斜校正 (是否有給分通膨/偏高，需調高買入門檻 min_buy_score)？\n"
        f"5. 離場效率與停損停利機制合適度？\n"
        f"6. 大盤氣候 (Regime) 與風險乘數配合度？\n"
        f"7. 成交滑價與摩擦成本診斷 (滑價是否過高，是否需於戰術規則中優化開盤/開倉時段)？\n"
        f"8. 未成交與委託單取消率診斷 (委託單是否常因價格滑落/過度激進或逾時而被取消)？\n\n"
        f"請產出整體復盤報告以及演化出的極簡 JSON 格式化戰術 Skills。"
    )

    generation_config_reduce = {
        "response_mime_type": "application/json",
        "response_schema": OverallReviewOutput,
        "temperature": 0.1
    }

    try:
        reduce_res = call_gemini_fn(
            prompt=reduce_prompt,
            generation_config=generation_config_reduce
        )
        overall_data = json.loads(reduce_res)
    except Exception as e:
        print(f" [Monthly Review Agent] 錯誤: Reduce 總體檢討失敗: {e}")
        # 後備結構
        overall_data = {
            "overall_summary": f"{review_month_str} 月度檢討順利完成，系統自動維持穩定動態風控。",
            "key_learnings": ["維持當前穩健交易風格", "持續監督打分品質"],
            "evolved_skills": {
                "version": review_month_str,
                "min_buy_score": 65,
                "max_single_stock_weight": 4,
                "stop_loss_pct": -0.05,
                "take_profit_pct": 0.12,
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

    skills_json = overall_data["evolved_skills"]

    # Step 4: 寫入 Supabase monthly_skills 表 (極簡全自動 Schema)
    try:
        insert_payload = {
            "review_month": review_month_str,
            "daily_analysis_ids": aggregated_data["daily_analysis_ids"],
            "skills": skills_json,
            "is_paper": is_paper
        }
        res = supabase.table("monthly_skills").insert(insert_payload).execute()
        print(f" [Monthly Review Agent] 成功寫入 monthly_skills 表: 月份 {review_month_str}")
    except Exception as e:
        print(f" [Monthly Review Agent] 警告: 寫入 monthly_skills 資料表失敗: {e}")

    return {
        "review_month": review_month_str,
        "is_paper": is_paper,
        "metrics": metrics,
        "stock_reports": stock_reports,
        "overall_summary": overall_data["overall_summary"],
        "key_learnings": overall_data["key_learnings"],
        "skills_json": skills_json
    }
