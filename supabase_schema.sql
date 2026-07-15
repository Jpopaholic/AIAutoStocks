-- =============================================================================
-- AIAutoStocks — Supabase 完整資料庫 Schema
-- 請在 Supabase 後台 → SQL Editor 中執行此檔案
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. watchlist — 自選監控股票清單
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  TEXT NOT NULL UNIQUE,   -- 4 碼股票代號，唯一鍵（支援 upsert）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 加速查詢索引
CREATE INDEX IF NOT EXISTS idx_watchlist_stock_code ON watchlist (stock_code);

-- 啟用 Row Level Security（建議，但 service role key 可繞過）
ALTER TABLE watchlist ENABLE ROW LEVEL SECURITY;

-- 初始測試資料（可選，執行後可從前端刪除）
-- INSERT INTO watchlist (stock_code) VALUES ('2330'), ('2454') ON CONFLICT DO NOTHING;


-- -----------------------------------------------------------------------------
-- 2. holdings — 目前持股明細
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS holdings (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    TEXT NOT NULL,
    quantity      NUMERIC(18, 4) NOT NULL DEFAULT 0,
    average_price NUMERIC(18, 4) NOT NULL DEFAULT 0,
    is_paper      BOOLEAN NOT NULL DEFAULT TRUE,   -- TRUE=沙盒模擬, FALSE=實盤
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (stock_code, is_paper)                  -- 支援 upsert on_conflict
);

CREATE INDEX IF NOT EXISTS idx_holdings_stock_code ON holdings (stock_code);
CREATE INDEX IF NOT EXISTS idx_holdings_is_paper   ON holdings (is_paper);

ALTER TABLE holdings ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 3. trade_orders — 交易訂單歷史紀錄
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_orders (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    TEXT NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    price         NUMERIC(18, 4) NOT NULL,
    quantity      NUMERIC(18, 4) NOT NULL,
    fee           NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_amount  NUMERIC(18, 4) NOT NULL,
    realized_pnl  NUMERIC(18, 4) NOT NULL DEFAULT 0,
    is_paper      BOOLEAN NOT NULL DEFAULT TRUE,
    executed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    execution_price NUMERIC(18, 4),                     -- 實際成交價
    status        TEXT NOT NULL DEFAULT 'PENDING',    -- 訂單狀態: PENDING, FILLED, CANCELLED, FAILED
    order_id      TEXT                                -- 券商委託單號
);

