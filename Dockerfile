FROM python:3.12-alpine

RUN apk add --no-cache nodejs npm git
RUN npm install -g @anthropic-ai/claude-code
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Commit-attribution setting for the Claude Code CLI: suppress the
# Co-Authored-By: Claude trailer. Baked into the image (not runtime config).
RUN mkdir -p /root/.claude && \
    echo '{"attribution":{"commit":""}}' > /root/.claude/settings.json

# The repo is bind-mounted from the host with host UID — tell git it's fine
# so `!update` can fetch/reset inside the container.
RUN git config --global --add safe.directory /repo

WORKDIR /repo/src
CMD ["python3", "-u", "main.py"]
