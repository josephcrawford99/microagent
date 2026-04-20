import glob
import json
import os
import time
from typing import Optional

from lib.config import DATA_DIR
from lib.interface import Interface, Message, Trigger


class Terminal(Interface):
    """File-based terminal interface. talk.py writes to inbox/, reads from outbox/.

    Owns its own storage under {DATA_DIR}/interfaces/terminal/ — the path matches
    talk.py so the two stay in sync. Inherits the default Interface.tools()
    which auto-generates `terminal_receive` and `terminal_send` MCP tools.
    """

    name = "terminal"

    def __init__(self) -> None:
        self.inbox = os.path.join(DATA_DIR, "interfaces", "terminal", "inbox")
        self.outbox = os.path.join(DATA_DIR, "interfaces", "terminal", "outbox")
        os.makedirs(self.inbox, exist_ok=True)
        os.makedirs(self.outbox, exist_ok=True)

    def trigger_wake(self) -> Optional[Trigger]:
        if not any(f.endswith(".json") for f in os.listdir(self.inbox)):
            return None
        return Trigger(interface=self)

    async def receive(self) -> list[Message]:
        out: list[Message] = []
        for path in sorted(glob.glob(os.path.join(self.inbox, "*.json"))):
            with open(path) as f:
                data = json.load(f)
            os.remove(path)
            out.append(
                Message(
                    body=str(data.get("body", "")),
                    sender=str(data.get("from", "user")),
                    to=str(data.get("to", "agent")),
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
