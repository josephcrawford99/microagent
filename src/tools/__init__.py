"""Pluggable tool modules — the agent's entire reach into the outside world.

Each module here exposes `build(ctx: ToolContext) -> list[ToolSpec]` and is
loaded by name (`AgentType.TOOLS`, or `[agents.<id>] tools = [...]`). A module
returns `[]` when the thing it wraps isn't enabled, so an agent can list every
channel and only get tools for the ones actually configured.

See `lib/tools.py` for the ToolSpec/ToolContext contract and the
`interface_tools()` factory the per-channel modules are built on.
"""
