# Path: src/agents/trading_agent.py
import json
from typing import List, Dict, Any, Optional

# 1. 為了保持原有單元測試的完全相容性，我們重導出所有資料型態與變數
from src.agents.analyst_agent import AnalystStockScore, AnalystAssessment, DEFAULT_TRADING_SKILLS
from src.agents.decision_agent import PMStockDecision, PortfolioDecision

# 2. 導入與測試相容的實體與工具函式（以便測試檔案 patch 時能順利被覆蓋）
from src.services.supabase_client import get_system_fault_status, get_pending_liquidation_stocks
from src.services.gemini_rotator import call_gemini_with_rotation
from src.agents import analyst_agent, decision_agent

def generate_portfolio_decisions(
    stock_codes: List[str],
    klines_map: Dict[str, List[Dict[str, Any]]],
    current_holdings: List[Dict[str, Any]],
    extra_skills: Optional[List[str]] = None,
    regime_assessment: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    雙層管線 Facade 協調器門面。
    它完美維持了舊有的 generate_portfolio_decisions 介面，並相容既有的單元測試模擬 Mock 機制。
    內部它依序調用第二層分析師評估（analyst_agent）與第三層投資組合配置經理決策（decision_agent）。
    """
    # ── [系統防禦故障阻斷] ───────────────────────────────────────────
    # 此處保留在協調器中，是為了讓既有單元測試（TestHybridLiquidation.test_generate_portfolio_decisions_fault_fallback）
    # 對其進行 patch 時，能順利運作並回退至防禦狀態。
    try:
        fault_state = get_system_fault_status()
        if fault_state.get("status") == "FAULT":
            print(f" [AI交易代理] 警告: 系統目前處於全局故障鎖定狀態！原因: {fault_state.get('detail')}")
            fallback_decisions = []
            for code in stock_codes:
                klines = klines_map.get(code, [])
                fallback_decisions.append({
                    "stock_code": code,
                    "action": "HOLD",
                    "price": klines[-1]["close"] if klines else 10.0,
                    "quantity": 0.0,
                    "confidence": 0.0,
                    "reason": f"系統處於故障安全防禦鎖定狀態 (SYSTEM FAULT)，已暫停所有交易。故障原因: {fault_state.get('detail')}",
                    "trend_score": 0,
                    "momentum_score": 0,
                    "volume_score": 0,
                    "safety_score": 0,
                    "regime_score": 0,
                    "total_score": 0
                })
            return {
                "ranking_analysis": f"系統目前處於全局故障鎖定狀態，已暫停交易。原因: {fault_state.get('detail')}",
                "decisions": fallback_decisions
            }
    except Exception as fault_err:
        print(f" [AI交易代理] 讀取系統故障狀態失敗: {str(fault_err)}")

    # ── [第一階段：第二層技術分析師評分] ──────────────────────────────
    # 傳入本類別引用的 call_gemini_with_rotation 函數指標，確保單元測試的 mock.patch 依舊有效
    analyst_scores = analyst_agent.generate_analyst_assessments(
        stock_codes=stock_codes,
        klines_map=klines_map,
        extra_skills=extra_skills,
        regime_assessment=regime_assessment,
        call_gemini_fn=call_gemini_with_rotation
    )

    # ── [第二階段：第三層投資組合經理決策與部位配置] ────────────────────────
    # 獲取平倉名單，此呼叫會被單元測試 Mock 覆蓋
    pending_stocks = []
    try:
        pending_stocks = get_pending_liquidation_stocks()
    except Exception as e:
        print(f" [AI交易代理] 無法讀取智慧平倉列表: {e}")

    # 同樣傳入本類別引用的 call_gemini_with_rotation 函數指標
    return decision_agent.generate_portfolio_decisions(
        stock_codes=stock_codes,
        analyst_scores=analyst_scores,
        klines_map=klines_map,
        current_holdings=current_holdings,
        regime_assessment=regime_assessment,
        call_gemini_fn=call_gemini_with_rotation,
        pending_stocks=pending_stocks
    )
