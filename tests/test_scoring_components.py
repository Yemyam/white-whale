"""Unit tests for the nine pure copy-score components."""

from __future__ import annotations

import math

import pytest

from whitewhale.scoring import ScoreInputs, ScoringConfig
from whitewhale.scoring.components import (
    _wilson_lower_bound,
    conviction_size_score,
    history_depth_score,
    non_arb_score,
    non_mm_score,
    organic_price_score,
    price_impact_score,
    time_to_resolution_score,
    wallet_pnl_score,
    wallet_winrate_score,
)

WEIGHTS = {
    "wallet_pnl_score": 0.20,
    "wallet_winrate_score": 0.15,
    "history_depth_score": 0.05,
    "conviction_size_score": 0.15,
    "time_to_resolution_score": 0.10,
    "price_impact_score": 0.05,
    "non_arb_score": 0.10,
    "non_mm_score": 0.10,
    "organic_price_score": 0.10,
}


def cfg(**overrides) -> ScoringConfig:
    base = dict(weights=dict(WEIGHTS))
    base.update(overrides)
    return ScoringConfig(**base)


def inp(**overrides) -> ScoreInputs:
    base = dict(trade_price=0.5, size_usdc=10000)
    base.update(overrides)
    return ScoreInputs(**base)


# --- wallet_pnl_score ----------------------------------------------------------

def test_pnl_neutral_when_unrankable() -> None:
    score, why = wallet_pnl_score(inp(pnl_percentile=None), cfg(neutral_score=50))
    assert score == 50
    assert why is None


@pytest.mark.parametrize("pct, expected", [(0.0, 0), (0.5, 50), (0.92, 92), (1.0, 100)])
def test_pnl_maps_percentile(pct, expected) -> None:
    score, _ = wallet_pnl_score(inp(pnl_percentile=pct), cfg())
    assert score == pytest.approx(expected)


def test_pnl_rationale_only_when_high() -> None:
    assert wallet_pnl_score(inp(pnl_percentile=0.92), cfg())[1] is not None
    assert wallet_pnl_score(inp(pnl_percentile=0.40), cfg())[1] is None


# --- wallet_winrate_score ------------------------------------------------------

def test_winrate_neutral_with_no_resolved() -> None:
    score, why = wallet_winrate_score(inp(win_count=0, loss_count=0), cfg(neutral_score=50))
    assert score == 50 and why is None


def test_winrate_wilson_penalizes_small_samples() -> None:
    # 3/3 should score well below a raw 100% because the sample is tiny.
    small, _ = wallet_winrate_score(inp(win_count=3, loss_count=0), cfg())
    big, _ = wallet_winrate_score(inp(win_count=300, loss_count=0), cfg())
    assert small < big
    assert small < 60  # Wilson LB of 3/3 is ~0.44


def test_wilson_lower_bound_matches_formula() -> None:
    # 8 wins / 10: known Wilson LB ~0.490 at z=1.96.
    assert _wilson_lower_bound(8, 10) == pytest.approx(0.490, abs=0.01)
    assert _wilson_lower_bound(0, 0) == 0.0


# --- history_depth_score -------------------------------------------------------

def test_history_depth_zero_history() -> None:
    score, why = history_depth_score(inp(trade_count=0), cfg())
    assert score == 0
    assert why == "No trade history"


def test_history_depth_saturates_at_config() -> None:
    score, _ = history_depth_score(inp(trade_count=100), cfg(history_depth_saturation=100))
    assert score == pytest.approx(100)
    over, _ = history_depth_score(inp(trade_count=10_000), cfg(history_depth_saturation=100))
    assert over == 100  # clamped


def test_history_depth_is_logarithmic() -> None:
    score, _ = history_depth_score(inp(trade_count=10), cfg(history_depth_saturation=100))
    expected = 100 * math.log1p(10) / math.log1p(100)
    assert score == pytest.approx(expected)


# --- conviction_size_score -----------------------------------------------------

def test_conviction_neutral_without_baseline() -> None:
    score, why = conviction_size_score(inp(median_size_usdc=0), cfg(neutral_score=50))
    assert score == 50 and why is None


@pytest.mark.parametrize("size, median, expected", [(10000, 10000, 50), (40000, 10000, 100), (5000, 10000, 25)])
def test_conviction_ratio(size, median, expected) -> None:
    score, _ = conviction_size_score(inp(size_usdc=size, median_size_usdc=median), cfg())
    assert score == pytest.approx(expected)


