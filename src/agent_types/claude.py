import logging

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    create_sdk_mcp_server,
    query,
)

from lib.agent import AgentType
from lib.config import load_soul_prompt

log = logging.getLogger("microagent.claude")


class Claude(AgentType):
    """Claude agent. Combines all interfaces' tools into an in-process MCP server
    and runs a single `query()` per wake. Logs every stream message so OAuth and
    tool-use issues are visible in the daemon log."""

    name = "claude"

    async def on_wake(self, triggers):
        soul_prompt = load_soul_prompt()  # reload each wake so edits take effect

        all_tools = []
        for iface in self.interfaces:
            all_tools.extend(iface.tools())

        server = create_sdk_mcp_server(
            name="interfaces",
            version="1.0.0",
            tools=all_tools,
        )

        options = ClaudeAgentOptions(
            system_prompt=soul_prompt,
            mcp_servers={"interfaces": server},
            allowed_tools=["mcp__interfaces__*"],
        )

        summary = ", ".join(
            f"{t.interface.name}({type(t).__name__})" for t in triggers
        )
        prompt = (
            f"You have been woken. Active triggers: {summary}.\n"
            "Your interface tools (e.g. mcp__interfaces__email_receive, "
            "mcp__interfaces__terminal_send) are already loaded and ready — "
            "call them directly. Do not use ToolSearch or try to load tool "
            "schemas first; the MCP tools you need are listed in your tool set "
            "from the start.\n"
            "Use them to read pending messages and respond as needed. "
            "Keep replies concise. If there is nothing meaningful to do, stop."
        )

        log.info("claude wake | triggers=%s tools=%d", summary, len(all_tools))

        # Let exceptions propagate — the base AgentType.wake() catches them
        # and notifies the triggering interfaces.
        async for msg in query(prompt=prompt, options=options):
            self._log_stream_message(msg)

    def _log_stream_message(self, msg):
        """Log every message from the SDK stream so auth/tool issues are visible."""
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                btype = type(block).__name__
                if hasattr(block, "text"):
                    log.info("assistant.%s: %s", btype, block.text[:500])
                elif hasattr(block, "name"):
                    log.info("assistant.%s: %s(%s)", btype, block.name, block.input)
                else:
                    log.info("assistant.%s: %r", btype, block)
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