CREATE INDEX IF NOT EXISTS idx_trade_orders_stock_code   ON trade_orders (stock_code);
CREATE INDEX IF NOT EXISTS idx_trade_orders_executed_at  ON trade_orders (executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_trade_orders_is_paper     ON trade_orders (is_paper);

ALTER TABLE trade_orders ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 4. stock_klines — 股票歷史 K 線數據
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_klines (
    id          BIGSERIAL PRIMARY KEY,
    stock_code  TEXT NOT NULL,
    date        DATE NOT NULL,
    open        NUMERIC(18, 4),
    high        NUMERIC(18, 4),
    low         NUMERIC(18, 4),
    close       NUMERIC(18, 4),
    volume      BIGINT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (stock_code, date)                     -- 支援 upsert on_conflict
);

CREATE INDEX IF NOT EXISTS idx_stock_klines_stock_date ON stock_klines (stock_code, date DESC);

ALTER TABLE stock_klines ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 5. system_logs — 系統執行日誌（自動 TTL 清理 7 天）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_logs (
    id         BIGSERIAL PRIMARY KEY,
    level      TEXT NOT NULL DEFAULT 'INFO',   -- INFO / WARN / ERROR
    message    TEXT NOT NULL,
    details    JSONB,
    is_paper   BOOLEAN NOT NULL DEFAULT TRUE,  -- TRUE=沙盒模擬, FALSE=實盤
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_logs_level      ON system_logs (level);
CREATE INDEX IF NOT EXISTS idx_system_logs_is_paper   ON system_logs (is_paper);

ALTER TABLE system_logs ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 6. system_config — 動態系統配置參數（key-value 形式）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS system_config (
    id         BIGSERIAL PRIMARY KEY,
    key        TEXT NOT NULL UNIQUE,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_system_config_key ON system_config (key);

ALTER TABLE system_config ENABLE ROW LEVEL SECURITY;

-- 預設動態配置初始值（可從前端覆蓋）
INSERT INTO system_config (key, value) VALUES
    ('PAPER_TRADING_MODE',           'true'),
    ('INITIAL_CASH',                 '1000000'),
    ('TRADING_LIMIT_SINGLE_STOCK_PCT', '0.1'),
    ('TRADING_LIMIT_DAILY_TOTAL_PCT',  '0.3'),
    ('SANDBOX_START_DATE',           '2026-05-01'),
    ('SANDBOX_END_DATE',             '2026-06-09'),
    ('GEMINI_MODEL',                 'gemini-1.5-flash'),
    ('AUTO_TRADING_ACTIVE',          'true'),
    ('TAIWAN_STOCK_TIMEZONE',        'Asia/Taipei')
ON CONFLICT (key) DO NOTHING;


-- -----------------------------------------------------------------------------
-- 7. gemini_keys_state — Gemini API 金鑰輪替狀態
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gemini_keys_state (
    id           BIGSERIAL PRIMARY KEY,
    key_hash     TEXT NOT NULL UNIQUE,   -- API key 的 SHA256 雜湊（不儲存明文）
    use_count    INTEGER NOT NULL DEFAULT 0,
    rpm_limit    INTEGER NOT NULL DEFAULT 15,
    rpd_limit    INTEGER NOT NULL DEFAULT 1500,
    last_used_at TIMESTAMPTZ,
    cooled_until TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gemini_keys_key_hash ON gemini_keys_state (key_hash);

ALTER TABLE gemini_keys_state ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 8. daily_analysis — 每日 AI 分析執行紀錄
-- 用於記錄每次執行自動/手動分析的紀錄，每次執行都是新的一行
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_analysis (
    id              BIGSERIAL PRIMARY KEY,
    analysis_date   DATE NOT NULL,                   -- 台灣當日日期 (e.g. 2026-07-08)
    is_paper        BOOLEAN NOT NULL DEFAULT TRUE,   -- TRUE=沙盒, FALSE=實盤
    trigger_type    TEXT NOT NULL DEFAULT 'auto',    -- 'auto' 或 'manual'
    regime          TEXT,                            -- 大盤氣候狀態 (e.g. 'BULLISH_TREND')
    posture         TEXT,                            -- 交易姿態 (e.g. 'MODERATE')
    risk_multiplier NUMERIC(6, 2),                   -- 風險乘數
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_daily_analysis_date    ON daily_analysis (analysis_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_analysis_paper   ON daily_analysis (is_paper);
CREATE INDEX IF NOT EXISTS idx_daily_analysis_trigger ON daily_analysis (trigger_type);

ALTER TABLE daily_analysis ENABLE ROW LEVEL SECURITY;


-- =============================================================================
-- 完成！以上 8 張資料表即為 AIAutoStocks 系統的完整 Schema。
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 9. unfilled_orders — 未成交/滑價取消訂單紀錄
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS unfilled_orders (
    id            BIGSERIAL PRIMARY KEY,
    stock_code    TEXT NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('BUY', 'SELL')),
    price         NUMERIC(18, 4) NOT NULL,
    quantity      NUMERIC(18, 4) NOT NULL,
    fee           NUMERIC(18, 4) NOT NULL DEFAULT 0,
    total_amount  NUMERIC(18, 4) NOT NULL,
    is_paper      BOOLEAN NOT NULL DEFAULT FALSE,
    executed_at   TIMESTAMPTZ NOT NULL,
    order_id      TEXT,
    reason        TEXT,                                -- 刪除原因: CANCELLED (券商取消), NOT_FOUND (未找到/過期)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_unfilled_orders_stock_code ON unfilled_orders (stock_code);
CREATE INDEX IF NOT EXISTS idx_unfilled_orders_executed_at ON unfilled_orders (executed_at DESC);

ALTER TABLE unfilled_orders ENABLE ROW LEVEL SECURITY;


-- -----------------------------------------------------------------------------
-- 10. stock_analysis_scores — 股票 AI 分析評分與決策紀錄
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_analysis_scores (
    id                BIGSERIAL PRIMARY KEY,
    daily_analysis_id BIGINT REFERENCES daily_analysis(id) ON DELETE CASCADE,
    analysis_date     DATE NOT NULL,                   -- 台灣當日日期 (e.g. 2026-07-08)
    stock_code        TEXT NOT NULL,                   -- 股票代號 (e.g. '2330')
    trend_score       INTEGER NOT NULL,                -- 趨勢得分 (0-20)
    momentum_score    INTEGER NOT NULL,                -- 動能得分 (0-20)
    volume_score      INTEGER NOT NULL,                -- 成交量得分 (0-20)
    safety_score      INTEGER NOT NULL,                -- 安全防守得分 (0-20)
    regime_score      INTEGER NOT NULL,                -- 與大盤一致性得分 (0-20)
    decision          TEXT NOT NULL CHECK (decision IN ('BUY', 'SELL', 'HOLD')), -- 決策
    is_paper          BOOLEAN NOT NULL DEFAULT TRUE,   -- TRUE=沙盒模擬, FALSE=實盤
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 加速查詢索引
CREATE INDEX IF NOT EXISTS idx_stock_analysis_scores_date ON stock_analysis_scores (analysis_date DESC);
CREATE INDEX IF NOT EXISTS idx_stock_analysis_scores_stock ON stock_analysis_scores (stock_code);
CREATE INDEX IF NOT EXISTS idx_stock_analysis_scores_analysis_id ON stock_analysis_scores (daily_analysis_id);

-- 啟用 Row Level Security
ALTER TABLE stock_analysis_scores ENABLE ROW LEVEL SECURITY;


