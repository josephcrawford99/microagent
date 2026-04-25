"""Per-agent, per-component state files at /state/<agent_id>/<component>.json.

Every bit of runtime state the harness owns — watermarks, session ids, idle
flags — routes through ComponentState. One file per (agent, component) pair
so there's no write contention between interfaces and the agent; each owner
just reads and writes its own slice independently.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

STATE_ROOT = Path("/state")


class ComponentState:
    def __init__(self, agent_id: str, component: str) -> None:
        self._path = STATE_ROOT / agent_id / f"{component}.json"

    @property
    def path(self) -> Path:
        return self._path

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
        return data if isinstance(data, dict) else dict(default)

    def load_or_init(self, init: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """First-boot-safe load: if the file doesn't exist, call `init()` to
        produce the initial contents, persist them, and return.

        iMessage uses this to seed last_seen=current_max_rowid on first boot so
        the agent doesn't drown in historical messages."""
        if self._path.exists():
            return self.load()
        seeded = init()
        self.save(seeded)
        return seeded

    def save(self, data: dict[str, Any]) -> None:
        """Atomic write via tmp + os.replace. Creates parent dirs as needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, self._path)
