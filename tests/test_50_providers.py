"""Step 5: providers. Ping runs offline and free. Claude and Gemini each get
exactly ONE real generate call (marked live+llm, skippable with -m "not llm"),
asserted through the same generate() -> parse_reply envelope path the harness
uses. A key
that failed step 0 fails here before any tokens are spent.
"""

from __future__ import annotations

import shutil
import uuid

import pytest

from lib.message import Message, parse_reply
from lib.plugins import load_provider
from lib.state import ComponentState
from providers.ping import Ping

ENVELOPE_PROMPT = (
    "This is an automated smoke test. Reply with ONLY this JSON array and "
    'nothing else: [{"channel": "web_chat", "body": "ok"}]'
)


async def test_ping_round_trip_is_channel_less():
    msgs = parse_reply(await Ping("t-ping", {}).generate("anything"))
    assert msgs == [Message(channel="", body="pong")]
    assert msgs[0].channel == "", "raw text carries no channel; routing is the harness's call"


def test_plugin_loaders_resolve():
    from providers.claude import Claude
    from providers.gemini import Gemini
    from lib.plugins import load_service
    from services.cron import Cron
    from services.web_chat import WebChat

    assert load_provider("ping") is Ping, "subclass found without a Plugin marker"
    assert load_provider("claude") is Claude
    assert load_provider("gemini") is Gemini
    assert load_service("web_chat") is WebChat
    assert load_service("cron") is Cron
    assert Claude.REQUIRED_ENV == ("CLAUDE_CODE_OAUTH_TOKEN",)
    assert Gemini.REQUIRED_ENV == ("GEMINI_API_KEY",)
    with pytest.raises(ModuleNotFoundError):
        load_provider("no_such_provider")


def _assert_envelope_reply(msgs: list[Message]) -> None:
    assert msgs, "provider returned nothing"
    routable = [m for m in msgs if m.channel]
    assert routable, f"model did not produce the JSON envelope: {msgs!r}"
    assert routable[0].channel == "web_chat"
    assert routable[0].body


@pytest.mark.live
@pytest.mark.llm
@pytest.mark.timeout(180)
async def test_gemini_one_real_generation(key_ok):
    key_ok("GEMINI_API_KEY")
    from providers.gemini import Gemini

    agent_id = f"t-gem-{uuid.uuid4().hex[:6]}"
    provider = Gemini(agent_id, {})
    _assert_envelope_reply(parse_reply(await provider.generate(ENVELOPE_PROMPT)))
    state = ComponentState(agent_id, "gemini").load()
    assert len(state.get("history", [])) == 2, "prompt and reply persisted"


@pytest.mark.live
@pytest.mark.llm
@pytest.mark.timeout(300)
async def test_claude_one_real_generation(key_ok):
    key_ok("CLAUDE_CODE_OAUTH_TOKEN")
    if not shutil.which("claude"):
        pytest.skip("claude CLI not on PATH")
    from providers.claude import Claude

    agent_id = f"t-cla-{uuid.uuid4().hex[:6]}"
    provider = Claude(agent_id, {"request_timeout": 240})
    _assert_envelope_reply(parse_reply(await provider.generate(ENVELOPE_PROMPT)))
    state = ComponentState(agent_id, "claude").load()
    assert state.get("session_id"), "session id persisted for --resume"
