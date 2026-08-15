# microagent

A small always-on personal assistant. Single Docker container, watches a set of pluggable **interfaces** (socket, email, telegram, imessage, web chat), and wakes a pluggable **agent type** (Claude, Gemini, or a no-LLM ping smoke test) the moment any of them has something to deal with. Everything the agent does to the outside world it does through pluggable **tools** (`src/tools/`) — poll a channel, send a message, schedule its own wake — and each agent type declares which of those it gets.

## Directory layout (inside the container)

Each top-level dir has one job. Mounts are declared in `docker-compose.yml`.

| Dir | Purpose | Agent access |
|----------------|------------------------------------------------------------------------|---|---|
| `/repo`        | source code, bind-mounted from host `~/microagent/`               | read/write |
| `/config`      | user-controlled: `config.toml`, `.env`, `soul.md`                      | read-only (convention) |
| `/state`       | harness state — `<agent_id>/{agent,telegram,imessage}.json`, `agent.log` | don't touch |
| `/space`       | agent scratch — `js/` npm workspace, `index.html`, anything it writes   | full read/write (cwd) |
| `/mnt/imessage`| host `~/Library/Messages/` read-only (iMessage feed)                   | read-only |

## Quick start

### Claude
```fish
# 1. Get a Claude OAuth token (uses Claude Max/Pro subscription, no API billing)
docker compose run --rm -it microagent claude setup-token
# copy the sk-ant-oat01-... value it prints

# 2. Run setup — prompts for the token, seeds config, builds & starts
./setup.sh

# 3. Open the dashboard at http://127.0.0.1:8767
#    Enable interfaces (email, telegram, socket, …) from there; the UI
#    prompts for any missing secrets when you flip a toggle on.
```

Only `CLAUDE_CODE_OAUTH_TOKEN` is required. `DASHBOARD_TOKEN` is generated
automatically. Every other secret (Telegram bot token, email password,
Cloudflare tunnel token, …) is optional and added from the dashboard as
needed. The config dir defaults to `~/.config/microagent/` (override with
`XDG_CONFIG_HOME`).

### Configuration

`~/.config/microagent/config.toml` — everything non-secret. Dashboard writes this file directly; you can also edit by hand. Example:

```toml
agent_type = "claude"
agent_id   = "primary"   # per-agent state dir (/state/<agent_id>/)

[user]
name = "Joey"

[agents.claude]
rotation_time = "03:00"

[interfaces.socket]
enabled = true
host = "0.0.0.0"
port  = 8765

[interfaces.web_chat]
enabled = true

[interfaces.email]
enabled = true
username = "your.agent@gmail.com"
allowed_senders = ["you@example.com"]
# ...imap/smtp hosts

[interfaces.telegram]
enabled = true
allowed_chat_ids = [123456789]

[interfaces.imessage]
enabled = true
db_path = "/mnt/imessage/chat.db"
allowed_senders = []

[dashboard]
enabled = true
port = 8767
```

