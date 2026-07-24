-- Alpha Loop Trade System — Initial Schema
-- Migration 001: All Phase 1 tables

CREATE TABLE IF NOT EXISTS market_snapshots (
    pair_address            VARCHAR         NOT NULL,
    timestamp               TIMESTAMPTZ     NOT NULL,
    timeframe               VARCHAR(8)      NOT NULL,
    symbol                  VARCHAR         NOT NULL,
    chain                   VARCHAR(16)     NOT NULL,
    open                    NUMERIC(30,10)  NOT NULL,
    high                    NUMERIC(30,10)  NOT NULL,
    low                     NUMERIC(30,10)  NOT NULL,
    close                   NUMERIC(30,10)  NOT NULL,
    volume                  NUMERIC(30,4)   NOT NULL,
    liquidity_usd           NUMERIC(20,4)   NOT NULL,
    token_status            VARCHAR(32)     NOT NULL DEFAULT 'active',
    launch_time             TIMESTAMPTZ,
    -- Feature columns (nullable until feature pipeline runs)
    feature_version         INTEGER,
    momentum_5              NUMERIC(20,10),
    relative_volume_10      NUMERIC(20,6),
    relative_volume_slope_3 NUMERIC(20,6),
    vwap_distance           NUMERIC(20,10),
    atr_14                  NUMERIC(30,10),
    volatility_regime       VARCHAR(16),
    pullback_depth          NUMERIC(20,10),
    time_since_launch_minutes NUMERIC(12,2),
    spread_estimate_bps     NUMERIC(12,4),
    min_recent_trades       INTEGER,
    PRIMARY KEY (pair_address, timestamp, timeframe)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_pair_time ON market_snapshots (pair_address, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_token_status ON market_snapshots (token_status);

-- ---

CREATE TABLE IF NOT EXISTS strategy_runs (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_name       VARCHAR         NOT NULL,
    strategy_version    VARCHAR         NOT NULL,
    config_hash         VARCHAR(64)     NOT NULL,
    mode                VARCHAR(16)     NOT NULL,   -- paper | live | backtest
    session_start       TIMESTAMPTZ     NOT NULL,
    session_end         TIMESTAMPTZ,
    capital_start       NUMERIC(20,4),
    capital_end         NUMERIC(20,4)
);

-- ---

CREATE TABLE IF NOT EXISTS signal_candidates (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp           TIMESTAMPTZ     NOT NULL,
    strategy_name       VARCHAR         NOT NULL,
    strategy_version    VARCHAR         NOT NULL,
    symbol              VARCHAR         NOT NULL,
    pair_address        VARCHAR         NOT NULL,
    signal_direction    VARCHAR(8)      NOT NULL DEFAULT 'long',
    feature_snapshot_id UUID,           -- FK to market_snapshots (logical, not enforced)
    passed_filters      BOOLEAN         NOT NULL,
    rejection_reason    VARCHAR,
    confidence          NUMERIC(6,4),
    session_id          UUID            REFERENCES strategy_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_signals_session ON signal_candidates (session_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_passed ON signal_candidates (passed_filters, timestamp DESC);

-- ---

CREATE TABLE IF NOT EXISTS positions (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              UUID            REFERENCES strategy_runs(id),
    symbol                  VARCHAR         NOT NULL,
    pair_address            VARCHAR         NOT NULL,
    chain                   VARCHAR(16)     NOT NULL,
    entry_time              TIMESTAMPTZ,
    entry_price             NUMERIC(30,10),
    position_size_usd       NUMERIC(20,4),
    position_size_tokens    NUMERIC(30,8),
    stop_price              NUMERIC(30,10),
    take_profit_levels      JSONB,          -- [{price, portion_pct, triggered}]
    strategy_name           VARCHAR         NOT NULL,
    strategy_version        VARCHAR         NOT NULL,
    feature_snapshot_id     UUID,
    status                  VARCHAR(32)     NOT NULL DEFAULT 'open',
    mae                     NUMERIC(20,10),
    mfe                     NUMERIC(20,10)
);

CREATE INDEX IF NOT EXISTS idx_positions_session_status ON positions (session_id, status);

-- ---

CREATE TABLE IF NOT EXISTS trades (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID            REFERENCES positions(id),
    session_id          UUID            REFERENCES strategy_runs(id),
    symbol              VARCHAR         NOT NULL,
    entry_time          TIMESTAMPTZ,
    exit_time           TIMESTAMPTZ,
    entry_price         NUMERIC(30,10),
    exit_price          NUMERIC(30,10),
    pnl_usd             NUMERIC(20,4),
    pnl_pct             NUMERIC(10,6),
    r_multiple          NUMERIC(10,4),
    mae                 NUMERIC(20,10),
    mfe                 NUMERIC(20,10),
    hold_time_minutes   INTEGER,
    exit_reason         VARCHAR(64),
    strategy_version    VARCHAR         NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_session ON trades (session_id, exit_time DESC);

-- ---

CREATE TABLE IF NOT EXISTS trade_events (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id            UUID            REFERENCES trades(id),
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT now(),
    event_type          VARCHAR(64)     NOT NULL,
    payload             JSONB
);

CREATE INDEX IF NOT EXISTS idx_events_trade ON trade_events (trade_id, timestamp DESC);

-- ---

CREATE TABLE IF NOT EXISTS orders (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID            REFERENCES positions(id),
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT now(),
    side                VARCHAR(4)      NOT NULL,   -- buy | sell
    requested_price     NUMERIC(30,10),
    fill_price          NUMERIC(30,10),
    slippage_bps        NUMERIC(10,4),
    size_usd            NUMERIC(20,4),
    execution_type      VARCHAR(8)      NOT NULL,   -- paper | live
    tx_hash             VARCHAR,
    fill_status         VARCHAR(16)     NOT NULL DEFAULT 'filled'
);

-- ---

CREATE TABLE IF NOT EXISTS trade_reviews (
    id                      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              UUID            REFERENCES strategy_runs(id),
    review_version          INTEGER         NOT NULL DEFAULT 1,
    reviewed_at             TIMESTAMPTZ     NOT NULL DEFAULT now(),
    hypothesis              TEXT,
    rationale               TEXT,
    expected_effect         TEXT,
    change_type             VARCHAR(32),    -- filter | entry | exit | risk
    fields_needed           JSONB,
    proposed_config_diff    JSONB,
    risk                    TEXT
);
