"""Phase 5 - backtester.

Replays historical whale trades in *resolved* markets through the scoring engine
and asks the only question that matters: **would copying this trade have made
money at settlement?** That forward-looking "copy EV" is the target the score is
supposed to predict, so the backtester measures how well `score.total` actually
ranks profitable copies - and can re-fit the weights to maximize that ranking on
a held-out tail (docs/research-notes.md §7).

Design notes:
- **Copy EV.** A resolved binary outcome settles to $1 (won) or $0 (lost). Copying
  a BUY of outcome i means going long it; copying a SELL means going short it
  (equivalently long the complement). Per dollar of the whale's notional:
      BUY  return = (settlement - price) / price
      SELL return = (price - settlement) / price
  i.e. `direction * (settlement - price) / price` with direction +1/-1. The PnL
  in dollars is `direction * size_shares * (settlement - price)`; dividing by the
  notional (`size_shares * price`) gives the per-dollar return above, so the two
  are consistent on a single basis.
- **Components are weight-independent.** Each of the nine sub-scores is a pure
  function of `ScoreInputs` and the non-weight config knobs; only the final
  weighted sum depends on `weights`. So we score each trade's nine components
  *once*, then evaluating any weight vector is a cheap dot product. That's what
  makes the weight search fast and keeps it free of the DB.
- **Holdout is data-relative.** The out-of-sample tail is the last `holdout_weeks`
  measured from the newest settled trade in the set, not the wall clock - so a
  replay is deterministic and doesn't drift as time passes.

Heavy, laptop-only job; never runs on the Pi.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from whitewhale.filter import WhaleConfig, iter_whale_trades
from whitewhale.scoring.components import COMPONENTS
from whitewhale.scoring.engine import build_inputs
from whitewhale.scoring.inputs import COMPONENT_ORDER, ScoringConfig

# Score-bucket edges for the monotonicity table; top bucket is inclusive of 100.
SCORE_BUCKETS: tuple[tuple[int, int], ...] = ((0, 25), (25, 50), (50, 75), (75, 101))


# --- Copy EV (pure) ------------------------------------------------------------


def settlement_value(outcome_index: int, winning_index: int) -> float:
    """$1 if the traded outcome is the one that resolved YES, else $0."""
    return 1.0 if outcome_index == winning_index else 0.0


def copy_return(side: str, price: float, settlement: float) -> float | None:
    """Per-dollar-of-notional return from copying this trade to settlement.

    Returns None for degenerate prices (<=0 or >=1) where the copy is ill-defined.
    """
    if not 0.0 < price < 1.0:
        return None
    direction = 1.0 if side.upper() == "BUY" else -1.0
    return direction * (settlement - price) / price


def copy_pnl_usdc(side: str, price: float, size_shares: float, settlement: float) -> float:
    """Dollar PnL of copying the whale's notional (long for BUY, short for SELL)."""
    direction = 1.0 if side.upper() == "BUY" else -1.0
    return direction * size_shares * (settlement - price)


# --- Sample assembly -----------------------------------------------------------


@dataclass(frozen=True)
class BacktestSample:
    """One settled whale trade: its nine components plus its realized copy EV."""

    tx_hash: str
    log_index: int
    occurred_at: datetime
    wallet: str | None
    label: str | None
    cluster_id: str | None
    condition_id: str
    side: str
    outcome_index: int
    price: float
    size_usdc: float
    size_shares: float
    winning_index: int
    components: dict[str, float]
    copy_return: float
    copy_pnl_usdc: float
    out_of_sample: bool = False

    def total(self, weights: dict[str, float]) -> float:
        """Weighted-sum score under an arbitrary weight vector (no DB, no rounding)."""
        return sum(weights[name] * self.components[name] for name in self.components)

    @property
    def copy_win(self) -> bool:
        return self.copy_pnl_usdc > 0


def _components_for(inp, cfg: ScoringConfig) -> dict[str, float]:
    """Run the nine component functions and keep just their raw 0-100 values."""
    return {name: fn(inp, cfg)[0] for name, fn in COMPONENTS.items()}


