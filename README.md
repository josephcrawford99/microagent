# microagent

A lightweight Docker-containerized personal assistant that communicates through pluggable interfaces (email, iMessage, custom) and runs any CLI-based AI agent.

## Quick Start

```bash
# 1. Edit soul/config.json with your settings
# 2. Start with the ping agent to test
docker compose up -d

# 3. Check logs
docker compose logs -f

# 4. Test: manually drop a message into the email inbox
docker compose exec microagent sh -c \
  'echo "{\"id\":\"1\",\"channel\":\"email\",\"from\":\"test\",\"to\":\"agent\",\"body\":\"ping\"}" > /data/interfaces/email/inbox/test.json'
```

## Directory Structure

```
soul/                  (bind mount, read-only)
  soul.md              personality & behavioral guidelines
  config.json          user settings, schedule, agent type, interfaces
  context/             additional .md context files

/data/                 (docker volume, read-write)
  interfaces/
    email/
      inbox/           incoming messages as .json
      outbox/          outgoing messages as .json
        sent/          successfully sent messages
  workspace/           agent's scratch space (starts empty)
  sessions/            session ID mappings
  agent.log            daemon log
```

## Configuration

`soul/config.json`:

```json
{
  "user": { "name": "Joey" },
  "schedule": "*/30 7-23 * * *",
  "idle_timeout": 300,
  "agent_type": "ping",
  "interfaces": {
    "email": {
      "enabled": true,
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "username": "agent@example.com",
      "password_env": "EMAIL_PASSWORD",
      "poll_interval": 30,
      "allowed_senders": ["joey@example.com"]
    }
  }
}
```

- `schedule` — cron expression for autonomous wake-ups
- `idle_timeout` — seconds to stay awake after last activity
- `agent_type` — name of the agent type module (see below)

## Agent Types

Agent types live in `src/agent_types/`. Each is a Python class extending `AgentType`:

```python
from agent_types.base import AgentType

class MyAgent(AgentType):
    def wake(self, messages, session_id=None):
        # messages: list of message dicts from inboxes
        # Return a response string, or None to stay silent
        ...
```

Built-in types:
- **`ping`** — test agent, responds "pong" to "ping", silent otherwise
- **`claude`** — Claude CLI with `--session-id` / `--resume` for conversation continuity

Set `"agent_type": "myagent"` in config.json (matches the filename without `.py`).

### Using Claude

1. Bind-mount the claude binary and config into the container (uncomment lines in `docker-compose.yml`)
2. Set `"agent_type": "claude"` in config.json

## Interfaces

Interfaces live in `src/interfaces/`. Each is a Python class extending `Interface`:

```python
from interfaces.base import Interface

class MyInterface(Interface):
    def poll(self):
        # Fetch from external source, write .json to self.inbox_dir
        # Return number of new messages
        ...

    def send(self, message_path):
        # Read .json from outbox, send via external protocol
        # Move to self.sent_dir on success
        ...
```

Built-in:
- **`email`** — IMAP polling + SMTP sending

Add your interface under `"interfaces"` in config.json with `"enabled": true`.

## Message Format

```json
{
  "id": "1712130000000",
  "channel": "email",
  "from": "joey@example.com",
  "to": "agent",
  "timestamp": "2026-04-03T10:00:00Z",
  "subject": "Hey",
  "thread": "email_joey_20260403",
  "body": "What's on my calendar today?"
}
```

## Running on Colima

```bash
colima start
docker compose up -d
```

The container is ~50MB (Alpine + Python, no pip packages).
