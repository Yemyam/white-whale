"""Tests for the Phase 6 wallet_stats refresh job."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from whitewhale import stats
from whitewhale.stats import (
    compute_wallet_stats,
    refresh_wallet_stats,
    resolution_map,
)

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed_market(db, condition_id, *, resolved=1, outcome_resolved=0) -> None:
    now = T0.isoformat()
    db.execute(
        """
        INSERT INTO markets
            (condition_id, slug, question, liquidity_usdc, current_price,
             resolves_at, resolved, outcome_resolved, first_seen, last_seen)
        VALUES (?, ?, 'Q?', 200000, 0.5, ?, ?, ?, ?, ?)
        """,
        (condition_id, f"s-{condition_id}", now, resolved, outcome_resolved, now, now),
    )


def _seed_trade(db, *, tx, condition_id, wallet, side, outcome_index, price, size_usdc,
                occurred_at=T0, log_index=0) -> None:
    db.execute(
        """
        INSERT INTO trades
            (tx_hash, log_index, occurred_at, wallet, condition_id, asset_id,
             outcome, outcome_index, side, price, size_shares, size_usdc)
        VALUES (?, ?, ?, ?, ?, 'tok', ?, ?, ?, ?, ?, ?)
        """,
        (
            tx, log_index, occurred_at.isoformat(), wallet, condition_id,
            "YES" if outcome_index == 0 else "NO", outcome_index, side, price,
            size_usdc / price, size_usdc,
        ),
    )


def _rows(db, wallet):
    return db.execute(
        """
        SELECT occurred_at, condition_id, side, price, size_usdc, size_shares, outcome_index
        FROM trades WHERE wallet = ? ORDER BY occurred_at, log_index
        """,
        (wallet,),
    ).fetchall()


# --- percentile helper ---------------------------------------------------------

def test_percentile_basic() -> None:
    assert stats._percentile([], 0.5) == 0.0
    assert stats._percentile([10], 0.9) == 10
    assert stats._percentile([0, 10], 0.5) == pytest.approx(5.0)
    assert stats._percentile([0, 10, 20, 30, 40], 0.5) == pytest.approx(20.0)
    assert stats._percentile([0, 10, 20, 30, 40], 0.9) == pytest.approx(36.0)


# --- realized PnL / win-loss ---------------------------------------------------

def test_pnl_and_winrate_from_resolved_markets(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)  # YES wins
    _seed_market(db, "0xb", outcome_resolved=1)  # NO wins
    # buy winning YES at 0.40 -> +0.60/share; buy losing YES at 0.70 -> -0.70/share
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.40, size_usdc=4000)   # 10000 shares -> +6000
    _seed_trade(db, tx="0x2", condition_id="0xb", wallet="0xw", side="BUY",
                outcome_index=0, price=0.70, size_usdc=7000)   # 10000 shares -> -7000
    s = compute_wallet_stats(_rows(db, "0xw"), resolution_map(db), as_of=T0)
    assert s["trade_count"] == 2
    assert s["resolved_trade_count"] == 2
    assert s["realized_pnl_usdc"] == pytest.approx(6000 - 7000)
    assert s["win_count"] == 1
    assert s["loss_count"] == 1


def test_unresolved_and_sentinel_excluded_from_pnl(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    _seed_market(db, "0xopen", resolved=0, outcome_resolved=None)
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000)
    _seed_trade(db, tx="0x2", condition_id="0xopen", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000)        # unresolved market
    _seed_trade(db, tx="0x3", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=-1, price=0.4, size_usdc=4000, log_index=1)  # sentinel
    s = compute_wallet_stats(_rows(db, "0xw"), resolution_map(db), as_of=T0)
    assert s["trade_count"] == 3          # all count toward trade_count / sizes
    assert s["resolved_trade_count"] == 1  # only the clean resolved one settles


def test_sizes_use_all_trades(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    for i, size in enumerate([1000, 2000, 3000, 4000, 5000]):
        _seed_trade(db, tx=f"0x{i}", condition_id="0xa", wallet="0xw", side="BUY",
                    outcome_index=0, price=0.5, size_usdc=size, log_index=i)
    s = compute_wallet_stats(_rows(db, "0xw"), resolution_map(db), as_of=T0)
    assert s["median_size_usdc"] == pytest.approx(3000)
    assert s["p90_size_usdc"] == pytest.approx(4600)


# --- churn signals -------------------------------------------------------------

def test_round_trip_counts_fast_flips(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    # BUY then SELL 30s later in the same market = one round trip (<= 60s window)
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.5, size_usdc=5000, occurred_at=T0)
    _seed_trade(db, tx="0x2", condition_id="0xa", wallet="0xw", side="SELL",
                outcome_index=0, price=0.5, size_usdc=5000, occurred_at=T0 + timedelta(seconds=30))
    # a third flip 10 minutes later is too slow to count
    _seed_trade(db, tx="0x3", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.5, size_usdc=5000, occurred_at=T0 + timedelta(minutes=10))
    s = compute_wallet_stats(_rows(db, "0xw"), resolution_map(db), as_of=T0 + timedelta(minutes=10),
                             round_trip_window_seconds=60)
    assert s["round_trip_count_30d"] == 1


def test_two_sided_ratio(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    _seed_market(db, "0xb", outcome_resolved=0)
    # market a: both sides (2 trades) -> two-sided. market b: one side -> not.
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.5, size_usdc=5000)
    _seed_trade(db, tx="0x2", condition_id="0xa", wallet="0xw", side="SELL",
                outcome_index=0, price=0.5, size_usdc=5000, log_index=1)
    _seed_trade(db, tx="0x3", condition_id="0xb", wallet="0xw", side="BUY",
                outcome_index=0, price=0.5, size_usdc=5000)
    s = compute_wallet_stats(_rows(db, "0xw"), resolution_map(db), as_of=T0)
    assert s["two_sided_ratio_30d"] == pytest.approx(2 / 3)


def test_thirty_day_window_excludes_old_trades(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    # old two-sided pair (40 days back) should fall outside the window
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.5, size_usdc=5000, occurred_at=T0 - timedelta(days=40))
    _seed_trade(db, tx="0x2", condition_id="0xa", wallet="0xw", side="SELL",
                outcome_index=0, price=0.5, size_usdc=5000, occurred_at=T0 - timedelta(days=40),
                log_index=1)
    s = compute_wallet_stats(_rows(db, "0xw"), resolution_map(db), as_of=T0)
    assert s["two_sided_ratio_30d"] == 0.0
    assert s["round_trip_count_30d"] == 0


# --- refresh_wallet_stats (DB integration) -------------------------------------

def test_refresh_upserts_and_is_idempotent(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000)
    n = refresh_wallet_stats(db, as_of=T0, only_stale=False)
    assert n == 1
    row = db.execute("SELECT realized_pnl_usdc, trade_count FROM wallet_stats WHERE wallet='0xw'").fetchone()
    assert row["trade_count"] == 1
    assert row["realized_pnl_usdc"] == pytest.approx(6000)
    # rerun overwrites in place (no duplicate rows)
    refresh_wallet_stats(db, as_of=T0 + timedelta(hours=1), only_stale=False)
    assert db.execute("SELECT COUNT(*) FROM wallet_stats").fetchone()[0] == 1


def test_refresh_only_stale_skips_unchanged(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000)
    assert refresh_wallet_stats(db, as_of=T0, only_stale=True) == 1
    # no new trades -> stale pass refreshes nobody
    assert refresh_wallet_stats(db, as_of=T0 + timedelta(hours=1), only_stale=True) == 0
    # a newer trade makes the wallet stale again
    _seed_trade(db, tx="0x2", condition_id="0xa", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000,
                occurred_at=T0 + timedelta(hours=2), log_index=1)
    assert refresh_wallet_stats(db, as_of=T0 + timedelta(hours=3), only_stale=True) == 1


def test_refresh_targets_explicit_wallets(db) -> None:
    _seed_market(db, "0xa", outcome_resolved=0)
    _seed_trade(db, tx="0x1", condition_id="0xa", wallet="0xw1", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000)
    _seed_trade(db, tx="0x2", condition_id="0xa", wallet="0xw2", side="BUY",
                outcome_index=0, price=0.4, size_usdc=4000)
    n = refresh_wallet_stats(db, as_of=T0, wallets=["0xw1"])
    assert n == 1
    assert db.execute("SELECT COUNT(*) FROM wallet_stats").fetchone()[0] == 1
    assert db.execute("SELECT wallet FROM wallet_stats").fetchone()["wallet"] == "0xw1"


def test_refresh_makes_scores_non_neutral(db) -> None:
    # End-to-end: after refresh, build_inputs sees real stats (not zeroed).
    from whitewhale.scoring import ScoringConfig, build_inputs
    from tests.test_scoring_engine import FULL_CFG

    _seed_market(db, "0xa", outcome_resolved=0)
    for i in range(8):
        _seed_trade(db, tx=f"0x{i}", condition_id="0xa", wallet="0xw", side="BUY",
                    outcome_index=0, price=0.4, size_usdc=4000, log_index=i)
    refresh_wallet_stats(db, as_of=T0, only_stale=False)
    cfg = ScoringConfig.from_config(FULL_CFG)
    inputs = build_inputs(db, cfg, wallet="0xw", condition_id="0xa",
                          trade_price=0.4, size_usdc=4000, occurred_at=T0)
    assert inputs.trade_count == 8
    assert inputs.median_size_usdc == pytest.approx(4000)
    assert inputs.win_count == 8
