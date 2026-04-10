import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib.interface import Interface, Trigger

log = logging.getLogger("microagent.agent")


class AgentType:
    """Base for agent types. Subclasses implement on_wake().

    The public wake() wraps on_wake() in error handling: if anything goes wrong
    during a wake (auth failure, network error, tool exception, …) every
    triggering interface receives a short error notification so the failure is
    visible to the user instead of silently disappearing into the log.

    Agents that need config or a soul prompt should read them themselves via
    lib.config (load_config(), load_soul_prompt()) — typically inside on_wake()
    so edits are picked up on the next wake without restarting the daemon.
    """

    name: str

    def __init__(self, interfaces: list["Interface"]):
        self.interfaces = interfaces

    async def wake(self, triggers: list["Trigger"]) -> None:
        """Called by the daemon. Do not override — implement on_wake() instead."""
        try:
            await self.on_wake(triggers)
        except Exception as e:
            log.exception("agent %s wake failed", self.name)
            await self._notify_failure(triggers, e)

    async def on_wake(self, triggers: list["Trigger"]) -> None:
        """Subclass entry point. Use the triggering interfaces' tools or methods
        to read and respond to whatever woke the agent."""
        raise NotImplementedError

    async def _notify_failure(
        self, triggers: list["Trigger"], error: Exception
    ) -> None:
        """Handle a failed wake by notifying each triggering interface and then
        discarding whatever caused the trigger so the daemon doesn't busy-loop.

        Discard is just `iface.receive()` with the result thrown away — the
        same call the agent would have made normally. After this returns, the
        interface's state is clean and the next poll tick will see no trigger.
        Both notify and drain are best-effort; failures here are only logged.
        """
        body = f"[microagent error] {type(error).__name__}: {error}"
        for t in triggers:
            iface = t.interface
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
