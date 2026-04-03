FROM python:3.12-alpine

RUN apk add --no-cache nodejs npm
RUN npm install -g @anthropic-ai/claude-code

COPY src/ /app/src/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/src/inbox_trigger.sh

WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
