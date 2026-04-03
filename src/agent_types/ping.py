import logging
from lib.base import AgentType
from lib.messages import make_message, write_message

log = logging.getLogger("microagent.ping")


class Ping(AgentType):
    """Test agent. Responds 'pong' to 'ping', otherwise stays silent."""

    name = "ping"

    def wake(self, messages, session_id=None):
        log.info("ping agent woke up | messages=%d session=%s", len(messages), session_id)

        if not messages:
            log.info("no messages, going back to sleep")
            return

        found_ping = None
        for msg in messages:
            body = msg.get("body", "").strip()
            sender = msg.get("from", "unknown")
            log.info("message from %s: %s", sender, body)
            if "ping" in body.lower():
                found_ping = msg

        if found_ping:
            log.info("ping received, responding pong")
            self._reply("pong", found_ping)
        else:
            log.info("no ping found, going back to sleep")

    def _reply(self, body, source_msg):
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
