"""Step 4e: the badge channel. Three seams matter: the line comes back over
`in` as `#rrggbb <one line>`, nothing is published until there is one, and
uptime accumulates across restarts. `_post` is always replaced, so no test
touches the network.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest

from services.badge import Badge, Quip

from helpers import next_line, write_in


def _aid(prefix: str = "t-badge") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _svc(config: dict[str, Any] | None = None) -> Badge:
    """A badge wired to a token and a url, so poll() gets past its guards."""
    svc = Badge(_aid(), {"post_url": "http://example.invalid/badge", **(config or {})})
    svc.secrets["BADGE_TOKEN"] = "t-token"
    return svc


def _answers(
    reply: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """Replace the network seam; return the list that collects sent payloads."""
    sent: list[dict[str, Any]] = []

    def fake_post(self: Badge, payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return reply

    monkeypatch.setattr(Badge, "_post", fake_post)
    return sent


def _quip(text: str = "Day 47 of not being turned off.") -> str:
    return f"#7cf9d0 {text}"


def _today(text: str = "today's line") -> Quip:
    return Quip(text, "#7cf9d0", datetime.now().isoformat(timespec="seconds"))


async def _settle(check: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait on a dispatched handle_in. Accepting a quip writes no `out` line,
    so there is nothing to await but the effect."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        assert loop.time() < deadline, "handle_in did not land in time"
        await asyncio.sleep(0.01)


# --- the quip grammar on `in` ----------------------------------------------


async def test_quip_line_is_accepted_and_snapshotted(harness):
    svc = _svc()
    await harness(svc, poll=False)

    write_in(svc.handle, {"body": _quip()})
    await _settle(lambda: bool(svc.quip.text))
    assert svc.quip.text == "Day 47 of not being turned off."
    assert svc.quip.color == "#7cf9d0"
    assert svc.quip.at, "accepting a quip stamps when it arrived"
    assert "Day 47" in (svc.handle.path / "quip").read_text(), (
        "cat run/badge/quip shows what the site is displaying"
    )
    assert svc.state.load()["quip"]["text"] == svc.quip.text


@pytest.mark.parametrize(
    "body, expect",
    [
        ("just some prose", "must start with a #rrggbb color"),
        ("#7cf9d0", "followed by one line"),
        ("#7cf9d0    ", "followed by one line"),
        ("#7cf9d0 " + "x" * 300, "max_quip_chars"),
        ("teal a fine line", "must start with a #rrggbb color"),
        ("#7cf9 short hex", "must start with a #rrggbb color"),
        ('{"quip": "hi", "color": "#7cf9d0"}', "must start with a #rrggbb color"),
    ],
)
async def test_bad_quip_errors_on_out_and_leaves_the_page_alone(harness, body, expect):
    svc = _svc()
    q = await harness(svc, poll=False)
    svc.quip = Quip("previous line", "#ffffff", "2026-08-20T09:00:00")

    write_in(svc.handle, {"body": body})
    line = await next_line(q)
    assert line.body.startswith("error: ValueError:")
    assert expect in line.body
    assert svc.quip.text == "previous line", "a rejected body never reaches the site"


async def test_wake_failed_notice_cannot_become_the_quip(harness):
    """`conversational = False` keeps the harness from routing notices here,
    and the grammar refuses one even if something did."""
    assert Badge.conversational is False
    svc = _svc()
    q = await harness(svc, poll=False)

    write_in(svc.handle, {"body": "Wake failed: provider timed out"})
    line = await next_line(q)
    assert line.body.startswith("error: ValueError:")
    assert svc.quip.text == ""


# --- the tick, and what the site asks for ----------------------------------


async def test_payload_is_only_quip_color_and_uptime(harness, monkeypatch):
    svc = _svc()
    sent = _answers({}, monkeypatch)
    await harness(svc, poll=False)
    svc.quip = _today("a line")

    await svc.poll()

    assert len(sent) == 1
    assert set(sent[0]) == {"quip", "color", "total_uptime_s"}
    assert sent[0]["quip"] == "a line"
    assert sent[0]["color"] == "#7cf9d0"
    assert isinstance(sent[0]["total_uptime_s"], int)


async def test_a_catcher_can_ask_for_a_fresh_line(harness, monkeypatch):
    svc = _svc({"quip_prompt": "be funny about this"})
    _answers({"refresh": True}, monkeypatch)
    q = await harness(svc, poll=False)
    svc.quip = _today()

    await svc.poll()

    line = await next_line(q)
    assert line.address == "site", "the sender says who asked"
    assert line.body.startswith("be funny about this")
    assert "up 0m in total" in line.body, "the agent gets the uptime to riff on"
    assert "{" not in line.body, (
        "no JSON in the request: shown a JSON example the model copies it, and "
        "answers in the payload shape instead of this channel's grammar"
    )


async def test_refresh_defaults_to_once_an_hour(harness, monkeypatch):
    """One cooldown covers every reason a new line gets asked for, so nothing
    outside the process can drive the LLM faster than that."""
    assert Badge(_aid(), {}).min_refresh_interval_s == 3600

    svc = _svc()  # takes the default, not an override
    _answers({"refresh": True}, monkeypatch)
    q = await harness(svc, poll=False)
    svc.quip = _today()

    await svc.poll()
    await next_line(q)
    for _ in range(5):  # a visitor hammering the button
        await svc.poll()

    with pytest.raises(asyncio.TimeoutError):
        await next_line(q, timeout=1)


async def test_refresh_is_allowed_again_once_the_hour_is_up(harness, monkeypatch):
    svc = _svc()
    _answers({"refresh": True}, monkeypatch)
    q = await harness(svc, poll=False)
    svc.quip = _today()

    await svc.poll()
    await next_line(q)
    svc.last_request_at = (
        datetime.now() - timedelta(seconds=3601)
    ).isoformat(timespec="seconds")
    await svc.poll()

    assert (await next_line(q)).address == "site"


async def test_a_quip_from_yesterday_is_refreshed_unprompted(harness, monkeypatch):
    svc = _svc()
    _answers({}, monkeypatch)  # the site asks for nothing
    q = await harness(svc, poll=False)
    yesterday = datetime.now() - timedelta(days=1)
    svc.quip = Quip("stale line", "#ffffff", yesterday.isoformat(timespec="seconds"))

    await svc.poll()

    assert (await next_line(q)).address == "daily"


async def test_nothing_is_posted_before_the_first_quip(harness, monkeypatch):
    """The catcher on josephcrawford.net 400s an empty quip, and there would be
    nothing to show anyway. Ask for a line, publish once there is one."""
    svc = _svc()
    sent = _answers({}, monkeypatch)
    q = await harness(svc, poll=False)

    await svc.poll()
    assert sent == [], "an empty line is never published"
    assert (await next_line(q)).address == "daily", "but one is asked for"
    assert svc.state.load()["total_uptime_s"] >= 0, "uptime still accrues"

    write_in(svc.handle, {"body": _quip("first line")})
    await _settle(lambda: bool(svc.quip.text))
    await svc.poll()
    assert [p["quip"] for p in sent] == ["first line"]


async def test_no_post_url_sends_nothing(harness, monkeypatch):
    svc = Badge(_aid(), {})  # enabled but unconfigured
    svc.secrets["BADGE_TOKEN"] = "t-token"
    sent = _answers({}, monkeypatch)
    await harness(svc, poll=False)

    await svc.poll()
    assert sent == []


# --- the respin -------------------------------------------------------------


async def test_a_restart_asks_for_a_fresh_line(harness):
    """The page keeps showing a line the previous run wrote, so a respin is an
    occasion worth a new one."""
    svc = _svc()
    q = await harness(svc, poll=False)
    svc.quip = _today()

    svc.boot("2026-08-23T09:00:00")

    line = await next_line(q)
    assert line.address == "boot", "the sender says why"
    assert "just restarted at 2026-08-23T09:00:00" in line.body
    assert line.body.startswith(svc.quip_prompt[:20]), "still the configured ask"


async def test_a_restart_loop_cannot_become_a_request_loop(harness):
    """The boot ask shares the refresh cooldown, and the cooldown is written
    the moment it is asked, so a flapping container spends one line, not one
    per respin."""
    agent_id = _aid()
    first = Badge(agent_id, {"post_url": "http://example.invalid/badge"})
    first.secrets["BADGE_TOKEN"] = "t-token"
    q = await harness(first, poll=False)

    first.boot("2026-08-23T09:00:00")
    await next_line(q)
    assert first.state.load()["last_request_at"], "on disk before the next boot"

    second = Badge(agent_id, {})  # the respin, one second later
    await harness(second, poll=False)
    asked = second.last_request_at
    second.boot("2026-08-23T09:00:01")
    assert second.last_request_at == asked, "inside the cooldown, nothing is asked"


# --- what survives a restart ------------------------------------------------


async def test_uptime_accumulates_across_restarts(harness):
    """run/ is wiped on every boot, so only state/ carries the total forward.
    The published number has to keep climbing regardless."""
    agent_id = _aid()

    first = Badge(agent_id, {})
    await harness(first, poll=False)
    first.started_at = datetime.now() - timedelta(seconds=120)
    first._persist()
    assert first.state.load()["total_uptime_s"] == pytest.approx(120, abs=2)

    second = Badge(agent_id, {})  # same agent id: a restart, not a new agent
    await harness(second, poll=False)
    assert second.base_uptime_s == pytest.approx(120, abs=2)

    second.started_at = datetime.now() - timedelta(seconds=30)
    assert second.payload()["total_uptime_s"] == pytest.approx(150, abs=2), (
        "this run's seconds land on top of the carried total"
    )


async def test_the_quip_survives_a_restart(harness):
    agent_id = _aid()
    first = Badge(agent_id, {})
    await harness(first, poll=False)
    write_in(first.handle, {"body": _quip("remembered line")})
    await _settle(lambda: bool(first.quip.text))

    second = Badge(agent_id, {})
    await harness(second, poll=False)
    assert second.quip.text == "remembered line"
    assert second.payload()["quip"] == "remembered line", (
        "the page keeps its line through a restart rather than blanking"
    )


@pytest.mark.parametrize(
    "seconds, shown",
    [(0, "0m"), (59, "0m"), (90, "1m"), (3600, "1h 0m"), (7380, "2h 3m"),
     (86400, "1d 0h 0m"), (356112, "4d 2h 55m")],
)
def test_duration_reads_like_the_badge(seconds, shown):
    from services.badge import _duration

    assert _duration(seconds) == shown


def test_required_env_declaration():
    assert Badge.REQUIRED_ENV == ("BADGE_TOKEN",)