def collect_samples(
    conn: sqlite3.Connection,
    wconfig: WhaleConfig,
    sconfig: ScoringConfig,
    *,
    since_iso: str | None = None,
    holdout_weeks: float = 0.0,
) -> list[BacktestSample]:
    """Build the backtest set: whale trades that landed in resolved markets.

    Mirrors the live path exactly - the same whale filter, the same component
    inputs - then joins each event to its market's resolved outcome to compute
    the copy EV the score is meant to predict. Trades in unresolved markets, with
    a sentinel outcome index, or at a degenerate price are skipped.
    """
    resolution = {
        row["condition_id"]: row["outcome_resolved"]
        for row in conn.execute(
            "SELECT condition_id, outcome_resolved FROM markets "
            "WHERE resolved = 1 AND outcome_resolved IS NOT NULL"
        )
    }
    labels = {
        row["address"]: (row["label"], row["cluster_id"])
        for row in conn.execute("SELECT address, label, cluster_id FROM wallets")
    }

    samples: list[BacktestSample] = []
    for event in iter_whale_trades(conn, wconfig, since_iso=since_iso):
        winning = resolution.get(event.condition_id)
        if winning is None:
            continue

        # Subgraph-backfilled rows carry outcome_index = -1 (token->outcome join
        # deferred); they can't be settled, so leave them out of the backtest.
        trade_row = conn.execute(
            "SELECT outcome_index, size_shares FROM trades "
            "WHERE tx_hash = ? AND log_index = ?",
            (event.tx_hash, event.log_index),
        ).fetchone()
        if trade_row is None or trade_row["outcome_index"] < 0:
            continue
        outcome_index = trade_row["outcome_index"]
        size_shares = trade_row["size_shares"]

        settlement = settlement_value(outcome_index, winning)
        ret = copy_return(event.side, event.price, settlement)
        if ret is None:
            continue

        inputs = build_inputs(
            conn,
            sconfig,
            wallet=event.wallet,
            condition_id=event.condition_id,
            trade_price=event.price,
            size_usdc=event.size_usdc,
            occurred_at=event.occurred_at,
        )
        label, cluster_id = labels.get(event.wallet, (None, None)) if event.wallet else (None, None)

        samples.append(
            BacktestSample(
                tx_hash=event.tx_hash,
                log_index=event.log_index,
                occurred_at=event.occurred_at,
                wallet=event.wallet,
                label=label,
                cluster_id=cluster_id,
                condition_id=event.condition_id,
                side=event.side,
                outcome_index=outcome_index,
                price=event.price,
                size_usdc=event.size_usdc,
                size_shares=size_shares,
                winning_index=winning,
                components=_components_for(inputs, sconfig),
                copy_return=ret,
                copy_pnl_usdc=copy_pnl_usdc(event.side, event.price, size_shares, settlement),
            )
        )

    _mark_holdout(samples, holdout_weeks)
    return samples


def _mark_holdout(samples: list[BacktestSample], holdout_weeks: float) -> None:
    """Flag the newest `holdout_weeks` of trades as out-of-sample, in place.

    Measured from the latest trade in the set (data-relative) so replays are
    deterministic. `BacktestSample` is frozen, so we rebuild the flagged ones.
    """
    if holdout_weeks <= 0 or not samples:
        return
    newest = max(s.occurred_at for s in samples)
    cutoff = newest - timedelta(weeks=holdout_weeks)
    for i, s in enumerate(samples):
        if s.occurred_at >= cutoff:
            samples[i] = _with_oos(s)


def _with_oos(s: BacktestSample) -> BacktestSample:
    return BacktestSample(**{**s.__dict__, "out_of_sample": True})


# --- Statistics (pure) ---------------------------------------------------------


def _ranks(values: list[float]) -> list[float]:
    """Average (fractional) ranks, so ties share a rank - for Spearman."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank across the tie block
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or None if undefined (n<2 or a constant series)."""
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation - robust to the score/return scales being nonlinear."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    return pearson(_ranks(xs), _ranks(ys))


# --- Report --------------------------------------------------------------------


@dataclass
class BucketStat:
    lo: int
    hi: int
    n: int
    hit_rate: float
    mean_return: float
    total_pnl_usdc: float


@dataclass
class CohortStat:
    name: str
    n: int
    mean_score: float
    median_score: float
    mean_return: float
    hit_rate: float


@dataclass
class BacktestReport:
    n_samples: int
    n_in_sample: int
    n_out_of_sample: int
    hit_rate: float
    mean_return: float
    total_pnl_usdc: float
    pearson_in: float | None
    pearson_out: float | None
    spearman_in: float | None
    spearman_out: float | None
    buckets: list[BucketStat]
    cohorts: list[CohortStat]
    alerted_n: int
    alerted_hit_rate: float | None
    alerted_mean_return: float | None
    min_score: int


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _hit_rate(samples: list[BacktestSample]) -> float:
    return _mean([1.0 if s.copy_win else 0.0 for s in samples])


def bucket_returns(
    samples: list[BacktestSample], weights: dict[str, float]
) -> list[BucketStat]:
    """Mean copy return per score band - the test of whether higher score = more EV."""
    out: list[BucketStat] = []
    for lo, hi in SCORE_BUCKETS:
        members = [s for s in samples if lo <= s.total(weights) < hi]
        out.append(
            BucketStat(
                lo=lo,
                hi=min(hi, 100),
                n=len(members),
                hit_rate=_hit_rate(members),
                mean_return=_mean([s.copy_return for s in members]),
                total_pnl_usdc=sum(s.copy_pnl_usdc for s in members),
            )
        )
    return out


def _cohort(name: str, members: list[BacktestSample], weights: dict[str, float]) -> CohortStat:
    scores = [s.total(weights) for s in members]
    return CohortStat(
        name=name,
        n=len(members),
        mean_score=_mean(scores),
        median_score=_median(scores),
        mean_return=_mean([s.copy_return for s in members]),
        hit_rate=_hit_rate(members),
    )


