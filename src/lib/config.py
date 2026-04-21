import json
import os
from typing import Any


SOUL_DIR = "/repo/soul"
DATA_DIR = "/data"


def load_config() -> dict[str, Any]:
    path = os.path.join(SOUL_DIR, "config.json")
    with open(path) as f:
        return json.load(f)


def load_soul_prompt() -> str:
    with open(os.path.join(SOUL_DIR, "soul.md")) as f:
        return f.read().strip()
