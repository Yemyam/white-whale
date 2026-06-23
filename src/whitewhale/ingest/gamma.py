"""Gamma REST client - Polymarket's market metadata source.

Cold path. Enriches the `markets` table with `liquidity_usdc`, `current_price`,
and `resolves_at`, all of which RTDS does not provide and the scoring engine
needs. Market discovery stays with RTDS; this module only refreshes rows that
already exist.

Endpoint shape pinned by direct probe:
  GET /markets?condition_ids=0x…&condition_ids=0x… (repeated-key multi-value)
  -> JSON array of market objects. Fields used: conditionId, slug, question,
     liquidityNum, lastTradePrice, endDate (ISO 8601 UTC with trailing Z).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from types import TracebackType

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://gamma-api.polymarket.com"
# Keep URL length sane on /markets?condition_ids=…&condition_ids=… queries.
# Empirically the API accepts at least this many ids per request.
_BATCH_SIZE = 50


class GammaMarket(BaseModel):
    """Subset of the Gamma /markets response we persist.

    extra="allow" mirrors RawTrade - lets schema additions on Polymarket's side
    land without crashing the enricher.
    """

    model_config = ConfigDict(extra="allow")

    conditionId: str
    slug: str | None = None
    question: str | None = None
    liquidityNum: float | None = None
    lastTradePrice: float | None = None
    endDate: str | None = None


class GammaClient:
    """Async context manager wrapping httpx.AsyncClient with rate-limit pacing."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 10.0,
        min_request_interval_seconds: float = 0.05,
    ) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self._client: httpx.AsyncClient | None = None
        self._gate = asyncio.Lock()
        self._last_request_at: float = 0.0

    async def __aenter__(self) -> "GammaClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
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

    async def fetch_by_condition_ids(self, ids: list[str]) -> list[GammaMarket]:
        """Fetch markets for the given condition_ids. Batches internally."""
        if not ids:
            return []
        if self._client is None:
            raise RuntimeError("GammaClient must be used as an async context manager")

        out: list[GammaMarket] = []
        for batch in _chunked(ids, _BATCH_SIZE):
            await self._pace()
            params = [("condition_ids", cid) for cid in batch]
            r = await self._client.get("/markets", params=params)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                logger.warning("unexpected /markets payload type: %s", type(data).__name__)
                continue
            for raw in data:
                try:
                    out.append(GammaMarket.model_validate(raw))
                except ValidationError as e:
                    logger.warning("gamma market validation failed: %s", e.errors()[:1])
        return out

    async def _pace(self) -> None:
        # Coarse single-process rate limiter. Token bucket is Phase 6.
        async with self._gate:
            now = asyncio.get_event_loop().time()
            wait = self.min_request_interval_seconds - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = asyncio.get_event_loop().time()


def _chunked(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _normalize_iso(value: str | None) -> str | None:
    """Coerce Gamma's `endDate` (e.g. "2026-07-31T12:00:00Z") into our canonical
    ISO 8601 UTC form. Returns None when the field is missing or unparseable.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("could not parse endDate: %r", value)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _pick_stale(
    conn: sqlite3.Connection,
    limit: int,
    max_age_seconds: int,
) -> list[str]:
    """Return condition_ids for markets whose metadata is missing or older than
    max_age_seconds. Oldest-first so refreshes drain the backlog.
    """
    if max_age_seconds <= 0:
        rows = conn.execute(
            "SELECT condition_id FROM markets "
            "ORDER BY metadata_updated_at IS NULL DESC, metadata_updated_at ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        cutoff_iso = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - max_age_seconds,
            tz=timezone.utc,
        ).isoformat()
        # ISO 8601 lexicographic order == chronological order, so < works.
        rows = conn.execute(
            """
            SELECT condition_id FROM markets
            WHERE metadata_updated_at IS NULL OR metadata_updated_at < ?
            ORDER BY metadata_updated_at IS NULL DESC, metadata_updated_at ASC
            LIMIT ?
            """,
            (cutoff_iso, limit),
        ).fetchall()
    return [r["condition_id"] for r in rows]


def upsert_market(conn: sqlite3.Connection, market: GammaMarket) -> None:
    """Upsert one Gamma market. RTDS owns slug/question (COALESCE-preserved);
    Gamma is canonical for liquidity_usdc, current_price, resolves_at.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO markets (condition_id, slug, question, liquidity_usdc,
                             current_price, resolves_at, metadata_updated_at,
                             first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(condition_id) DO UPDATE SET
            slug                = COALESCE(NULLIF(excluded.slug, ''),     markets.slug),
            question            = COALESCE(NULLIF(excluded.question, ''), markets.question),
            liquidity_usdc      = excluded.liquidity_usdc,
            current_price       = excluded.current_price,
            resolves_at         = excluded.resolves_at,
            metadata_updated_at = excluded.metadata_updated_at,
            last_seen           = excluded.last_seen
        """,
        (
            market.conditionId,
            market.slug or "",
            market.question or "",
            market.liquidityNum,
            market.lastTradePrice,
            _normalize_iso(market.endDate),
            now_iso,
            now_iso,
            now_iso,
        ),
    )


async def enrich_pending(
    conn: sqlite3.Connection,
    client: GammaClient,
    limit: int = 100,
    max_age_seconds: int = 3600,
) -> int:
    """Pick up to `limit` stale markets, fetch from Gamma, upsert. Returns the
    count of rows actually refreshed (i.e. that came back from Gamma).
    """
    ids = _pick_stale(conn, limit=limit, max_age_seconds=max_age_seconds)
    if not ids:
        logger.info("no stale markets to refresh")
        return 0
    logger.info("refreshing %d markets from gamma", len(ids))
    markets = await client.fetch_by_condition_ids(ids)
    for m in markets:
        upsert_market(conn, m)
    missing = len(ids) - len(markets)
    if missing > 0:
        logger.warning("gamma returned no rows for %d of %d requested ids", missing, len(ids))
    logger.info("refreshed %d markets", len(markets))
    return len(markets)
