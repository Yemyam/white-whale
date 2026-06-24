"""Tests for the score engine: config parsing, weighting, confidence, DB assembly."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from whitewhale.scoring import ScoreInputs, ScoringConfig, build_inputs, score_trade
from whitewhale.scoring.engine import _hours_to_resolution, _pnl_percentile

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

FULL_CFG = {
    "scoring": {
        "weights": {
            "wallet_pnl_score": 0.20,
            "wallet_winrate_score": 0.15,
            "history_depth_score": 0.05,
            "conviction_size_score": 0.15,
            "time_to_resolution_score": 0.10,
            "price_impact_score": 0.05,
            "non_arb_score": 0.10,
            "non_mm_score": 0.10,
            "organic_price_score": 0.10,
        },
        "thresholds": {"mid_price_band_bps": 30},
        "params": {"min_resolved_bets": 5, "neutral_score": 50},
        "confidence": {"high_depth": 70, "high_total": 60, "medium_depth": 40},
    }
}


# --- ScoringConfig.from_config -------------------------------------------------

def test_from_config_parses_block() -> None:
    c = ScoringConfig.from_config(FULL_CFG)
    assert c.weights["wallet_pnl_score"] == 0.20
    assert c.min_resolved_bets == 5
    assert c.mid_price_band_bps == 30
    assert c.conf_high_depth == 70


def test_from_config_rejects_missing_component() -> None:
    broken = {"scoring": {"weights": {"wallet_pnl_score": 1.0}}}
    with pytest.raises(ValueError, match="missing components"):
        ScoringConfig.from_config(broken)


def test_from_config_rejects_weights_not_summing_to_one() -> None:
    bad = {"scoring": dict(FULL_CFG["scoring"])}
    bad["scoring"] = {**FULL_CFG["scoring"], "weights": {**FULL_CFG["scoring"]["weights"]}}
    bad["scoring"]["weights"]["wallet_pnl_score"] = 0.99
    with pytest.raises(ValueError, match="sum to 1.0"):
        ScoringConfig.from_config(bad)


# --- score_trade: weighting & confidence ---------------------------------------

def _cfg() -> ScoringConfig:
    return ScoringConfig.from_config(FULL_CFG)


def test_score_trade_all_neutral_inputs() -> None:
    # No stats, no market context -> components fall back to neutral (50) or 0.
    result = score_trade(ScoreInputs(trade_price=0.5, size_usdc=10000), _cfg())
    assert 0 <= result.total <= 100
    assert set(result.components) == set(FULL_CFG["scoring"]["weights"])
    # history_depth is 0 (no trades) -> confidence low.
    assert result.components["history_depth_score"] == 0
    assert result.confidence == "low"


def test_score_trade_weighted_sum_matches_components() -> None:
    cfg = _cfg()
    inputs = ScoreInputs(
        trade_price=0.55,
        size_usdc=40000,
        trade_count=200,
        resolved_trade_count=50,
        realized_pnl_usdc=1_000_000,
        win_count=40,
        loss_count=10,
        median_size_usdc=10000,
        round_trip_count_30d=0,
        two_sided_ratio_30d=0.1,
        pnl_percentile=0.95,
        hours_to_resolution=24,
        mid_price=0.50,
    )
    result = score_trade(inputs, cfg)
    expected_total = round(
        sum(cfg.weights[k] * v for k, v in result.components.items())
    )
    # components are rounded ints, so recompute from those for an exact check
    recomputed = round(sum(cfg.weights[k] * v for k, v in result.components.items()))
    assert result.total == recomputed == expected_total
    # a strong wallet on a high-conviction trade should score well.
    assert result.total >= 70


def test_confidence_high_requires_depth_and_total() -> None:
    cfg = _cfg()
    strong = ScoreInputs(
        trade_price=0.55, size_usdc=40000, trade_count=500, resolved_trade_count=80,
        realized_pnl_usdc=5_000_000, win_count=70, loss_count=10, median_size_usdc=10000,
        two_sided_ratio_30d=0.0, pnl_percentile=0.99, hours_to_resolution=12, mid_price=0.50,
    )
    assert score_trade(strong, cfg).confidence == "high"


def test_confidence_medium_on_depth_alone() -> None:
    cfg = _cfg()
    # Deep history but weak signals -> total below high bar, depth above medium bar.
    inputs = ScoreInputs(
        trade_price=0.50, size_usdc=100, trade_count=60, resolved_trade_count=3,
        win_count=1, loss_count=2, median_size_usdc=10000, two_sided_ratio_30d=1.0,
        round_trip_count_30d=25, pnl_percentile=None, hours_to_resolution=None, mid_price=0.50,
    )
    result = score_trade(inputs, cfg)
    assert result.components["history_depth_score"] >= 40
    assert result.confidence == "medium"


# --- DB-facing helpers ---------------------------------------------------------

def test_hours_to_resolution() -> None:
    assert _hours_to_resolution((T0 + timedelta(hours=24)).isoformat(), T0) == pytest.approx(24)
    assert _hours_to_resolution(None, T0) is None
    assert _hours_to_resolution("not-a-date", T0) is None


def _seed_stats(db, wallet, pnl, resolved) -> None:
    db.execute(
        "INSERT INTO wallet_stats (wallet, resolved_trade_count, realized_pnl_usdc, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (wallet, resolved, pnl, T0.isoformat()),
    )


def test_pnl_percentile_ranks_against_population(db) -> None:
    # Five rankable wallets with pnl 0,1,2,3,4 (all >= min_resolved bets).
    for i in range(5):
        _seed_stats(db, f"0xw{i}", float(i), 10)
    # a wallet with pnl=4 ranks at the top: 5/5 wallets have pnl <= 4.
    assert _pnl_percentile(db, 4.0, 10, min_resolved=5) == pytest.approx(1.0)
    # pnl=2 -> 3 of 5 wallets (0,1,2) have pnl <= 2.
    assert _pnl_percentile(db, 2.0, 10, min_resolved=5) == pytest.approx(0.6)


def test_pnl_percentile_none_when_below_min_resolved(db) -> None:
    _seed_stats(db, "0xw", 100.0, 2)
    assert _pnl_percentile(db, 100.0, 2, min_resolved=5) is None


def test_build_inputs_assembles_from_db(db) -> None:
    cfg = _cfg()
    now = T0.isoformat()
    db.execute(
        """
        INSERT INTO wallet_stats
            (wallet, trade_count, resolved_trade_count, realized_pnl_usdc,
             win_count, loss_count, median_size_usdc, round_trip_count_30d,
             two_sided_ratio_30d, updated_at)
        VALUES ('0xw', 120, 40, 500000, 30, 10, 8000, 1, 0.2, ?)
        """,
        (now,),
    )
    db.execute(
        "INSERT INTO markets (condition_id, current_price, resolves_at, first_seen, last_seen) "
        "VALUES ('0xmkt', 0.48, ?, ?, ?)",
        ((T0 + timedelta(hours=48)).isoformat(), now, now),
    )

    inputs = build_inputs(
        db, cfg, wallet="0xw", condition_id="0xmkt",
        trade_price=0.52, size_usdc=24000, occurred_at=T0,
    )
    assert inputs.trade_count == 120
    assert inputs.median_size_usdc == 8000
    assert inputs.mid_price == 0.48
    assert inputs.hours_to_resolution == pytest.approx(48)
    assert inputs.pnl_percentile == pytest.approx(1.0)  # only rankable wallet


def test_build_inputs_defaults_for_unknown_wallet_and_market(db) -> None:
    cfg = _cfg()
    inputs = build_inputs(
        db, cfg, wallet="0xghost", condition_id="0xnope",
        trade_price=0.5, size_usdc=10000, occurred_at=T0,
    )
    assert inputs.trade_count == 0
    assert inputs.mid_price is None
    assert inputs.hours_to_resolution is None
    assert inputs.pnl_percentile is None