`~/.config/microagent/.env` — secrets only (never commit):

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
EMAIL_PASSWORD=...
TELEGRAM_BOT_TOKEN=...
DASHBOARD_TOKEN=...
DASHBOARD_DEMO_TOKEN=...   # optional: read-only demo session
```

Gmail needs App Passwords (https://myaccount.google.com/apppasswords) and IMAP enabled under Settings → Forwarding and POP/IMAP. `allowed_senders` is a strict allowlist; mail from anyone else is dropped at the trigger level so it never costs an LLM wake.

## Built-in agent types

- **`ping`** — no LLM. Replies `pong` to `ping`. Useful for isolating interface bugs from agent bugs.
- **`claude`** — runs `claude_agent_sdk.query()` once per wake with each tool module registered as its own in-process MCP server, `/space` as cwd, and the full Claude Code toolset. Reads `/config/soul.md` as the system prompt on every wake (edits land without restart). Resumes the prior session, rotating daily. Logs every stream message.
- **`gemini`** — the official `google-genai` SDK against the Gemini API. Brings no toolset of its own, so it also loads `tools/bash.py` for shell access. Because Gemini is stateless, this type owns its transcript and persists it to `/state/<agent_id>/agent.json`, so continuity survives a container rebuild (the `claude` type's does not — the CLI's session store lives in an unmounted `/root/.claude`).

Auth: the Claude SDK reads `CLAUDE_CODE_OAUTH_TOKEN` from the environment — get one with `claude setup-token` (no API billing). Gemini reads `GEMINI_API_KEY`.

Agent types are pluggable the same way interfaces are: nothing in `lib/` imports a vendor SDK. Tool modules publish provider-neutral `lib.tools.ToolSpec`s, and each agent type adapts them — `claude.py` into `SdkMcpTool`s, `gemini.py` into `types.FunctionDeclaration`s.

## Tools

`src/tools/` is the third plugin directory. Each module exposes `build(ctx) -> list[ToolSpec]` and returns nothing when what it wraps isn't enabled, so an agent can list every channel and only get tools for the ones configured.

| Module | Tools |
|---|---|
| `poll` | `poll_all` — drain every source at once |
| `telegram`, `email`, `socket`, `web_chat` | `poll`, `send`, `emit_pending` |
| `imessage` | `poll` (receive-only feed, so no `send`) |
| `session` | `session_idle` |
| `status` | `emit_pending` — fans out to every triggering channel |
| `cron` | `wake_in`, `wake_at`, `wake_daily`, `list`, `cancel` |
| `bash` | `run` — shell in `/space`, for agents without their own |

Names are bare verbs; the adapter namespaces them — Claude sees `mcp__telegram__send`, Gemini `telegram_send`. An agent gets its type's `TOOLS` default, overridable per agent:

```toml
[agents.primary]
tools = ["poll", "telegram", "session", "status", "cron"]
```

The per-channel modules are one-liners over `lib.tools.interface_tools`, which derives `send`'s schema from the channel's `Message` dataclass. A channel that wants more than send/receive adds it in its own file.

## Built-in interfaces

- **`socket`** — TCP line in/out. `nc host 8765`.
- **`email`** — IMAP (search UNSEEN) + SMTP. Server-side filter by `allowed_senders` so newsletters don't wake the agent.
- **`telegram`** — HTTP Bot API. `allowed_chat_ids` is the cost guard. Live status message reflects thinking / tool use.
- **`imessage`** — read-only host `chat.db` via `/mnt/imessage`. Receive-only; outbound goes via another channel.
- **`web_chat`** — the agent's side of the dashboard chat box.

## Self-scheduled wakes (cron)

Enable `[sources.cron]` and load the `cron` tool module to let the agent schedule its own wakes:

- `wake_in(seconds, reason)` — one-shot delay.
- `wake_at(at, reason)` — one-shot at HH:MM (next occurrence) or an ISO datetime.
- `wake_daily(time_of_day, reason)` — recurring daily wake at HH:MM.
- `list()` / `cancel(id)` — inspect and remove pending schedules.

When a schedule fires, the main loop wakes on `cron` and the agent polls to see the reason it left itself. Schedules persist at `/state/<agent_id>/cron.json` and catch up on boot (one-shots more than 24h past due are discarded; daily schedules advance to the next occurrence). Times are container-local.

Hard caps in config bound the "agent schedules a wake storm" failure mode — defaults are `max_active = 8`, `min_delay_seconds = 60`, `max_fires_per_day = 24`. Violating any of them returns an error to the tool call so the model sees the refusal.

## Dashboard

HTTP control panel at `:8767`. Not an agent interface — it's a separate view that reads `RootConfig`, writes `/config/config.toml` via `tomli-w`, rotates `/config/.env`, and proxies chat to the `web_chat` interface.

**Auth:**
- Direct LAN hits are trusted ("owner" role).
- Requests via Cloudflare Tunnel (detected by `CF-Connecting-IP`) need a token.
- `DASHBOARD_TOKEN` → owner (full read/write).
- `DASHBOARD_DEMO_TOKEN` → demo (reads return empty, writes are no-ops). Set it only if you want to share a read-only demo link.

**Public access via Cloudflare Tunnel** (optional, for `dashboard.yourdomain.com`):

1. Cloudflare Zero Trust → Networks → Tunnels → Create tunnel → Cloudflared.
2. Copy the `--token` value.
3. Add to `~/.config/microagent/.env`:
   ```
   CLOUDFLARED_TOKEN=eyJh...
   DASHBOARD_TOKEN=<long-random>
   ```
4. In Public Hostnames: `dashboard.<yourdomain>` → Service `HTTP` → `microagent:8767`.
5. `docker compose --profile public up -d`.

## Running locally (no Docker)

`/config`, `/state`, `/space` and `/repo` are defaults, not constants — override them with `MICROAGENT_CONFIG_DIR`, `MICROAGENT_STATE_DIR`, `MICROAGENT_SPACE_DIR`, `MICROAGENT_REPO_DIR` (see `src/lib/paths.py`). `scripts/run_local.sh` wires all four at a gitignored `./local/` tree, seeds `config.toml` from `src/defaults/config.local.toml` (gemini + socket + dashboard) and `soul.md` from the default, then runs `src/main.py`:

```fish
pip install -r requirements.txt      # or: .venv/bin/pip install -r requirements.txt
echo "GEMINI_API_KEY=..." >> .env    # repo-root .env, gitignored
./scripts/run_local.sh
```

`main.py` loads the repo-root `.env` first (bootstrap: `MICROAGENT_*` plus local keys, no override), then `$MICROAGENT_CONFIG_DIR/.env` with override — so a token rotation written by `POST /api/env` still wins in the container. Talk to it with `nc 127.0.0.1 8765`, or the dashboard at `http://127.0.0.1:8767`. Set `agent_type = "ping"` in `local/config/config.toml` for a no-LLM smoke test.

