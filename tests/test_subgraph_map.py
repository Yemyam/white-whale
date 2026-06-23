"""Tests for the subgraph fill -> trades row mapping."""

from __future__ import annotations

import pytest

from whitewhale.ingest.subgraph import (
    OrderFilledEvent,
    USDC_ASSET_ID,
    _derive_log_index,
    _map_fill_to_row,
    persist_fill,
)


def _event(
    *,
    maker: str = "0xmaker000000000000000000000000000000000a",
    taker: str = "0xtaker000000000000000000000000000000000b",
    maker_asset: str = USDC_ASSET_ID,
    taker_asset: str = "777777777777777777777777777777777777",
    maker_amount: str = "100_000000",
    taker_amount: str = "200_000000",
    tx: str = "0xtx00000000000000000000000000000000000000000000000000000000000001",
    order_hash: str = "0xa1b2c3d4e5f600000000000000000000000000000000000000000000000000ff",
    timestamp: str = "1750000000",
) -> OrderFilledEvent:
    return OrderFilledEvent(
        id="evt-1",
        transactionHash=tx,
        timestamp=timestamp,
        orderHash=order_hash,
        maker=maker,
        taker=taker,
        makerAssetId=maker_asset,
        takerAssetId=taker_asset,
        makerAmountFilled=maker_amount.replace("_", ""),
        takerAmountFilled=taker_amount.replace("_", ""),
        fee="0",
    )


def test_buy_when_wallet_is_maker_and_pays_usdc() -> None:
    event = _event(maker_amount="50_000000", taker_amount="100_000000")  # $50 -> 100 shares
    out = _map_fill_to_row(event, event.maker.lower())
    assert out is not None
    _, asset_id, side, price, shares, usdc = out
    assert side == "BUY"
    assert asset_id == event.takerAssetId
    assert shares == pytest.approx(100.0)
    assert usdc == pytest.approx(50.0)
    assert price == pytest.approx(0.5)


def test_sell_when_wallet_is_taker_and_receives_usdc() -> None:
    event = _event(maker_amount="50_000000", taker_amount="100_000000")
    out = _map_fill_to_row(event, event.taker.lower())
    assert out is not None
    _, _, side, *_ = out
    assert side == "SELL"


def test_sell_when_wallet_is_maker_and_offers_token() -> None:
    event = _event(
        maker_asset="888888888888888888888888888888888888",
        taker_asset=USDC_ASSET_ID,
        maker_amount="100_000000",  # 100 shares out
        taker_amount="40_000000",   # $40 in
    )
    out = _map_fill_to_row(event, event.maker.lower())
    assert out is not None
    _, asset_id, side, price, shares, usdc = out
    assert side == "SELL"
    assert asset_id == "888888888888888888888888888888888888"
    assert shares == pytest.approx(100.0)
    assert usdc == pytest.approx(40.0)
    assert price == pytest.approx(0.4)


def test_buy_when_wallet_is_taker_and_offers_usdc_against_token_maker() -> None:
    event = _event(
        maker_asset="888888888888888888888888888888888888",
        taker_asset=USDC_ASSET_ID,
    )
    out = _map_fill_to_row(event, event.taker.lower())
    assert out is not None
    _, _, side, *_ = out
    assert side == "BUY"


def test_skip_when_wallet_not_in_event() -> None:
    event = _event()
    assert _map_fill_to_row(event, "0xnotinvolved00000000000000000000000000000") is None


def test_skip_when_both_legs_usdc() -> None:
    event = _event(maker_asset=USDC_ASSET_ID, taker_asset=USDC_ASSET_ID)
    assert _map_fill_to_row(event, event.maker.lower()) is None


def test_skip_when_token_to_token_swap() -> None:
    event = _event(
        maker_asset="999999999999999999999999999999999999",
        taker_asset="888888888888888888888888888888888888",
    )
    assert _map_fill_to_row(event, event.maker.lower()) is None


def test_skip_when_token_amount_zero() -> None:
    event = _event(maker_amount="100_000000", taker_amount="0")
    assert _map_fill_to_row(event, event.maker.lower()) is None


def test_derive_log_index_stable_and_in_range() -> None:
    h = "0xa1b2c3d4e5f600000000000000000000000000000000000000000000000000ff"
    idx = _derive_log_index(h)
    assert idx == _derive_log_index(h)
    assert 0 <= idx < 2**31


def test_persist_fill_is_idempotent(db) -> None:
    event = _event()
    wallet_lc = event.maker.lower()
    assert persist_fill(db, wallet_lc, event) is True
    assert persist_fill(db, wallet_lc, event) is True
    (count,) = db.execute("SELECT COUNT(*) FROM trades").fetchone()
    assert count == 1

    row = db.execute("SELECT condition_id, outcome, outcome_index, wallet FROM trades").fetchone()
    assert row["wallet"] == wallet_lc
    # Subgraph fills don't carry market/outcome metadata - sentinels are written.
    assert row["condition_id"] == ""
    assert row["outcome"] == ""
    assert row["outcome_index"] == -1


def test_persist_fill_returns_false_when_wallet_uninvolved(db) -> None:
    event = _event()
    assert persist_fill(db, "0xunrelated00000000000000000000000000000000", event) is False
    (count,) = db.execute("SELECT COUNT(*) FROM trades").fetchone()
    assert count == 0
