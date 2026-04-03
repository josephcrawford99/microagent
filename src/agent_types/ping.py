import logging
from lib.base import AgentType

log = logging.getLogger("microagent.ping")


class Ping(AgentType):
    """Test agent. Responds 'pong' to 'ping', otherwise stays silent."""

    name = "ping"

    def wake(self, messages, session_id=None):
        log.info("ping agent woke up | messages=%d session=%s", len(messages), session_id)

        if not messages:
            log.info("no messages, going back to sleep")
            return None

        found_ping = False
        for msg in messages:
            body = msg.get("body", "").strip()
            sender = msg.get("from", "unknown")
            log.info("message from %s: %s", sender, body)
            if "ping" in body.lower():
                found_ping = True

        if found_ping:
            log.info("ping received, responding pong")
            return "pong"

        log.info("no ping found, going back to sleep")
        return None
