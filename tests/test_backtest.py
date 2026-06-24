"""Tests for the Phase 5 backtester: copy-EV math, sample assembly, holdout,
buckets/cohorts, correlation, and the deterministic weight optimizer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from whitewhale import backtest as bt
from whitewhale.filter import WhaleConfig
from whitewhale.scoring import ScoringConfig

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

WHALE_CFG = {
    "whale_filter": {
        "min_size_usdc": 5000,
        "min_market_liquidity_usdc": 50000,
        "dedupe_window_seconds": 90,
        "allow_unknown_liquidity": False,
    }
}


def _scfg() -> ScoringConfig:
    return ScoringConfig.from_config(FULL_CFG)


def _wcfg() -> WhaleConfig:
    return WhaleConfig.from_config(WHALE_CFG)


# --- copy EV (pure) ------------------------------------------------------------


def test_settlement_value() -> None:
    assert bt.settlement_value(0, 0) == 1.0
    assert bt.settlement_value(1, 0) == 0.0


def test_copy_return_buy_winner_is_positive() -> None:
    # bought the winning outcome at 0.40 -> settles to 1.0
    assert bt.copy_return("BUY", 0.40, 1.0) == pytest.approx((1.0 - 0.40) / 0.40)


def test_copy_return_buy_loser_is_minus_one() -> None:
    # bought a losing outcome -> lose the whole stake
    assert bt.copy_return("BUY", 0.40, 0.0) == pytest.approx(-1.0)


def test_copy_return_sell_winner_is_negative() -> None:
    # sold (shorted) the outcome that then won -> copying the short loses
    assert bt.copy_return("SELL", 0.40, 1.0) == pytest.approx(-(1.0 - 0.40) / 0.40)
    # sold the outcome that lost -> the short pays off
    assert bt.copy_return("SELL", 0.40, 0.0) == pytest.approx(1.0)


def test_copy_return_degenerate_price_is_none() -> None:
    assert bt.copy_return("BUY", 0.0, 1.0) is None
    assert bt.copy_return("BUY", 1.0, 0.0) is None
    assert bt.copy_return("BUY", -0.1, 1.0) is None


def test_copy_pnl_buy_matches_shares_times_edge() -> None:
    # 100 shares bought at 0.40, settles to 1.0 -> +60
    assert bt.copy_pnl_usdc("BUY", 0.40, 100, 1.0) == pytest.approx(60.0)
    assert bt.copy_pnl_usdc("BUY", 0.40, 100, 0.0) == pytest.approx(-40.0)
    # short flips the sign
    assert bt.copy_pnl_usdc("SELL", 0.40, 100, 1.0) == pytest.approx(-60.0)


# --- statistics (pure) ---------------------------------------------------------


def test_pearson_perfect_and_degenerate() -> None:
    assert bt.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert bt.pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert bt.pearson([1, 1, 1], [1, 2, 3]) is None  # constant series
    assert bt.pearson([1], [1]) is None  # n < 2


def test_spearman_is_rank_based() -> None:
    # monotone but nonlinear -> Spearman 1.0 though Pearson < 1
    xs = [1, 2, 3, 4]
    ys = [1, 4, 9, 16]
    assert bt.spearman(xs, ys) == pytest.approx(1.0)
    assert bt.pearson(xs, ys) < 1.0


def test_ranks_average_ties() -> None:
    # two-way tie at the bottom shares rank 1.5
    assert bt._ranks([5, 5, 9]) == [1.5, 1.5, 3.0]


# --- sample assembly from a seeded DB ------------------------------------------


def _seed_market(db, condition_id, *, resolved, outcome_resolved, liquidity=200000) -> None:
    now = T0.isoformat()
    db.execute(
        """
        INSERT INTO markets
            (condition_id, slug, question, liquidity_usdc, current_price,
             resolves_at, resolved, outcome_resolved, first_seen, last_seen)
        VALUES (?, ?, ?, ?, 0.5, ?, ?, ?, ?, ?)
        """,
        (
            condition_id,
            f"slug-{condition_id}",
            "Q?",
            liquidity,
            (T0 + timedelta(hours=24)).isoformat(),
            resolved,
            outcome_resolved,
            now,
            now,
        ),
    )


def _seed_trade(
    db,
    *,
    tx,
    condition_id,
    wallet,
    side,
    outcome_index,
    price,
    size_usdc,
    occurred_at=T0,
    log_index=0,
) -> None:
    shares = size_usdc / price
    db.execute(
        """
        INSERT INTO trades
            (tx_hash, log_index, occurred_at, wallet, condition_id, asset_id,
             outcome, outcome_index, side, price, size_shares, size_usdc)
        VALUES (?, ?, ?, ?, ?, 'tok', ?, ?, ?, ?, ?, ?)
        """,
        (
            tx,
            log_index,
            occurred_at.isoformat(),
            wallet,
            condition_id,
            "YES" if outcome_index == 0 else "NO",
            outcome_index,
            side,
            price,
            shares,
            size_usdc,
        ),
    )


def _seed_wallet(db, address, *, label=None, cluster_id=None) -> None:
    now = T0.isoformat()
    db.execute(
        "INSERT INTO wallets (address, label, cluster_id, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (address, label, cluster_id, now, now),
    )


def test_collect_samples_only_resolved_markets(db) -> None:
    _seed_market(db, "0xres", resolved=1, outcome_resolved=0)
    _seed_market(db, "0xopen", resolved=0, outcome_resolved=None)
    _seed_trade(db, tx="0x1", condition_id="0xres", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=10000)
    _seed_trade(db, tx="0x2", condition_id="0xopen", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=10000)

    samples = bt.collect_samples(db, _wcfg(), _scfg())
    assert len(samples) == 1
    s = samples[0]
    assert s.condition_id == "0xres"
    assert s.copy_return == pytest.approx((1.0 - 0.4) / 0.4)
    assert s.copy_win is True


def test_collect_samples_skips_sentinel_outcome(db) -> None:
    # subgraph-backfilled rows carry outcome_index = -1 and can't be settled
    _seed_market(db, "0xres", resolved=1, outcome_resolved=0)
    _seed_trade(db, tx="0x1", condition_id="0xres", wallet="0xw", side="BUY",
                outcome_index=-1, price=0.4, size_usdc=10000)
    assert bt.collect_samples(db, _wcfg(), _scfg()) == []


def test_collect_samples_skips_subthreshold_size(db) -> None:
    _seed_market(db, "0xres", resolved=1, outcome_resolved=0)
    _seed_trade(db, tx="0x1", condition_id="0xres", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=1000)  # below 5000 floor
    assert bt.collect_samples(db, _wcfg(), _scfg()) == []


def test_collect_samples_attaches_labels(db) -> None:
    _seed_market(db, "0xres", resolved=1, outcome_resolved=0)
    _seed_wallet(db, "0xtheo", label="smart_money", cluster_id="theo_cluster")
    _seed_trade(db, tx="0x1", condition_id="0xres", wallet="0xtheo", side="BUY",
                outcome_index=0, price=0.4, size_usdc=10000)
    s = bt.collect_samples(db, _wcfg(), _scfg())[0]
    assert s.label == "smart_money"
    assert s.cluster_id == "theo_cluster"


def test_holdout_marks_recent_tail(db) -> None:
    _seed_market(db, "0xres", resolved=1, outcome_resolved=0)
    # old trade 10 weeks back, recent trade at T0
    _seed_trade(db, tx="0xold", condition_id="0xres", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=10000,
                occurred_at=T0 - timedelta(weeks=10))
    _seed_trade(db, tx="0xnew", condition_id="0xres", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=10000, occurred_at=T0)

    samples = bt.collect_samples(db, _wcfg(), _scfg(), holdout_weeks=6)
    by_tx = {s.tx_hash: s for s in samples}
    assert by_tx["0xold"].out_of_sample is False
    assert by_tx["0xnew"].out_of_sample is True


# --- weighting equivalence & report --------------------------------------------


def _one_sample(total_components: dict[str, float], **kw) -> bt.BacktestSample:
    base = dict(
        tx_hash="0x1", log_index=0, occurred_at=T0, wallet="0xw", label=None,
        cluster_id=None, condition_id="0xm", side="BUY", outcome_index=0, price=0.4,
        size_usdc=10000.0, size_shares=25000.0, winning_index=0,
        components=total_components, copy_return=0.5, copy_pnl_usdc=100.0,
    )
    base.update(kw)
    return bt.BacktestSample(**base)


def test_total_matches_engine_dot_product(db) -> None:
    # collect a real sample, then confirm sample.total(weights) == sum(w*comp)
    _seed_market(db, "0xres", resolved=1, outcome_resolved=0)
    _seed_trade(db, tx="0x1", condition_id="0xres", wallet="0xw", side="BUY",
                outcome_index=0, price=0.4, size_usdc=10000)
    s = bt.collect_samples(db, _wcfg(), _scfg())[0]
    cfg = _scfg()
    expected = sum(cfg.weights[k] * s.components[k] for k in s.components)
    assert s.total(cfg.weights) == pytest.approx(expected)


def test_evaluate_weights_uses_in_sample_only() -> None:
    comps = {k: 50.0 for k in FULL_CFG["scoring"]["weights"]}
    # in-sample: score tracks return; out-of-sample: score anti-tracks return.
    in1 = _one_sample({**comps, "wallet_pnl_score": 10.0}, copy_return=0.0)
    in2 = _one_sample({**comps, "wallet_pnl_score": 90.0}, copy_return=1.0, tx_hash="0x2")
    out1 = _one_sample({**comps, "wallet_pnl_score": 90.0}, copy_return=0.0,
                       tx_hash="0x3", out_of_sample=True)
    out2 = _one_sample({**comps, "wallet_pnl_score": 10.0}, copy_return=1.0,
                       tx_hash="0x4", out_of_sample=True)
    weights = _scfg().weights
    rho = bt.evaluate_weights([in1, in2, out1, out2], weights, in_sample_only=True)
    assert rho == pytest.approx(1.0)  # perfect on the two in-sample points


def test_bucket_returns_partition_samples() -> None:
    weights = _scfg().weights
    comps_lo = {k: 10.0 for k in weights}   # total 10 -> [0,25)
    comps_hi = {k: 90.0 for k in weights}   # total 90 -> [75,100]
    lo = _one_sample(comps_lo, copy_return=-1.0, copy_pnl_usdc=-100.0)
    hi = _one_sample(comps_hi, copy_return=1.0, copy_pnl_usdc=100.0, tx_hash="0x2")
    buckets = bt.bucket_returns([lo, hi], weights)
    counts = {(b.lo, b.hi): b.n for b in buckets}
    assert counts[(0, 25)] == 1
    assert counts[(75, 100)] == 1
    assert sum(b.n for b in buckets) == 2


def test_summarize_alerted_subset_and_pnl() -> None:
    weights = _scfg().weights
    hi = _one_sample({k: 90.0 for k in weights}, copy_return=1.0, copy_pnl_usdc=500.0)
    lo = _one_sample({k: 10.0 for k in weights}, copy_return=-1.0,
                     copy_pnl_usdc=-200.0, tx_hash="0x2")
    report = bt.summarize([hi, lo], weights, min_score=60)
    assert report.n_samples == 2
    assert report.total_pnl_usdc == pytest.approx(300.0)
    assert report.alerted_n == 1  # only the score-90 sample clears 60
    assert report.alerted_mean_return == pytest.approx(1.0)


def test_cohort_stats_group_by_label_and_cluster() -> None:
    weights = _scfg().weights
    a = _one_sample({k: 80.0 for k in weights}, label="smart_money",
                    cluster_id="theo_cluster")
    b = _one_sample({k: 20.0 for k in weights}, label="arb", cluster_id=None, tx_hash="0x2")
    cohorts = {c.name: c for c in bt.cohort_stats([a, b], weights)}
    assert "cluster:theo_cluster" in cohorts
    assert "label:smart_money" in cohorts
    assert "label:arb" in cohorts
    assert cohorts["label:smart_money"].mean_score == pytest.approx(80.0)


# --- optimizer -----------------------------------------------------------------


def _separable_samples() -> list[bt.BacktestSample]:
    """Samples where one component perfectly orders copy return - the optimizer
    should be able to lean on it and not lose in-sample rank correlation."""
    comps = {k: 50.0 for k in FULL_CFG["scoring"]["weights"]}
    out = []
    for i, ret in enumerate([-1.0, -0.3, 0.2, 0.9]):
        c = {**comps, "wallet_pnl_score": (i + 1) * 20.0}
        out.append(_one_sample(c, copy_return=ret, tx_hash=f"0x{i}"))
    return out


def test_optimize_weights_stays_on_simplex_and_improves() -> None:
    samples = _separable_samples()
    sconfig = _scfg()
    start = bt.evaluate_weights(samples, sconfig.weights)
    weights, obj_in, _ = bt.optimize_weights(samples, sconfig, step=0.1, max_rounds=10)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert all(w >= 0 for w in weights.values())
    assert obj_in >= start  # coordinate ascent never regresses the objective


def test_optimize_weights_is_deterministic() -> None:
    samples = _separable_samples()
    sconfig = _scfg()
    a = bt.optimize_weights(samples, sconfig, step=0.1, max_rounds=10)
    b = bt.optimize_weights(samples, sconfig, step=0.1, max_rounds=10)
    assert a[0] == b[0]
    assert a[1] == b[1]


def test_optimize_reports_holdout_objective() -> None:
    samples = _separable_samples()
    # mark the last two as holdout, keeping the same monotone relationship
    samples[2] = bt._with_oos(samples[2])
    samples[3] = bt._with_oos(samples[3])
    _, _, oos = bt.optimize_weights(samples, _scfg(), step=0.1, max_rounds=10)
    # two holdout points with monotone score<->return -> spearman defined
    assert oos == pytest.approx(1.0)
