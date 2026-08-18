"""Step 0: every configured credential is checked against its real endpoint
before anything else runs, so a bad key fails the suite immediately. None of
these calls spends LLM tokens. Offline runs: `pytest -m "not live"`.
"""

from __future__ import annotations

import imaplib
import json
import os
import shutil
import subprocess
import tomllib
import urllib.request

import pytest

from conftest import KEY_STATUS, REPO

pytestmark = pytest.mark.live


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set")
    return value


def _record(name: str):
    """Mark the key valid only if the test body finished without failing."""

    class _Recorder:
        def __enter__(self):
            KEY_STATUS[name] = False
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                KEY_STATUS[name] = True
            return False

    return _Recorder()


def _real_config() -> dict:
    """The user's live config.toml (not the sandbox seed), for service opts."""
    path = REPO / ".microagent" / "config.toml"
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text())


def test_gemini_key_is_valid():
    _require("GEMINI_API_KEY")
    with _record("GEMINI_API_KEY"):
        from google import genai

        client = genai.Client()
        models = list(client.models.list())
        assert models, "Gemini key accepted but no models visible"


def test_claude_cli_present_and_token_set():
    _require("CLAUDE_CODE_OAUTH_TOKEN")
    with _record("CLAUDE_CODE_OAUTH_TOKEN"):
        assert shutil.which("claude"), "claude CLI not on PATH"
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=20
        )
        assert out.returncode == 0, out.stderr
        # No free auth-check endpoint exists; real auth is proven by the single
        # llm-marked generate call in test_50_providers.


def test_telegram_token_is_valid():
    token = _require("TELEGRAM_BOT_TOKEN")
    with _record("TELEGRAM_BOT_TOKEN"):
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=15
        ) as r:
            body = json.loads(r.read())
        assert body.get("ok") is True, body


def test_email_credentials_log_in():
    password = _require("EMAIL_PASSWORD")
    email_cfg = (_real_config().get("services") or {}).get("email") or {}
    username = email_cfg.get("username", "")
    if not username:
        pytest.skip("no email username configured in .microagent/config.toml")
    with _record("EMAIL_PASSWORD"):
        imap = imaplib.IMAP4_SSL(
            str(email_cfg.get("imap_host", "imap.gmail.com")),
            int(email_cfg.get("imap_port", 993)),
        )
        try:
            imap.login(username, password)
        finally:
            imap.logout()
