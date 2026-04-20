FROM python:3.12-alpine

RUN apk add --no-cache nodejs npm git
RUN npm install -g @anthropic-ai/claude-code
RUN pip install --no-cache-dir claude-agent-sdk python-dotenv

# The repo is bind-mounted from the host with host UID — tell git it's fine
# so `!update` can fetch/reset inside the container.
RUN git config --global --add safe.directory /repo

WORKDIR /repo/src
CMD ["python3", "-u", "main.py"]
