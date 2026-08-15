"""Provider-neutral tool descriptors + the `src/tools/` module contract.

`src/tools/` is the third plugin directory (alongside `sources/` and
`agent_types/`). Everything the agent does to the outside world — poll a
source, send a message, show a typing indicator, mark the session idle,
schedule a wake, run a command — is a tool module there. Each module exposes
one entry point:

    def build(ctx: ToolContext) -> list[ToolSpec]

returning `[]` when whatever it wraps isn't configured, so the same TOOLS
tuple works against any config. An AgentType declares which modules it loads
(`AgentType.TOOLS`, overridable per-agent in config.toml) and adapts the
resulting ToolSpecs into whatever its provider wants — `agent_types/claude.py`
builds one in-process MCP server per module, `agent_types/gemini.py` emits
flat `types.FunctionDeclaration`s.

`ToolSpec.name` is the bare verb (`poll`, `send`); namespacing by module is
the adapter's job. The result shape is MCP-flavoured —
`{"content": [{"type": "text", ...}]}` — because it keeps the Claude adapter a
pass-through. `result_text()` flattens it for providers that want a string.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from lib.agent import AgentType
    from lib.source import Message, Source, Trigger

log = logging.getLogger(__name__)

ToolArgs = dict[str, Any]
ToolResult = dict[str, Any]
ToolHandler = Callable[[ToolArgs], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    params: dict[str, type]  # flat {field: python type}; {} = no args
    handler: ToolHandler


def tool(
    name: str, description: str, params: dict[str, type]
) -> Callable[[ToolHandler], ToolSpec]:
    """Decorator mirroring `claude_agent_sdk.tool`'s call shape, so tool
    modules read the same as SDK-native tool definitions."""

    def decorate(fn: ToolHandler) -> ToolSpec:
        return ToolSpec(
            name=name, description=description, params=params, handler=fn
        )

    return decorate


@dataclass
class ToolContext:
    """Everything a tool module needs to reach the running harness. Built once
    per wake by `AgentType.build_tools()` and handed to every module's
    `build()`."""

    agent: "AgentType"
    triggers: list["Trigger"] = field(default_factory=list)
    # Per-wake scratch shared between tools and the agent type. `session_idle`
    # sets flags["idle"]; the agent reads it after the wake to decide rotation.
    flags: dict[str, Any] = field(default_factory=dict)

    @property
    def sources(self) -> list["Source"]:
        return self.agent.interfaces

    def source(self, name: str) -> Optional["Source"]:
        """The enabled Source with this name, or None if it isn't configured."""
        for s in self.sources:
            if s.name == name:
                return s
        return None

    def triggering(self) -> list["Source"]:
        """Sources that woke this wake, deduped by identity, order preserved."""
        from lib.agent import _distinct  # local: lib.agent imports us

        return _distinct(self.triggers)


# --- result helpers --------------------------------------------------------


def _ok(msg: str) -> ToolResult:
    return {"content": [{"type": "text", "text": msg}]}


def _error(msg: str) -> ToolResult:
    return {"content": [{"type": "text", "text": msg}], "is_error": True}


def _json(payload: Any) -> ToolResult:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def result_text(result: ToolResult) -> str:
    """Flatten a ToolResult's content blocks into a single string."""
    parts = [
        str(b.get("text", ""))
        for b in result.get("content", [])
        if isinstance(b, dict)
    ]
    return "\n".join(p for p in parts if p)


# --- shared building blocks for the per-interface tool modules -------------


_OMIT_ALWAYS = ("sender",)


def _derive_schema(msg_cls: type["Message"]) -> dict[str, type]:
    """Build a send-tool schema from a Message dataclass's fields.

    Tool schemas are a flat {name: type} mapping, so each dataclass field maps
    to its annotation type, except fields in TOOL_OMIT or the always-omitted
    set (currently just `sender`, which the harness fills in from its own
    knowledge of who's talking)."""
    omit = set(_OMIT_ALWAYS) | set(msg_cls.TOOL_OMIT)
    out: dict[str, type] = {}
    for f in dataclasses.fields(msg_cls):
        if f.name in omit:
            continue
        out[f.name] = f.type if isinstance(f.type, type) else str
    return out


async def drain(src: "Source") -> list[dict[str, Any]]:
    """Consume a Source's pending messages as plain dicts."""
    return [asdict(m) for m in await src.receive()]


def interface_tools(ctx: ToolContext, name: str) -> list[ToolSpec]:
    """The standard tool trio for one channel — `poll`, `send`, `emit_pending`
    — or `[]` when that channel isn't enabled. `send` is omitted for
    receive-only Sources; its schema comes from the source's `message_class`.

    Per-channel modules in `src/tools/` are usually a one-line call to this,
    and add their own hand-written tools alongside when a channel has more to
    offer than send/receive."""
    from lib.interface import Interface  # local: lib.interface imports us

    src = ctx.source(name)
    if src is None:
        return []

    @tool(
        "poll",
        f"Read any pending {name} messages. Consumes them — they will not be "
        f"returned again. Returns a JSON array of message objects.",
        {},
    )
    async def poll_tool(args: ToolArgs) -> ToolResult:
        del args
        try:
            return _json(await drain(src))
        except Exception as e:
            log.exception("%s poll failed", name)
            return _error(f"{name} poll failed: {e}")

    @tool(
        "emit_pending",
        f"Show a short live-status note on {name} ('thinking', 'checking your "
        f"calendar') so the user knows you're working. Call it whenever what "
        f"you're doing changes; the next send clears it.",
        {"note": str},
    )
    async def pending_tool(args: ToolArgs) -> ToolResult:
        note = str(args.get("note", "")).strip()
        if not note:
            return _error("emit_pending: 'note' is required")
        try:
            await src.indicate_pending(note)
        except Exception as e:
            log.exception("%s emit_pending failed", name)
            return _error(f"{name} emit_pending failed: {e}")
        return _ok("ok")

    out = [poll_tool, pending_tool]
    if not isinstance(src, Interface):
        return out

    msg_cls = src.message_class
    send_fn = src.send

    @tool(
        "send",
        f"Send a message via {name}.",
        _derive_schema(msg_cls),
    )
    async def send_tool(args: ToolArgs) -> ToolResult:
        try:
            status = await send_fn(msg_cls(**args))
        except Exception as e:
            log.exception("%s send failed", name)
            return _error(f"{name} send failed: {e}")
        return _ok(status or "sent")

    return [*out, send_tool]
