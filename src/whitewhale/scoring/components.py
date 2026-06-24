"""The nine copy-score components.

Each is a pure function `(ScoreInputs, ScoringConfig) -> (score, rationale)`:
- `score` is a float on the 0-100 "copyability" scale (higher = more copyable;
  anti-signals are inverted so the weighted sum stays sign-consistent).
- `rationale` is an optional templated string, emitted only when the component
  is a notable driver. No LLM - see docs/research-notes.md §5.

When a required input is missing (e.g. a wallet with no median size, or a market
with no known mid), the component returns `config.neutral_score` rather than
guessing - the `confidence` field downgrades thin-data scores separately (§6).
"""

from __future__ import annotations

import math

from whitewhale.scoring.inputs import COMPONENT_ORDER, ScoreInputs, ScoringConfig


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound at ~95% CI. Penalizes small samples."""
    if n <= 0:
        return 0.0
    p_hat = wins / n
    denom = 1 + z * z / n
    center = p_hat + z * z / (2 * n)
    margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def _slippage_bps(inp: ScoreInputs) -> float | None:
    """|trade price - mid| in basis points, or None if mid is unknown."""
    if inp.mid_price is None or inp.mid_price <= 0:
        return None
    return abs(inp.trade_price - inp.mid_price) * 10_000


def wallet_pnl_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """Realized-PnL percentile vs. wallets with >= min_resolved_bets resolved."""
    if inp.pnl_percentile is None:
        return cfg.neutral_score, None
    pct = _clamp(inp.pnl_percentile * 100)
    rationale = f"Realized PnL ranks in the {round(pct)}th percentile" if pct >= 75 else None
    return pct, rationale


def wallet_winrate_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """Win rate on resolved markets, Wilson-lower-bounded for small samples."""
    n = inp.win_count + inp.loss_count
    if n == 0:
        return cfg.neutral_score, None
    lower = _wilson_lower_bound(inp.win_count, n)
    score = _clamp(lower * 100)
    rationale = (
        f"Win rate {lower * 100:.0f}% (Wilson LB) over {n} resolved bets"
        if lower >= 0.60
        else None
    )
    return score, rationale


def history_depth_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """log(trade_count) saturating at history_depth_saturation -> 0-100."""
    if inp.trade_count <= 0:
        return 0.0, "No trade history"
    sat = cfg.history_depth_saturation
    score = _clamp(100 * math.log1p(inp.trade_count) / math.log1p(sat))
    rationale = None
    if inp.trade_count < max(5, sat // 20):
        rationale = f"Thin history ({inp.trade_count} trades)"
    elif inp.trade_count >= sat:
        rationale = f"Deep history ({inp.trade_count} trades)"
    return score, rationale


def conviction_size_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """This trade vs. the wallet's median size: min(100, 50 * ratio)."""
    if inp.median_size_usdc <= 0:
        return cfg.neutral_score, None
    ratio = inp.size_usdc / inp.median_size_usdc
    score = _clamp(50 * ratio)
    rationale = f"Bet is {ratio:.1f}x wallet median size" if ratio >= 2 else None
    return score, rationale


def time_to_resolution_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """Higher as resolution nears; tapers from 100 (now) to a floor at the window."""
    h = inp.hours_to_resolution
    if h is None:
        return cfg.neutral_score, None
    h = max(0.0, h)
    window, floor = cfg.time_to_resolution_hours, cfg.time_to_resolution_floor
    if h >= window:
        return floor, None
    score = floor + (100 - floor) * (1 - h / window)
    return _clamp(score), f"Resolves in {h:.0f}h"


def price_impact_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """Willingness to take slippage: larger |price - mid| -> higher score."""
    bps = _slippage_bps(inp)
    if bps is None:
        return cfg.neutral_score, None
    score = _clamp(100 * bps / cfg.price_impact_saturation_bps)
    rationale = f"Took {bps:.0f}bps of slippage (conviction)" if bps >= 50 else None
    return score, rationale


def non_arb_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """NOT an arb bot: penalize recent same-market round-trips."""
    sat = cfg.round_trip_saturation
    rt = inp.round_trip_count_30d
    score = _clamp(100 * (1 - min(rt, sat) / sat)) if sat > 0 else 100.0
    if rt == 0:
        return score, "No recent round-trip arb pattern"
    if score <= 25:
        return score, f"Round-trip arb pattern ({rt} in 30d)"
    return score, None


def non_mm_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """NOT a market maker: penalize high two-sided turnover."""
    tsr = _clamp(inp.two_sided_ratio_30d, 0.0, 1.0)
    score = _clamp(100 * (1 - tsr))
    if tsr >= 0.60:
        return score, "High two-sided turnover (MM-like)"
    if tsr <= 0.20:
        return score, "Directional, not market-making"
    return score, None


def organic_price_score(inp: ScoreInputs, cfg: ScoringConfig) -> tuple[float, str | None]:
    """NOT trading at fair value: penalize fills inside a tight band around mid."""
    bps = _slippage_bps(inp)
    if bps is None:
        return cfg.neutral_score, None
    band = cfg.mid_price_band_bps
    score = _clamp(100 * bps / band) if band > 0 else 100.0
    rationale = f"Filled within {bps:.0f}bps of mid (possible MM/arb)" if bps < band else None
    return score, rationale


# Registry in alert-JSON order; the engine iterates this.
COMPONENTS = {
    "wallet_pnl_score": wallet_pnl_score,
    "wallet_winrate_score": wallet_winrate_score,
    "history_depth_score": history_depth_score,
    "conviction_size_score": conviction_size_score,
    "time_to_resolution_score": time_to_resolution_score,
    "price_impact_score": price_impact_score,
    "non_arb_score": non_arb_score,
    "non_mm_score": non_mm_score,
    "organic_price_score": organic_price_score,
}

assert tuple(COMPONENTS) == COMPONENT_ORDER, "component registry/order mismatch"
