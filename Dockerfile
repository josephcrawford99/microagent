FROM python:3.12-alpine

RUN apk add --no-cache nodejs npm git tzdata   # tzdata: time.tzset() needs zoneinfo
RUN npm install -g @anthropic-ai/claude-code
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN git config --global --add safe.directory /microagent   # bind-mount UID mismatch

WORKDIR /microagent
CMD ["python3", "-u", "src/main.py"]
