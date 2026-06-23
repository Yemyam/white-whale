"""Phase 2 - whale filter.

Selects whale events from the firehose of ingested trades. A trade is a whale
event when it clears two configurable floors:

    size_usdc >= min_size_usdc AND market.liquidity_usdc >= min_market_liquidity_usdc

and isn't a duplicate of a recently accepted event for the same wallet+market
(a whale scaling in/out of one position would otherwise emit a burst).

`passes_thresholds` is a pure predicate (easy to unit-test and reuse on the hot
path). `WhaleFilter` layers stateful dedupe on top for the live stream.
`iter_whale_trades` runs the same logic as a batch scan over the DB - useful for
inspecting what Phase 2 would emit against already-ingested data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class WhaleConfig:
    """Tunable knobs for the whale filter, loaded from the `whale_filter` block."""

    min_size_usdc: float
    min_market_liquidity_usdc: float
    dedupe_window_seconds: float
    allow_unknown_liquidity: bool = False

    @classmethod
    def from_config(cls, cfg: dict) -> WhaleConfig:
        wf = cfg["whale_filter"]
        return cls(
            min_size_usdc=float(wf["min_size_usdc"]),
            min_market_liquidity_usdc=float(wf["min_market_liquidity_usdc"]),
            dedupe_window_seconds=float(wf.get("dedupe_window_seconds", 90)),
            allow_unknown_liquidity=bool(wf.get("allow_unknown_liquidity", False)),
        )


@dataclass
class WhaleEvent:
    """A trade that passed the whale filter, joined with its market liquidity."""

    tx_hash: str
    log_index: int
    occurred_at: datetime
    wallet: str | None
    condition_id: str
    side: str
    outcome: str
    price: float
    size_usdc: float
    market_liquidity_usdc: float | None


def passes_thresholds(
    size_usdc: float,
    market_liquidity_usdc: float | None,
    config: WhaleConfig,
) -> bool:
    """True if the trade clears both the size and market-liquidity floors.

    Unknown liquidity (market not yet enriched) can't satisfy the floor, so it
    fails unless `allow_unknown_liquidity` is set.
    """
    if size_usdc < config.min_size_usdc:
        return False
    if market_liquidity_usdc is None:
        return config.allow_unknown_liquidity
    return market_liquidity_usdc >= config.min_market_liquidity_usdc


class WhaleFilter:
    """Stateful whale filter for the live trade stream.

    Holds the last-accepted timestamp per (wallet, condition_id) so repeated
    fills inside `dedupe_window_seconds` collapse to a single event. The window
    is measured from the last *accepted* event, so a wallet churning a position
    for minutes yields at most one event per window rather than going silent.
    """

    def __init__(self, config: WhaleConfig) -> None:
        self.config = config
        self._last_accepted: dict[tuple[str | None, str], datetime] = {}

    def accept(
        self,
        *,
        wallet: str | None,
        condition_id: str,
        size_usdc: float,
        market_liquidity_usdc: float | None,
        occurred_at: datetime,
    ) -> bool:
        """Decide whether this trade should surface as a whale event."""
        if not passes_thresholds(size_usdc, market_liquidity_usdc, self.config):
            return False

        key = (wallet, condition_id)
        prev = self._last_accepted.get(key)
        if (
            prev is not None
            and (occurred_at - prev).total_seconds() < self.config.dedupe_window_seconds
        ):
            return False

        self._last_accepted[key] = occurred_at
        return True


def iter_whale_trades(
    conn: sqlite3.Connection,
    config: WhaleConfig,
    *,
    since_iso: str | None = None,
) -> Iterator[WhaleEvent]:
    """Replay ingested trades through the whale filter in chronological order.

    Pre-filters on `size_usdc` in SQL (hits idx_trades_size); the liquidity
    floor and dedupe run in Python so the logic matches the live path exactly.
    """
    params: list[object] = [config.min_size_usdc]
    since_clause = ""
    if since_iso:
        since_clause = "AND t.occurred_at >= ?"
        params.append(since_iso)

    sql = f"""
        SELECT t.tx_hash, t.log_index, t.occurred_at, t.wallet, t.condition_id,
               t.side, t.outcome, t.price, t.size_usdc, m.liquidity_usdc
        FROM trades t
        LEFT JOIN markets m ON m.condition_id = t.condition_id
        WHERE t.size_usdc >= ? {since_clause}
        ORDER BY t.occurred_at, t.tx_hash, t.log_index
    """

    wf = WhaleFilter(config)
    for row in conn.execute(sql, params):
        occurred_at = datetime.fromisoformat(row["occurred_at"])
        if wf.accept(
            wallet=row["wallet"],
            condition_id=row["condition_id"],
            size_usdc=row["size_usdc"],
            market_liquidity_usdc=row["liquidity_usdc"],
            occurred_at=occurred_at,
        ):
            yield WhaleEvent(
                tx_hash=row["tx_hash"],
                log_index=row["log_index"],
                occurred_at=occurred_at,
                wallet=row["wallet"],
                condition_id=row["condition_id"],
                side=row["side"],
                outcome=row["outcome"],
                price=row["price"],
                size_usdc=row["size_usdc"],
                market_liquidity_usdc=row["liquidity_usdc"],
            )
