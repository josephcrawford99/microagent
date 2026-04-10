FROM python:3.12-alpine

RUN apk add --no-cache nodejs npm
RUN npm install -g @anthropic-ai/claude-code
RUN pip install --no-cache-dir claude-agent-sdk

COPY src/ /app/src/

WORKDIR /app/src
CMD ["python3", "-u", "main.py"]
