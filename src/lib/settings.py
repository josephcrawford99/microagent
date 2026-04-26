"""Typed config: schema + read + write + introspection.

One module owns the on-disk config end to end. Two files drive everything:
  - /config/config.toml  — every non-secret setting; dashboard-editable
  - /config/.env         — secrets only (tokens, passwords); rotates independently

`RootConfig` inherits from `pydantic_settings.BaseSettings`, so a no-arg
`RootConfig()` reads both files and produces a frozen snapshot; the process
must be restarted to pick up edits — there is no live-reload path by design
(plugins hold slices captured at construction).

Plugin-specific settings live next to their plugin's implementation and
subclass `RootConfig` (via `InputSettings` / `AgentSettings` bases in
`lib.source` and `lib.agent`). Each is constructible from the parent
`RootConfig` — `SocketSettings(settings)` extracts the
`[interfaces.socket]` slice and layers it onto the parent's data.
"""

from __future__ import annotations
import os
import tomllib
from pathlib import Path
from typing import Any
import tomli_w
from pydantic import Field, SecretStr, TypeAdapter, ValidationError, model_validator
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    TomlConfigSettingsSource,
    PydanticBaseSettingsSource,

)
from lib.source import Source

CONFIG_DIR = Path("/config")
CONFIG_TOML = CONFIG_DIR / "config.toml"
CONFIG_ENV = CONFIG_DIR / ".env"


class RootConfig(BaseSettings):
    """Root config. Plugin-specific settings subclass this and override
    `__init__` to extract their slice — see `lib.source.InputSettings` and
    `lib.agent.AgentSettings`."""

    model_config = SettingsConfigDict(
        toml_file=str(CONFIG_TOML),
        env_file=str(CONFIG_ENV),
        case_sensitive=True,
        extra="allow",
    )

    dashboard_enabled: bool = False
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8767
    dashboard_public_url: str = ""
    dashboard_token: SecretStr | None = Field(
        default=None, validation_alias="DASHBOARD_TOKEN"
    )
    dashboard_demo_token: SecretStr | None = Field(
        default=None, validation_alias="DASHBOARD_DEMO_TOKEN"
    )
    interfaces: dict[str, dict[str, Any]] = Field(default_factory=dict)
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agents: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        """Pydantic way of defining how to load settings in order"""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
        )

    @model_validator(mode="before")
    @classmethod
    def _flatten_dashboard(cls, data: Any) -> Any:
        # Lift [dashboard].* into top-level dashboard_* fields. TOML on disk
        # stays nested; this is purely a model-shape concession so we don't
        # need a separate DashboardSettings class.
        if isinstance(data, dict) and isinstance(data.get("dashboard"), dict):
            for k, v in data.pop("dashboard").items():
                data.setdefault(f"dashboard_{k}", v)
        # Default to a single primary claude agent if [agents.*] is missing.
        if isinstance(data, dict) and not data.get("agents"):
            data["agents"] = {"primary": {"agent_type": "claude"}}
        return data


def enabled_sources(settings: "RootConfig") -> list["Source"]:
    """Lazy-load and instantiate every enabled Interface and Source. Iterates
    the loaded `[interfaces.*]` / `[sources.*]` sections; for each section
    with `enabled = true`, imports `<kind>.<name>` and constructs its
    `Plugin` class with `(agent_id, settings)`.

    Sources need an agent_id for their per-agent state paths — we use the
    first [agents.*] id, matching the daemon's single-agent runtime."""
    from lib.plugins import load_input  # local — avoid cycle

    if not settings.agents:
        raise RuntimeError("no [agents.*] section in config.toml")
    agent_id = next(iter(settings.agents))
    out: list["Source"] = []
    for kind, section in (
        ("interfaces", settings.interfaces),
        ("sources", settings.sources),
    ):
        for name, cfg in section.items():
            if not isinstance(cfg, dict) or not cfg.get("enabled"):
                continue
            cls = load_input(kind, name)
            out.append(cls(agent_id, settings))
    return out


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
    _write_toml(tomllib.loads(text))


def _load_toml() -> dict[str, Any]:
    try:
        return tomllib.loads(CONFIG_TOML.read_text())
    except FileNotFoundError:
        return {}