def cohort_stats(
    samples: list[BacktestSample], weights: dict[str, float]
) -> list[CohortStat]:
    """Per-label and per-cluster score/EV summaries for the §7 sanity checks."""
    cohorts: list[CohortStat] = []
    by_cluster: dict[str, list[BacktestSample]] = {}
    by_label: dict[str, list[BacktestSample]] = {}
    for s in samples:
        if s.cluster_id:
            by_cluster.setdefault(s.cluster_id, []).append(s)
        if s.label:
            by_label.setdefault(s.label, []).append(s)
    for name in sorted(by_cluster):
        cohorts.append(_cohort(f"cluster:{name}", by_cluster[name], weights))
    for name in sorted(by_label):
        cohorts.append(_cohort(f"label:{name}", by_label[name], weights))
    return cohorts


def summarize(
    samples: list[BacktestSample],
    weights: dict[str, float],
    *,
    min_score: int,
) -> BacktestReport:
    """Roll a sample set up into the headline metrics, buckets, and cohorts."""
    in_s = [s for s in samples if not s.out_of_sample]
    out_s = [s for s in samples if s.out_of_sample]

    def corr(fn, subset):
        return fn([s.total(weights) for s in subset], [s.copy_return for s in subset])

    alerted = [s for s in samples if s.total(weights) >= min_score]
    return BacktestReport(
        n_samples=len(samples),
        n_in_sample=len(in_s),
        n_out_of_sample=len(out_s),
        hit_rate=_hit_rate(samples),
        mean_return=_mean([s.copy_return for s in samples]),
        total_pnl_usdc=sum(s.copy_pnl_usdc for s in samples),
        pearson_in=corr(pearson, in_s),
        pearson_out=corr(pearson, out_s),
        spearman_in=corr(spearman, in_s),
        spearman_out=corr(spearman, out_s),
        buckets=bucket_returns(samples, weights),
        cohorts=cohort_stats(samples, weights),
        alerted_n=len(alerted),
        alerted_hit_rate=_hit_rate(alerted) if alerted else None,
        alerted_mean_return=_mean([s.copy_return for s in alerted]) if alerted else None,
        min_score=min_score,
    )


# --- Weight optimization -------------------------------------------------------


def evaluate_weights(
    samples: list[BacktestSample], weights: dict[str, float], *, in_sample_only: bool = True
) -> float:
    """Objective: rank correlation between score and copy return. -inf if undefined.

    Spearman (not Pearson) because we only need the score to *order* trades by EV;
    the absolute 0-100 scale is arbitrary. In-sample by default so the optimizer
    never peeks at the holdout tail.
    """
    subset = [s for s in samples if not (in_sample_only and s.out_of_sample)]
    rho = spearman([s.total(weights) for s in subset], [s.copy_return for s in subset])
    return rho if rho is not None else float("-inf")


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    """Clamp negatives to 0 and renormalize to sum 1 (stay on the simplex)."""
    clamped = {k: max(0.0, v) for k, v in weights.items()}
    total = sum(clamped.values())
    if total <= 0:
        # Degenerate - fall back to uniform.
        return {k: 1.0 / len(clamped) for k in clamped}
    return {k: v / total for k, v in clamped.items()}


def optimize_weights(
    samples: list[BacktestSample],
    sconfig: ScoringConfig,
    *,
    step: float = 0.05,
    max_rounds: int = 25,
) -> tuple[dict[str, float], float, float]:
    """Deterministic coordinate ascent on in-sample rank correlation.

    Starts from the configured weights and, each round, tries nudging every
    component up and down by `step` (renormalizing to the simplex), greedily
    taking the single best improving move. Stops when no move helps or after
    `max_rounds`. No randomness, so the result is reproducible.

    Returns `(weights, objective_in_sample, objective_out_of_sample)`. The OOS
    figure is reported, never optimized against - it's the promote/reject signal.
    """
    weights = _normalize(dict(sconfig.weights))
    best = evaluate_weights(samples, weights)

    for _ in range(max_rounds):
        candidates: list[tuple[float, dict[str, float]]] = []
        for name in COMPONENT_ORDER:
            for delta in (step, -step):
                trial = _normalize({**weights, name: weights[name] + delta})
                candidates.append((evaluate_weights(samples, trial), trial))
        score, trial = max(candidates, key=lambda c: c[0])
        if score <= best + 1e-9:
            break
        best, weights = score, trial

    oos = evaluate_weights(samples, weights, in_sample_only=False)
    # OOS objective should be measured on the holdout tail only, when there is one.
    holdout = [s for s in samples if s.out_of_sample]
    if holdout:
        rho = spearman([s.total(weights) for s in holdout], [s.copy_return for s in holdout])
        oos = rho if rho is not None else float("-inf")
    return weights, best, oos
