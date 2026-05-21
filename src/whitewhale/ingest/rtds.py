"""RTDS WebSocket client - Polymarket's real-time trade firehose.

Connects to wss://ws-live-data.polymarket.com, subscribes to activity/trades,
and forwards parsed trades to a sink. Self-reconnects with exponential backoff.

The subscription wire format is inferred from the official TypeScript client at
github.com/Polymarket/real-time-data-client. The `tap` CLI command exists so we
can verify the exact shape against a live connection before relying on the
parser for the hot path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import ssl
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import certifi
import websockets
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed

from whitewhale.models import RawTrade

logger = logging.getLogger(__name__)

DEFAULT_RTDS_URL = "wss://ws-live-data.polymarket.com"

# Explicit CA bundle - macOS Python.framework ships without system CAs, so
# letting websockets/asyncio pull a default context fails to verify Polymarket's
# cert chain. certifi gives us identical behavior on macOS dev and the Pi.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class RTDSClient:
    """Self-reconnecting client for the RTDS activity/trades feed."""

    def __init__(
        self,
        url: str = DEFAULT_RTDS_URL,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self.url = url
        self.max_backoff = max_backoff_seconds
        self._stop = asyncio.Event()

    async def raw_messages(self) -> AsyncIterator[dict]:
        """Yield decoded JSON messages indefinitely. Reconnects on drop."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                logger.info("connecting to RTDS at %s", self.url)
                async with websockets.connect(self.url, ssl=_SSL_CONTEXT) as ws:
                    await self._subscribe(ws)
                    logger.info("subscribed to activity/trades")
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            logger.warning("non-JSON message: %s", str(raw)[:200])
            except ConnectionClosed as e:
                logger.warning("RTDS connection closed: %s", e)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("unexpected RTDS error")
            sleep_for = min(backoff, self.max_backoff)
            logger.info("reconnecting in %.1fs", sleep_for)
            await asyncio.sleep(sleep_for)
            backoff *= 2

    async def trades(self) -> AsyncIterator[RawTrade]:
        """Yield parsed RawTrade events. Non-trade messages are skipped silently."""
        async for msg in self.raw_messages():
            trade = _extract_trade(msg)
            if trade is not None:
                yield trade

    async def _subscribe(self, ws) -> None:
        payload = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "activity", "type": "trades"},
            ],
        }
        await ws.send(json.dumps(payload))

    def stop(self) -> None:
        self._stop.set()


def _extract_trade(msg: dict) -> RawTrade | None:
    """Try to coerce a raw RTDS message into a RawTrade.

    The wire envelope wraps the trade payload - exact shape varies. We probe
    a few common shapes and validate.
    """
    candidates: list[dict] = []
    if isinstance(msg, dict):
        if "transactionHash" in msg:
            candidates.append(msg)
        for key in ("payload", "data", "message"):
            inner = msg.get(key)
            if isinstance(inner, dict) and "transactionHash" in inner:
                candidates.append(inner)
            elif isinstance(inner, list):
                candidates.extend(x for x in inner if isinstance(x, dict))
    for cand in candidates:
        try:
            return RawTrade.model_validate(cand)
        except ValidationError:
            continue
    return None


def _normalize_timestamp(ts: int | str) -> datetime:
    if isinstance(ts, int):
        # Polymarket timestamps may be seconds or ms - disambiguate by magnitude.
        if ts > 10_000_000_000:
            ts = ts // 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def persist(conn: sqlite3.Connection, trade: RawTrade) -> None:
    """Write a parsed trade to SQLite. Idempotent on (tx_hash, log_index).

    Wallet attribution is deferred. A follow-up resolver job (Polygon RPC or
    subgraph) will fill in `wallet` and set `wallet_resolved = 1`.
    """
    occurred_at = _normalize_timestamp(trade.timestamp)
    size_usdc = trade.size * trade.price
    now_iso = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO markets (condition_id, slug, question, event_slug, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(condition_id) DO UPDATE SET
            slug       = COALESCE(NULLIF(excluded.slug, ''), markets.slug),
            question   = COALESCE(NULLIF(excluded.question, ''), markets.question),
            event_slug = COALESCE(excluded.event_slug, markets.event_slug),
            last_seen  = excluded.last_seen
        """,
        (
            trade.conditionId,
            trade.slug or "",
            trade.title or "",
            trade.eventSlug,
            now_iso,
            now_iso,
        ),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO trades
            (tx_hash, log_index, occurred_at, wallet, wallet_resolved,
             condition_id, asset_id, outcome, outcome_index, side,
             price, size_shares, size_usdc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade.transactionHash,
            0,
            occurred_at.isoformat(),
            None,
            0,
            trade.conditionId,
            trade.asset,
            trade.outcome,
            trade.outcomeIndex,
            trade.side,
            trade.price,
            trade.size,
            size_usdc,
        ),
    )
