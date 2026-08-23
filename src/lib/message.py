"""The envelope the harness and its services agree on."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import cast

log = logging.getLogger(__name__)

SENDER = "from"       # inbound wire key: who sent it
ADDRESS = "address"   # outbound wire key: who it's for
STATUS = "status"     # outbound wire key: what the agent is doing right now


class MessageError(ValueError):
    """One message doesn't fit the contract."""


@dataclass(frozen=True)
class Message:
    """One line on a channel. `address` is the other end. An empty `channel` is
    unrouted text: a reply the provider couldn't shape into messages, left for
    the harness to place.

    A line is a message or a status, never both, and `status` says which: None
    for a message, whose text is its `body`. A status is the harness saying
    what the agent is doing while it works on that channel's message, for the
    channel to show however it likes, or to ignore; an empty one says the work
    is over and there is nothing left to show."""

    channel: str
    address: str = ""
    body: str = ""
    status: str | None = None

    def to_wire(self, key: str) -> dict[str, str]:
        """The line written to a handle, `key` naming the field the address
        travels in. The channel doesn't travel — it's the dir we write into."""
        text = {"body": self.body} if self.status is None else {STATUS: self.status}
        return {key: self.address, **text}

    @classmethod
    def from_wire(cls, channel: str, line: dict[str, str], key: str) -> Message:
        """One line off a handle. Lenient, unlike `parse_reply`: the agent may
        write these by hand, and a service should see a message with empty
        fields (and refuse it itself) rather than never see it."""
        return cls(channel, line.get(key, ""), line.get("body", ""), line.get(STATUS))


def parse_reply(reply: str) -> list[Message]:
    """A provider's whole reply → the messages it asked to send. Every loop
    shape answers in the same envelope, so extraction lives here."""
    try:
        objects = _array(reply)
    except MessageError as e:
        log.warning("unparseable response (%s); keeping raw text: %r", e, reply)
        return [Message(channel="", body=reply)]
    out: list[Message] = []
    for obj in objects:
        try:
            out.append(_message(obj))
        except MessageError as e:
            log.warning("dropping malformed message: %s", e)
    return out


def _array(reply: str) -> list[object]:
    """The JSON array out of a reply that may have prose wrapped around it.
    The slice starts at `[`, so a successful parse is always a list."""
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        raise MessageError("no JSON array in response")
    try:
        return json.loads(reply[start : end + 1])
    except json.JSONDecodeError as e:
        raise MessageError(f"malformed JSON: {e}") from e


def _message(obj: object) -> Message:
    """One object out of the array. Shape only — whether a channel accepts the
    envelope is the service's call, at delivery time."""
    if not isinstance(obj, dict):
        raise MessageError(f"not an object: {obj!r}")
    fields = cast(dict[str, object], obj)  # JSON object keys are always str
    return Message(_field(fields, "channel"), _field(fields, ADDRESS, ""), _field(fields, "body"))


def _field(obj: dict[str, object], key: str, default: str | None = None) -> str:
    """One string field; no default means required and non-empty."""
    value = obj.get(key, default)
    if not isinstance(value, str) or (default is None and not value):
        raise MessageError(f"bad `{key}`: {value!r}")
    return value


class Batch(list[Message]):
    """One wake's messages, in arrival order across all channels."""

    @property
    def channels(self) -> list[str]:
        """Contributing channels, first-arrival order, no repeats."""
        return list(dict.fromkeys(m.channel for m in self))

    def reply_to(self, channel: str) -> str:
        """Sender of the most recent message on `channel` — who an unaddressed
        -text or error notice is owed a reply."""
        return next((m.address for m in reversed(self) if m.channel == channel), "")
