"""Step 4d: the credentialed channels (telegram, email). Offline half checks
the contract each keeps at the FIFO boundary when credentials or addresses are
missing; live half (opt-in via MICROAGENT_TEST_SEND=1, since it sends real
messages) pushes one envelope through each for real.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from services.email import Email
from services.telegram import Telegram

from helpers import next_line, write_in


def _aid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def test_required_env_declarations():
    assert Telegram.REQUIRED_ENV == ("TELEGRAM_BOT_TOKEN",)
    assert Email.REQUIRED_ENV == ("EMAIL_PASSWORD",)


async def test_telegram_without_token_errors_on_out(harness, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    svc = Telegram(_aid("t-tg"), {})
    q = await harness(svc)  # no token: poll loop is not started

    write_in(svc.handle, {"address": "12345", "body": "hi"})
    line = await next_line(q)
    assert line.body == "error: RuntimeError: telegram token not set"


async def test_telegram_without_address_errors_on_out(harness, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    svc = Telegram(_aid("t-tg"), {})
    q = await harness(svc)
    svc.token = "fake-token-never-used"  # fails before any network call

    write_in(svc.handle, {"body": "no address"})
    line = await next_line(q)
    assert "needs an `address`" in line.body


async def test_email_without_address_errors_on_out(harness, monkeypatch):
    monkeypatch.setattr(Email, "_drain", lambda self: [])  # keep poll off the network
    svc = Email(_aid("t-em"), {})
    q = await harness(svc)

    write_in(svc.handle, {"body": "Subject: hi\n\nno recipient"})
    line = await next_line(q)
    assert line.body == "error: RuntimeError: email needs an `address`"


async def test_email_inbound_mail_joins_subject_into_body(harness, monkeypatch):
    drained = []

    def fake_drain(self):
        if not drained:
            drained.append(True)
            return [("friend@example.com", "Greetings", "hello there")]
        return []

    monkeypatch.setattr(Email, "_drain", fake_drain)
    svc = Email(_aid("t-em"), {})
    q = await harness(svc)

    line = await next_line(q)
    assert (line.address, line.body) == (
        "friend@example.com", "Subject: Greetings\n\nhello there",
    ), "inbound mail arrives in the same Subject-line shape the model writes"


def test_email_body_contract_subject_line():
    assert Email._split_subject("Subject: Hi\n\nbody text") == ("Hi", "body text")
    assert Email._split_subject("no header at all") == ("(no subject)", "no header at all")
    assert Email._split_subject("Subject:\n\nx") == ("(no subject)", "x")


# --- live sends: real messages leave the machine, so double opt-in ----------

_SEND = os.environ.get("MICROAGENT_TEST_SEND") == "1"
send_gate = pytest.mark.skipif(
    not _SEND, reason="set MICROAGENT_TEST_SEND=1 to send real test messages"
)


@pytest.mark.live
@send_gate
async def test_telegram_real_send(harness):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_TEST_CHAT_ID")
    if not token or not chat_id:
        pytest.skip("TELEGRAM_BOT_TOKEN and TELEGRAM_TEST_CHAT_ID required")
    svc = Telegram(_aid("t-tg-live"), {})
    # poll=False: skip the getUpdates loop so we never steal updates from a
    # production bot instance.
    q = await harness(svc, poll=False)
    write_in(svc.handle, {"address": chat_id, "body": "microagent test suite ping"})
    with pytest.raises(asyncio.TimeoutError):
        await next_line(q, timeout=8)  # silence on `out` means the send worked


@pytest.mark.live
@send_gate
async def test_email_real_send(harness, repo):
    import tomllib

    password = os.environ.get("EMAIL_PASSWORD")
    config_path = repo / ".microagent" / "config.toml"
    if not password or not config_path.is_file():
        pytest.skip("EMAIL_PASSWORD and a real config.toml required")
    email_cfg = (tomllib.loads(config_path.read_text()).get("services") or {}).get("email") or {}
    if not email_cfg.get("username"):
        pytest.skip("no email username configured")

    svc = Email(_aid("t-em-live"), email_cfg)
    q = await harness(svc, poll=False)  # no IMAP poll loop
    write_in(svc.handle, {
        "address": email_cfg["username"],
        "body": "Subject: microagent test suite\n\nself-addressed test send",
    })
    with pytest.raises(asyncio.TimeoutError):
        await next_line(q, timeout=15)
