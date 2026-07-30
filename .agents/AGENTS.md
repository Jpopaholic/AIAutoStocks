# AIAutoStocks Project Directives & Future Architecture

## 未來架構規劃：月度決策檢討 Agent 與 Skills AI 自我演化控場

月底開發「月度決策檢討 Agent (Monthly Review Agent)」時，需同步調整與執行的重點架構：

1. **解耦硬編碼分數覆寫 (Remove Hardcoded Score Overrides)**：
   - 目前在 `src/agents/decision_agent.py` 中，程式會根據 `total_score < 60` 強制賣出、`total_score < 50` 強制禁買、`total_score < 70` 強制平倉。
   - 月底整合檢討 Agent 時，需移除這些後端程式碼的分數強硬覆寫邏輯。

2. **Skills 全權控場與自我演化 (Skills AI Self-Evolution)**：
   - 買賣決策與部位控制的邏輯，全面交由月度檢討 Agent 產出的動態 Skills 規範。
   - 讓投資組合經理 AI (`decision_agent`) 根據最新 Skills 文本與推理自主決定 `action` 與 `allocation_weight`。

3. **後端僅保留極限安全防線 (Safety Net)**：
   - 後端 Python 程式碼不再干涉 AI 的買賣邏輯與分數裁態，僅保留實體帳戶與交易所安全底線（如現金餘額不足阻斷、券商 API 連線異常阻斷、單筆絕對上限防爆倉等）。
