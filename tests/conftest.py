"""Shared pytest fixtures for the ingestion test suite."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from whitewhale import db as db_module

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """Fresh in-memory SQLite with the full schema applied."""
    conn = db_module.connect(":memory:")
    db_module.init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def rtds_fixtures() -> list[dict]:
    """Captured RTDS messages covering the envelope shapes we probe.

    Order matches tests/fixtures/rtds_sample.jsonl:
      0  bare trade at root
      1  trade wrapped in {payload: {...}}
      2  trade inside {data: [...]}
      3  subscription ack (no trade)
      4  bare trade without proxyWallet
    """
    path = FIXTURES_DIR / "rtds_sample.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
