FROM python:3.12-alpine

RUN apk add --no-cache nodejs npm git
RUN npm install -g @anthropic-ai/claude-code
RUN pip install --no-cache-dir claude-agent-sdk python-dotenv

WORKDIR /repo/src
CMD ["python3", "-u", "main.py"]
