# Research Notes — White Whale

Synthesis of public information on Polymarket data sources, known smart-money clusters, arbitrage/MM signatures, and existing copy-trade tooling. This document drives the default copy-score configuration that will land in Phase 3.

All claims here cite a source. Anything we'll need to verify empirically against our own ingested data is flagged with **[CALIBRATE]**.

---

## 1. Data sources we'll consume

### 1.1 Real-time trade feed (RTDS) — primary firehose
- **Endpoint:** `wss://ws-live-data.polymarket.com`
- **Auth:** none for public activity
- **Topic / type:** `activity` / `trades`
- **Fields per trade:** `asset, conditionId, eventSlug, outcome, outcomeIndex, price, side, size, slug, timestamp, transactionHash, title`
- This is what the live whale-detection loop subscribes to.
- Source: [Polymarket real-time-data-client README](https://github.com/Polymarket/real-time-data-client/blob/main/README.md)

### 1.2 CLOB WebSocket — market channel (orderbook)
- **Endpoint:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Auth:** none
- **Subscribe payload:** `{"assets_ids": ["<token_id>", ...], "type": "market"}`
- **Heartbeat:** send `PING` every 10s, expect `PONG`
- **Used for:** live orderbook → price-impact sub-score, organic-price sub-score
- Source: [Polymarket WSS overview](https://docs.polymarket.com/developers/CLOB/websocket/wss-overview)

### 1.3 Gamma API — market metadata
- **Base URL:** `https://gamma-api.polymarket.com`
- **Auth:** none
- **Key endpoints:** `/events`, `/markets`, `/tags`, `/series`, `/public-search`
- **Filtering:** `active`, `closed`, `tag_id`; sort by `volume_24hr`, `liquidity`, `competitive`
- **Rate limits:** general 4000/10s; `/events` 500/10s; `/markets` 300/10s
- **Used for:** market metadata, liquidity, resolution dates, slug ↔ condition_id ↔ token_id lookups
- Source: [Polymarket API for developers (Chainstack)](https://chainstack.com/polymarket-api-for-developers/), [AgentBets Gamma guide](https://agentbets.ai/guides/polymarket-gamma-api-guide/)

### 1.4 Subgraph (Goldsky) — historical backfill
- Polymarket maintains multiple subgraphs: `activity-subgraph`, `pnl-subgraph`, `wallet-subgraph`, `orderbook-subgraph`, `oi-subgraph`, `fpmm-subgraph`, `sports-oracle-subgraph`
- **Used for:** initial wallet-history backfill (PnL, win rate, trade count). Heavy job — run on laptop, not Pi.
- Source: [Polymarket/polymarket-subgraph](https://github.com/Polymarket/polymarket-subgraph)
- **Confirmed (Phase 1, 2026-05-21):**
  - Public endpoint: `https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn`. No API key.
  - `activity-subgraph` and `positions-subgraph` return 404 at the current public project id; only `orderbook-subgraph` (v0.0.1) and `pnl-subgraph` (v0.0.14) are live.
  - `OrderFilledEvent` carries `maker`/`taker`/`makerAssetId`/`takerAssetId`/`makerAmountFilled`/`takerAmountFilled` — **no `conditionId`, `outcome`, or `outcome_index`**. Token id → condition id must be resolved separately (Gamma `clobTokenIds`). Phase 1 ingestor writes sentinel values (`condition_id=''`, `outcome=''`, `outcome_index=-1`) and defers the join.
  - **Indexing lag ≈ 3 weeks** behind real time (latest event observed at ts 1777374040 ≈ 2026-04-28 UTC against system clock 2026-05-21). Backfills must run with `--since 0`; narrow date windows near "now" silently return 0 rows. Biggest gotcha for downstream phases.
  - GraphQL `or` requires filters on each branch (`{ or: [{maker, timestamp_gte, id_gt}, {taker, timestamp_gte, id_gt}] }`); flat-with-`or` is rejected.

### 1.5 Other (probably won't need in v1)
- **Polygon RPC** — direct on-chain reads. Last resort.
- **Dune** — `polymarket_polygon.market_trades` table is the canonical activity dataset; used by competitor analytics. SQL access via Dune API. Optional.
- **Allium / Goldsky CryptoHouse** — paid SQL access.
- Source: [Polymarket blockchain data resources](https://docs.polymarket.com/resources/blockchain-data)

---

## 2. Known smart-money cluster: the Théo wallets

A French ex-Wall Street trader operated **11 coordinated accounts** that turned ~$30M of bets into ~$85M of profits on the 2024 US election. Chainalysis linked the 11 wallets via funding flows and synchronized trading. Publicly identified usernames:

| Username | Lifetime PnL (approx.) | Leaderboard rank |
|---|---|---|
| Theo4 | +$22.05M | #1 |
| Fredi9999 | +$16.62M | #2 |
| PrincessCaro | +$6.08M | #8 |
| Michie | (not stated) | #14 |
| + 7 unidentified accounts | — | — |

These are our **gold-standard smart-money labels** for calibration. Wallet addresses are not in public reporting but are discoverable by clicking a leaderboard profile on `polymarket.com/leaderboard/overall/all/profit`.

Sources: [Entrepreneur — Trump Whale Théo](https://www.entrepreneur.com/business-news/how-trump-whale-theo-made-48-million-neighbor-effect/482539), [Cointrenches — Fredi9999](https://cointrenches.io/fredi9999-polymarket-trump-election-whale-85m/), [Cointelegraph — French whale WSJ](https://cointelegraph.com/news/polymarket-french-whale-donald-trump-election-odds)

**Implication for scoring:** the cluster trades large size relative to typical book depth, takes meaningful slippage (urgency), holds through to resolution (not round-tripping), and trades same-direction across linked wallets. A good `non_arb_score` and `non_mm_score` should rate these as high (i.e., not arb/MM); a good `conviction_size_score` should rate them high.

---

## 3. Arbitrage / MM signatures (the anti-signals)

Detailed empirical characterization of bot behavior on Polymarket:

- **Arb windows shrank from 12.3s (2024) to 2.7s (2025)**, and **73% of arb profits are captured by sub-100ms bots**. Source: [Automated Trading on Polymarket — QuantVPS](https://www.quantvps.com/blog/automated-trading-polymarket)
- **In 5–15 minute markets, automated addresses control 55–62% of volume**. April 2024 → April 2025, arb traders earned >$40M. Source: [Polymarket blockchain data — Dune fast markets](https://dune.com/blog/polymarket-fast-markets)
- **Mechanics:** simultaneous buys of YES and NO when YES+NO < $1 — the round-trip is fast and the wallet trades both legs.
- **Copy-trade bots** typically use 5–30s randomized delay after a target wallet trades. Source: [QuantVPS automated trading](https://www.quantvps.com/blog/automated-trading-polymarket)

**Implication for scoring — anti-signals (component renamed so high = good):**
- `non_arb_score` low when: same wallet trades opposite sides of the same market within seconds, OR trades both YES and NO of complementary markets within seconds.
- `non_mm_score` low when: wallet posts both sides of an orderbook repeatedly with quick turnover (we won't see passive maker quotes directly in trades, but we can infer from buy/sell mix and inventory turnover).
- `organic_price_score` low when: the trade lands within a tight band around mid-price (suggesting MM/arb fill at fair value, not conviction).

**[CALIBRATE]** the round-trip window (seconds) and the mid-price proximity band (bps) against our labeled set.

---

## 4. Existing copy-trade tooling — what competitors do

| Tool | Notable mechanic | Source |
|---|---|---|
| **PolyTrack** | "Cluster detection" (funding + timing + behavior); severity scores per anomaly; fresh-wallet flag | [PolyTrack whale tracker](https://www.polytrackhq.app/blog/polytrack-best-whale-tracker), [Detect insider trading](https://www.polytrackhq.app/blog/detect-insider-trading-polymarket) |
| **Kreo / KreoPoly** | Telegram-first, sub-second mirror, win-rate filtering | [KreoPoly](https://kreopoly.app/) |
| **DropsBot** | Wallet→trader name mapping; per-trader push alerts | [DropsBot research](https://dropstab.com/research/product/polymarket-telegram-bot) |
| **Polycop / Polygun** | Sub-second replication; advertised "high-win-rate wallet" focus | [Solana Levelup roundup](https://medium.com/@gemQueenx/best-polymarket-bots-for-copy-trade-and-sniper-on-web-and-telegram-4992d9f24004) |
| **PolyTradeAlerts** | USDC-size filter; Telegram alerts | [PolyTradeAlerts](https://www.polytradealerts.com/) |
| **Kalshi "Poirot"** | Pattern-recognition for suspicious trades; 200+ investigations in 2024 (reference for what "insider trade" patterns look like even though it's a different platform) | [SI — protect prediction markets](https://www.si.com/betting/prediction-market/kalshi/inside-the-battle-to-protect-prediction-markets-from-insider-trading) |

**Common detection levers we should mirror:**
1. Wallet age + total trade count (the "fresh wallet" flag)
2. Bet size vs. account history (conviction)
3. Position timing relative to news / resolution
4. Historical win rate on **resolved** markets only (luck-vs-skill cut)
5. Wallet clustering by funding flow & coordinated timing

---

## 5. The 9 sub-scores — research-backed rationale

Each component returns 0–100 where **higher = more copyable**. Anti-signals are inverted so the weighted sum stays sign-consistent. Pure arithmetic, no model.

| Sub-score | Signal | Computation sketch | Source |
|---|---|---|---|
| `wallet_pnl_score` | Realized PnL on resolved markets | Rank wallet PnL into 0–100 vs. all observed wallets with ≥5 resolved bets | "Historical performance to distinguish luck from skill" — PolyTrack |
| `wallet_winrate_score` | Win rate, resolved markets only | Wilson lower bound at 95% CI to penalize small samples | Standard practice for noisy rate estimates |
| `history_depth_score` | Sample-size confidence | log(trade_count) saturating at ~100 trades → 0–100 | Cold-start mitigation; also feeds top-level `confidence` field |
| `conviction_size_score` | This trade vs. wallet's median size | `min(100, 50 * size_ratio)` where ratio = trade_size / wallet_median_size | Théo pattern: outsized bets relative to baseline |
| `time_to_resolution_score` | Insider edge concentrates near events | Higher when `hours_to_resolution` < 168h (1 week), tapered | Kalshi Poirot heuristic; news-window concentration |
| `price_impact_score` | Willingness to take slippage | Effective price vs. mid at entry, in bps; higher slippage → higher score | Conviction signal — bots avoid slippage |
| `non_arb_score` | NOT an arb bot | 100 minus penalty for round-trip in same market within N seconds (N **[CALIBRATE]**, start 60s) | QuantVPS arb-window data |
| `non_mm_score` | NOT a market maker | 100 minus penalty for high two-sided turnover ratio in last K trades (K **[CALIBRATE]**, start 50) | Dune fast-markets bot share |
| `organic_price_score` | NOT trading at fair value | 100 minus penalty when `|price − mid| < B bps` (B **[CALIBRATE]**, start 30bps) | Mid-price clustering = arb/MM fingerprint |

**Default weights (starting point, to be calibrated in Phase 5 backtest):**

```yaml
weights:
  wallet_pnl_score:          0.20
  wallet_winrate_score:      0.15
  history_depth_score:       0.05
  conviction_size_score:     0.15
  time_to_resolution_score:  0.10
  price_impact_score:        0.05
  non_arb_score:             0.10
  non_mm_score:              0.10
  organic_price_score:       0.10
# sums to 1.00
```

These are deliberately conservative defaults that lean on durable PnL + win rate (skill), conviction sizing, and the three anti-bot signals. The backtest in Phase 5 will move weights based on what actually predicts profitable copy trades on a holdout set.

---

## 6. The `confidence` field

`confidence` is **independent of `total`**. A wallet with great stats but only 6 lifetime trades is high-score, low-confidence — your bot can filter on both. Mapping:

- `history_depth_score >= 70` AND `score.total >= 60` → `"high"`
- `history_depth_score >= 40` → `"medium"`
- else → `"low"`

This is the same logic Kalshi's Poirot uses to deprioritize fresh-wallet flags below confidence threshold.

---

## 7. Calibration plan (Phase 5 will execute this)

1. **Label set:** populate `data/labeled-wallets.csv` (seeded in Phase 0; addresses filled in Phase 1 via leaderboard scrape).
2. **Hold out** the last 4–8 weeks of resolved markets as the test set.
3. **Train weights** by maximizing out-of-sample correlation between `score.total` and a forward-looking "copy EV" metric: (would you have made money copying this trade on the close-to-resolution settlement?).
4. **Sanity checks:**
   - Théo cluster trades should consistently score ≥ 75.
   - Round-trip arb trades should consistently score ≤ 25.
   - 5-min weather/sports markets should have median score well below political markets.
5. **Promote** new weights only if out-of-sample EV beats the previous config.

---

## 8. Open questions / things to verify with our own data

- **[CALIBRATE]** What round-trip window catches arb without false-flagging legitimate scalps? (start 60s)
- **[CALIBRATE]** What mid-price band catches MM fills? (start 30bps; will vary by market liquidity)
- **[CALIBRATE]** Does `time_to_resolution_score` actually correlate with EV, or is it noise outside of major events?
- **[CALIBRATE]** Does Théo cluster's behavior generalize, or is it idiosyncratic to a single trader's style?
- **Open data question:** Is the Polymarket subgraph queryable without an API key, or do we need Goldsky / TheGraph creds? Resolve in Phase 1.

---

## 9. Decisions locked from this research

1. **Real-time trade source:** RTDS `wss://ws-live-data.polymarket.com` (activity / trades). Not the CLOB user channel.
2. **Market metadata source:** Gamma API. Honor rate limits with a token bucket.
3. **Wallet history source:** Polymarket subgraph (Goldsky), backfilled on laptop.
4. **Score architecture:** 9 sub-scores → weighted sum → 0–100, plus independent `confidence`. Hot path is precomputed-stats lookup; the heavy work happens in background refresh jobs.
5. **No ML, no LLM.** Pure arithmetic. Weights in YAML, hot-reloadable.
6. **Labeled wallet set:** seeded with Théo cluster + leaderboard top wallets (smart money) + manually-flagged arb wallets identified via behavior (round-trip + mid-price + sub-second turnover) once we have data.
