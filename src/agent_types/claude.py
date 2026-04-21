import json
import logging
import os
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
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

from lib.agent import AgentType
from lib.config import DATA_DIR, load_config, load_soul_prompt

if TYPE_CHECKING:
    from lib.interface import Trigger

log = logging.getLogger("microagent.claude")

STATE_FILE = os.path.join(DATA_DIR, "session.json")
DEFAULT_ROTATION_TIME = "03:00"


class Claude(AgentType):
    """Thin wrapper over claude-agent-sdk. Each wake:

    - Reloads the soul prompt so edits take effect immediately.
    - Exposes all interfaces' receive/send tools via an in-process MCP server,
      plus a `session_idle` tool the agent calls when the conversation has
      naturally concluded.
    - Grants the full Claude Code toolset (Read, Write, Edit, Glob, Grep,
      Bash, …) with DATA_DIR as cwd, so the agent can keep notes, task lists,
      and other working files across wakes.
    - Resumes the prior session for continuity, but rotates to a fresh session
      once per day (after `agents.claude.rotation_time` local, configurable)
      — only if the previous wake ended with `session_idle` being called, to
      avoid cutting a live conversation mid-thread.
    """

    name = "claude"

    async def on_wake(self, triggers: "list[Trigger]") -> None:
        soul_prompt = load_soul_prompt()
        agents_cfg: dict[str, Any] = load_config().get("agents") or {}
        my_cfg: dict[str, Any] = agents_cfg.get(self.name) or {}
        rotation_time = _parse_rotation_time(
            str(my_cfg.get("rotation_time", DEFAULT_ROTATION_TIME))
        )

        os.makedirs(DATA_DIR, exist_ok=True)
        state = self._load_state()
        prior_session = state.get("session_id") if isinstance(state.get("session_id"), str) else None
        rotated = self._should_rotate(state, rotation_time)
        if rotated:
            prior_session = None

        all_tools: list[SdkMcpTool[Any]] = []
        for iface in self.interfaces:
            all_tools.extend(iface.tools())
        idle_flag = {"set": False}
        all_tools.append(_make_idle_tool(idle_flag))

        server = create_sdk_mcp_server(
            name="interfaces",
            version="1.0.0",
            tools=all_tools,
        )
        mcp_servers: dict[str, McpServerConfig] = {"interfaces": server}

        options = ClaudeAgentOptions(
            system_prompt=soul_prompt,
            mcp_servers=mcp_servers,
            permission_mode="bypassPermissions",
            cwd=DATA_DIR,
            resume=prior_session,
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
        try:
            async for msg in query(prompt=prompt, options=options):
                self._log_stream_message(msg)
                if isinstance(msg, SystemMessage):
                    sid = self._extract_session_id(msg)
                    if sid:
                        new_session = sid
        except Exception:
            if prior_session:
                log.exception("claude wake failed with resume=%s; clearing", prior_session)
                self._save_state({})
            raise

        effective_session = new_session or prior_session
        new_state: dict[str, Any] = {
            "session_id": effective_session,
            "idle": idle_flag["set"],
            "last_rotation": (
                date.today().isoformat() if rotated else state.get("last_rotation")
            ),
        }
        self._save_state(new_state)

    def _should_rotate(self, state: dict[str, Any], rotation_time: time) -> bool:
        if not state.get("session_id"):
            return False
        if not state.get("idle"):
            return False
        if state.get("last_rotation") == date.today().isoformat():
            return False
        return datetime.now().time() >= rotation_time

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(STATE_FILE) as f:
                data: Any = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            log.exception("failed to read %s; treating as empty", STATE_FILE)
            return {}
        if not isinstance(data, dict):
            return {}
        return cast(dict[str, Any], data)

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except OSError:
            log.exception("failed to save session state")

    @staticmethod
    def _extract_session_id(msg: SystemMessage) -> str | None:
        data: dict[str, Any] = msg.data
        sid = data.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
        return None

    def _log_stream_message(self, msg: object) -> None:
        """Log every message from the SDK stream so auth/tool issues are visible."""
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                btype = type(block).__name__
                if isinstance(block, TextBlock):
                    log.info("assistant.%s: %s", btype, block.text[:500])
                elif isinstance(block, ThinkingBlock):
                    log.info("assistant.%s: %s", btype, block.thinking[:500])
                elif isinstance(block, ToolUseBlock):
                    log.info("assistant.%s: %s(%s)", btype, block.name, block.input)
                else:
                    log.info("assistant.%s: %s", btype, str(block.content)[:500])
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


def _parse_rotation_time(raw: str) -> time:
    try:
        hh, mm = raw.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, AttributeError):
        log.warning("invalid rotation_time %r, falling back to %s", raw, DEFAULT_ROTATION_TIME)
        hh, mm = DEFAULT_ROTATION_TIME.split(":")
        return time(hour=int(hh), minute=int(mm))


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
