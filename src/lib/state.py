"""Per-agent, per-component state files at state/<agent_id>/<component>.json.
"""

from __future__ import annotations

import json
import logging
from typing import Any, cast, overload

from lib.paths import STATE_DIR as STATE_ROOT, write_atomic

log = logging.getLogger(__name__)


class ComponentState:
    def __init__(self, agent_id: str, component: str) -> None:
        self._path = STATE_ROOT / agent_id / f"{component}.json"

    def load(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return parsed JSON, or `default` (or {}) if missing/corrupt.
        Corruption is logged so it's visible, not silenced."""
        if default is None:
            default = {}
        try:
            with self._path.open() as f:
                data = json.load(f)
        except FileNotFoundError:
            return dict(default)
        except (OSError, json.JSONDecodeError):
            log.exception("failed to load %s; using default", self._path)
            return dict(default)
        if not isinstance(data, dict):
            return dict(default)
        return cast(dict[str, Any], data)  # JSON object keys are always str

    @overload
    def get_int(self, key: str) -> int | None: ...
    @overload
    def get_int(self, key: str, default: int) -> int: ...

    def get_int(self, key: str, default: int | None = None) -> int | None:
        """One int field out of the state file, tolerating garbage."""
        try:
            return int(self.load()[key])
        except (KeyError, TypeError, ValueError):
            return default

    def save(self, data: dict[str, Any]) -> None:
        """Atomic write via tmp + os.replace. Creates parent dirs as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self._path, json.dumps(data, indent=2) + "\n")
