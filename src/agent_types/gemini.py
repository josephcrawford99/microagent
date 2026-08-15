"""Gemini agent type, on the official google-genai SDK.

Gemini brings no toolset of its own, so everything it can do comes from
`src/tools/` — including the shell, via `tools/bash.py`, which the Claude type
has no need to load. It's the harness's proof that an AgentType only needs
`lib.tools.ToolSpec`, not any particular vendor SDK.

Each wake:
  - Reloads soul.md as the system instruction.
  - Declares every loaded ToolSpec as a Gemini FunctionDeclaration and runs a
    manual function-calling loop, bounded by `max_turns`. Tool names are
    flattened to `<module>_<tool>` (`telegram_send`, `poll_poll_all`) since
    Gemini has no namespacing of its own.
  - Persists the conversation itself. Gemini is stateless, so unlike the Claude
    type (which stores a session id and lets the CLI own the transcript) this
    one keeps the transcript in /state/<agent_id>/agent.json — which means
    continuity survives a container rebuild.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from google import genai
from google.genai import types
from pydantic import Field, SecretStr

from lib.agent import AgentSettings, AgentType
from lib.state import ComponentState
from lib.tools import ToolSpec, result_text
from tools.status import set_pending

if TYPE_CHECKING:
    from lib.source import Trigger

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.6-flash"

_JSON_TYPES: dict[type, str] = {
    str: "STRING",
    int: "INTEGER",
    float: "NUMBER",
    bool: "BOOLEAN",
    list: "ARRAY",
    dict: "OBJECT",
}


class GeminiSettings(AgentSettings):
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = ("GEMINI_API_KEY",)
    agent_type: str = "gemini"
    model: str = DEFAULT_MODEL
    # Hard bound on the tool-calling loop. One "turn" is one model response;
    # a reply that needs receive → send costs two.
    max_turns: int = 12
    # Conversation entries retained across wakes. Each user message, model
    # response and tool-result batch is one entry.
    history_turns: int = 60
    temperature: float | None = None
    # Ceiling on a single model call. The SDK retries 5xx internally with no
    # overall deadline, so an overloaded model (503 UNAVAILABLE) would
    # otherwise wedge the whole daemon — the wake loop is single-threaded and
    # nothing else runs until on_wake returns.
    request_timeout: int = 120
    api_key: SecretStr | None = Field(
        default=None, validation_alias="GEMINI_API_KEY"
    )


class Gemini(AgentType):
    name = "gemini"
    settings_cls = GeminiSettings
    # Same channels as the Claude type, plus a shell — Gemini has no built-in
    # toolset, so without `bash` it can only talk, not act.
    TOOLS = (
        "poll",
        "telegram",
        "email",
        "socket",
        "web_chat",
        "imessage",
        "session",
        "status",
        "cron",
        "bash",
    )

    def __init__(self, agent_id: str, settings, interfaces):
        super().__init__(agent_id, settings, interfaces)
        self._state = ComponentState(agent_id, "agent")
        self.last_wake_stats: dict[str, Any] | None = None

    def get_usage(self) -> dict[str, Any]:
        return {"last_wake": self.last_wake_stats} if self.last_wake_stats else {}

    async def on_wake(self, triggers: "list[Trigger]") -> None:
        cfg = GeminiSettings(self.settings, agent_id=self.agent_id)
        if cfg.api_key is None:
            raise RuntimeError("GEMINI_API_KEY is not set")
        client = genai.Client(api_key=cfg.api_key.get_secret_value())

        tools_by_module, tool_ctx = self.build_tools(triggers, cfg.tools)
        specs = _flatten(tools_by_module)
        dispatch = {s.name: s for s in specs}
        tool_config = (
            [types.Tool(function_declarations=[_declare(s) for s in specs])]
            if specs
            else None
        )

        history = self._load_history()
        summary = ", ".join(t.interface.name for t in triggers)
        history.append(
            types.Content(
                role="user",
                parts=[types.Part(text=f"Woken. Active triggers: {summary}.")],
            )
        )

        log.info(
            "gemini wake | model=%s triggers=%s tools=%d history=%d",
            cfg.model,
            summary,
            len(specs),
            len(history) - 1,
        )

        config = types.GenerateContentConfig(
            system_instruction=self.load_soul() or None,
            tools=tool_config,
            temperature=cfg.temperature,
            http_options=types.HttpOptions(timeout=cfg.request_timeout * 1000),
        )

        started = datetime.now()
        turns = 0
        usage: dict[str, Any] = {}
        try:
            while turns < cfg.max_turns:
                turns += 1
                await set_pending(tool_ctx, "thinking")
                # Belt and braces: http_options bounds each attempt, wait_for
                # bounds the SDK's retry chain around them.
                resp = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=cfg.model, contents=history, config=config
                    ),
                    timeout=cfg.request_timeout,
                )
                usage = _usage(resp) or usage
                content = _response_content(resp)
                if content is None:
                    log.warning("gemini returned no content; ending wake")
                    break
                history.append(content)
                self._log_content(content)

                calls = [p.function_call for p in content.parts or []
                         if p.function_call is not None]
                if not calls:
                    break

                results: list[types.Part] = []
                for call in calls:
                    await set_pending(tool_ctx, f"using {call.name}")
                    results.append(
                        await _invoke(dispatch, call)
                    )
                history.append(types.Content(role="user", parts=results))
            else:
                log.warning(
                    "gemini hit max_turns=%d; ending wake mid-loop", cfg.max_turns
                )
        finally:
            await self.emit_idle(triggers)
            self.last_wake_stats = {
                "at": started.isoformat(timespec="seconds"),
                "usage": usage,
                "num_turns": turns,
                "duration_ms": int(
                    (datetime.now() - started).total_seconds() * 1000
                ),
                "model": cfg.model,
            }
            self._save_history(history, cfg.history_turns)

    # --- history ---

    def _load_history(self) -> list[types.Content]:
        raw = self._state.load().get("history")
        if not isinstance(raw, list):
            return []
        out: list[types.Content] = []
        for entry in raw:
            try:
                out.append(types.Content.model_validate(entry))
            except Exception:
                log.warning("dropping unparseable history entry")
        return out

    def _save_history(self, history: list[types.Content], limit: int) -> None:
        trimmed = history[-limit:] if limit > 0 else history
        self._state.save({
            "history": [c.model_dump(mode="json", exclude_none=True)
                        for c in trimmed],
        })

    def _log_content(self, content: types.Content) -> None:
        for part in content.parts or []:
            if part.text:
                log.info("assistant.text: %s", part.text[:500])
            elif part.function_call is not None:
                log.info(
                    "assistant.call: %s(%s)",
                    part.function_call.name,
                    part.function_call.args,
                )


# --- helpers ---------------------------------------------------------------


def _flatten(tools_by_module: dict[str, list[ToolSpec]]) -> list[ToolSpec]:
    """`{"telegram": [poll, send]}` → specs named `telegram_poll`,
    `telegram_send`. Gemini has one flat function namespace, so the module
    prefix the Claude adapter gets from MCP has to be baked into the name."""
    return [
        dataclasses.replace(s, name=f"{module}_{s.name}")
        for module, specs in tools_by_module.items()
        for s in specs
    ]


async def _invoke(
    dispatch: dict[str, ToolSpec], call: types.FunctionCall
) -> types.Part:
    """Run one tool call and wrap the outcome as a function_response Part.
    Errors come back as tool output rather than raising, so the model can see
    what went wrong and recover within the same wake."""
    spec = dispatch.get(call.name or "")
    if spec is None:
        text = f"unknown tool: {call.name}"
        log.warning("gemini requested %s", text)
    else:
        try:
            text = result_text(await spec.handler(dict(call.args or {})))
        except Exception as e:
            log.exception("tool %s failed", call.name)
            text = f"{type(e).__name__}: {e}"
    return types.Part.from_function_response(
        name=call.name or "unknown", response={"result": text}
    )


def _declare(spec: ToolSpec) -> types.FunctionDeclaration:
    """ToolSpec → Gemini FunctionDeclaration. `spec.params` is a flat
    {field: python type} map (for send tools, what lib.tools._derive_schema
    produced); every field is required, matching the Message dataclass
    contract."""
    if not spec.params:
        return types.FunctionDeclaration(
            name=spec.name, description=spec.description
        )
    props = {
        field: _schema(annotation) for field, annotation in spec.params.items()
    }
    return types.FunctionDeclaration(
        name=spec.name,
        description=spec.description,
        parameters=types.Schema(
            type="OBJECT", properties=props, required=list(props)
        ),
    )


def _schema(annotation: Any) -> types.Schema:
    kind = _JSON_TYPES.get(annotation, "STRING")
    if kind == "ARRAY":
        return types.Schema(type="ARRAY", items=types.Schema(type="STRING"))
    return types.Schema(type=kind)


def _response_content(resp: types.GenerateContentResponse) -> types.Content | None:
    candidates = resp.candidates or []
    if not candidates:
        return None
    return candidates[0].content


def _usage(resp: types.GenerateContentResponse) -> dict[str, Any]:
    meta = resp.usage_metadata
    if meta is None:
        return {}
    return {
        "prompt_tokens": meta.prompt_token_count,
        "candidates_tokens": meta.candidates_token_count,
        "thoughts_tokens": meta.thoughts_token_count,
        "total_tokens": meta.total_token_count,
    }


Plugin = Gemini
