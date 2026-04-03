import json
import logging
import subprocess
from lib.base import AgentType
from lib.sessions import save_session_id

log = logging.getLogger("microagent.claude")


class Claude(AgentType):
    """Claude CLI agent type. Uses claude -p with session management."""

    name = "claude"

    def wake(self, messages, session_id=None):
        log.info("claude agent woke up | messages=%d session=%s", len(messages), session_id)

        if not messages:
            log.info("no messages, nothing to do")
            return None

        prompt = self._build_conversation_prompt(messages)
        thread = messages[0].get("thread")

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
            )
        except subprocess.TimeoutExpired:
            log.error("claude timed out after 120s")
            return None
        except FileNotFoundError:
            log.error("claude binary not found")
            return None

        if result.returncode != 0:
            log.error("claude exited %d: %s", result.returncode, result.stderr.strip())
            return None

        # Parse JSON response to get session_id and result
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

    def _build_conversation_prompt(self, messages):
        parts = [self.soul_prompt, "", "---", ""]
        for msg in messages:
            sender = msg.get("from", "unknown")
            body = msg.get("body", "")
            parts.append(f"[{sender}]: {body}")
        return "\n".join(parts)
