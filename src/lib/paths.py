"""Filesystem roots, env-overridable.

Defaults are the container paths, so a Docker run behaves exactly as before.
Set the `MICROAGENT_*` vars to run the daemon on a host — see
`scripts/run_local.sh`.

These are read once at import time, so anything that sets them (a shell
export, or the bootstrap `load_dotenv()` in main.py) must run before the
first `lib.*` import.
"""

from __future__ import annotations

import os
from pathlib import Path


def _root(var: str, default: str) -> Path:
    return Path(os.getenv(var, default)).expanduser()


CONFIG_DIR = _root("MICROAGENT_CONFIG_DIR", "/config")
STATE_DIR = _root("MICROAGENT_STATE_DIR", "/state")
SPACE_DIR = _root("MICROAGENT_SPACE_DIR", "/space")
REPO_DIR = _root("MICROAGENT_REPO_DIR", "/repo")

CONFIG_TOML = CONFIG_DIR / "config.toml"
CONFIG_ENV = CONFIG_DIR / ".env"
SOUL_PATH = CONFIG_DIR / "soul.md"
