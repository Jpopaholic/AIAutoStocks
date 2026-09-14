# AIAutoStocks Project Directives & Future Architecture

## 未來架構規劃：年度決策檢討 Agent 規範 (Annual Review Agent)

1. **週期復盤跨度規範 (Review Interval Spans)**：
   - 未來實作年度 (Annual) 檢討 Agent 時，資料區間統一以「週六復盤日 (`get_monthly_analysis_date`)」作為邊界基準。
   - 年度檢討涵蓋 12 個月度復盤日跨度（12 格跨度，對齊完整 4 季），確保所有跨週期分析、訂單與宏觀演化紀錄無縫無死角銜接。

2. **復盤檢討門檻規範 (Review Threshold Requirements)**：
   - **年度檢討 (Annual Review)**：該年度至少需具備完整 4 季的有效季度檢討紀錄（4 季皆具備在 `quarterly_skills` 的檢討紀錄，缺一不可），方可啟動年度大復盤與跨週期戰略基因檢驗。

3. **Skills 彙整與衝突裁決規範 (Skills Precedence & Hierarchy)**：
   - 當年度檢討 Skills 與季度、月度檢討 Skills 產生矛盾或衝突時，遵循**「年度 > 季度 > 月度」**的最高指導原則。
   - 跨週期的長期資本保全、極端市場壓力測試防線與系統性大盤姿態，由年度宏觀基因優先裁決並定調。