def test_conviction_rationale_when_outsized() -> None:
    assert conviction_size_score(inp(size_usdc=40000, median_size_usdc=10000), cfg())[1] is not None
    assert conviction_size_score(inp(size_usdc=12000, median_size_usdc=10000), cfg())[1] is None


# --- time_to_resolution_score --------------------------------------------------

def test_time_neutral_when_unknown() -> None:
    score, why = time_to_resolution_score(inp(hours_to_resolution=None), cfg(neutral_score=50))
    assert score == 50 and why is None


def test_time_tapers_to_floor() -> None:
    c = cfg(time_to_resolution_hours=168, time_to_resolution_floor=20)
    now, _ = time_to_resolution_score(inp(hours_to_resolution=0), c)
    edge, _ = time_to_resolution_score(inp(hours_to_resolution=168), c)
    beyond, _ = time_to_resolution_score(inp(hours_to_resolution=1000), c)
    assert now == pytest.approx(100)
    assert edge == pytest.approx(20)
    assert beyond == pytest.approx(20)


def test_time_negative_clamped_to_now() -> None:
    score, _ = time_to_resolution_score(inp(hours_to_resolution=-5), cfg())
    assert score == pytest.approx(100)


# --- price_impact_score --------------------------------------------------------

def test_price_impact_neutral_without_mid() -> None:
    score, why = price_impact_score(inp(mid_price=None), cfg(neutral_score=50))
    assert score == 50 and why is None


def test_price_impact_scales_with_slippage() -> None:
    c = cfg(price_impact_saturation_bps=200)
    # price 0.52 vs mid 0.50 = 0.02 = 200 bps -> saturates at 100.
    score, why = price_impact_score(inp(trade_price=0.52, mid_price=0.50), c)
    assert score == pytest.approx(100)
    assert why is not None
    small, _ = price_impact_score(inp(trade_price=0.501, mid_price=0.50), c)
    assert small == pytest.approx(5)  # 10 bps / 200 * 100


# --- non_arb_score -------------------------------------------------------------

def test_non_arb_clean_wallet() -> None:
    score, why = non_arb_score(inp(round_trip_count_30d=0), cfg(round_trip_saturation=20))
    assert score == 100
    assert "No recent round-trip" in why


def test_non_arb_penalizes_round_trips() -> None:
    c = cfg(round_trip_saturation=20)
    half, _ = non_arb_score(inp(round_trip_count_30d=10), c)
    assert half == pytest.approx(50)
    maxed, why = non_arb_score(inp(round_trip_count_30d=25), c)
    assert maxed == 0
    assert "Round-trip arb pattern" in why


# --- non_mm_score --------------------------------------------------------------

@pytest.mark.parametrize("tsr, expected", [(0.0, 100), (0.5, 50), (1.0, 0)])
def test_non_mm_inverts_two_sided_ratio(tsr, expected) -> None:
    score, _ = non_mm_score(inp(two_sided_ratio_30d=tsr), cfg())
    assert score == pytest.approx(expected)


def test_non_mm_rationale_edges() -> None:
    assert "MM-like" in non_mm_score(inp(two_sided_ratio_30d=0.8), cfg())[1]
    assert "Directional" in non_mm_score(inp(two_sided_ratio_30d=0.1), cfg())[1]
    assert non_mm_score(inp(two_sided_ratio_30d=0.4), cfg())[1] is None


# --- organic_price_score -------------------------------------------------------

def test_organic_neutral_without_mid() -> None:
    score, why = organic_price_score(inp(mid_price=None), cfg(neutral_score=50))
    assert score == 50 and why is None


def test_organic_penalizes_fills_near_mid() -> None:
    c = cfg(mid_price_band_bps=30)
    # 0.5005 vs 0.50 = 5 bps, inside the 30 bps band -> low score + rationale.
    near, why = organic_price_score(inp(trade_price=0.5005, mid_price=0.50), c)
    assert near < 50
    assert why is not None and "mid" in why
    # 0.51 vs 0.50 = 100 bps, well outside the band -> full score, no flag.
    away, why2 = organic_price_score(inp(trade_price=0.51, mid_price=0.50), c)
    assert away == 100 and why2 is None
