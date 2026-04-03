import json
import logging
import os
import subprocess
from lib.base import AgentType
from lib.messages import make_message, write_message
from lib.sessions import save_session_id

log = logging.getLogger("microagent.claude")

AUTH_ERROR_HINTS = ["not logged in", "invalid_api_key", "authentication", "unauthorized"]


class Claude(AgentType):
    """Claude CLI agent type. Uses claude -p with session management."""

    name = "claude"

    def wake(self, messages, session_id=None):
        log.info("claude agent woke up | messages=%d session=%s", len(messages), session_id)

        if not messages:
            return

        body = messages[0].get("body", "").strip()

        # No token — treat this message as a token attempt
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = body
            if self._ping():
                log.info("token accepted")
                self._reply("Authenticated. Send your message again.", messages[0])
            else:
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                log.warning("token rejected")
                self._broadcast("Auth failed. Run `claude setup-token` and paste the token here.")
            return

        # Have token — try the real command
        result = self._run_claude(messages, session_id)
        if result == "auth_failed":
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            log.warning("auth expired, cleared token")
            self._broadcast("Auth expired. Run `claude setup-token` and paste the token here.")
            return

        if result:
            self._reply(result, messages[0])

    def _run_claude(self, messages, session_id):
        """Run claude CLI. Returns response text, 'auth_failed', or None."""
        prompt = self._build_prompt(messages)
        thread = messages[0].get("thread")

        cmd = ["claude", "-p", "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]

        log.info("invoking: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=120,
                env={**os.environ},
            )
        except subprocess.TimeoutExpired:
            log.error("claude timed out after 120s")
            return None
        except FileNotFoundError:
            log.error("claude binary not found")
            return None

        if result.returncode != 0:
            combined = (result.stdout + result.stderr).lower()
            if any(hint in combined for hint in AUTH_ERROR_HINTS):
                return "auth_failed"
            log.error("claude exited %d: %s", result.returncode, result.stderr.strip())
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.error("failed to parse claude JSON output")
            return result.stdout.strip() or None

        new_session_id = data.get("session_id")
        if new_session_id and thread:
            save_session_id(self.data_dir, thread, new_session_id)
            log.info("saved session %s for thread %s", new_session_id, thread)

        response = data.get("result", "").strip()
        if response:
            log.info("claude responded (%d chars)", len(response))
        return response or None

    def _ping(self):
        """Verify auth with a trivial claude call."""
        try:
            result = subprocess.run(
                ["claude", "-p", "--output-format", "json"],
                input="respond with exactly: ok",
                capture_output=True, text=True, timeout=30,
                env={**os.environ},
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _reply(self, body, source_msg):
        """Send a response to the interface the message came from."""
        iface_name = source_msg.get("_source_interface")
        iface = next((i for i in self.interfaces if i.name == iface_name), None)
        if not iface:
            log.warning("no interface %s, dropping response", iface_name)
            return
        reply = make_message(
            channel=iface_name,
            sender="agent",
            recipient=source_msg.get("from", ""),
            body=body,
            thread=source_msg.get("thread"),
        )
        iface.send(write_message(iface.outbox_dir, reply))

    def _broadcast(self, body):
        """Send a message to all interfaces."""
        for iface in self.interfaces:
            msg = make_message(
                channel=iface.name,
                sender="agent",
                recipient="",
                body=body,
            )
            iface.send(write_message(iface.outbox_dir, msg))

    def _build_prompt(self, messages):
        parts = [self.soul_prompt, "", "---", ""]
        for msg in messages:
            sender = msg.get("from", "unknown")
            body = msg.get("body", "")
            parts.append(f"[{sender}]: {body}")
        return "\n".join(parts)
