"""Phase 6 - health endpoint and heartbeats.

Two halves:

- **Heartbeats.** Long-running processes (`ingest`, `refresh-stats`, ...) call
  `write_heartbeat` to record "I was alive at T" in the `health` table. It's one
  upserted row per component, so liveness survives restarts and is visible to any
  other process reading the same SQLite file - no shared memory, no extra service.
- **Status.** `gather_status` rolls the heartbeats up with a few cheap DB counts
  (trades, resolved markets, wallet-stats coverage, last trade/alert time) into a
  snapshot, and decides `healthy`: every heartbeat that exists must be fresher
  than `stale_after_seconds`. `serve` exposes that snapshot over GET /health
  (200 healthy / 503 stale) using only the standard library.

Kept dependency-free on purpose: an HTTP server on a Pi shouldn't drag in a web
framework.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from whitewhale import db as db_module

logger = logging.getLogger(__name__)


def write_heartbeat(
    conn: sqlite3.Connection,
    component: str,
    detail: dict | None = None,
    *,
    at: datetime | None = None,
) -> None:
    """Record that `component` is alive now (upsert one row)."""
    at = at or datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO health (component, updated_at, detail_json)
        VALUES (?, ?, ?)
        ON CONFLICT(component) DO UPDATE SET
            updated_at = excluded.updated_at,
            detail_json = excluded.detail_json
        """,
        (component, at.isoformat(), json.dumps(detail or {})),
    )


def _scalar(conn: sqlite3.Connection, sql: str) -> object:
    row = conn.execute(sql).fetchone()
    return row[0] if row is not None else None


def gather_status(
    conn: sqlite3.Connection,
    *,
    stale_after_seconds: float = 900.0,
    now: datetime | None = None,
) -> dict:
    """Assemble a health snapshot. `healthy` is False if any heartbeat is stale."""
    now = now or datetime.now(timezone.utc)

    metrics = {
        "trades": _scalar(conn, "SELECT COUNT(*) FROM trades"),
        "markets": _scalar(conn, "SELECT COUNT(*) FROM markets"),
        "markets_resolved": _scalar(conn, "SELECT COUNT(*) FROM markets WHERE resolved = 1"),
        "wallets": _scalar(conn, "SELECT COUNT(*) FROM wallets"),
        "wallet_stats": _scalar(conn, "SELECT COUNT(*) FROM wallet_stats"),
        "alerts": _scalar(conn, "SELECT COUNT(*) FROM alerts"),
        "last_trade_at": _scalar(conn, "SELECT MAX(occurred_at) FROM trades"),
        "last_alert_at": _scalar(conn, "SELECT MAX(emitted_at) FROM alerts"),
    }

    heartbeats: dict[str, dict] = {}
    stale: list[str] = []
    for row in conn.execute("SELECT component, updated_at, detail_json FROM health"):
        age = _age_seconds(row["updated_at"], now)
        is_stale = age is None or age > stale_after_seconds
        heartbeats[row["component"]] = {
            "updated_at": row["updated_at"],
            "age_seconds": age,
            "stale": is_stale,
            "detail": json.loads(row["detail_json"] or "{}"),
        }
        if is_stale:
            stale.append(row["component"])

    return {
        "healthy": not stale,
        "generated_at": now.isoformat(),
        "stale_after_seconds": stale_after_seconds,
        "stale_components": sorted(stale),
        "metrics": metrics,
        "heartbeats": heartbeats,
    }


def _age_seconds(updated_at: str, now: datetime) -> float | None:
    try:
        ts = datetime.fromisoformat(updated_at)
    except (ValueError, TypeError):
        return None
    return (now - ts).total_seconds()


# --- HTTP server ---------------------------------------------------------------


def make_handler(db_path: str, stale_after_seconds: float):
    """Build a request handler bound to a DB path. One sqlite conn per request
    (connections aren't safe to share across threads)."""

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's required name
            if self.path.rstrip("/") not in ("/health", "/healthz"):
                self.send_error(404, "not found")
                return
            conn = db_module.connect(db_path)
            try:
                status = gather_status(conn, stale_after_seconds=stale_after_seconds)
            finally:
                conn.close()
            body = json.dumps(status, indent=2).encode()
            self.send_response(200 if status["healthy"] else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            logger.info("health %s - %s", self.address_string(), fmt % args)

    return _HealthHandler


def serve(
    db_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    stale_after_seconds: float = 900.0,
) -> None:
    """Run the health HTTP server forever (blocking)."""
    server = ThreadingHTTPServer((host, port), make_handler(db_path, stale_after_seconds))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
