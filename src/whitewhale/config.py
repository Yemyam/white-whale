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
