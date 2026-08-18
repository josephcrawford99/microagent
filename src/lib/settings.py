"""Everything that touches the on-disk config: `config.toml` (non-secret
settings) at the microagent home root, and `.env` (secrets) at either the home
root or the repo root. see `lib/paths.py`.
"""

from __future__ import annotations

from typing import Any

import tomllib
from dotenv import dotenv_values

from lib.paths import CONFIG_ENV, CONFIG_TOML, write_atomic


class Config:
    """Parsed config.toml. `Config()` reads the file; attributes are plain
    dicts — services and providers pull what they need with .get()."""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        if raw is None:
            try:
                raw = tomllib.loads(CONFIG_TOML.read_text())
            except FileNotFoundError:
                raw = {}
        services: dict[str, Any] = raw.get("services") or {}
        self.services: dict[str, dict[str, Any]] = {
            name: cfg for name, cfg in services.items() if isinstance(cfg, dict)
        }
        agents = raw.get("agents") or {"primary": {"provider": "ping"}}
        self.agent_id, agent_cfg = next(iter(agents.items()))
        self.agent: dict[str, Any] = agent_cfg or {}
        self.provider = str(self.agent.get("provider", ""))
        dash: dict[str, Any] = raw.get("dashboard") or {}
        self.dashboard_enabled = bool(dash.get("enabled", False))
        self.dashboard_host = str(dash.get("host", "0.0.0.0"))
        self.dashboard_port = int(dash.get("port", 8767))

    def enabled_services(self) -> dict[str, dict[str, Any]]:
        return {n: c for n, c in self.services.items() if c.get("enabled")}


def read_env() -> dict[str, str]:
    """Secrets as a plain dict; a missing file reads as empty."""
    return {k: v or "" for k, v in dotenv_values(CONFIG_ENV).items()}


def write_env(entries: dict[str, str]) -> None:
    """Preserve comments / order of existing keys; append new ones at the end.
    Keys absent from `entries` are dropped (UI delete)."""
    try:
        with CONFIG_ENV.open() as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(entries)
    out: list[str] = []
    for line in lines:
        s = line.lstrip()
        if s.startswith("#") or "=" not in s:
            out.append(line)
            continue
        k = s.split("=", 1)[0].strip()
        if k in remaining:
            out.append(f"{k}={remaining.pop(k)}\n")
    for k, v in remaining.items():
        out.append(f"{k}={v}\n")
    CONFIG_ENV.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(CONFIG_ENV, "".join(out))
