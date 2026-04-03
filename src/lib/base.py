import os


class AgentType:
    """Base class for all agent types.

    Subclasses must set `name` and implement wake().
    """

    name: str  # must be set by subclass

    def __init__(self, config, soul_prompt, data_dir, interfaces):
        self.config = config
        self.soul_prompt = soul_prompt
        self.data_dir = data_dir
        self.interfaces = interfaces  # list of Interface instances

    def wake(self, messages, session_id=None):
        """Process incoming messages. Send responses via self.interfaces directly.

        Args:
            messages: list of message dicts from inboxes
            session_id: optional session identifier for conversation continuity
        """
        raise NotImplementedError


class Interface:
    """Base class for all communication interfaces.

    Subclasses must set `name` and implement poll() and send().
    """

    name: str  # must be set by subclass

    def __init__(self, config, data_dir):
        self.name = self.__class__.name
        self.config = config
        self.data_dir = data_dir
        self.inbox_dir = os.path.join(data_dir, "interfaces", self.name, "inbox")
        self.outbox_dir = os.path.join(data_dir, "interfaces", self.name, "outbox")
        self.sent_dir = os.path.join(self.outbox_dir, "sent")
        os.makedirs(self.inbox_dir, exist_ok=True)
        os.makedirs(self.outbox_dir, exist_ok=True)
        os.makedirs(self.sent_dir, exist_ok=True)

    def poll(self):
        """Fetch new messages from the external source and write them as JSON to self.inbox_dir.

        Returns:
            Number of new messages fetched.
        """
        raise NotImplementedError

    def send(self, message_path):
        """Send a message from the outbox via this interface's external protocol.

        After successful send, move the file to self.sent_dir.

        Args:
            message_path: path to the .json message file in outbox
        """
        raise NotImplementedError
