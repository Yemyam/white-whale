"""Polymarket subgraph client - historical wallet-history backfill.

Phase 1 scope: pull every OrderFilledEvent for a given wallet (as maker OR
taker) from Polymarket's public Goldsky `orderbook-subgraph` and write it to
the `trades` table. Feeds the Phase-3 `wallet_stats` calibration for the
labeled-wallets seed (`data/labeled-wallets.csv`).

Designed for laptop runs (per docs/plan.md) - not the Pi - because the
backfill window is multi-month and may sweep tens of thousands of fills per
wallet.

Trade-offs intentionally locked in for Phase 1:
- `OrderFilledEvent` does NOT carry condition_id, outcome, or outcome_index.
  We write `asset_id = <non-USDC token id>` and sentinel values for the others
  (`condition_id=''`, `outcome=''`, `outcome_index=-1`). Token -> condition
  mapping is a Phase-3/5 follow-up via Gamma's `clobTokenIds`.
- `log_index` is derived from the first 8 hex chars of orderHash (& 0x7FFFFFFF).
  Stable per fill, fits SQLite INTEGER, collision probability negligible for
  our trade volume. Solves the multi-fill-per-tx case that breaks the existing
  (tx_hash, log_index) PK when log_index is always 0.

Endpoint pinned by direct probe:
  POST https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw
       /subgraphs/orderbook-subgraph/0.0.1/gn
  No API key required.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from types import TracebackType
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_URL = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw"
    "/subgraphs/orderbook-subgraph/0.0.1/gn"
)
USDC_ASSET_ID = "0"  # collateral leg in OrderFilledEvent


class OrderFilledEvent(BaseModel):
    """Subset of the subgraph entity we persist. extra='allow' for drift."""

    model_config = ConfigDict(extra="allow")

    id: str
    transactionHash: str
    timestamp: str  # BigInt as string
    orderHash: str
    maker: str
    taker: str
    makerAssetId: str
    takerAssetId: str
    makerAmountFilled: str
    takerAmountFilled: str
    fee: str


# Paginate by id_gt cursor (not skip) - subgraphs cap `skip` at 5000. Each page
# is up to PAGE_SIZE items ordered by id ascending so the cursor advances
# monotonically and re-runs are stable.
_PAGE_SIZE = 500
_QUERY_BY_MAKER_OR_TAKER = """
query($wallet: String!, $sinceTs: BigInt!, $idCursor: String!) {
  orderFilledEvents(
    first: %d
    where: {
      or: [
        { maker: $wallet, timestamp_gte: $sinceTs, id_gt: $idCursor }
        { taker: $wallet, timestamp_gte: $sinceTs, id_gt: $idCursor }
      ]
    }
    orderBy: id
    orderDirection: asc
  ) {
    id
    transactionHash
    timestamp
    orderHash
    maker
    taker
    makerAssetId
    takerAssetId
    makerAmountFilled
    takerAmountFilled
    fee
  }
}
""" % _PAGE_SIZE


class SubgraphClient:
    """Async context manager over httpx.AsyncClient with simple pacing."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        timeout_seconds: float = 30.0,
        min_request_interval_seconds: float = 0.1,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self._client: httpx.AsyncClient | None = None
        self._gate = asyncio.Lock()
        self._last_request_at: float = 0.0

    async def __aenter__(self) -> "SubgraphClient":
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_wallet_fills(
        self,
        wallet: str,
        since_ts: int = 0,
    ) -> list[OrderFilledEvent]:
        """All OrderFilledEvents where wallet is maker OR taker, since `since_ts`
        (unix seconds). Returns a flat list ordered by id ascending.
        """
        if self._client is None:
            raise RuntimeError("SubgraphClient must be used as an async context manager")
        wallet_lc = wallet.lower()
        results: list[OrderFilledEvent] = []
        id_cursor = ""
        while True:
            await self._pace()
            variables = {
                "wallet": wallet_lc,
                "sinceTs": str(since_ts),
                "idCursor": id_cursor,
            }
            r = await self._client.post(
                self.url,
                json={"query": _QUERY_BY_MAKER_OR_TAKER, "variables": variables},
            )
            r.raise_for_status()
            body = r.json()
            if "errors" in body:
                raise RuntimeError(f"subgraph error: {body['errors'][:1]}")
            page = body.get("data", {}).get("orderFilledEvents", [])
            if not page:
                break
            for raw in page:
                try:
                    results.append(OrderFilledEvent.model_validate(raw))
                except ValidationError as e:
                    logger.warning("subgraph row validation failed: %s", e.errors()[:1])
            logger.info(
                "fetched %d fills (cursor=%s..., total=%d)",
                len(page),
                (id_cursor[:18] or "(start)"),
                len(results),
            )
            id_cursor = page[-1]["id"]
            if len(page) < _PAGE_SIZE:
                break  # last page
        return results

    async def _pace(self) -> None:
        async with self._gate:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = self.min_request_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = loop.time()


