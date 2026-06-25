"""Phase 6 - per-wallet stats refresh.

Populates the `wallet_stats` table the score engine reads on the hot path. Until
this job runs, every scored wallet falls back to neutral defaults (the Phase 3
known gap); once it runs, `wallet_pnl_score`, `wallet_winrate_score`,
`conviction_size_score`, `non_arb_score`, and `non_mm_score` become real.

Everything here is derived from already-ingested `trades` joined to resolved
`markets` - no network. It's the "cold path" from the architecture diagram: slow
is fine, it runs daily (a systemd timer on the Pi, or `--loop` on a laptop).

Realized PnL is computed per fill to settlement, long for BUY / short for SELL -
the same convention the backtester copies (see `backtest.copy_pnl_usdc`). We do
**not** reconstruct FIFO inventory or cost basis; each fill is scored
independently against its market's resolved outcome. That's a deliberate
simplification: it's deterministic, needs only data we already have, and matches
how the copy-score treats a trade in isolation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from whitewhale.backtest import copy_pnl_usdc, settlement_value

THIRTY_DAYS_SECONDS = 30 * 24 * 3600
DEFAULT_ROUND_TRIP_WINDOW_SECONDS = 60.0

# Columns the upsert writes, in order. `mid_price_proximity_30d` has no historical
# mid to compute against (live orderbook is a separate deferred gap) and the engine
# doesn't read it, so it stays 0.
_STATS_COLUMNS = (
    "trade_count",
    "resolved_trade_count",
    "realized_pnl_usdc",
    "win_count",
    "loss_count",
    "median_size_usdc",
    "p90_size_usdc",
    "round_trip_count_30d",
    "two_sided_ratio_30d",
    "mid_price_proximity_30d",
)


def _percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1). 0 for an empty series."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(s):
        return float(s[-1])
    return float(s[lo] + (s[lo + 1] - s[lo]) * frac)


def resolution_map(conn: sqlite3.Connection) -> dict[str, int]:
    """condition_id -> winning outcome index, for every resolved market."""
    return {
        row["condition_id"]: row["outcome_resolved"]
        for row in conn.execute(
            "SELECT condition_id, outcome_resolved FROM markets "
            "WHERE resolved = 1 AND outcome_resolved IS NOT NULL"
        )
    }


def compute_wallet_stats(
    rows: Sequence[sqlite3.Row],
    resolution: dict[str, int],
    *,
    as_of: datetime,
    round_trip_window_seconds: float = DEFAULT_ROUND_TRIP_WINDOW_SECONDS,
    window_seconds: int = THIRTY_DAYS_SECONDS,
) -> dict[str, float]:
    """Compute one wallet's stats from its trade rows. Pure over `rows`.

    `rows` must be every trade for the wallet, ordered by time. Each row needs
    occurred_at, condition_id, side, price, size_usdc, size_shares, outcome_index.
    """
    sizes = [r["size_usdc"] for r in rows]
    realized_pnl = 0.0
    resolved_count = win = loss = 0

    for r in rows:
        winning = resolution.get(r["condition_id"])
        # Sentinel outcome (-1) or degenerate price can't be settled.
        if winning is None or r["outcome_index"] < 0 or not 0.0 < r["price"] < 1.0:
            continue
        settlement = settlement_value(r["outcome_index"], winning)
        pnl = copy_pnl_usdc(r["side"], r["price"], r["size_shares"], settlement)
        realized_pnl += pnl
        resolved_count += 1
        if pnl > 0:
            win += 1
        elif pnl < 0:
            loss += 1

    cutoff = as_of - timedelta(seconds=window_seconds)
    recent = [r for r in rows if _parse(r["occurred_at"]) >= cutoff]
    round_trips, two_sided_ratio = _churn_signals(recent, round_trip_window_seconds)

    return {
        "trade_count": len(rows),
        "resolved_trade_count": resolved_count,
        "realized_pnl_usdc": realized_pnl,
        "win_count": win,
        "loss_count": loss,
        "median_size_usdc": _percentile(sizes, 0.5),
        "p90_size_usdc": _percentile(sizes, 0.9),
        "round_trip_count_30d": round_trips,
        "two_sided_ratio_30d": two_sided_ratio,
        "mid_price_proximity_30d": 0.0,
    }


def _churn_signals(
    recent: Sequence[sqlite3.Row], round_trip_window_seconds: float
) -> tuple[int, float]:
    """Arb/MM proxies over a wallet's recent trades.

    - round_trip_count: fast direction flips in the same market (a BUY then SELL,
      or vice versa, within `round_trip_window_seconds`) - the arb fingerprint.
    - two_sided_ratio: share of recent trades that sit in a market where the
      wallet traded *both* sides at all - the market-maker fingerprint.
    """
    by_market: dict[str, list[sqlite3.Row]] = {}
    for r in recent:
        by_market.setdefault(r["condition_id"], []).append(r)

    round_trips = 0
    two_sided_trades = 0
    for trades in by_market.values():
        sides = {t["side"] for t in trades}
        if len(sides) > 1:
            two_sided_trades += len(trades)
        ordered = sorted(trades, key=lambda t: _parse(t["occurred_at"]))
        for prev, cur in zip(ordered, ordered[1:]):
            dt = (_parse(cur["occurred_at"]) - _parse(prev["occurred_at"])).total_seconds()
            if cur["side"] != prev["side"] and dt <= round_trip_window_seconds:
                round_trips += 1

    ratio = two_sided_trades / len(recent) if recent else 0.0
    return round_trips, ratio


def refresh_wallet_stats(
    conn: sqlite3.Connection,
    *,
    as_of: datetime | None = None,
    wallets: Sequence[str] | None = None,
    only_stale: bool = True,
    round_trip_window_seconds: float = DEFAULT_ROUND_TRIP_WINDOW_SECONDS,
) -> int:
    """Recompute and upsert `wallet_stats`. Returns the number of wallets refreshed.

    Targeting: an explicit `wallets` list, else every wallet seen in `trades`. With
    `only_stale` (the default for the daily job) a wallet is skipped unless it has
    no stats row or a trade newer than its last refresh - so reruns are cheap.
    """
    as_of = as_of or datetime.now(timezone.utc)
    resolution = resolution_map(conn)
    targets = _select_wallets(conn, wallets, only_stale)

    refreshed = 0
    for wallet in targets:
        rows = conn.execute(
            """
            SELECT occurred_at, condition_id, side, price, size_usdc,
                   size_shares, outcome_index
            FROM trades WHERE wallet = ? ORDER BY occurred_at, log_index
            """,
            (wallet,),
        ).fetchall()
        if not rows:
            continue
        stats = compute_wallet_stats(
            rows, resolution, as_of=as_of, round_trip_window_seconds=round_trip_window_seconds
        )
        _upsert(conn, wallet, stats, as_of)
        refreshed += 1
    return refreshed


def _select_wallets(
    conn: sqlite3.Connection, wallets: Sequence[str] | None, only_stale: bool
) -> list[str]:
    if wallets is not None:
        return [w for w in wallets if w]
    if not only_stale:
        return [
            row["wallet"]
            for row in conn.execute(
                "SELECT DISTINCT wallet FROM trades WHERE wallet IS NOT NULL"
            )
        ]
    # Stale = no stats row, or a trade newer than the row's updated_at.
    return [
        row["wallet"]
        for row in conn.execute(
            """
            SELECT t.wallet AS wallet
            FROM trades t
            LEFT JOIN wallet_stats s ON s.wallet = t.wallet
            WHERE t.wallet IS NOT NULL
            GROUP BY t.wallet
            HAVING s.updated_at IS NULL OR MAX(t.occurred_at) > s.updated_at
            """
        )
    ]


def _upsert(
    conn: sqlite3.Connection, wallet: str, stats: dict[str, float], as_of: datetime
) -> None:
    cols = ", ".join(_STATS_COLUMNS)
    placeholders = ", ".join("?" for _ in _STATS_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _STATS_COLUMNS)
    conn.execute(
        f"""
        INSERT INTO wallet_stats (wallet, {cols}, updated_at)
        VALUES (?, {placeholders}, ?)
        ON CONFLICT(wallet) DO UPDATE SET {updates}, updated_at = excluded.updated_at
        """,
        (wallet, *(stats[c] for c in _STATS_COLUMNS), as_of.isoformat()),
    )


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)
