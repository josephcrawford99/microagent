"""Session sandbox + shared fixtures.

The block at module top runs before any `src` import anywhere in the suite:
`lib/paths.py` freezes MICROAGENT_HOME and CONFIG_ENV at import time, and
CONFIG_ENV falls back to the real repo-root .env when HOME/.env is missing —
so the tmp home and its empty .env must exist first, or config tests would
write into the user's real secrets file.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_HOME = Path(tempfile.mkdtemp(prefix="microagent-test-home-"))
os.environ["MICROAGENT_HOME"] = str(_HOME)
(_HOME / ".env").write_text("")
sys.path.insert(0, str(REPO / "src"))

from dotenv import dotenv_values  # noqa: E402

# Real keys reach live tests through the environment; file I/O stays sandboxed.
if (REPO / ".env").is_file():
    for _k, _v in dotenv_values(REPO / ".env").items():
        if _v is not None:
            os.environ.setdefault(_k, _v)

from lib import paths  # noqa: E402

paths.ensure_home()

import pytest  # noqa: E402

# Filled by test_00_credentials, read by the llm tests in test_50_providers:
# a key that failed validation must never reach a token-spending call.
KEY_STATUS: dict[str, bool] = {}


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_HOME, ignore_errors=True)


@pytest.fixture(scope="session")
def home() -> Path:
    return _HOME


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture
def key_ok():
    """Fail fast on keys that already failed step-0 validation."""

    def check(name: str) -> None:
        if not os.environ.get(name):
            pytest.skip(f"{name} not set")
        if KEY_STATUS.get(name) is False:
            pytest.fail(f"{name} failed validation in test_00_credentials; not spending tokens")

    return check


@pytest.fixture
async def harness():
    """Start services and collect their `out` FIFO lines into a queue.

    Yields an async `start(service) -> asyncio.Queue`; teardown detaches and
    closes every reader fd this fixture opened.
    """
    opened: list[int] = []
    loop = asyncio.get_running_loop()

    async def start(svc, **kwargs):
        await svc.start(**kwargs)
        q: asyncio.Queue = asyncio.Queue()
        fd = svc.handle.add_reader("out", q.put_nowait)
        opened.append(fd)
        return q

    yield start
    for fd in opened:
        loop.remove_reader(fd)
        os.close(fd)
