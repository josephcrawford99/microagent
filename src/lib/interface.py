import json
from dataclasses import asdict, dataclass
from typing import ClassVar, Optional

from claude_agent_sdk import tool


@dataclass
class Message:
    """Canonical message payload — every interface speaks in these.

    `to` and `sender` are universal addressing fields so any agent (including
    the ping smoke test) can build a reply by swapping them. Subclasses extend
    with extra fields (e.g. EmailMessage adds `subject`).

    SCHEMA is a ClassVar (not a dataclass field) describing the JSON schema the
    send tool exposes to the agent — keys must match dataclass field names so
    `cls(**args)` constructs the message directly from tool args.
    """

    body: str = ""
    to: str = ""
    sender: str = ""

    SCHEMA: ClassVar[dict] = {"body": str}


@dataclass
class Trigger:
    """Base wake-trigger. Each interface subclass defines its own with extra fields."""

    interface: "Interface"


class Interface:
    """Base for communication interfaces.

    Subclasses set `name`, optionally set `message_class` (defaults to Message),
    and implement trigger_wake(), receive(), and send(). The default tools()
    auto-generates `{name}_receive` and `{name}_send` MCP tools that delegate
    to receive() and send() — no override needed.
    """

    name: str
    message_class: type[Message] = Message

    def __init__(self, config):
        self.config = config

    # --- lifecycle ---

    def trigger_wake(self) -> Optional[Trigger]:
        """Return a Trigger if the agent should wake, else None.

        Called every poll tick — keep cheap. Each interface owns its own Trigger
        subclass and constructs it here when it has something to report.
        """
        raise NotImplementedError

    # --- I/O ---

    async def receive(self) -> list[Message]:
        """Consume and return any pending inbound messages.

        Subclasses may return a list of a Message subclass with richer fields;
        those extra fields will appear in the receive tool's JSON output.
        Raise on failure — the tool wrapper turns it into an MCP error result.
        """
        raise NotImplementedError

    async def send(self, message: Message) -> str:
        """Send an outbound message. Return a short status string on success.
        Raise on failure — the tool wrapper turns it into an MCP error result.
        """
        raise NotImplementedError

    # --- MCP tool exposure ---

    def tools(self) -> list:
        """Auto-generate `{name}_receive` and `{name}_send` MCP tools."""
        iface_name = self.name
        msg_cls = self.message_class
        receive_fn = self.receive
        send_fn = self.send

        # MCP tool handlers must accept an `args` dict even when the schema is `{}`.
        @tool(
            f"{iface_name}_receive",
            f"Receive any pending messages from the {iface_name} interface. "
            f"Consumes them — they will not be returned again.",
            {},
        )
        async def receive_tool(args):
            try:
                messages = await receive_fn()
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"receive failed: {e}"}],
                    "is_error": True,
                }
            payload = [asdict(m) for m in messages]
            return {"content": [{"type": "text", "text": json.dumps(payload)}]}

        @tool(
            f"{iface_name}_send",
            f"Send a message via the {iface_name} interface.",
            msg_cls.SCHEMA,
        )
        async def send_tool(args):
            try:
                status = await send_fn(msg_cls(**args))
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"send failed: {e}"}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": status or "sent"}]}

        return [receive_tool, send_tool]
