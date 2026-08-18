"""Gemini via the official google-genai SDK. Stateless API, so this provider
owns its transcript: a text-only history persisted to
state/<agent_id>/gemini.json, trimmed to `history_turns`. No tools — the
harness's JSON message contract is the whole interface."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from providers.base_provider import Provider

log = logging.getLogger(__name__)


class Gemini(Provider):
    name = "gemini"
    REQUIRED_ENV = ("GEMINI_API_KEY",)

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.model = str(config.get("model", "gemini-3.6-flash"))
        self.history_turns = int(config.get("history_turns", 60))
        self.temperature = config.get("temperature")
        self.timeout = float(config.get("request_timeout", 120))

    async def generate(self, prompt: str) -> str:
        from google import genai  # lazy — optional dependency
        from google.genai import types

        client = genai.Client()  # reads GEMINI_API_KEY from the environment
        history = self.state.load().get("history", [])
        history.append({"role": "user", "text": prompt})
        contents = [
            types.Content(role=h["role"], parts=[types.Part(text=h["text"])])
            for h in history
        ]
        config = types.GenerateContentConfig(
            **({"temperature": self.temperature} if self.temperature is not None else {}),
        )
        # wait_for guard: a hung request must not wedge the daemon.
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=self.model, contents=contents, config=config
            ),
            timeout=self.timeout,
        )
        text = resp.text or ""
        history.append({"role": "model", "text": text})
        self.state.save({"history": history[-self.history_turns :]})
        usage = getattr(resp, "usage_metadata", None)
        log.info(
            "gemini wake done | tokens=%s",
            getattr(usage, "total_token_count", "?"),
        )
        return text
