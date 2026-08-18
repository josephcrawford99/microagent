from __future__ import annotations

from abc import ABC, abstractmethod
from functools import cached_property
from typing import Any

from lib.state import ComponentState


class Provider(ABC):
    """Base for every LLM provider. Subclasses set `name` and implement
    `generate`. `config` is the raw `[agents.<id>]` dict; secrets come from
    the environment."""

    name: str
    # Env vars the provider needs; surfaced by the dashboard as missing-cred hints.
    REQUIRED_ENV: tuple[str, ...] = ()
    # True when the model runs with a real cwd and tools of its own (the claude
    # CLI). The harness only spends prompt on the filesystem layout for these
    # a plain text-in/text-out API can't act on a path it's told about.
    has_filesystem: bool = False

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.config = config

    @cached_property
    def state(self) -> ComponentState:
        """This plugin's state file under state/<agent_id>/."""
        return ComponentState(self.agent_id, self.name)

    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """Text in, text out — all a vendor file has to implement. The whole
        prompt — soul, contract, and the wake's messages — arrives as one
        string; a provider that has a system-prompt slot of its own doesn't get
        to split it back apart. Parsing the reply is the harness's job."""
