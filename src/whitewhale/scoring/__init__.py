"""Copy-score engine (Phase 3).

Nine pure 0-100 sub-scores over precomputed wallet stats + the trade + market,
combined by a config-driven weighted sum into a 0-100 total with an independent
confidence label. See docs/research-notes.md §5-6.
"""

from whitewhale.scoring.engine import build_inputs, score_trade, score_whale_event
from whitewhale.scoring.inputs import (
    COMPONENT_ORDER,
    ScoreInputs,
    ScoreResult,
    ScoringConfig,
)

__all__ = [
    "COMPONENT_ORDER",
    "ScoreInputs",
    "ScoreResult",
    "ScoringConfig",
    "build_inputs",
    "score_trade",
    "score_whale_event",
]
