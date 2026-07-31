# AIAutoStocks Project Directives & Future Architecture

## 未來架構規劃：季度與年度決策檢討 Agent 規範

1. **週期復盤跨度規範 (Review Interval Spans)**：
   - 未來實作季度 (Quarterly) 與年度 (Annual) 檢討 Agent 時，資料區間統一以「週六復盤日 (`get_monthly_analysis_date`)」作為邊界基準。
   - 季度檢討涵蓋 3 個月度復盤日跨度（3 格跨度），年度檢討涵蓋 12 個月度復盤日跨度（12 格跨度），確保所有分析與交易紀錄無縫無死角銜接。
