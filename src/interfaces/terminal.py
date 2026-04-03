import json
import logging
import os
import shutil

from lib.base import Interface

log = logging.getLogger("microagent.terminal")


class Terminal(Interface):
    """Dumb terminal interface. No protocol — just files. A client script writes to inbox and reads from outbox."""

    name = "terminal"

    def __init__(self, config, data_dir):
        super().__init__(config, data_dir)

    def poll(self):
        # nothing to poll — the client script writes directly to inbox
        return 0

    def send(self, message_path):
        # leave the file in outbox — talk.py will consume and delete it
        pass
