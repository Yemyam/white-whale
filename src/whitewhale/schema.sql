-- White Whale SQLite schema. WAL and other pragmas set in db.py.

CREATE TABLE IF NOT EXISTS markets (
    condition_id     TEXT PRIMARY KEY,
    slug             TEXT NOT NULL DEFAULT '',
    question         TEXT NOT NULL DEFAULT '',
    event_slug       TEXT,
    liquidity_usdc   REAL,
    resolves_at      TEXT,
    resolved         INTEGER NOT NULL DEFAULT 0,
    outcome_resolved INTEGER,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);
CREATE INDEX IF NOT EXISTS idx_markets_resolves_at ON markets(resolves_at);

CREATE TABLE IF NOT EXISTS wallets (
    address          TEXT PRIMARY KEY,
    display_name     TEXT,
    label            TEXT,
    cluster_id       TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wallets_cluster ON wallets(cluster_id);
CREATE INDEX IF NOT EXISTS idx_wallets_label ON wallets(label);

CREATE TABLE IF NOT EXISTS trades (
    tx_hash          TEXT NOT NULL,
    log_index        INTEGER NOT NULL DEFAULT 0,
    occurred_at      TEXT NOT NULL,
    wallet           TEXT,
    wallet_resolved  INTEGER NOT NULL DEFAULT 0,
    condition_id     TEXT NOT NULL,
    asset_id         TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    outcome_index    INTEGER NOT NULL,
    side             TEXT NOT NULL,
    price            REAL NOT NULL,
    size_shares      REAL NOT NULL,
    size_usdc        REAL NOT NULL,
    PRIMARY KEY (tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS idx_trades_wallet_time ON trades(wallet, occurred_at);
CREATE INDEX IF NOT EXISTS idx_trades_market_time ON trades(condition_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_trades_size ON trades(size_usdc);
CREATE INDEX IF NOT EXISTS idx_trades_unresolved ON trades(wallet_resolved) WHERE wallet_resolved = 0;

CREATE TABLE IF NOT EXISTS wallet_stats (
    wallet                    TEXT PRIMARY KEY,
    trade_count               INTEGER NOT NULL DEFAULT 0,
    resolved_trade_count      INTEGER NOT NULL DEFAULT 0,
    realized_pnl_usdc         REAL NOT NULL DEFAULT 0,
    win_count                 INTEGER NOT NULL DEFAULT 0,
    loss_count                INTEGER NOT NULL DEFAULT 0,
    median_size_usdc          REAL NOT NULL DEFAULT 0,
    p90_size_usdc             REAL NOT NULL DEFAULT 0,
    round_trip_count_30d      INTEGER NOT NULL DEFAULT 0,
    two_sided_ratio_30d       REAL NOT NULL DEFAULT 0,
    mid_price_proximity_30d   REAL NOT NULL DEFAULT 0,
    updated_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id         TEXT PRIMARY KEY,
    emitted_at       TEXT NOT NULL,
    tx_hash          TEXT NOT NULL,
    log_index        INTEGER NOT NULL,
    score_total      INTEGER NOT NULL,
    confidence       TEXT NOT NULL,
    payload_json     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_emitted_at ON alerts(emitted_at);
CREATE INDEX IF NOT EXISTS idx_alerts_score ON alerts(score_total);
