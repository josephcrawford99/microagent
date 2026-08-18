"""No-LLM smoke test. Always answers "pong" — deliberately not JSON, so it
exercises the harness's malformed-output path end to end (raw text gets
notified back to the channel that woke us)."""

from __future__ import annotations

from providers.base_provider import Provider


class Ping(Provider):
    name = "ping"

    async def generate(self, prompt: str) -> str:
        return "pong"
