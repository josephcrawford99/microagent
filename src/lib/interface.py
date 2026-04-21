import json
from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Optional

from claude_agent_sdk import SdkMcpTool, tool

# The SDK's @tool decorator hands handlers a dict[str, Any] and expects a
# dict[str, Any] back — aliases keep our signatures honest without
# redefining the SDK's contract.
ToolArgs = dict[str, Any]
ToolResult = dict[str, Any]


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

    SCHEMA: ClassVar[dict[str, type]] = {"body": str}


@dataclass
class Trigger:
    """Base wake-trigger. Each interface subclass defines its own with extra fields."""

    interface: "Interface"


class Interface:
    """Base for communication interfaces.

    Subclasses set `name`, optionally set `message_class` (defaults to Message),
    define their own typed `__init__`, and implement trigger_wake(), receive(),
    and send(). The default tools() auto-generates `{name}_receive` and
    `{name}_send` MCP tools that delegate to receive() and send().
    """

    name: str
    message_class: type[Message] = Message

    def trigger_wake(self) -> Optional[Trigger]:
        raise NotImplementedError

    async def receive(self) -> list[Message]:
        raise NotImplementedError

    async def send(self, message: Message) -> str:
        del message
        raise NotImplementedError

    async def indicate_pending(self, note: str) -> None:
        """Optional hook — the agent wrapper calls this while it's thinking or
        using tools so the interface can surface a transient "typing…" signal.
        Default is no-op; interfaces whose medium can't express it (email, sms
        outbox) simply don't override. Called repeatedly during a wake; the
        next real send() implicitly clears the indicator."""
        del note

    def tools(self) -> list[SdkMcpTool[Any]]:
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
        async def receive_tool(args: ToolArgs) -> ToolResult:
            del args  # schema is {}, no inputs to read
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
        async def send_tool(args: ToolArgs) -> ToolResult:
            try:
                status = await send_fn(msg_cls(**args))
            except Exception as e:
                return {
                    "content": [{"type": "text", "text": f"send failed: {e}"}],
                    "is_error": True,
                }
            return {"content": [{"type": "text", "text": status or "sent"}]}

        return [receive_tool, send_tool]
