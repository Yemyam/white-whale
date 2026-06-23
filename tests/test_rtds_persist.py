"""Tests for the RTDS parser and persist() write path."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from whitewhale.ingest.rtds import _extract_trade, _normalize_timestamp, persist


def test_extract_trade_bare_envelope(rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[0])
    assert trade is not None
    assert trade.transactionHash == "0xaaaa000000000000000000000000000000000000000000000000000000000001"
    assert trade.proxyWallet == "0xwalletaaa0000000000000000000000000000001"


def test_extract_trade_payload_wrapper(rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[1])
    assert trade is not None
    assert trade.side == "SELL"


def test_extract_trade_data_list(rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[2])
    assert trade is not None
    assert trade.size == 5000.0


def test_extract_trade_skips_non_trade(rtds_fixtures: list[dict]) -> None:
    assert _extract_trade(rtds_fixtures[3]) is None


def test_extract_trade_handles_missing_wallet(rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[4])
    assert trade is not None
    assert trade.proxyWallet is None


@pytest.mark.parametrize(
    "ts, expected_iso",
    [
        (1_750_000_000, "2025-06-15T15:06:40+00:00"),
        (1_750_000_000_000, "2025-06-15T15:06:40+00:00"),
        ("2026-06-01T12:00:00Z", "2026-06-01T12:00:00+00:00"),
    ],
)
def test_normalize_timestamp(ts: int | str, expected_iso: str) -> None:
    assert _normalize_timestamp(ts) == datetime.fromisoformat(expected_iso)


def test_persist_inserts_trade_market_and_wallet(db, rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[0])
    assert trade is not None
    persist(db, trade)

    row = db.execute("SELECT * FROM trades").fetchone()
    assert row["tx_hash"] == trade.transactionHash
    assert row["wallet"] == "0xwalletaaa0000000000000000000000000000001"
    assert row["wallet_resolved"] == 1
    assert row["size_usdc"] == pytest.approx(trade.size * trade.price)

    market = db.execute("SELECT * FROM markets WHERE condition_id = ?", (trade.conditionId,)).fetchone()
    assert market["slug"] == "trump-vs-newsom-2026"
    assert market["question"] == "Will Trump beat Newsom in 2026?"

    wallet = db.execute("SELECT * FROM wallets WHERE address = ?", (trade.proxyWallet,)).fetchone()
    assert wallet["pseudonym"] == "Theo4"
    assert wallet["display_name"] == "Theo"


def test_persist_is_idempotent_on_pk(db, rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[0])
    assert trade is not None
    persist(db, trade)
    persist(db, trade)
    (count,) = db.execute("SELECT COUNT(*) FROM trades").fetchone()
    assert count == 1


def test_persist_preserves_seeded_label_and_cluster(db, rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[0])
    assert trade is not None
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        INSERT INTO wallets (address, label, cluster_id, first_seen, last_seen)
        VALUES (?, 'theo', 'theo-cluster', ?, ?)
        """,
        (trade.proxyWallet, now, now),
    )

    persist(db, trade)

    wallet = db.execute(
        "SELECT label, cluster_id, pseudonym, display_name FROM wallets WHERE address = ?",
        (trade.proxyWallet,),
    ).fetchone()
    assert wallet["label"] == "theo"
    assert wallet["cluster_id"] == "theo-cluster"
    assert wallet["pseudonym"] == "Theo4"
    assert wallet["display_name"] == "Theo"


def test_persist_handles_missing_wallet(db, rtds_fixtures: list[dict]) -> None:
    trade = _extract_trade(rtds_fixtures[4])
    assert trade is not None
    persist(db, trade)
    row = db.execute("SELECT wallet, wallet_resolved FROM trades").fetchone()
    assert row["wallet"] is None
    assert row["wallet_resolved"] == 0
    (wallet_rows,) = db.execute("SELECT COUNT(*) FROM wallets").fetchone()
    assert wallet_rows == 0