def _write_toml(data: dict[str, Any]) -> None:
    _atomic_write_bytes(CONFIG_TOML, tomli_w.dumps(data).encode("utf-8"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
    os.replace(tmp, path)


# --- typed mutations -------------------------------------------------------


def _lookup(name: str) -> tuple[str, type]:
    """Resolve a section name to (kind, settings_cls). Reads the TOML to
    decide kind, then lazy-imports the plugin's settings class."""
    from lib.plugins import load_input_settings

    toml = _load_toml()
    if name in (toml.get("interfaces") or {}):
        return "interfaces", load_input_settings("interfaces", name)
    if name in (toml.get("sources") or {}):
        return "sources", load_input_settings("sources", name)
    raise ValueError(f"unknown input: {name}")


def _write_section_field(kind: str, name: str, field: str, value: Any) -> None:
    data = _load_toml()
    data.setdefault(kind, {}).setdefault(name, {})[field] = value
    _write_toml(data)


def toggle(name: str, enabled: bool) -> None:
    """Flip `[<kind>.<name>].enabled` where kind is interfaces or sources."""
    kind, _ = _lookup(name)
    _write_section_field(kind, name, "enabled", bool(enabled))


def set_wake(name: str, wake: bool) -> None:
    """Flip `[sources.<name>].wake_on_event`. Sources only — interfaces always wake."""
    kind, _ = _lookup(name)
    if kind != "sources":
        raise ValueError(f"wake toggle only valid for sources: {name}")
    _write_section_field(kind, name, "wake_on_event", bool(wake))


def set_field(name: str, field: str, value: Any) -> Any:
    """Validate + coerce + write a ui-tagged settings field. Confirms the
    field is in the Input's editable schema (anti-clobber), runs the value
    through pydantic's TypeAdapter for the field's annotation, and persists
    the coerced value."""
    kind, settings_cls = _lookup(name)
    if not any(f["name"] == field for f in editable_fields(settings_cls)):
        raise ValueError(f"field {field!r} is not editable on {name}")
    finfo = settings_cls.model_fields[field]
    try:
        coerced = TypeAdapter(finfo.annotation).validate_python(value)
    except ValidationError as e:
        raise ValueError(str(e)) from None
    _write_section_field(kind, name, field, coerced)
    return coerced


# --- introspection (dashboard) ---------------------------------------------


def editable_fields(settings_cls) -> list[dict[str, Any]]:
    """Return the ui-tagged fields on settings_cls. Today only
    `ui: "whitelist"` is recognized; extend the schema-extra contract here
    when adding new editor kinds (string, secret, …)."""
    if settings_cls is None:
        return []
    out: list[dict[str, Any]] = []
    for fname, finfo in settings_cls.model_fields.items():
        extra = finfo.json_schema_extra or {}
        if not isinstance(extra, dict) or extra.get("ui") != "whitelist":
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
    """One row per discovered wake-input: name, kind, enabled (from TOML),
    required_env, missing_env. Catalog is whatever sections appear in
    config.toml — every plugin shipped with the harness gets seeded into
    config.default.toml on first boot."""
    from lib.plugins import load_input_settings

    env = read_env()
    data = _load_toml()

    def row(kind: str, name: str) -> dict[str, Any] | None:
        try:
            settings_cls = load_input_settings(kind, name)
        except (ImportError, AttributeError):
            return None  # plugin module missing — silently skip
        section = data.get(kind, {}).get(name, {})
        required = list(getattr(settings_cls, "REQUIRED_ENV", ()))
        out: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": bool(section.get("enabled", False)),
            "required_env": required,
            "missing_env": [k for k in required if not env.get(k)],
        }
        if kind == "sources":
            wake_field = settings_cls.model_fields.get("wake_on_event")
            default_wake = (
                bool(wake_field.default) if wake_field is not None else True
            )
            out["wake_on_event"] = bool(section.get("wake_on_event", default_wake))
        fields = editable_fields(settings_cls)
        if fields:
            out["editable_fields"] = fields
            # Whitelist editor expects a list; tolerate hand-edited toml that
            # set a scalar by surfacing it as empty so the user can re-edit.
            out["field_values"] = {
                f["name"]: v if isinstance(v := section.get(f["name"]), list) else []
                for f in fields
            }
        return out

    rows: list[dict[str, Any]] = []
    for n in sorted(data.get("interfaces", {})):
        if r := row("interfaces", n):
            rows.append(r)
    for n in sorted(data.get("sources", {})):
        if r := row("sources", n):
            rows.append(r)
    return rows
