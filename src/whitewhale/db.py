"""SQLite connection and schema bootstrap."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection tuned for our workload.

    WAL gives us concurrent reads during writes - important on Pi 3 where the
    ingestor writes continuously while the scoring loop reads stats. NORMAL
    sync is fine: trades reappear on next ingest if we lose the most recent
    write to a power cut.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes. Idempotent."""
    conn.executescript(SCHEMA_PATH.read_text())
    _apply_migrations(conn)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    # CREATE TABLE IF NOT EXISTS skips column additions on existing DBs;
    # ALTER TABLE ... ADD COLUMN is the cheapest forward-compat path.
    for stmt in (
        "ALTER TABLE wallets ADD COLUMN pseudonym TEXT",
        "ALTER TABLE markets ADD COLUMN current_price REAL",
        "ALTER TABLE markets ADD COLUMN metadata_updated_at TEXT",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
