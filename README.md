# microagent

A small always-on personal assistant. Single Docker container, polls a set of pluggable **interfaces** (socket, email, telegram, imessage, web chat), and wakes a pluggable **agent type** (Claude or a no-LLM ping smoke test) when any of them has something to deal with. The agent acts on the world by calling each interface's `receive` / `send` as MCP tools.

## Directory layout (inside the container)

Each top-level dir has one job. Mounts are declared in `docker-compose.yml`.

| Dir | Purpose | Survives `!update`? | Agent access |
|---|---|---|---|
| `/repo`        | source code, bind-mounted from host `~/microagent/`                    | ❌ wiped by update | read/write |
| `/config`      | user-controlled: `config.toml`, `.env`, `soul.md`                      | ✅                 | read-only (convention) |
| `/state`       | harness state — `<agent_id>/{agent,telegram,imessage}.json`, `agent.log` | ✅                 | don't touch |
| `/space`       | agent scratch — `js/` npm workspace, `index.html`, anything it writes   | ✅                 | full read/write (cwd) |
| `/mnt/imessage`| host `~/Library/Messages/` read-only (iMessage feed)                   | n/a                | read-only |

## Quick start

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

## Configuration

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
- **`claude`** — runs `claude_agent_sdk.query()` once per wake with all interfaces' tools combined into one in-process MCP server. Reads `/config/soul.md` as the system prompt on every wake (edits land without restart). Logs every stream message.

Auth: the SDK reads `CLAUDE_CODE_OAUTH_TOKEN` from the environment. Get one with `claude setup-token` (no API billing).

## Built-in interfaces

- **`socket`** — TCP line in/out. `nc host 8765`.
- **`email`** — IMAP (search UNSEEN) + SMTP. Server-side filter by `allowed_senders` so newsletters don't wake the agent.
- **`telegram`** — HTTP Bot API. `allowed_chat_ids` is the cost guard. Live status message reflects thinking / tool use.
- **`imessage`** — read-only host `chat.db` via `/mnt/imessage`. Receive-only; outbound goes via another channel.
- **`web_chat`** — the agent's side of the dashboard chat box.

## Dashboard

HTTP control panel at `:8767`. Not an agent interface — it's a separate view that reads `Settings`, writes `/config/config.toml` via `tomli-w`, rotates `/config/.env`, and proxies chat to the `web_chat` interface.

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

## Adding an interface

One file in `src/interfaces/` with a class setting `name`, a constructor `(agent_id, settings_slice, ...secrets)`, and `trigger_wake()` / `receive()` / `send()`. The base `Interface.tools()` auto-generates `{name}_receive` and `{name}_send` MCP tools by introspecting the message dataclass — no hand-written schema.

```python
from dataclasses import dataclass
from lib.interface import Interface, Message, Trigger
from lib.settings import SlackSettings   # add to lib/settings.py

@dataclass
class SlackMessage(Message):
    channel: str = ""

class Slack(Interface):
    name = "slack"
    message_class = SlackMessage

    def __init__(self, agent_id, settings: SlackSettings, token: str):
        super().__init__(agent_id)
        ...

    def trigger_wake(self): ...
    async def receive(self): ...
    async def send(self, message): ...
```

Wire it up in `src/main.py::build_interfaces` and add an `[interfaces.slack]` section to `config.toml`.

## Adding an agent type

Subclass `AgentType`, implement `on_wake()`. The base `wake()` wraps in try/except that notifies every triggering interface and drains it on failure. State (if any) goes through `ComponentState(agent_id, "agent")` → `/state/<agent_id>/agent.json`.

## How a wake works

1. `main.py` polls every `POLL_INTERVAL` seconds (3s by default).
2. Each interface's `trigger_wake()` returns a `Trigger | None` — cheap check, no message fetch.
3. If any triggers are non-None, `agent.wake(triggers)` runs.
4. Claude builds an MCP server from every interface's tools and calls `query()` once. Uses `{name}_receive` / `{name}_send` to read and reply.
5. On exception, the base notifies each triggering interface and drains it so the next poll sees a clean state.
