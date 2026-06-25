"""Tests for Phase 6 health snapshot, heartbeats, and the HTTP handler."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from whitewhale import db as db_module
from whitewhale import health

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _drive_get(handler_cls, path: str) -> tuple[int, bytes]:
    """Invoke a request handler's do_GET in-process with fake I/O.

    The standard way to unit-test an http.server handler without a real socket:
    bypass __init__, stub the response-writing methods to capture the status, and
    send the body to a BytesIO. Keeps these tests fast and deterministic (a live
    ThreadingHTTPServer adds seconds of socket/teardown latency on macOS).
    """
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.wfile = io.BytesIO()
    captured: dict[str, int] = {}
    h.send_response = lambda code, *a: captured.__setitem__("status", code)
    h.send_error = lambda code, *a, **k: captured.__setitem__("status", code)
    h.send_header = lambda *a, **k: None
    h.end_headers = lambda: None
    h.do_GET()
    return captured["status"], h.wfile.getvalue()


def test_write_heartbeat_upserts(db) -> None:
    health.write_heartbeat(db, "ingest", {"trades": 5}, at=T0)
    health.write_heartbeat(db, "ingest", {"trades": 9}, at=T0 + timedelta(minutes=1))
    rows = db.execute("SELECT component, detail_json FROM health").fetchall()
    assert len(rows) == 1  # upsert, not insert
    assert json.loads(rows[0]["detail_json"])["trades"] == 9


def test_gather_status_healthy_when_fresh(db) -> None:
    health.write_heartbeat(db, "ingest", at=T0)
    status = health.gather_status(db, stale_after_seconds=900, now=T0 + timedelta(seconds=60))
    assert status["healthy"] is True
    assert status["stale_components"] == []
    assert status["heartbeats"]["ingest"]["age_seconds"] == 60


def test_gather_status_unhealthy_when_stale(db) -> None:
    health.write_heartbeat(db, "ingest", at=T0)
    status = health.gather_status(db, stale_after_seconds=900, now=T0 + timedelta(hours=1))
    assert status["healthy"] is False
    assert status["stale_components"] == ["ingest"]


def test_gather_status_no_heartbeats_is_healthy(db) -> None:
    # Nothing has registered liveness yet -> vacuously healthy, with empty map.
    status = health.gather_status(db, now=T0)
    assert status["healthy"] is True
    assert status["heartbeats"] == {}


def test_gather_status_includes_metrics(db) -> None:
    now = T0.isoformat()
    db.execute(
        """
        INSERT INTO trades (tx_hash, log_index, occurred_at, wallet, condition_id,
            asset_id, outcome, outcome_index, side, price, size_shares, size_usdc)
        VALUES ('0x1', 0, ?, '0xw', '0xa', 'tok', 'YES', 0, 'BUY', 0.5, 100, 50)
        """,
        (now,),
    )
    status = health.gather_status(db, now=T0)
    assert status["metrics"]["trades"] == 1
    assert status["metrics"]["last_trade_at"] == now


def test_http_handler_serves_health(tmp_path) -> None:
    # On-disk DB: the handler opens its own connection per request.
    db_path = tmp_path / "wh.db"
    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    health.write_heartbeat(conn, "ingest", at=datetime.now(timezone.utc))
    conn.close()

    handler = health.make_handler(str(db_path), stale_after_seconds=900)
    status, body = _drive_get(handler, "/health")
    assert status == 200
    payload = json.loads(body)
    assert payload["healthy"] is True
    assert "ingest" in payload["heartbeats"]

    # unknown path 404s
    code, _ = _drive_get(handler, "/nope")
    assert code == 404


def test_http_handler_503_when_stale(tmp_path) -> None:
    db_path = tmp_path / "wh.db"
    conn = db_module.connect(db_path)
    db_module.init_schema(conn)
    health.write_heartbeat(conn, "ingest", at=T0)  # far in the past -> stale
    conn.close()

    handler = health.make_handler(str(db_path), stale_after_seconds=1)
    status, body = _drive_get(handler, "/health")
    assert status == 503
    assert json.loads(body)["healthy"] is False
