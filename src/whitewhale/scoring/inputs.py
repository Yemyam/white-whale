"""Dataclasses for the copy-score engine.

`ScoreInputs` is the fully-resolved bundle a single trade needs to be scored -
the trade itself, the wallet's precomputed stats, and a couple of market-derived
values. `engine.build_inputs` assembles it from SQLite; the component functions
in `components.py` are pure functions of it, so they unit-test without a DB.

`ScoringConfig` parses the `scoring` block of the YAML once into typed fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The nine components, in the order they appear in the alert JSON schema.
COMPONENT_ORDER = (
    "wallet_pnl_score",
    "wallet_winrate_score",
    "history_depth_score",
    "conviction_size_score",
    "time_to_resolution_score",
    "price_impact_score",
    "non_arb_score",
    "non_mm_score",
    "organic_price_score",
)


@dataclass
class ScoreInputs:
    """Everything one trade needs to be scored, resolved and DB-free."""

    # The trade.
    trade_price: float
    size_usdc: float

    # Precomputed wallet stats (zeroed for a wallet we've never backfilled).
    trade_count: int = 0
    resolved_trade_count: int = 0
    realized_pnl_usdc: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    median_size_usdc: float = 0.0
    round_trip_count_30d: int = 0
    two_sided_ratio_30d: float = 0.0

    # Derived / cross-wallet context.
    pnl_percentile: float | None = None      # 0..1 vs ranked population; None if unrankable
    hours_to_resolution: float | None = None
    mid_price: float | None = None           # current_price proxy until live orderbook (Phase 6)


@dataclass
class ScoringConfig:
    """Typed view of the `scoring` config block."""

    weights: dict[str, float]
    # params
    min_resolved_bets: int = 5
    history_depth_saturation: int = 100
    time_to_resolution_hours: float = 168.0
    time_to_resolution_floor: float = 20.0
    price_impact_saturation_bps: float = 200.0
    round_trip_saturation: int = 20
    mid_price_band_bps: float = 30.0
    neutral_score: float = 50.0
    # confidence thresholds (research-notes §6)
    conf_high_depth: float = 70.0
    conf_high_total: float = 60.0
    conf_medium_depth: float = 40.0

    @classmethod
    def from_config(cls, cfg: dict) -> ScoringConfig:
        s = cfg["scoring"]
        weights = {k: float(v) for k, v in s["weights"].items()}

        missing = set(COMPONENT_ORDER) - set(weights)
        if missing:
            raise ValueError(f"scoring.weights missing components: {sorted(missing)}")
        total = sum(weights.values())
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"scoring.weights must sum to 1.0, got {total:.4f}")

        params = s.get("params", {})
        thresholds = s.get("thresholds", {})
        conf = s.get("confidence", {})
        return cls(
            weights=weights,
            min_resolved_bets=int(params.get("min_resolved_bets", 5)),
            history_depth_saturation=int(params.get("history_depth_saturation", 100)),
            time_to_resolution_hours=float(params.get("time_to_resolution_hours", 168)),
            time_to_resolution_floor=float(params.get("time_to_resolution_floor", 20)),
            price_impact_saturation_bps=float(params.get("price_impact_saturation_bps", 200)),
            round_trip_saturation=int(params.get("round_trip_saturation", 20)),
            # mid-price band is a calibration knob, lives under thresholds.
            mid_price_band_bps=float(thresholds.get("mid_price_band_bps", 30)),
            neutral_score=float(params.get("neutral_score", 50)),
            conf_high_depth=float(conf.get("high_depth", 70)),
            conf_high_total=float(conf.get("high_total", 60)),
            conf_medium_depth=float(conf.get("medium_depth", 40)),
        )


@dataclass
class ScoreResult:
    """Output of the engine - mirrors the `score` object in the alert JSON."""

    total: int
    confidence: str
    components: dict[str, int]
    rationale: list[str] = field(default_factory=list)
