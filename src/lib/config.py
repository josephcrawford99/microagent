import json
import os
from typing import Any


SOUL_DIR = "/repo/soul"
DATA_DIR = "/data"
OVERLAY_PATH = os.path.join(DATA_DIR, "config.local.json")


def load_base_config() -> dict[str, Any]:
    with open(os.path.join(SOUL_DIR, "config.json")) as f:
        return json.load(f)


def load_overlay() -> dict[str, Any]:
    try:
        with open(OVERLAY_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_overlay(overlay: dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = OVERLAY_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(overlay, f, indent=2)
        f.write("\n")
    os.replace(tmp, OVERLAY_PATH)


def deep_merge(base: Any, overlay: Any) -> Any:
    """Overlay wins. Dicts merge recursively; lists and scalars replace."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = deep_merge(base.get(k), v) if k in base else v
        return out
    return overlay


_SAME = object()


def compute_overlay(base: Any, desired: Any) -> Any:
    """Minimal structure that, deep-merged over base, yields desired.

    Returns _SAME when desired matches base exactly (caller drops the key).
    Keys present in base but absent from desired are ignored — overlay can't
    represent deletion, which is acceptable for runtime edits.
    """
    if isinstance(base, dict) and isinstance(desired, dict):
        out: dict[str, Any] = {}
        for k, v in desired.items():
            if k not in base:
                out[k] = v
                continue
            sub = compute_overlay(base[k], v)
            if sub is not _SAME:
                out[k] = sub
        return out if out else _SAME
    return _SAME if base == desired else desired


def load_config() -> dict[str, Any]:
    return deep_merge(load_base_config(), load_overlay())


def load_soul_prompt() -> str:
    with open(os.path.join(SOUL_DIR, "soul.md")) as f:
        return f.read().strip()
