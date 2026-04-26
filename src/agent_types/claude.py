"""Thin wrapper over claude-agent-sdk.

Each wake:
  - Reloads the soul prompt so edits take effect immediately.
  - Exposes all interfaces' receive/send tools via an in-process MCP server,
    plus a `session_idle` tool the agent calls when the conversation has
    naturally concluded.
  - Grants the full Claude Code toolset with /space as cwd so the agent can
    keep notes / task lists / pages across wakes.
  - Resumes the prior session for continuity, but rotates to a fresh session
    once per day (after `agents.<id>.rotation_time`) — only if the previous
    wake ended with `session_idle`, to avoid cutting a live conversation.

State lives at /state/<agent_id>/agent.json. Usage is in-memory only — the
dashboard reads it off `get_usage()`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookCallback,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpServerConfig,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_agent_sdk.types import RateLimitEvent
from pydantic import Field, SecretStr

from lib.agent import AgentSettings, AgentType
from lib.state import ComponentState

if TYPE_CHECKING:
    from lib.interface import Trigger
    from lib.settings import RootConfig

log = logging.getLogger(__name__)

DEFAULT_ROTATION_TIME = "03:00"
AGENT_CWD = "/space"
SOUL_PATH = Path("/config/soul.md")


def _load_soul() -> str:
    """Read /config/soul.md. Empty string if missing."""
    return SOUL_PATH.read_text().strip() if SOUL_PATH.exists() else ""


class ClaudeSettings(AgentSettings):
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = ("CLAUDE_CODE_OAUTH_TOKEN",)
    agent_type: str = "claude"
    rotation_time: str = DEFAULT_ROTATION_TIME
    # claude-agent-sdk reads CLAUDE_CODE_OAUTH_TOKEN from os.environ via the
    # CLI it spawns; this field is for symmetry + dashboard introspection.
    oauth_token: SecretStr | None = Field(
        default=None, validation_alias="CLAUDE_CODE_OAUTH_TOKEN"
    )


class Claude(AgentType):
    name = "claude"

    def __init__(self, agent_id: str, settings: "RootConfig", interfaces):
        super().__init__(agent_id, settings, interfaces)
        self._state = ComponentState(agent_id, "agent")
        self.last_wake_stats: dict[str, Any] | None = None
        self.rate_limit: dict[str, Any] | None = None

    def get_usage(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.last_wake_stats is not None:
            out["last_wake"] = self.last_wake_stats
        if self.rate_limit is not None:
            out["rate_limit"] = self.rate_limit
        return out

    async def on_wake(self, triggers: "list[Trigger]") -> None:
        soul_prompt = _load_soul()
        cfg = ClaudeSettings(self.settings, agent_id=self.agent_id)
        rotation_time = _parse_rotation_time(cfg.rotation_time)

        state = self._state.load()
        prior_session = (
            state.get("session_id") if isinstance(state.get("session_id"), str) else None
        )
        rotated = self._should_rotate(state, rotation_time)
        if rotated:
            prior_session = None

        all_tools: list[SdkMcpTool[Any]] = []
        for iface in self.interfaces:
            all_tools.extend(iface.tools())
        idle_flag = {"set": False}
        all_tools.append(_make_idle_tool(idle_flag))

        server = create_sdk_mcp_server(
            name="interfaces", version="1.0.0", tools=all_tools
        )
        mcp_servers: dict[str, McpServerConfig] = {"interfaces": server}

        options = ClaudeAgentOptions(
            system_prompt=soul_prompt,
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
            cwd=AGENT_CWD,
            resume=prior_session,
            hooks={"Stop": [HookMatcher(hooks=[_make_space_stop_hook()])]},
        )

        summary = ", ".join(t.interface.name for t in triggers)
        prompt = f"Woken. Active triggers: {summary}."

        log.info(
            "claude wake | triggers=%s tools=%d resume=%s rotated=%s",
            summary,
            len(all_tools),
            prior_session or "none",
            rotated,
        )

        new_session: str | None = None
        last_result: ResultMessage | None = None
        last_rate_limit: RateLimitEvent | None = None
        try:
            async for msg in query(prompt=prompt, options=options):
                self._log_stream_message(msg)
                await self._emit_pending(msg, triggers)
                if isinstance(msg, SystemMessage):
                    sid = _extract_session_id(msg)
                    if sid:
                        new_session = sid
                elif isinstance(msg, ResultMessage):
                    last_result = msg
                elif isinstance(msg, RateLimitEvent):
                    last_rate_limit = msg
        except Exception:
            if prior_session:
                log.exception(
                    "claude wake failed with resume=%s; clearing", prior_session
                )
                self._state.save({})
            raise
        finally:
            await self._emit_idle(triggers)
            self._update_usage(last_result, last_rate_limit)

        effective_session = new_session or prior_session
        self._state.save({
            "session_id": effective_session,
            "idle": idle_flag["set"],
            "last_rotation": (
                date.today().isoformat() if rotated else state.get("last_rotation")
            ),
        })

    def _should_rotate(self, state: dict[str, Any], rotation_time: time) -> bool:
        if not state.get("session_id"):
            return False
        if not state.get("idle"):
            return False
        if state.get("last_rotation") == date.today().isoformat():
            return False
        return datetime.now().time() >= rotation_time

    def _update_usage(
        self,
        result: ResultMessage | None,
        rate_limit: RateLimitEvent | None,
    ) -> None:
        """Stash the latest usage snapshot in memory for the dashboard to read.
        Not persisted — restart clears it, which is correct: these numbers are
        about the live process."""
        if result is not None:
            self.last_wake_stats = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "usage": result.usage,
                "total_cost_usd": result.total_cost_usd,
                "num_turns": result.num_turns,
                "duration_ms": result.duration_ms,
                "subtype": result.subtype,
            }
        if rate_limit is not None:
            info = rate_limit.rate_limit_info
            self.rate_limit = {
                "status": info.status,
                "resets_at": info.resets_at,
                "rate_limit_type": info.rate_limit_type,
                "utilization": info.utilization,
                "overage_status": info.overage_status,
            }

    async def _emit_pending(
        self, msg: object, triggers: "list[Trigger]"
    ) -> None:
        """Translate SDK stream events into indicate_pending() on the triggering
        interfaces. Text blocks are skipped — once the agent is writing the reply,
        the indicator is noise."""
        note: str | None = None
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    note = "thinking"
                elif isinstance(block, ToolUseBlock):
                    note = f"using {block.name}"
                if note:
                    break
        if not note:
            return
        seen: set[int] = set()
        for t in triggers:
            iface = t.interface
            if id(iface) in seen:
                continue
            seen.add(id(iface))
            try:
                await iface.indicate_pending(note)
            except Exception:
                log.exception("indicate_pending failed on %s", iface.name)

    async def _emit_idle(self, triggers: "list[Trigger]") -> None:
        seen: set[int] = set()
        for t in triggers:
            iface = t.interface
            if id(iface) in seen:
                continue
            seen.add(id(iface))
            try:
                await iface.indicate_idle()
            except Exception:
                log.exception("indicate_idle failed on %s", iface.name)

    def _log_stream_message(self, msg: object) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                btype = type(block).__name__
                if isinstance(block, TextBlock):
                    log.info("assistant.%s: %s", btype, block.text[:500])
                elif isinstance(block, ThinkingBlock):
                    log.info("assistant.%s: %s", btype, block.thinking[:500])
                elif isinstance(block, ToolUseBlock):
                    log.info(
                        "assistant.%s: %s(%s)", btype, block.name, block.input
                    )
                else:
                    log.info(
                        "assistant.%s: %s", btype, str(block.content)[:500]
                    )
        elif isinstance(msg, UserMessage):
            log.info("user (tool result): %s", str(msg)[:500])
        elif isinstance(msg, SystemMessage):
            log.info("system: %s", str(msg)[:500])
        elif isinstance(msg, ResultMessage):
            log.info(
                "result subtype=%s cost=%s result=%s",
                msg.subtype,
                getattr(msg, "total_cost_usd", None),
                (msg.result or "")[:500] if msg.result else "",
            )
        else:
            log.info("stream %s: %r", type(msg).__name__, msg)


def _extract_session_id(msg: SystemMessage) -> str | None:
    data: dict[str, Any] = msg.data
    sid = data.get("session_id")
    if isinstance(sid, str) and sid:
        return sid
    return None


def _parse_rotation_time(raw: str) -> time:
    try:
        hh, mm = raw.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, AttributeError):
        log.warning(
            "invalid rotation_time %r, falling back to %s",
            raw,
            DEFAULT_ROTATION_TIME,
        )
        hh, mm = DEFAULT_ROTATION_TIME.split(":")
        return time(hour=int(hh), minute=int(mm))


def _make_space_stop_hook() -> HookCallback:
    """Stop hook: on the first stop of a wake, nudge the agent to update its
    space if anything worth capturing happened. `stop_hook_active=True` on
    subsequent stops lets the agent actually end."""
    fired = {"done": False}

    async def _hook(
        input_data: HookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, context
        if fired["done"] or input_data.get("stop_hook_active"):
            return {}
        fired["done"] = True
        return {
            "decision": "block",
            "reason": (
                "Before stopping: is there anything from this exchange worth "
                "capturing in /space/? Notes, reminders, a shopping list item, "
                "an update to an existing page, something the user might like "
                "to see. If yes, update the space now. If not, just stop — no "
                "need to respond or explain."
            ),
        }

    return _hook


def _make_idle_tool(idle_flag: dict[str, bool]) -> SdkMcpTool[Any]:
    @tool(
        "session_idle",
        "Mark the current conversation as complete. Call this when you have "
        "nothing more to do and aren't expecting an immediate follow-up — the "
        "daemon may then rotate to a fresh session at the next scheduled "
        "rotation time. Don't call it if you just asked a question or are "
        "mid-task; wait for the exchange to settle first.",
        {},
    )
    async def session_idle_tool(args: dict[str, Any]) -> dict[str, Any]:
        del args
        idle_flag["set"] = True
        return {"content": [{"type": "text", "text": "marked idle"}]}

    return session_idle_tool


Plugin = Claude