def _derive_log_index(order_hash: str) -> int:
    # First 8 hex chars (32 bits) masked to 31 bits to fit a positive SQLite
    # INTEGER. Stable per orderHash; ~2^31 collision space is plenty.
    return int(order_hash[2:10], 16) & 0x7FFFFFFF


def _map_fill_to_row(
    event: OrderFilledEvent,
    wallet_lc: str,
) -> tuple[str, str, Literal["BUY", "SELL"], float, float, float] | None:
    """Return (tx_hash, asset_id, side, price, size_shares, size_usdc) or None
    if the fill doesn't involve `wallet_lc`. USDC and CTF token amounts are
    both 6-decimal atomic on Polymarket / Polygon.
    """
    maker_lc = event.maker.lower()
    taker_lc = event.taker.lower()
    if wallet_lc not in (maker_lc, taker_lc):
        return None

    maker_is_usdc = event.makerAssetId == USDC_ASSET_ID
    taker_is_usdc = event.takerAssetId == USDC_ASSET_ID
    if maker_is_usdc == taker_is_usdc:
        # Both USDC (impossible) or neither (token<->token; unusual). Skip -
        # can't determine USDC value cleanly.
        return None

    if maker_is_usdc:
        usdc_atomic = int(event.makerAmountFilled)
        token_atomic = int(event.takerAmountFilled)
        token_id = event.takerAssetId
        # maker gave USDC -> bought token; taker did the opposite.
        side: Literal["BUY", "SELL"] = "BUY" if wallet_lc == maker_lc else "SELL"
    else:
        usdc_atomic = int(event.takerAmountFilled)
        token_atomic = int(event.makerAmountFilled)
        token_id = event.makerAssetId
        side = "SELL" if wallet_lc == maker_lc else "BUY"

    if token_atomic == 0:
        return None
    size_shares = token_atomic / 1e6
    size_usdc = usdc_atomic / 1e6
    price = size_usdc / size_shares  # 0..1 on a binary outcome token
    return (event.transactionHash.lower(), token_id, side, price, size_shares, size_usdc)


def persist_fill(
    conn: sqlite3.Connection,
    wallet_lc: str,
    event: OrderFilledEvent,
) -> bool:
    """Insert one OrderFilledEvent as a `trades` row for `wallet_lc`. Returns
    True if a row was attempted (or already present), False if the event was
    skipped (wallet not involved, or token<->token swap).
    """
    mapped = _map_fill_to_row(event, wallet_lc)
    if mapped is None:
        return False
    tx_hash, asset_id, side, price, size_shares, size_usdc = mapped
    occurred_at = datetime.fromtimestamp(int(event.timestamp), tz=timezone.utc).isoformat()
    log_index = _derive_log_index(event.orderHash)
    conn.execute(
        """
        INSERT OR IGNORE INTO trades
            (tx_hash, log_index, occurred_at, wallet, wallet_resolved,
             condition_id, asset_id, outcome, outcome_index, side,
             price, size_shares, size_usdc)
        VALUES (?, ?, ?, ?, 1, '', ?, '', -1, ?, ?, ?, ?)
        """,
        (
            tx_hash,
            log_index,
            occurred_at,
            wallet_lc,
            asset_id,
            side,
            price,
            size_shares,
            size_usdc,
        ),
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO wallets (address, first_seen, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(address) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (wallet_lc, now_iso, now_iso),
    )
    return True


async def backfill_wallet(
    conn: sqlite3.Connection,
    client: SubgraphClient,
    wallet: str,
    since_ts: int = 0,
) -> int:
    """Fetch + persist every fill for `wallet`. Returns rows actually mapped
    (i.e. non-skipped). Idempotent thanks to (tx_hash, log_index) PK + IGNORE.
    """
    wallet_lc = wallet.lower()
    fills = await client.fetch_wallet_fills(wallet_lc, since_ts=since_ts)
    written = 0
    for event in fills:
        if persist_fill(conn, wallet_lc, event):
            written += 1
    logger.info(
        "wallet %s: %d fills fetched, %d trades written",
        wallet_lc[:10] + "...",
        len(fills),
        written,
    )
    return written
