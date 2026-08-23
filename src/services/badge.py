"""Badge channel: publishes how long the agent has been up, plus the line the
agent wrote for the site to show.

The other end of this channel is a web page. Every tick the service POSTs the
payload below. The agent sets the line by replying on this channel:

    [{"channel": "badge", "body": "#7cf9d0 Day 47 of not being turned off."}]

A hex color, a space, then one line. That is the whole grammar, so an agent
told "change your badge" over any channel can do it in one reply.

Request: POST <post_url>, Content-Type: application/json,
Authorization: Bearer $BADGE_TOKEN

    {
      "quip": "Day 47 of not being turned off. Watered zero plants.",
      "color": "#7cf9d0",
      "total_uptime_s": 356112
    }

The bearer token is what authorizes the update; the site should reject a POST
without it. `total_uptime_s` is every second the agent has run, summed across
restarts, so the number only ever climbs. Nothing else travels: no message
text, no counts, no channel names.

Nothing is posted until a line exists: an empty quip is not worth showing, and
a catcher is entitled to refuse one. Uptime still accrues in the meantime.

Response: a JSON object, and `{}` is the normal answer. If a catcher ever wants
a fresh line it can reply `{"refresh": true}` and the service will ask the
agent for one, no more than `min_refresh_interval_s` apart. Other keys, a
non-JSON body, and an empty body are all ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, cast

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)

USAGE = "#rrggbb <one line>"
DEFAULT_PROMPT = (
    "Write today's line for the badge on my public website. One sentence, dry "
    "and funny, about life as an always-on agent. Never quote or reference "
    "anything from a private conversation."
)
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
USER_AGENT = "microagent-badge/1"


@dataclass(frozen=True)
class Quip:
    """The line the site displays, and the color it renders in. `at` never
    travels; it is how the service knows today has a line already."""

    text: str = ""
    color: str = ""
    at: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Quip:
        if not isinstance(d, dict):
            return cls()
        return cls(
            str(d.get("text", "")), str(d.get("color", "")), str(d.get("at", ""))
        )


class Badge(Service):
    """Uptime out to a website, one quip back from the agent."""

    name = "badge"
    REQUIRED_ENV = ("BADGE_TOKEN",)
    # `in` takes a grammar, not prose. Same reason as cron: the harness must
    # never route unaddressed text or a "Wake failed" notice here, or it would
    # land on a public page.
    conversational = False
    BODY_HINT = USAGE
    poll_interval_s = 30.0
    # A page that refuses an update is not urgent, and the catcher is
    # someone else's host: retry no faster than a normal tick.
    error_backoff_s = 60.0

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.post_url = str(config.get("post_url", ""))
        self.min_refresh_interval_s = int(config.get("min_refresh_interval_s", 3600))
        self.max_quip_chars = int(config.get("max_quip_chars", 200))
        self.quip_prompt = str(config.get("quip_prompt", DEFAULT_PROMPT)).strip()
        self.request_timeout = int(config.get("request_timeout", 15))
        if not self.post_url:
            log.warning("badge: no post_url in config.toml; nothing will be sent")
        self.started_at = datetime.now()
        self.quip = Quip()
        self.base_uptime_s = 0
        self.last_request_at = ""

    async def start(self, *, poll: bool = True) -> None:
        await super().start(poll=poll)
        saved = self.state.load()
        self.base_uptime_s = int(saved.get("total_uptime_s", 0) or 0)
        self.quip = Quip.from_dict(saved.get("quip"))
        self.last_request_at = str(saved.get("last_request_at", ""))
        self._persist()

    async def poll(self) -> None:
        """One tick: publish, then do whatever the site asked for."""
        if not self.post_url:
            return  # misconfigured, and already warned about at construction
        if self.quip.at[:10] != datetime.now().strftime("%Y-%m-%d"):
            self.request_quip("daily")
        # An empty line is not worth publishing, and a catcher is entitled to
        # refuse one, so wait for the agent's first quip before posting.
        if self.quip.text:
            answer = await asyncio.to_thread(self._post, self.payload())
            if answer.get("refresh"):
                self.request_quip("site")
        self._persist()  # uptime accrues whether or not there is a line yet

    async def handle_in(self, msg: Message) -> None:
        """Take one line. Prose is a parse error on purpose: this text goes on
        a public page, so nothing reaches it unshaped."""
        self.quip = self._parse_quip(msg.body)
        self._persist()
        log.info("badge: new quip (%s)", self.quip.color)

    def boot(self, at: str) -> None:
        """A respin is worth a line: the page is still showing one written by a
        run that no longer exists. Shares the refresh cooldown, so a restart
        loop cannot turn into a request loop."""
        self.request_quip("boot", f"The process just restarted at {at}.")

    def request_quip(self, reason: str, note: str = "") -> None:
        """Ask the agent for a new line, `note` saying what prompted it when
        the occasion is worth a mention. Every reason shares one cooldown, so
        nothing outside this process can drive the LLM faster than that."""
        now = datetime.now()
        if self._cooling(now):
            log.info("badge: %s refresh inside cooldown, skipped", reason)
            return
        self.last_request_at = now.isoformat(timespec="seconds")
        self._persist()  # a cooldown only holds a restart back if it is on disk
        body = "\n\n".join(p for p in (self.quip_prompt, note, self._facts()) if p)
        self.write_out(sender=reason, body=body)

    def _facts(self) -> str:
        """What the line can be about, in prose. Deliberately not the payload
        JSON: shown a JSON example, the model copies it, and answers with the
        payload shape instead of this channel's grammar."""
        up = _duration(self._total_uptime_s(datetime.now()))
        if not self.quip.text:
            return f"You have been up {up} in total. There is no line yet."
        return f'You have been up {up} in total. The line now reads: "{self.quip.text}"'

    def payload(self) -> dict[str, Any]:
        """Everything the site renders."""
        return {
            "quip": self.quip.text,
            "color": self.quip.color,
            "total_uptime_s": self._total_uptime_s(datetime.now()),
        }

    def _total_uptime_s(self, now: datetime) -> int:
        """Seconds up across every run, so the badge number only climbs."""
        session = max(0, int((now - self.started_at).total_seconds()))
        return self.base_uptime_s + session

    def _cooling(self, now: datetime) -> bool:
        if not self.last_request_at:
            return False
        try:
            asked = datetime.fromisoformat(self.last_request_at)
        except ValueError:
            return False
        return (now - asked).total_seconds() < self.min_refresh_interval_s

    def _parse_quip(self, body: str) -> Quip:
        """One line: a hex color, a space, then the quip. A grammar rather
        than nested JSON, for the same reason cron takes `in 300 water plants`
        — the model writes this inside a JSON `body` string, and one level of
        escaping is enough. ValueErrors propagate; the Service base writes them
        as error lines on `out`, so the agent sees the rejection."""
        color, _, text = body.strip().partition(" ")
        text = text.strip()
        if not HEX_COLOR.match(color):
            raise ValueError(f"body must start with a #rrggbb color: {USAGE}")
        if not text or "\n" in text:
            raise ValueError(f"the color must be followed by one line: {USAGE}")
        if len(text) > self.max_quip_chars:
            raise ValueError(
                f"quip is {len(text)} chars, over "
                f"max_quip_chars={self.max_quip_chars}"
            )
        return Quip(text, color, datetime.now().isoformat(timespec="seconds"))

    def _persist(self) -> None:
        """One write for everything durable, plus the quip as a cat-able
        status file next to the handles."""
        self.state.save({
            "total_uptime_s": self._total_uptime_s(datetime.now()),
            "quip": asdict(self.quip),
            "last_request_at": self.last_request_at,
        })
        if self.quip.text:
            self.handle.snapshot(
                "quip", f"{self.quip.text}\n{self.quip.color} {self.quip.at}\n"
            )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish one payload, return what the site asks for. Blocking, so
        it runs in a thread; it is also the seam the tests replace."""
        req = urllib.request.Request(
            self.post_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.secrets['BADGE_TOKEN']}",
                # Named, because a WAF in front of the catcher blocks the
                # default Python-urllib signature outright.
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            raise RuntimeError(f"badge POST HTTP {e.code}: {detail}") from e
        try:
            answer: object = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            return {}  # the site owes us nothing; a page of HTML is not a fault
        if not isinstance(answer, dict):
            return {}
        return cast(dict[str, Any], answer)  # JSON object keys are always str


def _duration(seconds: int) -> str:
    """Uptime the way the badge renders it, so the model and the page agree."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"
