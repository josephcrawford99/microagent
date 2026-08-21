FROM python:3.12-alpine

# bash: the claude CLI's Bash tool refuses to run without it (busybox sh is
# not enough). tzdata: time.tzset() needs zoneinfo.
RUN apk add --no-cache nodejs npm git bash tzdata
RUN npm install -g @anthropic-ai/claude-code
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN git config --global --add safe.directory /microagent   # bind-mount UID mismatch

WORKDIR /microagent
CMD ["python3", "-u", "src/main.py"]
