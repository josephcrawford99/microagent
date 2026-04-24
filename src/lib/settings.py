"""Typed config via pydantic-settings.

One model, two sources:
  - /config/config.toml  — every non-secret setting lives here
  - /config/.env         — secrets only (tokens, passwords)

TOML is what the dashboard edits and what a human hand-edits. .env is what
rotates independently (token refreshes, password changes) and what survives
!update because it sits in /config, not /repo. Secrets are flat top-level
fields on Settings so they bind to env vars by upper-case name with no alias
gymnastics; non-secrets are nested models that mirror the TOML structure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr  # noqa: F401 — Field used in defaults
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
    allowed_senders: list[str] = Field(default_factory=list)


class TelegramSettings(BaseModel):
    enabled: bool = False
    allowed_chat_ids: list[int] = Field(default_factory=list)
    # getUpdates long-poll timeout. 30s keeps traffic near-zero while idle.
    poll_timeout: int = 30


class IMessageSettings(BaseModel):
    enabled: bool = False
    db_path: str = "/mnt/imessage/chat.db"
    allowed_senders: list[str] = Field(default_factory=list)


class CronSettings(BaseModel):
    # Agent-schedulable wake source. Hard caps below bound how much the agent
    # can spend on self-scheduled wakes; see src/sources/cron.py.
    enabled: bool = False
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
