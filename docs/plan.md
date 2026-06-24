# White Whale - Implementation Plan

Self-hosted Polymarket whale alert system that replaces a third-party Telegram channel with a configurable, transparent copy score.

## Goal
Watch live Polymarket trades, filter for whales, score each one 0-100 on copyability using pure arithmetic over precomputed wallet stats, and emit JSON alerts to the existing downstream bot.

## Locked-in stack
| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Mature ecosystem, good WS / async libs |
| Layout | `src/whitewhale/` | Cleaner imports than flat package |
| Package manager | `uv` | Fast, modern, lockfile-driven |
| Storage | SQLite (WAL mode) | Single-file, zero ops, fits Pi 3 |
| Host | Raspberry Pi 3 (colocated with existing bot) | Already paid for; latency acceptable |
| Frontend (later) | JS, against a FastAPI surface | Deferred until backend is producing alerts |
| ML / LLM | None | Score is pure arithmetic, deterministic, unit-testable |

## Architecture overview

```
RTDS WebSocket (firehose)
        |
        v
  Ingestor -----> SQLite (trades, markets, wallets)
        |              ^
        |              | (precomputed stats refreshed in background)
        v              |
  Whale filter         |
        |              |
        v              |
  Score engine --------+
        |
        v
  Alert emitter (JSON drop to existing bot)
```

Hot path (per trade): ~60-210 ms on Pi 3, dominated by one HTTP call for orderbook (`price_impact_score`). Everything else is SQLite lookups against `wallet_stats`.

Cold path (background, can be slow): wallet stats refresh, market-resolution updates, periodic subgraph backfill. Heavy backfill runs once on laptop, then scp to Pi.

## Alert JSON schema (agreed)

```json
{
  "schema_version": "1.0",
  "alert_id": "<uuid-v4>",
  "emitted_at": "<ISO 8601 UTC>",
  "trade": {
    "tx_hash": "0x...",
    "occurred_at": "<ISO 8601 UTC>",
    "wallet": "0x...",
    "side": "BUY|SELL",
    "outcome": "YES|NO|...",
    "outcome_token_id": "<asset>",
    "price": 0.0,
    "size_usdc": 0.0,
    "shares": 0.0
  },
  "market": {
    "condition_id": "0x...",
    "slug": "<slug>",
    "question": "<text>",
    "url": "https://polymarket.com/event/...",
    "liquidity_usdc": 0.0,
    "current_price": 0.0,
    "resolves_at": "<ISO 8601 UTC>",
    "hours_to_resolution": 0.0
  },
  "score": {
    "total": 0,
    "confidence": "high|medium|low",
    "components": {
      "wallet_pnl_score": 0,
      "wallet_winrate_score": 0,
      "history_depth_score": 0,
      "conviction_size_score": 0,
      "time_to_resolution_score": 0,
      "price_impact_score": 0,
      "non_arb_score": 0,
      "non_mm_score": 0,
      "organic_price_score": 0
    },
    "rationale": ["<templated string>", "..."]
  }
}
```

Notes:
- All components on 0-100 "copyability" scale (higher = more copyable). Anti-signals inverted so the weighted sum stays sign-consistent.
- `confidence` is independent of `total` - a wallet with great stats but only 6 lifetime trades is high-score, low-confidence.
- `rationale` is templated strings, no LLM.
- See `docs/research-notes.md` section 5 for component computations and default weights.

## Phased plan

| Phase | Title | Status | Effort |
|---|---|---|---|
| 0 | Research smart-money detection + labeled wallet seed | done | 1-2 days |
| 1 | Ingestion (WS + REST + subgraph backfill) | done | 1-2 days |
| 2 | Whale filter | done | 0.5 day |
| 3 | Copy score engine | done | 2-3 days |
| 4 | Alert emission (JSON drop) | pending | 0.5 day |
| 5 | Backtester | pending | 2-3 days |
| 6 | Ops (health, refresh jobs, config hot-reload) | pending | 0.5 day |
|   | Total | | ~8-12 focused days |

### Phase 0 - Research & labeled wallet set (done)
- `docs/research-notes.md` - sources, sub-score rationale, default weights, calibration plan
- `data/labeled-wallets.csv` - seeded with the Theo cluster (Theo4, Fredi9999, PrincessCaro, Michie)
- `data/README.md` - CSV schema and population plan

