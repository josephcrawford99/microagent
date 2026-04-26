"""Base AgentType. Subclasses implement on_wake().

The public wake() wraps on_wake() in error handling: if anything goes wrong
during a wake, every triggering interface receives a short error notification
so failures surface to the user instead of vanishing into the log.

Agents read config themselves via lib.settings — typically once per wake so
dashboard edits take effect on the next wake without a restart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from lib.settings import CONFIG_ENV, RootConfig

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

    def __init__(
        self, agent_id: str, settings: "RootConfig", interfaces: list["Source"]
    ) -> None:
        self.agent_id = agent_id
        self.settings = settings
        (Path("/state") / agent_id).mkdir(parents=True, exist_ok=True)
        # Named `interfaces` for back-compat — the list now holds any Source
        # (send-capable Interface or receive-only Source subclass).
        self.interfaces = interfaces

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
