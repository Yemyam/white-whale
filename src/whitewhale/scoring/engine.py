"""Copy-score engine: assemble inputs, run the components, weight, classify.

`score_trade` is pure (ScoreInputs -> ScoreResult) and is the unit-test surface.
`build_inputs` / `score_whale_event` do the SQLite lookups that turn a whale
event into a ScoreInputs (wallet stats, market mid, cross-wallet PnL percentile).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from whitewhale.filter import WhaleEvent
from whitewhale.scoring.components import COMPONENTS
from whitewhale.scoring.inputs import ScoreInputs, ScoreResult, ScoringConfig


def score_trade(inp: ScoreInputs, cfg: ScoringConfig) -> ScoreResult:
    """Run all nine components, take the config-weighted sum, classify confidence."""
    components: dict[str, float] = {}
    rationale: list[str] = []
    for name, fn in COMPONENTS.items():
        value, why = fn(inp, cfg)
        components[name] = value
        if why:
            rationale.append(why)

    total = sum(cfg.weights[name] * value for name, value in components.items())
    confidence = _confidence(components["history_depth_score"], total, cfg)

    return ScoreResult(
        total=round(total),
        confidence=confidence,
        components={name: round(value) for name, value in components.items()},
        rationale=rationale,
    )


def _confidence(history_depth: float, total: float, cfg: ScoringConfig) -> str:
    """research-notes §6: confidence is independent of the total score."""
    if history_depth >= cfg.conf_high_depth and total >= cfg.conf_high_total:
        return "high"
    if history_depth >= cfg.conf_medium_depth:
        return "medium"
    return "low"


# --- DB-facing assembly --------------------------------------------------------

_STATS_FIELDS = (
    "trade_count",
    "resolved_trade_count",
    "realized_pnl_usdc",
    "win_count",
    "loss_count",
    "median_size_usdc",
    "round_trip_count_30d",
    "two_sided_ratio_30d",
)


def build_inputs(
    conn: sqlite3.Connection,
    cfg: ScoringConfig,
    *,
    wallet: str | None,
    condition_id: str,
    trade_price: float,
    size_usdc: float,
    occurred_at: datetime,
) -> ScoreInputs:
    """Resolve a trade into a fully-populated ScoreInputs from SQLite."""
    stats_row = None
    if wallet:
        stats_row = conn.execute(
            f"SELECT {', '.join(_STATS_FIELDS)} FROM wallet_stats WHERE wallet = ?",
            (wallet,),
        ).fetchone()

    stats = {f: (stats_row[f] if stats_row is not None else 0) for f in _STATS_FIELDS}

    market = conn.execute(
        "SELECT current_price, resolves_at FROM markets WHERE condition_id = ?",
        (condition_id,),
    ).fetchone()
    mid_price = market["current_price"] if market is not None else None
    hours_to_resolution = _hours_to_resolution(
        market["resolves_at"] if market is not None else None, occurred_at
    )

    pnl_percentile = _pnl_percentile(
        conn, stats["realized_pnl_usdc"], stats["resolved_trade_count"], cfg.min_resolved_bets
    )

    return ScoreInputs(
        trade_price=trade_price,
        size_usdc=size_usdc,
        trade_count=stats["trade_count"],
        resolved_trade_count=stats["resolved_trade_count"],
        realized_pnl_usdc=stats["realized_pnl_usdc"],
        win_count=stats["win_count"],
        loss_count=stats["loss_count"],
        median_size_usdc=stats["median_size_usdc"],
        round_trip_count_30d=stats["round_trip_count_30d"],
        two_sided_ratio_30d=stats["two_sided_ratio_30d"],
        pnl_percentile=pnl_percentile,
        hours_to_resolution=hours_to_resolution,
        mid_price=mid_price,
    )


def score_whale_event(
    conn: sqlite3.Connection, event: WhaleEvent, cfg: ScoringConfig
) -> ScoreResult:
    """Convenience: score a Phase 2 WhaleEvent straight from the DB."""
    inputs = build_inputs(
        conn,
        cfg,
        wallet=event.wallet,
        condition_id=event.condition_id,
        trade_price=event.price,
        size_usdc=event.size_usdc,
        occurred_at=event.occurred_at,
    )
    return score_trade(inputs, cfg)


def _hours_to_resolution(resolves_at: str | None, occurred_at: datetime) -> float | None:
    if not resolves_at:
        return None
    try:
        resolves = datetime.fromisoformat(resolves_at)
    except ValueError:
        return None
    return (resolves - occurred_at).total_seconds() / 3600.0


def _pnl_percentile(
    conn: sqlite3.Connection, pnl: float, resolved_count: int, min_resolved: int
) -> float | None:
    """Fraction of rankable wallets whose realized PnL is <= this wallet's.

    Only wallets with >= min_resolved resolved bets are in the population, and a
    wallet itself must clear that bar to be ranked (else None -> neutral score).
    """
    if resolved_count < min_resolved:
        return None
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN realized_pnl_usdc <= ? THEN 1 ELSE 0 END) AS le
        FROM wallet_stats
        WHERE resolved_trade_count >= ?
        """,
        (pnl, min_resolved),
    ).fetchone()
    n = row["n"]
    if not n:
        return None
    return (row["le"] or 0) / n
