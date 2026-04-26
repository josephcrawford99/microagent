# AgentType plugins live as flat modules under this package; each module
# exports its concrete `AgentType` subclass via `Plugin = ClassName`. The
# loader is `lib.plugins.load_agent_type(name)` — no registry walk.
