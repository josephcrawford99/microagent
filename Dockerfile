FROM python:3.12-alpine

# Install Node.js (required for claude CLI)
RUN apk add --no-cache nodejs npm

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

COPY src/ /app/src/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh /app/src/inbox_trigger.sh

# Create data directories
RUN mkdir -p /data/interfaces /data/workspace /data/sessions

WORKDIR /app

ENTRYPOINT ["/app/entrypoint.sh"]
