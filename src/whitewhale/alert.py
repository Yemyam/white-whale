"""Phase 4 - alert emission.

Turns a scored whale event into the agreed alert JSON (see docs/plan.md), drops
it where the downstream bot can read it, and records it in the `alerts` table for
audit. Three gates keep a churning market from spamming the bot:

1. score gate    - only emit when `score.total >= min_score`
2. market cooldown - at most one alert per market per `market_cooldown_seconds`
                     (measured on trade time, so it's deterministic for replay)
3. idempotency   - `INSERT OR IGNORE` on alerts' UNIQUE(tx_hash, log_index); a
                   trade already alerted is never emitted twice

The sink is a file drop (one JSON file per alert) - the architecture's "JSON drop
to existing bot". `_write_sink` is the only I/O seam, so a webhook sink slots in
there later without touching the gating logic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from whitewhale.filter import WhaleEvent
from whitewhale.scoring.inputs import ScoreResult

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class AlertConfig:
    """Tunables for the `alerts` config block."""

    min_score: int = 60
    market_cooldown_seconds: float = 300.0
    drop_dir: str = "./alerts"
    schema_version: str = SCHEMA_VERSION

    @classmethod
    def from_config(cls, cfg: dict) -> AlertConfig:
        a = cfg.get("alerts", {})
        return cls(
            min_score=int(a.get("min_score", 60)),
            market_cooldown_seconds=float(a.get("market_cooldown_seconds", 300)),
            drop_dir=str(a.get("drop_dir", "./alerts")),
            schema_version=str(a.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass
class EmitOutcome:
    """Result of attempting to emit one alert."""

    emitted: bool
    reason: str  # "ok" | "below_min_score" | "market_cooldown" | "duplicate"
    alert_id: str | None = None
    path: str | None = None


def build_payload(
    conn: sqlite3.Connection,
    event: WhaleEvent,
    result: ScoreResult,
    *,
    alert_id: str,
    emitted_at: str,
    schema_version: str = SCHEMA_VERSION,
) -> dict:
    """Assemble the full alert JSON for a scored whale event (docs/plan.md schema)."""
    trade_row = conn.execute(
        "SELECT asset_id, size_shares FROM trades WHERE tx_hash = ? AND log_index = ?",
        (event.tx_hash, event.log_index),
    ).fetchone()
    market_row = conn.execute(
        """
        SELECT slug, question, event_slug, liquidity_usdc, current_price, resolves_at
        FROM markets WHERE condition_id = ?
        """,
        (event.condition_id,),
    ).fetchone()

    asset_id = trade_row["asset_id"] if trade_row else None
    shares = trade_row["size_shares"] if trade_row else None

    slug = question = event_slug = resolves_at = None
    liquidity = current_price = None
    if market_row is not None:
        slug = market_row["slug"] or None
        question = market_row["question"] or None
        event_slug = market_row["event_slug"]
        liquidity = market_row["liquidity_usdc"]
        current_price = market_row["current_price"]
        resolves_at = market_row["resolves_at"]

    url_slug = event_slug or slug
    hours_to_resolution = _hours_to_resolution(resolves_at, event.occurred_at)

    return {
        "schema_version": schema_version,
        "alert_id": alert_id,
        "emitted_at": emitted_at,
        "trade": {
            "tx_hash": event.tx_hash,
            "occurred_at": event.occurred_at.isoformat(),
            "wallet": event.wallet,
            "side": event.side,
            "outcome": event.outcome,
            "outcome_token_id": asset_id,
            "price": event.price,
            "size_usdc": event.size_usdc,
            "shares": shares,
        },
        "market": {
            "condition_id": event.condition_id,
            "slug": slug,
            "question": question,
            "url": f"https://polymarket.com/event/{url_slug}" if url_slug else None,
            "liquidity_usdc": liquidity,
            "current_price": current_price,
            "resolves_at": resolves_at,
            "hours_to_resolution": hours_to_resolution,
        },
        "score": {
            "total": result.total,
            "confidence": result.confidence,
            "components": result.components,
            "rationale": result.rationale,
        },
    }


class AlertEmitter:
    """Applies the gates, persists the audit row, and drops the alert file."""

    def __init__(self, conn: sqlite3.Connection, config: AlertConfig) -> None:
        self.conn = conn
        self.config = config
        self._last_market_alert: dict[str, datetime] = {}

    def emit(self, event: WhaleEvent, result: ScoreResult) -> EmitOutcome:
        if result.total < self.config.min_score:
            return EmitOutcome(False, "below_min_score")

        prev = self._last_market_alert.get(event.condition_id)
        if (
            prev is not None
            and (event.occurred_at - prev).total_seconds() < self.config.market_cooldown_seconds
        ):
            return EmitOutcome(False, "market_cooldown")

        alert_id = str(uuid.uuid4())
        emitted_at = datetime.now(timezone.utc).isoformat()
        payload = build_payload(
            self.conn,
            event,
            result,
            alert_id=alert_id,
            emitted_at=emitted_at,
            schema_version=self.config.schema_version,
        )

        if not self._persist(event, result, alert_id, emitted_at, payload):
            # A row already exists for this trade -> already alerted.
            return EmitOutcome(False, "duplicate")

        path = self._write_sink(alert_id, payload)
        self._last_market_alert[event.condition_id] = event.occurred_at
        return EmitOutcome(True, "ok", alert_id=alert_id, path=path)

    def _persist(
        self,
        event: WhaleEvent,
        result: ScoreResult,
        alert_id: str,
        emitted_at: str,
        payload: dict,
    ) -> bool:
        """Insert the audit row. Returns False if this trade was already alerted."""
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO alerts
                (alert_id, emitted_at, tx_hash, log_index, score_total, confidence, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                emitted_at,
                event.tx_hash,
                event.log_index,
                result.total,
                result.confidence,
                json.dumps(payload),
            ),
        )
        return cur.rowcount > 0

    def _write_sink(self, alert_id: str, payload: dict) -> str:
        """File-drop sink: one JSON file per alert. The only I/O seam."""
        drop_dir = Path(self.config.drop_dir)
        drop_dir.mkdir(parents=True, exist_ok=True)
        path = drop_dir / f"{alert_id}.json"
        path.write_text(json.dumps(payload, indent=2))
        logger.info("alert %s dropped to %s (score %s)", alert_id, path, payload["score"]["total"])
        return str(path)


def _hours_to_resolution(resolves_at: str | None, occurred_at: datetime) -> float | None:
    if not resolves_at:
        return None
    try:
        resolves = datetime.fromisoformat(resolves_at)
    except ValueError:
        return None
    return (resolves - occurred_at).total_seconds() / 3600.0
