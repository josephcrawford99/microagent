import json
import logging
import os
import subprocess
from lib.base import AgentType
from lib.sessions import save_session_id

log = logging.getLogger("microagent.claude")

TOKEN_PATH = os.path.join(os.environ.get("DATA_DIR", "/data"), "claude_token")


class Claude(AgentType):
    """Claude CLI agent type. Uses claude -p with session management."""

    name = "claude"

    def wake(self, messages, session_id=None):
        log.info("claude agent woke up | messages=%d session=%s", len(messages), session_id)

        if not messages:
            log.info("no messages, nothing to do")
            return None

        # Check auth before trying
        if not self._has_auth():
            log.warning("no claude auth token found")
            return (
                "I'm not authenticated yet. Please run this on the server:\n\n"
                "  claude setup-token\n\n"
                "Then paste the token into a message to me."
            )

        # Check if the message is a token being pasted
        body = messages[0].get("body", "").strip()
        if body.startswith("eyJ") and len(body) > 100:
            self._save_token(body)
            log.info("oauth token saved")
            return "Token saved. I'm authenticated now."

        prompt = self._build_conversation_prompt(messages)
        thread = messages[0].get("thread")

        env = os.environ.copy()
        token = self._load_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        cmd = ["claude", "-p", "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]

        log.info("invoking: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        except subprocess.TimeoutExpired:
            log.error("claude timed out after 120s")
            return None
        except FileNotFoundError:
            log.error("claude binary not found")
            return None

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Check for auth errors in stdout too (JSON response)
            try:
                data = json.loads(result.stdout)
                if data.get("is_error") and "Not logged in" in data.get("result", ""):
                    self._clear_token()
                    log.warning("token expired or invalid, cleared")
                    return (
                        "My auth token expired. Please run on the server:\n\n"
                        "  claude setup-token\n\n"
                        "Then paste the new token to me."
                    )
            except (json.JSONDecodeError, KeyError):
                pass
            log.error("claude exited %d: %s", result.returncode, stderr)
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            log.error("failed to parse claude JSON output")
            return result.stdout.strip() or None

        # Save session ID for future resumption
        new_session_id = data.get("session_id")
        if new_session_id and thread:
            save_session_id(self.data_dir, thread, new_session_id)
            log.info("saved session %s for thread %s", new_session_id, thread)

        response = data.get("result", "").strip()
        if response:
            log.info("claude responded (%d chars)", len(response))
        return response or None

    def _has_auth(self):
        return bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or self._load_token())

    def _load_token(self):
        if os.path.exists(TOKEN_PATH):
            with open(TOKEN_PATH) as f:
                return f.read().strip()
        return None

    def _save_token(self, token):
        with open(TOKEN_PATH, "w") as f:
            f.write(token.strip())
        os.chmod(TOKEN_PATH, 0o600)

    def _clear_token(self):
        if os.path.exists(TOKEN_PATH):
            os.remove(TOKEN_PATH)

    def _build_conversation_prompt(self, messages):
        parts = [self.soul_prompt, "", "---", ""]
        for msg in messages:
            sender = msg.get("from", "unknown")
            body = msg.get("body", "")
            parts.append(f"[{sender}]: {body}")
        return "\n".join(parts)
