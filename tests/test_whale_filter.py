"""Tests for the Phase 2 whale filter: thresholds, dedupe, and DB scan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from whitewhale.filter import (
    WhaleConfig,
    WhaleFilter,
    iter_whale_trades,
    passes_thresholds,
)

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_config(**overrides) -> WhaleConfig:
    base = dict(
        min_size_usdc=5000.0,
        min_market_liquidity_usdc=50000.0,
        dedupe_window_seconds=90.0,
        allow_unknown_liquidity=False,
    )
    base.update(overrides)
    return WhaleConfig(**base)


def test_from_config_reads_whale_filter_block() -> None:
    cfg = {
        "whale_filter": {
            "min_size_usdc": 1000,
            "min_market_liquidity_usdc": 20000,
            "dedupe_window_seconds": 30,
            "allow_unknown_liquidity": True,
        }
    }
    wc = WhaleConfig.from_config(cfg)
    assert wc.min_size_usdc == 1000.0
    assert wc.min_market_liquidity_usdc == 20000.0
    assert wc.dedupe_window_seconds == 30.0
    assert wc.allow_unknown_liquidity is True


def test_from_config_defaults_optional_keys() -> None:
    wc = WhaleConfig.from_config(
        {"whale_filter": {"min_size_usdc": 5000, "min_market_liquidity_usdc": 50000}}
    )
    assert wc.dedupe_window_seconds == 90.0
    assert wc.allow_unknown_liquidity is False


@pytest.mark.parametrize(
    "size, liq, expected",
    [
        (6000, 60000, True),   # both clear
        (4000, 60000, False),  # size too small
        (6000, 40000, False),  # liquidity too thin
        (5000, 50000, True),   # exactly at the floors
    ],
)
def test_passes_thresholds(size, liq, expected) -> None:
    assert passes_thresholds(size, liq, make_config()) is expected


def test_passes_thresholds_unknown_liquidity_blocked_by_default() -> None:
    assert passes_thresholds(10000, None, make_config()) is False


def test_passes_thresholds_unknown_liquidity_allowed_when_configured() -> None:
    cfg = make_config(allow_unknown_liquidity=True)
    assert passes_thresholds(10000, None, cfg) is True
    # still gated on size
    assert passes_thresholds(1000, None, cfg) is False


def test_filter_accepts_then_dedupes_within_window() -> None:
    wf = WhaleFilter(make_config())
    common = dict(condition_id="0xmkt", size_usdc=6000, market_liquidity_usdc=60000)

    assert wf.accept(wallet="0xw", occurred_at=T0, **common) is True
    # 60s later, same wallet+market: still inside the 90s window -> dropped
    assert wf.accept(wallet="0xw", occurred_at=T0 + timedelta(seconds=60), **common) is False
    # 100s after the accepted event: window elapsed -> accepted again
    assert wf.accept(wallet="0xw", occurred_at=T0 + timedelta(seconds=100), **common) is True


def test_filter_window_measured_from_last_accepted() -> None:
    wf = WhaleFilter(make_config())
    common = dict(condition_id="0xmkt", size_usdc=6000, market_liquidity_usdc=60000)
    assert wf.accept(wallet="0xw", occurred_at=T0, **common) is True
    # churn every 60s should not reset the window; second is dropped, and the
    # third (120s after accept) is the next accepted one.
    assert wf.accept(wallet="0xw", occurred_at=T0 + timedelta(seconds=60), **common) is False
    assert wf.accept(wallet="0xw", occurred_at=T0 + timedelta(seconds=120), **common) is True


def test_filter_dedupe_keyed_per_wallet_and_market() -> None:
    wf = WhaleFilter(make_config())
    common = dict(size_usdc=6000, market_liquidity_usdc=60000, occurred_at=T0)
    assert wf.accept(wallet="0xw", condition_id="0xa", **common) is True
    # different market, same instant -> not a dupe
    assert wf.accept(wallet="0xw", condition_id="0xb", **common) is True
    # different wallet, same market -> not a dupe
    assert wf.accept(wallet="0xother", condition_id="0xa", **common) is True


def test_filter_rejects_below_threshold_without_touching_dedupe() -> None:
    wf = WhaleFilter(make_config())
    # too small -> rejected and NOT recorded, so a later qualifying trade passes
    assert wf.accept(
        wallet="0xw", condition_id="0xa", size_usdc=100,
        market_liquidity_usdc=60000, occurred_at=T0,
    ) is False
    assert wf.accept(
        wallet="0xw", condition_id="0xa", size_usdc=6000,
        market_liquidity_usdc=60000, occurred_at=T0 + timedelta(seconds=1),
    ) is True


def _insert_market(db, condition_id: str, liquidity: float | None) -> None:
    now = T0.isoformat()
    db.execute(
        "INSERT INTO markets (condition_id, liquidity_usdc, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?)",
        (condition_id, liquidity, now, now),
    )


def _insert_trade(
    db, tx: str, wallet: str, condition_id: str, size_usdc: float, occurred_at: datetime
) -> None:
    db.execute(
        """
        INSERT INTO trades
            (tx_hash, log_index, occurred_at, wallet, condition_id, asset_id,
             outcome, outcome_index, side, price, size_shares, size_usdc)
        VALUES (?, 0, ?, ?, ?, 'asset', 'YES', 0, 'BUY', 0.5, ?, ?)
        """,
        (tx, occurred_at.isoformat(), wallet, condition_id, size_usdc * 2, size_usdc),
    )


def test_iter_whale_trades_applies_thresholds_and_dedupe(db) -> None:
    _insert_market(db, "0xliquid", 60000)
    _insert_market(db, "0xthin", 10000)

    # qualifies
    _insert_trade(db, "0x1", "0xw", "0xliquid", 6000, T0)
    # same wallet+market 30s later -> deduped away
    _insert_trade(db, "0x2", "0xw", "0xliquid", 7000, T0 + timedelta(seconds=30))
    # liquid market but size too small -> dropped
    _insert_trade(db, "0x3", "0xw", "0xliquid", 1000, T0 + timedelta(seconds=200))
    # big trade but market liquidity too thin -> dropped
    _insert_trade(db, "0x4", "0xw", "0xthin", 9000, T0 + timedelta(seconds=300))
    # 100s after the first accept, same wallet+market -> accepted again
    _insert_trade(db, "0x5", "0xw", "0xliquid", 8000, T0 + timedelta(seconds=120))

    events = list(iter_whale_trades(db, make_config()))
    txs = [e.tx_hash for e in events]
    assert txs == ["0x1", "0x5"]
    assert events[0].market_liquidity_usdc == 60000


def test_iter_whale_trades_unknown_liquidity_dropped_by_default(db) -> None:
    # trade on a market we've never enriched (no markets row) -> NULL liquidity
    _insert_trade(db, "0x1", "0xw", "0xunknown", 9000, T0)
    assert list(iter_whale_trades(db, make_config())) == []
    allowed = list(iter_whale_trades(db, make_config(allow_unknown_liquidity=True)))
    assert [e.tx_hash for e in allowed] == ["0x1"]


def test_iter_whale_trades_since_filter(db) -> None:
    _insert_market(db, "0xliquid", 60000)
    _insert_trade(db, "0xold", "0xw", "0xliquid", 6000, T0 - timedelta(days=2))
    _insert_trade(db, "0xnew", "0xw", "0xliquid", 6000, T0)

    since = (T0 - timedelta(days=1)).isoformat()
    events = list(iter_whale_trades(db, make_config(), since_iso=since))
    assert [e.tx_hash for e in events] == ["0xnew"]
