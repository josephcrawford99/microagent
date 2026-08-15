"""Interface: a Source that can also send.

Channels that are bidirectional (socket, email, telegram, web_chat)
subclass this. Receive-only feeds (imessage, calendars, file watchers)
subclass `lib.source.Source` directly.

The only difference is `send()`. The agent-facing surface (`poll`, `send`,
`emit_pending`) is generated in `src/tools/<name>.py` via
`lib.tools.interface_tools`, which keys off this class to decide whether a
channel gets a send tool at all.
"""

from __future__ import annotations

from lib.source import Message, Source, Trigger


class Interface(Source):
    """A Source that also has an outbound path. Subclasses implement
    `send()`, returning a short status string for the tool result."""

    async def send(self, message: Message) -> str:
        del message
        raise NotImplementedError


# Re-exports so `from lib.interface import Interface, Message, Trigger`
# (the pattern every existing interface file uses) keeps working.
__all__ = ["Interface", "Message", "Source", "Trigger"]
