import glob
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from lib.config import DATA_DIR
from lib.interface import Interface, Message, Trigger


@dataclass
class TerminalTrigger(Trigger):
    pending: int


class Terminal(Interface):
    """File-based terminal interface. talk.py writes to inbox/, reads from outbox/.

    Owns its own storage under {DATA_DIR}/interfaces/terminal/ — the path matches
    talk.py so the two stay in sync. Inherits the default Interface.tools()
    which auto-generates `terminal_receive` and `terminal_send` MCP tools.
    """

    name = "terminal"

    def __init__(self, config):
        super().__init__(config)
        self.inbox = os.path.join(DATA_DIR, "interfaces", "terminal", "inbox")
        self.outbox = os.path.join(DATA_DIR, "interfaces", "terminal", "outbox")
        os.makedirs(self.inbox, exist_ok=True)
        os.makedirs(self.outbox, exist_ok=True)

    def trigger_wake(self) -> Optional[TerminalTrigger]:
        pending = sum(1 for f in os.listdir(self.inbox) if f.endswith(".json"))
        if pending == 0:
            return None
        return TerminalTrigger(interface=self, pending=pending)

    async def receive(self) -> list[Message]:
        out = []
        for path in sorted(glob.glob(os.path.join(self.inbox, "*.json"))):
            with open(path) as f:
                data = json.load(f)
            os.remove(path)
            out.append(
                Message(
                    body=data.get("body", ""),
                    sender=data.get("from", "user"),
                    to=data.get("to", "agent"),
                )
            )
        return out

    async def send(self, message: Message) -> str:
        ts = str(int(time.time() * 1000))
        path = os.path.join(self.outbox, f"{ts}.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "body": message.body,
                    "from": message.sender or "agent",
                    "to": message.to or "user",
                },
                f,
            )
        return "sent to terminal"