## Adding an interface

One file in `src/sources/interfaces/` with a class setting `name`, a `Plugin = ClassName` at the bottom, an `InputSettings` subclass, and `start()` / `receive()` / `send()`. A Source is pure plumbing — it has no tool surface of its own.

```python
from dataclasses import dataclass
from typing import ClassVar
from lib.interface import Interface, Message
from lib.source import InputSettings

@dataclass
class SlackMessage(Message):
    channel: str = ""

class SlackSettings(InputSettings):
    KIND: ClassVar[str] = "interfaces"
    SECTION: ClassVar[str] = "slack"
    REQUIRED_ENV: ClassVar[tuple[str, ...]] = ("SLACK_TOKEN",)

class Slack(Interface):
    name = "slack"
    message_class = SlackMessage
    settings_cls = SlackSettings

    def __init__(self, agent_id, settings):
        super().__init__(agent_id, settings)
        cfg = SlackSettings(settings)
        ...

    async def start(self, trigger_q): ...   # call self._signal() on new work
    async def receive(self): ...
    async def send(self, message): ...

Plugin = Slack
```

Add an `[interfaces.slack]` section with `enabled = true` to `config.toml` — that's the whole wiring. `lib.settings.enabled_sources` walks the config and `lib.plugins.load_input` lazy-imports by name; there's no registry to update.

Then give the agent a handle on it — `src/tools/slack.py`:

```python
from lib.tools import ToolContext, ToolSpec, interface_tools

def build(ctx: ToolContext) -> list[ToolSpec]:
    return interface_tools(ctx, "slack")   # poll / send / emit_pending
```

and add `"slack"` to the agent type's `TOOLS` (or the `tools = [...]` list in config). `send`'s schema comes from `SlackMessage`, so `channel` shows up for free. Hand-written tools go in the same file, alongside or instead of the factory call.

## Adding an agent type

One file in `src/agent_types/`: subclass `AgentType`, set `name`, implement `async def on_wake(triggers)`, add `Plugin = ClassName`. Then `agent_type = "<name>"` in `[agents.<id>]`.

What the base gives you: `wake()` wraps `on_wake()` in a try/except that notifies every triggering interface and drains it on failure; `load_soul()` reads `/config/soul.md`; `build_tools(triggers, cfg.tools)` loads this agent's tool modules; `emit_idle(triggers)` tears down the typing indicator; `ComponentState(agent_id, "agent")` → `/state/<agent_id>/agent.json` for state. Declare a `settings_cls` (an `AgentSettings` subclass with `REQUIRED_ENV`) and the dashboard reports missing credentials for you.

`build_tools` returns `{module: [ToolSpec]}` plus the shared `ToolContext`. A `ToolSpec` is `name`, `description`, a flat `{field: type}` param map, and an async handler returning `{"content": [{"type": "text", ...}]}`. Adapt them to your provider's tool format and read `ctx.flags["idle"]` afterwards if sessions mean anything to you; `gemini.py` is the smallest complete example.

## How a wake works

1. Each Source/Interface owns background monitoring (thread, asyncio task, long-poll) and calls `self._signal()` the moment it has work, pushing a `Trigger` onto a shared `asyncio.Queue`. Zero-CPU idle.
2. `main.py` awaits that queue, coalesces any burst that arrived while it was busy, dedupes by interface identity, and calls `agent.wake(triggers)`.
3. The agent type calls `build_tools()`, adapts the resulting ToolSpecs into its provider's tool format, and runs one conversation — the agent polls, replies, and marks itself idle through those tools.
4. On exception, the base notifies each triggering interface and drains it so the loop doesn't spin on the same trigger.
