"""One mutable directory: MICROAGENT_HOME (default <repo>/.microagent).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import dotenv_values

REPO_DIR = Path(__file__).resolve().parents[2]  # src/lib/paths.py -> repo root

# The repo-root .env may set MICROAGENT_HOME, so it loads before HOME resolves.
# setdefault: a variable already in the environment always wins.
for _k, _v in dotenv_values(REPO_DIR / ".env").items():
    if _v is not None:
        os.environ.setdefault(_k, _v)

HOME = Path(os.getenv("MICROAGENT_HOME", str(REPO_DIR / ".microagent"))).expanduser()

CONFIG_TOML = HOME / "config.toml"
CONFIG_ENV = next(
    (p for p in (HOME / ".env", REPO_DIR / ".env") if p.is_file()), HOME / ".env"
)
SOUL_PATH = HOME / "soul.md"
STATE_DIR = HOME / "state"
SPACE_DIR = HOME / "space"
RUN_DIR = HOME / "run"  # ephemeral IPC; contents wiped each boot

_DEFAULTS = REPO_DIR / "src" / "defaults"


def ensure_home() -> None:
    """Create the home tree and seed config on first boot. Idempotent."""
    for d in (STATE_DIR, SPACE_DIR, RUN_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Wipe run/'s contents; unlink symlinks rather than following them.
    for p in RUN_DIR.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    if not CONFIG_TOML.exists():
        shutil.copy(_DEFAULTS / "config.default.toml", CONFIG_TOML)
    if not SOUL_PATH.exists():
        shutil.copy(_DEFAULTS / "soul.default.md", SOUL_PATH)


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write a file via tmp + rename so readers never see a partial write."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)
