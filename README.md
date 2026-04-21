# microagent

A small always-on personal assistant. Runs in a single Docker container, polls a set of pluggable **interfaces** (socket, email, …), and wakes a pluggable **agent type** (currently Claude or a no-LLM ping smoke test) when any of them have something to deal with. The agent acts on the world by calling each interface's `receive` / `send` as MCP tools.

## Quick start

```fish
# 1. Get a Claude OAuth token (uses your Claude Max / Pro subscription, no API billing)
docker compose run --rm -it microagent claude setup-token
# Copy the printed `sk-ant-oat01-...` value.

# 2. Drop it in a .env next to docker-compose.yml
echo "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-..." > .env

# 3. Build and start
docker compose up -d --build
docker compose logs -f microagent

# 4. Talk to it via the socket interface
echo "ping" | nc 127.0.0.1 8765
```

## Layout

```
soul/                  bind mount, read-only
  config.json          which agent type, which interfaces, per-interface config
  soul.md              system prompt for the LLM agent
data/                  docker volume, read-write
  agent.log            daemon log mirror
src/
  main.py              async daemon: poll interfaces, wake agent
  lib/
    agent.py           AgentType base — wraps on_wake() with error notify+drain
    interface.py       Interface + Message + Trigger bases, default MCP tools()
    config.py          load_config / load_soul_prompt
  agent_types/
    ping.py            no-LLM smoke test
    claude.py          claude-agent-sdk query() with in-process MCP server
  interfaces/
    socket.py          tcp line-in/line-out
    email.py           direct IMAP/SMTP, no filesystem inbox
    meta.py            control-plane: !update / !restart / !env
    dashboard.py       http ui for editing .env / soul/config.json
```

## Configuration

`soul/config.json`:

```json
{
  "user": { "name": "Joey" },
  "agent_type": "claude",
  "interfaces": {
    "socket": { "enabled": true, "host": "0.0.0.0", "port": 8765 },
    "email": {
      "enabled": true,
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "username": "your.agent@gmail.com",
      "password_env": "EMAIL_PASSWORD",
      "allowed_senders": ["you@example.com"]
    }
  }
}
```

`agent_type` and the keys under `interfaces` map to module names in `src/agent_types/` and `src/interfaces/` (auto-discovered).

Secrets live in `.env` and pass through `docker-compose.yml`:

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
EMAIL_PASSWORD=your-16-char-app-password
```

For Gmail: 2-Step Verification → App Passwords (https://myaccount.google.com/apppasswords), and enable IMAP under Settings → Forwarding and POP/IMAP. `allowed_senders` is a strict allowlist; mail from anyone else is dropped at the trigger level so it never costs an LLM wake.

## Built-in agent types

- **`ping`** — no LLM. Iterates triggers, replies `pong` to `ping`. Useful for isolating interface bugs from agent bugs.
- **`claude`** — runs `claude_agent_sdk.query()` once per wake with all interfaces' tools combined into one in-process MCP server. Reads `soul/soul.md` as the system prompt on every wake (so edits land without a restart). Logs every stream message so OAuth and tool-use issues are visible in `docker compose logs`.

Auth: the SDK reads `CLAUDE_CODE_OAUTH_TOKEN` from the environment. Get one with `claude setup-token` (which uses your Claude Max / Pro subscription — no API billing).

## Built-in interfaces

- **`socket`** — tcp line-in/line-out. `nc host 8765`, type a line, read the reply.
- **`email`** — direct IMAP (search UNSEEN) and SMTP. `trigger_wake` filters by `allowed_senders` server-side so newsletters and notifications don't fire wakes.
- **`dashboard`** — HTTP control panel (default `:8767`) for editing `.env` and `soul/config.json` at runtime. See [Dashboard](#dashboard).

## Dashboard

Web UI for editing secrets and config without ssh. Runs on port `8767` inside the container and never wakes the agent.

**Auth model:**
- Direct LAN hits (e.g. `http://192.168.0.6:8767`) are trusted — no login.
- Requests arriving via Cloudflare Tunnel (detected by the `CF-Connecting-IP` header) require `DASHBOARD_TOKEN` from `.env`.
- `/demo` is always public and renders a mocked UI with fake values — safe to link from a portfolio.

**Local use:** just hit `http://<box-ip>:8767` from a device on the same network.

**Public access via Cloudflare Tunnel** (for `dashboard.yourdomain.com`):

1. In Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel** → pick *Cloudflared*. Name it (e.g. `microagent`).
2. On the "Install and run connector" screen, copy the **token** (the long string after `--token` in the docker snippet).
3. Add to `.env` on the box:
   ```
   CLOUDFLARED_TOKEN=eyJh...
   DASHBOARD_TOKEN=<any-long-random-string>
   ```
4. In the tunnel's **Public Hostnames** tab, add:
   - Subdomain: `dashboard` (or whatever)
   - Domain: `yourdomain.com` (must already be on Cloudflare)
   - Service: `HTTP` → `microagent:8767` (container name on the compose network)
5. Start the tunnel container:
   ```fish
   docker compose --profile public up -d
   ```
6. Visit `https://dashboard.yourdomain.com` — it'll prompt for `DASHBOARD_TOKEN`. Share `https://dashboard.yourdomain.com/demo` as the read-only portfolio link.

To stop exposing publicly: `docker compose stop cloudflared`. The dashboard stays up on the LAN.

## Adding an interface

A new interface is one file in `src/interfaces/` defining a class with `name`, `trigger_wake()`, `receive()`, `send()`, and (optionally) a `Message` subclass with extra fields. The base `Interface.tools()` auto-generates `{name}_receive` and `{name}_send` MCP tools from `message_class.SCHEMA` — no override needed for the common case.

```python
from dataclasses import dataclass
from typing import ClassVar, Optional
from lib.interface import Interface, Message, Trigger

@dataclass
class SlackMessage(Message):
    channel: str = ""
    SCHEMA: ClassVar[dict] = {"channel": str, "body": str}

@dataclass
class SlackTrigger(Trigger):
    pending: int

class Slack(Interface):
    name = "slack"
    message_class = SlackMessage

    def trigger_wake(self) -> Optional[SlackTrigger]: ...
    async def receive(self) -> list[SlackMessage]: ...
    async def send(self, message: SlackMessage) -> str: ...
```

Then add `"slack": { "enabled": true, ... }` under `interfaces` in `soul/config.json`.

## Adding an agent type

Subclass `AgentType` and implement `on_wake()`. The base `wake()` wraps it in a try/except that notifies every triggering interface and drains their state on failure, so a broken agent can't busy-loop.

```python
from lib.agent import AgentType

class MyAgent(AgentType):
    name = "myagent"

    async def on_wake(self, triggers):
        for t in triggers:
            for msg in await t.interface.receive():
                ...
```

## How a wake works

1. `main.py` polls every `POLL_INTERVAL` seconds (3s by default).
2. Each interface's `trigger_wake()` returns a `Trigger | None` — cheap check, no message fetch.
3. If any triggers are non-None, `agent.wake(triggers)` runs.
4. The Claude agent builds an MCP server from every interface's tools and calls `query()` once. It uses `{name}_receive` to read pending messages and `{name}_send` to reply.
5. On exception, the base `AgentType.wake()` notifies each triggering interface with the error and calls `receive()` to drain whatever caused the trigger, so the next poll sees a clean state.
