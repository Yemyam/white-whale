"""Configuration loader. YAML files with optional local override."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "default.yaml"


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Load config from `path` (or the default). Returns a dict."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(target) as f:
        return yaml.safe_load(f)


class ReloadableConfig:
    """Config that re-reads its YAML when the file changes on disk (Phase 6).

    Periodic loops call `reload_if_changed()` each cycle and rebuild their typed
    config (WhaleConfig, ScoringConfig, ...) only when it returns True - so you can
    tune thresholds and weights without restarting the daemon. A parse error on
    reload is swallowed (keeping the last-good config) so a half-written edit can't
    take the process down; the caller is told nothing changed.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        self.data: dict[str, Any] = load(self.path)
        self._mtime = self._current_mtime()

    def _current_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def reload_if_changed(self) -> bool:
        """Reload if the file's mtime advanced. Returns True iff `data` was replaced."""
        mtime = self._current_mtime()
        if mtime is None or mtime == self._mtime:
            return False
        try:
            new_data = load(self.path)
        except (OSError, yaml.YAMLError):
            # Mid-edit / unreadable: keep the last-good config, retry next cycle.
            return False
        self._mtime = mtime
        self.data = new_data
        return True
