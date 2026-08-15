"""Base AgentType. Subclasses implement on_wake().

The public wake() wraps on_wake() in error handling: if anything goes wrong
during a wake, every triggering interface receives a short error notification
so failures surface to the user instead of vanishing into the log.

Agents read config themselves via lib.settings — typically once per wake so
dashboard edits take effect on the next wake without a restart.

An agent's power over the outside world is exactly the set of tool modules it
loads: `TOOLS` is the class default, `[agents.<id>] tools = [...]` overrides
it, and `build_tools()` turns that list into ToolSpecs for the subclass to
adapt into its provider's format.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from lib.paths import CONFIG_ENV, SOUL_PATH, STATE_DIR
from lib.plugins import load_tool_builder
from lib.settings import RootConfig
from lib.tools import ToolContext, ToolSpec

if TYPE_CHECKING:
    from lib.source import Source, Trigger

log = logging.getLogger(__name__)


class AgentSettings(BaseSettings):
    """Base for every AgentType settings model. Constructible from a parent
    RootConfig + agent_id — `ClaudeSettings(settings, agent_id="primary")`
    extracts the `[agents.<id>]` slice and feeds it to BaseSettings as init
    kwargs; pydantic-settings' env+dotenv sources fill in any
    `validation_alias` fields (credentials)."""

    model_config = SettingsConfigDict(
        env_file=str(CONFIG_ENV),
        case_sensitive=True,
        extra="allow",
    )

    REQUIRED_ENV: ClassVar[tuple[str, ...]] = ()

    agent_type: str = ""
    # Tool modules from src/tools/ this agent may use. Empty = the agent
    # type's own `TOOLS` default.
    tools: list[str] = []

    def __init__(
        self,
        parent: Optional[RootConfig] = None,
        /,
        *,
        agent_id: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        if isinstance(parent, RootConfig) and agent_id is not None:
            section = parent.agents.get(agent_id, {}) or {}
            super().__init__(**{**section, **kwargs})
        else:
            super().__init__(**kwargs)


class AgentType:
    name: str
    # Pydantic settings model for this agent type. The dashboard introspects it
    # to report REQUIRED_ENV / missing credentials, mirroring `Source.settings_cls`.
    settings_cls: ClassVar[Optional[type[AgentSettings]]] = None
    # Default tool modules (src/tools/<name>.py). Modules whose channel isn't
    # enabled contribute nothing, so listing them all is safe.
    TOOLS: ClassVar[tuple[str, ...]] = ("poll", "session", "status")

    def __init__(
        self, agent_id: str, settings: "RootConfig", interfaces: list["Source"]
    ) -> None:
        self.agent_id = agent_id
        self.settings = settings
        (STATE_DIR / agent_id).mkdir(parents=True, exist_ok=True)
        # Named `interfaces` for back-compat — the list now holds any Source
        # (send-capable Interface or receive-only Source subclass).
        self.interfaces = interfaces

    @staticmethod
    def load_soul() -> str:
        """Read the soul prompt from /config/soul.md. Empty string if missing.
        Re-read per wake so edits take effect without a restart."""
        return SOUL_PATH.read_text().strip() if SOUL_PATH.exists() else ""

    async def wake(self, triggers: list["Trigger"]) -> None:
        """Called by the daemon. Do not override — implement on_wake()."""
        try:
            await self.on_wake(triggers)
        except Exception as e:
            log.exception("agent %s wake failed", self.name)
            await self._notify_failure(triggers, e)

    async def on_wake(self, triggers: list["Trigger"]) -> None:
        raise NotImplementedError

    def get_usage(self) -> dict:
        """Live usage snapshot for the dashboard. Default: nothing to report."""
        return {}

    def build_tools(
        self,
        triggers: list["Trigger"],
        names: Optional[list[str]] = None,
    ) -> tuple[dict[str, list[ToolSpec]], ToolContext]:
        """Load this wake's tool modules. `names` (typically `cfg.tools` from
        the agent's own settings) overrides the class `TOOLS` default when
        non-empty. Returns {module: specs} — modules that contribute nothing
        are dropped — plus the shared ToolContext, whose `flags` the caller
        reads after the wake (e.g. `flags["idle"]`).

        A module that fails to import is logged and skipped: one broken tool
        file shouldn't take the whole agent down."""
        ctx = ToolContext(agent=self, triggers=triggers)
        wanted = list(names) if names else list(self.TOOLS)
        out: dict[str, list[ToolSpec]] = {}
        for name in wanted:
            try:
                specs = load_tool_builder(name)(ctx)
            except Exception:
                log.exception("tool module %r failed to load; skipping", name)
                continue
            if specs:
                out[name] = specs
        return out, ctx

    async def emit_idle(self, triggers: list["Trigger"]) -> None:
        """Tear down whatever emit_pending() posted. Call once per wake."""
        for iface in _distinct(triggers):
            try:
                await iface.indicate_idle()
            except Exception:
                log.exception("indicate_idle failed on %s", iface.name)

    async def _notify_failure(
        self, triggers: list["Trigger"], error: Exception
    ) -> None:
        """Notify each triggering interface of the failure, then drain it so
        the daemon doesn't busy-loop on the same trigger."""
        body = f"[microagent error] {type(error).__name__}: {error}"
        from lib.interface import Interface  # local: avoid circular import
        for t in triggers:
            iface = t.interface
            if isinstance(iface, Interface):
                try:
                    await iface.send(
                        iface.message_class(body=body, sender="agent", to="user")
                    )
                except Exception:
                    log.exception("failed to notify %s of wake failure", iface.name)
            try:
                discarded = await iface.receive()
                if discarded:
                    log.info(
                        "discarded %d message(s) from %s after wake failure",
                        len(discarded),
                        iface.name,
                    )
            except Exception:
                log.exception("failed to drain %s after wake failure", iface.name)


def _distinct(triggers: list["Trigger"]) -> list["Source"]:
    """Triggering interfaces, deduped by identity, order preserved."""
    seen: set[int] = set()
    out: list["Source"] = []
    for t in triggers:
        if id(t.interface) not in seen:
            seen.add(id(t.interface))
            out.append(t.interface)
    return out
