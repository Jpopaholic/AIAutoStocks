# AIAutoStocks Project Directives & Future Architecture

## 未來架構規劃：季度與年度決策檢討 Agent 規範

1. **週期復盤跨度規範 (Review Interval Spans)**：
   - 未來實作季度 (Quarterly) 與年度 (Annual) 檢討 Agent 時，資料區間統一以「週六復盤日 (`get_monthly_analysis_date`)」作為邊界基準。
   - 季度檢討涵蓋 3 個月度復盤日跨度（3 格跨度），年度檢討涵蓋 12 個月度復盤日跨度（12 格跨度），確保所有分析與交易紀錄無縫無死角銜接。

2. **復盤檢討門檻規範 (Review Threshold Requirements)**：
   - **月度檢討 (Monthly Review)**：該月資料區間內至少需累積 10 筆（含）以上的日分析紀錄 (`daily_analysis >= 10`)。若日分析資料不足 10 筆，為避免樣本不足導致模型誤判，安全跳過檢討。
   - **季度檢討 (Quarterly Review)**：對應的該季度 3 個月份中，每個月都必須至少有一筆有效月度檢討紀錄（3 個月份皆具備至少 1 筆月檢討，缺一不可），確保季度的宏觀策略評估具備扎實的月度基石支撐。
   - **年度檢討 (Annual Review)**：該年度至少需具備完整 4 季的季度檢討紀錄（4 季皆具備有效季檢討），方可啟動年度大復盤與跨週期戰略檢驗。
