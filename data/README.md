# data/

Reference data used by White Whale for calibration and label-driven evaluation.

## `labeled-wallets.csv`

Ground-truth labels used to calibrate the copy-score weights (see Phase 5 in `docs/research-notes.md`).

### Schema

| Column | Type | Notes |
|---|---|---|
| `wallet_address` | `0x…` 42-char hex | Polygon address. May be empty in seed rows — filled in Phase 1 from Polymarket leaderboard scraper. |
| `display_name` | string | Polymarket username (may change; address is the canonical key). |
| `label` | enum | `smart_money` \| `arb_bot` \| `mm_bot` \| `insider_cluster` \| `retail` \| `unknown` |
| `cluster_id` | string | Optional grouping key (e.g., `theo_cluster`). Multiple rows with the same `cluster_id` mark a coordinated wallet group. |
| `source` | string | Where the label came from (`polymarket_leaderboard`, `chainalysis_report`, `manual_observation`, etc.). |
| `confidence` | enum | `high` \| `medium` \| `low`. How sure we are of the label. |
| `notes` | string | Free-text evidence / context. |

### Seeded rows

The file ships with the four publicly-identified members of the Théo cluster (Theo4, Fredi9999, PrincessCaro, Michie). The remaining 7 cluster wallets and the broader leaderboard top 100 will be added in Phase 1 by a scraper that:

1. Walks `polymarket.com/leaderboard/overall/all/profit` (and the by-window leaderboards).
2. Resolves each profile to its Polygon address via the Polymarket public profile API or by inspecting the page.
3. Optionally re-runs Chainalysis-style cluster heuristics (shared funding wallet, synchronized first-trade timestamps) to mark `cluster_id`.

Arb / MM wallets are **not** seeded here. They will be auto-flagged in Phase 3 by behavioral heuristics (round-trip detection, two-sided turnover, mid-price clustering) and graduated into this file with `source=behavioral` once we have enough evidence.

### How this file is used

- **Phase 3 scoring:** read at startup to give known wallets a `cluster_id` and a label-derived prior on `confidence`.
- **Phase 5 backtest:** the labeled set is the ground truth against which we measure how well the score separates smart money from bots. Weights that score Théo-cluster trades ≥ 75 and round-trip arb trades ≤ 25 on the holdout set win.

### Adding entries by hand

Append a row. Lowercase the address. Keep `notes` short but evidence-cited. If you're inferring (not certain), set `confidence: low` and explain in `notes`.
