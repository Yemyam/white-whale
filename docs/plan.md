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
| 1 | Ingestion (WS + REST + subgraph backfill) | in progress | 1-2 days |
| 2 | Whale filter | pending | 0.5 day |
| 3 | Copy score engine | pending | 2-3 days |
| 4 | Alert emission (JSON drop) | pending | 0.5 day |
| 5 | Backtester | pending | 2-3 days |
| 6 | Ops (health, refresh jobs, config hot-reload) | pending | 0.5 day |
|   | Total | | ~8-12 focused days |

### Phase 0 - Research & labeled wallet set (done)
- `docs/research-notes.md` - sources, sub-score rationale, default weights, calibration plan
- `data/labeled-wallets.csv` - seeded with the Theo cluster (Theo4, Fredi9999, PrincessCaro, Michie)
- `data/README.md` - CSV schema and population plan

### Phase 1 - Ingestion (in progress)
- Project skeleton: `pyproject.toml`, `.gitignore`, `.python-version`, `config/default.yaml`
- SQLite schema + connection helper (`src/whitewhale/db.py`, `schema.sql`)
- Pydantic models tolerant of RTDS schema drift (`models.py`)
- RTDS WebSocket client with auto-reconnect + certifi SSL context (`ingest/rtds.py`)
- CLI: `init-db`, `tap`, `ingest` (`cli.py`)
- Verified live via `tap`: RTDS payload includes `proxyWallet` - wallet attribution comes for free, no Polygon RPC resolver needed
- Remaining in Phase 1:
  - Parse `proxyWallet`, `pseudonym`, `name` into trades + wallets tables
  - `ingest/gamma.py` - REST client for market metadata enrichment (liquidity, resolves_at, current_price)
  - `ingest/subgraph.py` - historical wallet history backfill via Polymarket subgraph (run on laptop)

### Phase 2 - Whale filter
- Configurable rule: `size_usdc >= threshold AND market.liquidity_usdc >= threshold`
- Dedupe: same wallet + market within N seconds = one event

### Phase 3 - Copy score engine
- 9 sub-scores computed from precomputed `wallet_stats` table + the trade + market orderbook
- Weighted sum, config-driven (`config/default.yaml`, hot-reloadable in Phase 6)
- Sub-score formulas in `docs/research-notes.md` section 5
- Outputs `{total, confidence, components, rationale[]}`

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
