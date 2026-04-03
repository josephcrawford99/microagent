import json
import os
import glob as globmod


SOUL_DIR = os.environ.get("SOUL_DIR", "/soul")
DATA_DIR = os.environ.get("DATA_DIR", "/data")


def load_config():
    path = os.path.join(SOUL_DIR, "config.json")
    with open(path) as f:
        return json.load(f)


def load_soul_prompt():
    parts = []

    soul_path = os.path.join(SOUL_DIR, "soul.md")
    if os.path.exists(soul_path):
        with open(soul_path) as f:
            parts.append(f.read().strip())

    context_dir = os.path.join(SOUL_DIR, "context")
    if os.path.isdir(context_dir):
        for md in sorted(globmod.glob(os.path.join(context_dir, "*.md"))):
            with open(md) as f:
                parts.append(f.read().strip())

    return "\n\n".join(parts)
