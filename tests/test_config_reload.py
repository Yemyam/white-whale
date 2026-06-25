"""Tests for Phase 6 config hot-reload (ReloadableConfig)."""

from __future__ import annotations

import os
import time

from whitewhale.config import ReloadableConfig


def _write(path, text) -> None:
    path.write_text(text)


def test_loads_initial_data(tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, "db:\n  path: ./x.db\nstats:\n  refresh_interval_seconds: 100\n")
    rc = ReloadableConfig(cfg)
    assert rc.data["stats"]["refresh_interval_seconds"] == 100


def test_reload_picks_up_changes(tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, "stats:\n  refresh_interval_seconds: 100\n")
    rc = ReloadableConfig(cfg)
    assert rc.reload_if_changed() is False  # unchanged

    _write(cfg, "stats:\n  refresh_interval_seconds: 200\n")
    _bump_mtime(cfg)
    assert rc.reload_if_changed() is True
    assert rc.data["stats"]["refresh_interval_seconds"] == 200
    # second call with no further change -> False
    assert rc.reload_if_changed() is False


def test_reload_keeps_last_good_on_parse_error(tmp_path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, "stats:\n  refresh_interval_seconds: 100\n")
    rc = ReloadableConfig(cfg)

    _write(cfg, "stats:\n  refresh_interval_seconds: [unterminated\n")
    _bump_mtime(cfg)
    # malformed YAML -> swallowed, data unchanged, reports no change
    assert rc.reload_if_changed() is False
    assert rc.data["stats"]["refresh_interval_seconds"] == 100


def _bump_mtime(path) -> None:
    # Ensure the mtime visibly advances even on coarse-grained filesystems.
    t = time.time() + 10
    os.utime(path, (t, t))