### Phase 1 - Ingestion (done)
- Project skeleton: `pyproject.toml`, `.gitignore`, `.python-version`, `config/default.yaml`
- SQLite schema + connection helper (`src/whitewhale/db.py`, `schema.sql`); idempotent ALTER-based migrations
- Pydantic models tolerant of RTDS schema drift (`models.py`)
- RTDS WebSocket client with auto-reconnect + certifi SSL context (`ingest/rtds.py`); writes `proxyWallet` → `trades.wallet`, upserts `wallets` with `pseudonym`/`display_name` while preserving seed `label`/`cluster_id`
- Gamma REST enricher (`ingest/gamma.py`): pulls `liquidity_usdc`, `current_price`, `resolves_at` for known markets; idempotent under `refresh_max_age_seconds`
- Subgraph backfill (`ingest/subgraph.py`): pulls every `OrderFilledEvent` for a wallet from Goldsky `orderbook-subgraph`. Sentinel values for `condition_id`/`outcome`/`outcome_index` (subgraph doesn't carry them); token→condition map deferred to Phase 3/5. **Subgraph indexing lags ~3 weeks** (see `docs/research-notes.md` §1.4) — backfills must use `--since 0`.
- CLI: `init-db`, `tap`, `ingest`, `enrich-markets`, `backfill-wallets` (`cli.py`)

### Phase 2 - Whale filter (done)
- `filter.py`: `passes_thresholds` (pure size + liquidity floor predicate), `WhaleFilter` (stateful per-wallet+market dedupe for the live stream), `iter_whale_trades` (batch DB scan running identical logic)
- Configurable rule: `size_usdc >= min_size_usdc AND market.liquidity_usdc >= min_market_liquidity_usdc`
- Dedupe window measured from the last *accepted* event, so a churning wallet emits at most one event per `dedupe_window_seconds` rather than going silent
- Unknown (un-enriched) market liquidity fails the floor unless `allow_unknown_liquidity` is set
- CLI: `whales` scans ingested trades and prints/JSON-dumps what the filter would emit
- Config keys added: `whale_filter.dedupe_window_seconds`, `whale_filter.allow_unknown_liquidity`

### Phase 3 - Copy score engine (done)
- `scoring/` package: `inputs.py` (typed `ScoreInputs`/`ScoringConfig`/`ScoreResult`), `components.py` (9 pure 0-100 sub-scores + Wilson lower bound), `engine.py` (`score_trade` pure weighting + confidence; `build_inputs`/`score_whale_event` DB assembly)
- 9 sub-scores from precomputed `wallet_stats` + the trade + market, per `docs/research-notes.md` §5; anti-signals inverted so higher = more copyable
- Weighted sum is config-driven; `ScoringConfig.from_config` validates weights sum to 1.0 and that all 9 components are present
- `confidence` independent of `total` (§6): high needs depth≥70 AND total≥60; medium needs depth≥40; else low
- Missing inputs (un-backfilled wallet, no known mid) fall back to `neutral_score`; `wallet_pnl_score` ranks PnL percentile across wallets with ≥`min_resolved_bets` resolved bets
- `rationale[]` is templated, notable-driver-only strings (no LLM)
- CLI: `score` runs whale events through the engine end-to-end (`--json` emits the full score object)
- Config keys added: `scoring.params.*` (mapping knobs), `scoring.confidence.*`
- **Known gap (deferred):** `price_impact_score`/`organic_price_score` use `markets.current_price` as a mid proxy; true mid-at-entry needs a live CLOB orderbook fetch on the hot path (Phase 6). `wallet_stats` population (the stats refresh job) is also Phase 6 — until then scored wallets fall back to neutral defaults. Anti-signal weight calibration (arb trades → ≤25) is a Phase 5 backtest outcome, not guaranteed by the default weights.

### Phase 4 - Alert emission
- JSON written to a file drop or webhook (depends on bot interface; TBD)
- Rate-limit + dedupe so a single market churn doesn't spam
- Persist alert to `alerts` table for audit

### Phase 5 - Backtester
- Replay historical resolved markets through the scoring engine
- For each historical whale trade: was copying it positive EV at settlement?
- Hold out last 4-8 weeks for out-of-sample evaluation
- Calibration target: Theo cluster trades consistently >= 75, round-trip arb trades consistently <= 25
- Runs on laptop, not Pi.

### Phase 6 - Ops
- Health endpoint / heartbeat
- Per-wallet stats refresh job (daily incremental)
- Config hot-reload (re-read YAML without restart)
- `systemd` unit for the Pi

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Polymarket API/WS schema drift | Medium | High | `RawTrade` is `extra="allow"`; `tap` CLI verifies wire shape; contract tests in Phase 6 |
| Subgraph backfill too slow on Pi | High | Medium | Run on laptop, scp SQLite to Pi |
| Cold start: wallet history too thin -> noisy scores | High | High | Backfill before launch; `confidence` field surfaces thin samples to downstream bot |
| Backtest overfits | High | Medium | Hold out final 4-8 weeks; require out-of-sample EV > current config to promote |
| Insider/arb detection has false positives | Medium | High | Make anti-signal weights tunable; log rejections for audit |
| Pi 3 SD card wear from constant SQLite writes | Medium | Medium | Plan for SD card swap or USB SSD |

## Out of scope (v1)
- Sub-second execution latency / racing other copy bots
- Multi-user / hosted product
- Real-time JS dashboard (deferred to post-v1)
- ML-based scoring
