"""Tests for Phase 4 alert emission: payload, gates, persistence, file drop."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from whitewhale.alert import AlertConfig, AlertEmitter, build_payload
from whitewhale.filter import WhaleEvent
from whitewhale.scoring.inputs import ScoreResult

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_event(tx="0xabc", condition_id="0xmkt", occurred_at=T0, **overrides) -> WhaleEvent:
    base = dict(
        tx_hash=tx,
        log_index=0,
        occurred_at=occurred_at,
        wallet="0xwhale",
        condition_id=condition_id,
        side="BUY",
        outcome="YES",
        price=0.53,
        size_usdc=25000.0,
        market_liquidity_usdc=200000.0,
    )
    base.update(overrides)
    return WhaleEvent(**base)


def make_result(total=82, confidence="high") -> ScoreResult:
    return ScoreResult(
        total=total,
        confidence=confidence,
        components={"wallet_pnl_score": 100, "non_arb_score": 100},
        rationale=["Realized PnL ranks in the 99th percentile"],
    )


def seed_trade_and_market(db, *, tx="0xabc", condition_id="0xmkt") -> None:
    now = T0.isoformat()
    db.execute(
        """
        INSERT INTO trades
            (tx_hash, log_index, occurred_at, wallet, condition_id, asset_id,
             outcome, outcome_index, side, price, size_shares, size_usdc)
        VALUES (?, 0, ?, '0xwhale', ?, 'token-123', 'YES', 0, 'BUY', 0.53, 47000, 25000)
        """,
        (tx, now, condition_id),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO markets
            (condition_id, slug, question, event_slug, liquidity_usdc, current_price,
             resolves_at, first_seen, last_seen)
        VALUES (?, 'trump-test', 'Will it?', 'trump-event', 200000, 0.50, ?, ?, ?)
        """,
        (condition_id, (T0 + timedelta(hours=48)).isoformat(), now, now),
    )


# --- config --------------------------------------------------------------------

def test_alert_config_defaults() -> None:
    c = AlertConfig.from_config({})
    assert c.min_score == 60
    assert c.market_cooldown_seconds == 300
    assert c.schema_version == "1.0"


def test_alert_config_reads_block() -> None:
    c = AlertConfig.from_config({"alerts": {"min_score": 75, "market_cooldown_seconds": 60}})
    assert c.min_score == 75
    assert c.market_cooldown_seconds == 60


# --- build_payload -------------------------------------------------------------

def test_build_payload_matches_schema(db) -> None:
    seed_trade_and_market(db)
    payload = build_payload(
        db, make_event(), make_result(),
        alert_id="aid-1", emitted_at="2026-06-01T12:05:00+00:00",
    )
    assert payload["schema_version"] == "1.0"
    assert payload["alert_id"] == "aid-1"

    trade = payload["trade"]
    assert trade["tx_hash"] == "0xabc"
    assert trade["outcome_token_id"] == "token-123"
    assert trade["shares"] == 47000
    assert trade["size_usdc"] == 25000.0

    market = payload["market"]
    assert market["question"] == "Will it?"
    assert market["url"] == "https://polymarket.com/event/trump-event"
    assert market["liquidity_usdc"] == 200000
    assert market["hours_to_resolution"] == 48.0

    assert payload["score"]["total"] == 82
    assert payload["score"]["confidence"] == "high"


def test_build_payload_tolerates_missing_market(db) -> None:
    # trade present, market never enriched
    db.execute(
        """
        INSERT INTO trades
            (tx_hash, log_index, occurred_at, wallet, condition_id, asset_id,
             outcome, outcome_index, side, price, size_shares, size_usdc)
        VALUES ('0xabc', 0, ?, '0xwhale', '0xmkt', 'token-9', 'NO', 1, 'SELL', 0.4, 100, 40)
        """,
        (T0.isoformat(),),
    )
    payload = build_payload(
        db, make_event(), make_result(), alert_id="a", emitted_at="t",
    )
    assert payload["market"]["question"] is None
    assert payload["market"]["url"] is None
    assert payload["market"]["hours_to_resolution"] is None


# --- AlertEmitter gates --------------------------------------------------------

def emitter(db, tmp_path, **cfg_overrides) -> AlertEmitter:
    base = dict(min_score=60, market_cooldown_seconds=300, drop_dir=str(tmp_path / "drop"))
    base.update(cfg_overrides)
    return AlertEmitter(db, AlertConfig(**base))


def test_emit_below_min_score_is_skipped(db, tmp_path) -> None:
    seed_trade_and_market(db)
    out = emitter(db, tmp_path).emit(make_event(), make_result(total=40))
    assert out.emitted is False
    assert out.reason == "below_min_score"
    assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_emit_writes_file_and_audit_row(db, tmp_path) -> None:
    seed_trade_and_market(db)
    out = emitter(db, tmp_path).emit(make_event(), make_result(total=82))
    assert out.emitted is True
    assert out.reason == "ok"

    # audit row
    row = db.execute("SELECT score_total, confidence, payload_json FROM alerts").fetchone()
    assert row["score_total"] == 82
    assert row["confidence"] == "high"

    # file drop matches the persisted payload
    from pathlib import Path
    written = json.loads(Path(out.path).read_text())
    assert written == json.loads(row["payload_json"])
    assert written["trade"]["tx_hash"] == "0xabc"


def test_emit_dedupes_same_trade(db, tmp_path) -> None:
    seed_trade_and_market(db)
    em = emitter(db, tmp_path)
    assert em.emit(make_event(), make_result()).emitted is True
    # a fresh emitter (no in-memory cooldown state) still won't double-alert the trade
    second = AlertEmitter(db, em.config).emit(make_event(), make_result())
    assert second.emitted is False
    assert second.reason == "duplicate"
    assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_emit_market_cooldown_blocks_then_allows(db, tmp_path) -> None:
    seed_trade_and_market(db, tx="0xa", condition_id="0xmkt")
    seed_trade_and_market(db, tx="0xb", condition_id="0xmkt")
    seed_trade_and_market(db, tx="0xc", condition_id="0xmkt")
    em = emitter(db, tmp_path, market_cooldown_seconds=300)

    first = em.emit(make_event(tx="0xa", occurred_at=T0), make_result())
    assert first.emitted is True
    # 100s later on the same market -> inside cooldown
    blocked = em.emit(make_event(tx="0xb", occurred_at=T0 + timedelta(seconds=100)), make_result())
    assert blocked.emitted is False and blocked.reason == "market_cooldown"
    # 400s after the first -> cooldown elapsed
    allowed = em.emit(make_event(tx="0xc", occurred_at=T0 + timedelta(seconds=400)), make_result())
    assert allowed.emitted is True
    assert db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2


def test_emit_cooldown_is_per_market(db, tmp_path) -> None:
    seed_trade_and_market(db, tx="0xa", condition_id="0xmkt1")
    seed_trade_and_market(db, tx="0xb", condition_id="0xmkt2")
    em = emitter(db, tmp_path, market_cooldown_seconds=300)
    assert em.emit(make_event(tx="0xa", condition_id="0xmkt1"), make_result()).emitted is True
    # different market at the same instant is unaffected by the cooldown
    assert em.emit(make_event(tx="0xb", condition_id="0xmkt2"), make_result()).emitted is True
