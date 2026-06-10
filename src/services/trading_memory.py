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
