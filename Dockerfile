# 使用官方輕量版 Python 映像檔
FROM python:3.11-slim

# 設定環境變數，防止 Python 生成 pyc 快取，並確保 stdout 輸出不被緩衝以利日誌紀錄
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei

# 安裝 tzdata 以設定台灣時區 (UTC+8)，並清理快取以減少映像檔體積
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 設定工作目錄
WORKDIR /app

# 複製依賴檔案並安裝 Python 套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製系統實體程式碼
COPY src/ ./src/

# 複製配置檔與加密憑證檔（若存在的話，採 Docker 條件複製語法避免建置失敗）
COPY config.jso[n] ./
COPY credentials.en[c] ./
COPY Sinopac.pf[x] ./



EXPOSE 8080

# 預設執行指令：運行網頁後端伺服器 (提供前端 API)
CMD ["python", "-m", "src.web_server"]
