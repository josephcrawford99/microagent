"""Typed config: schema + read + write + introspection.

One module owns the on-disk config end to end. Two files drive everything:
  - /config/config.toml  — every non-secret setting; dashboard-editable
  - /config/.env         — secrets only (tokens, passwords); rotates independently

Settings() is a read-only snapshot loaded at boot. Mutations below write the
files on disk; the process must be restarted to pick them up — there is no
live-reload path by design (interfaces hold slices of Settings captured at
construction).

Secrets are flat top-level fields on Settings so they bind to env vars by
upper-case name with no alias gymnastics; non-secrets are nested models that
mirror the TOML structure.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field, SecretStr, TypeAdapter, ValidationError  # noqa: F401
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_DIR = Path("/config")
CONFIG_TOML = CONFIG_DIR / "config.toml"
CONFIG_ENV = CONFIG_DIR / ".env"
SOUL_MD = CONFIG_DIR / "soul.md"


# --- non-secret config models (mirror config.toml) -------------------------


class UserSettings(BaseModel):
    name: str = ""


class SocketSettings(BaseModel):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8765


class EmailSettings(BaseModel):
    enabled: bool = False
    username: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    allowed_senders: list[str] = Field(
        default_factory=list,
        json_schema_extra={
            "ui": "whitelist",
            "label": "Allowed senders",
            "placeholder": "alice@example.com",
            "help": "Email addresses permitted to wake the agent. Empty = no allowlist (every inbound email wakes).",
            "required_to_enable": False,
        },
    )


class TelegramSettings(BaseModel):
    enabled: bool = False
    allowed_chat_ids: list[int] = Field(
        default_factory=list,
        json_schema_extra={
            "ui": "whitelist",
            "label": "Allowed chat IDs",
            "placeholder": "12345678",
            "help": "Telegram chat IDs permitted to message the agent. Empty = none can.",
            "required_to_enable": True,
        },
    )
    # getUpdates long-poll timeout. 30s keeps traffic near-zero while idle.
    poll_timeout: int = 30


class IMessageSettings(BaseModel):
    enabled: bool = False
    wake_on_event: bool = False
    db_path: str = "/mnt/imessage/chat.db"


class CronSettings(BaseModel):
    # Agent-schedulable wake source. Hard caps below bound how much the agent
    # can spend on self-scheduled wakes; see src/sources/cron.py.
    enabled: bool = False
    wake_on_event: bool = True
    max_active: int = 8
    min_delay_seconds: int = 60
    max_fires_per_day: int = 24


class WebChatSettings(BaseModel):
    enabled: bool = False


class InterfacesSettings(BaseModel):
    socket: SocketSettings = SocketSettings()
    email: EmailSettings = EmailSettings()
    telegram: TelegramSettings = TelegramSettings()
    web_chat: WebChatSettings = WebChatSettings()


class SourcesSettings(BaseModel):
    imessage: IMessageSettings = IMessageSettings()
    cron: CronSettings = CronSettings()


class DashboardSettings(BaseModel):
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8767
    public_url: str = ""


class ClaudeAgentSettings(BaseModel):
    rotation_time: str = "03:00"
    cli_settings: dict[str, Any] = Field(default_factory=dict)


class AgentsSettings(BaseModel):
    claude: ClaudeAgentSettings = ClaudeAgentSettings()


# --- top-level Settings -----------------------------------------------------


class Settings(BaseSettings):
    """Root config. Instantiate once at boot; pass slices down."""

    model_config = SettingsConfigDict(
        toml_file=str(CONFIG_TOML),
        env_file=str(CONFIG_ENV),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Meta
    agent_type: str = "claude"
    agent_id: str = "primary"

    # Non-secret, from TOML
    user: UserSettings = UserSettings()
    interfaces: InterfacesSettings = InterfacesSettings()
    sources: SourcesSettings = SourcesSettings()
    dashboard: DashboardSettings = DashboardSettings()
    agents: AgentsSettings = AgentsSettings()

    # Secrets, from .env (flat top-level bind to upper-case env var names)
    email_password: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    dashboard_token: SecretStr = SecretStr("")
    dashboard_demo_token: SecretStr = SecretStr("")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority (first wins): init kwargs > env > .env > TOML > defaults.
        # Secrets live in env/.env; non-secrets in TOML. No overlap expected.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_soul() -> str:
    """Read /config/soul.md. Returns empty string if missing — agents should
    treat that as 'no identity prompt' and log a warning rather than crash."""
    if not SOUL_MD.exists():
        return ""
    return SOUL_MD.read_text().strip()


# --- file I/O --------------------------------------------------------------


def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with CONFIG_ENV.open() as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def write_env(entries: dict[str, str]) -> None:
    """Preserve comments / order of existing keys; append new ones at the end.
    Keys absent from `entries` are dropped (UI delete)."""
    try:
        with CONFIG_ENV.open() as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(entries)
    out: list[str] = []
    for line in lines:
        s = line.lstrip()
        if s.startswith("#") or "=" not in s:
            out.append(line)
            continue
        k = s.split("=", 1)[0].strip()
        if k in remaining:
            out.append(f"{k}={remaining.pop(k)}\n")
    for k, v in remaining.items():
        out.append(f"{k}={v}\n")
    _atomic_write_bytes(CONFIG_ENV, "".join(out).encode("utf-8"))


def read_toml_text() -> str:
    try:
        return CONFIG_TOML.read_text()
    except FileNotFoundError:
        return ""


def write_toml_text(text: str) -> None:
    """Validate raw TOML, then round-trip through tomli_w so the file stays
    well-formed and canonically formatted."""
    _write_toml_data(tomllib.loads(text))


def _load_toml_data() -> dict[str, Any]:
    try:
        return tomllib.loads(CONFIG_TOML.read_text())
    except FileNotFoundError:
        return {}


def _write_toml_data(data: dict[str, Any]) -> None:
    buf = tomli_w.dumps(data).encode("utf-8")
    _atomic_write_bytes(CONFIG_TOML, buf)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
    os.replace(tmp, path)


# --- typed mutations -------------------------------------------------------


def registry_kind(name: str) -> str | None:
    """Return 'interfaces' or 'sources' depending on which registry owns the
    given input name. None if unknown. Lazy registry import keeps this module
    a leaf at import time."""
    from interfaces import INTERFACES
    from sources import SOURCES

    if name in INTERFACES:
        return "interfaces"
    if name in SOURCES:
        return "sources"
    return None


def _lookup(name: str) -> tuple[str, type]:
    from interfaces import INTERFACES
    from sources import SOURCES

    if name in INTERFACES:
        return "interfaces", INTERFACES[name]
    if name in SOURCES:
        return "sources", SOURCES[name]
    raise ValueError(f"unknown input: {name}")


def _write_section_field(name: str, field: str, value: Any) -> None:
    """Write `[<kind>.<name>].<field> = value` into config.toml, preserving
    the rest of the file."""
    kind, _ = _lookup(name)
    data = _load_toml_data()
    data.setdefault(kind, {}).setdefault(name, {})[field] = value
    _write_toml_data(data)


def toggle(name: str, enabled: bool) -> None:
    """Flip `[<kind>.<name>].enabled` where kind is interfaces or sources."""
    _write_section_field(name, "enabled", bool(enabled))


def set_wake(name: str, wake: bool) -> None:
    """Flip `[sources.<name>].wake_on_event`. Sources only — interfaces
    always wake."""
    kind, _ = _lookup(name)
    if kind != "sources":
        raise ValueError(f"wake toggle only valid for sources: {name}")
    _write_section_field(name, "wake_on_event", bool(wake))


def set_field(name: str, field: str, value: Any) -> Any:
    """Validate + coerce + write a ui-tagged settings field. Confirms the
    field is in the Input's editable schema (anti-clobber), runs the value
    through pydantic's TypeAdapter for the field's annotation, and persists
    the coerced value."""
    _, cls = _lookup(name)
    if field not in {f["name"] for f in editable_fields(cls)}:
        raise ValueError(f"field {field!r} is not editable on {name}")
    finfo = cls.settings_cls.model_fields[field]
    try:
        coerced = TypeAdapter(finfo.annotation).validate_python(value)
    except ValidationError as e:
        raise ValueError(str(e)) from None
    _write_section_field(name, field, coerced)
    return coerced


# --- introspection (dashboard) ---------------------------------------------


def editable_fields(cls) -> list[dict[str, Any]]:
    """Return the ui-tagged fields on cls.settings_cls. Today only
    `ui: "whitelist"` is recognized; extend the schema-extra contract here
    when adding new editor kinds (string, secret, …)."""
    settings_cls = getattr(cls, "settings_cls", None)
    if settings_cls is None:
        return []
    out: list[dict[str, Any]] = []
    for fname, finfo in settings_cls.model_fields.items():
        extra = finfo.json_schema_extra or {}
        if not isinstance(extra, dict):
            continue
        if extra.get("ui") != "whitelist":
            continue
        out.append({
            "name": fname,
            "label": extra.get("label", fname),
            "placeholder": extra.get("placeholder", ""),
            "help": extra.get("help", ""),
            "required_to_enable": bool(extra.get("required_to_enable", False)),
        })
    return out


def inputs_status() -> list[dict[str, Any]]:
    """One row per discovered wake-input (Interface or Source): name, kind,
    enabled (from config.toml), required_env, and which of those are
    currently missing from .env. Shape is the dashboard bootstrap contract."""
    from interfaces import INTERFACES
    from sources import SOURCES

    env = read_env()
    data = _load_toml_data()
    interfaces_section = data.get("interfaces", {}) if isinstance(data, dict) else {}
    sources_section = data.get("sources", {}) if isinstance(data, dict) else {}

    def row(kind: str, name: str, cls) -> dict[str, Any]:
        section_root = interfaces_section if kind == "interfaces" else sources_section
        section = section_root.get(name, {}) if isinstance(section_root, dict) else {}
        section = section if isinstance(section, dict) else {}
        required = list(getattr(cls, "required_env", []) or [])
        out: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": bool(section.get("enabled", False)),
            "required_env": required,
            "missing_env": [k for k in required if not env.get(k)],
        }
        if kind == "sources":
            # Fallback mirrors the source's settings-model default so the UI
            # matches code behaviour when the field is absent from config.toml.
            default_wake = False
            if cls.settings_cls is not None:
                try:
                    default_wake = bool(cls.settings_cls().wake_on_event)
                except Exception:
                    default_wake = False
            out["wake_on_event"] = bool(section.get("wake_on_event", default_wake))
        fields = editable_fields(cls)
        if fields:
            out["editable_fields"] = fields
            values: dict[str, Any] = {}
            for f in fields:
                v = section.get(f["name"])
                values[f["name"]] = v if isinstance(v, list) else []
            out["field_values"] = values
        return out

    rows: list[dict[str, Any]] = []
    for n in sorted(INTERFACES):
        rows.append(row("interfaces", n, INTERFACES[n]))
    for n in sorted(SOURCES):
        rows.append(row("sources", n, SOURCES[n]))
    return rows
