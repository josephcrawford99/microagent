"""IMAP/SMTP email channel.
One poll fetches UNSEEN mail """

from __future__ import annotations

import asyncio
import email.utils
import imaplib
import logging
import smtplib
from email import policy
from email.message import EmailMessage as StdEmailMessage
from typing import Any

from lib.message import Message
from services.base_service import Service

log = logging.getLogger(__name__)


class Email(Service):
    """Shared interface for sending and recieving emails"""
    name = "email"
    REQUIRED_ENV = ("EMAIL_PASSWORD",)
    ADDRESS_HINT = "<recipient email address>"
    BODY_HINT = "Subject: <subject line>\n\n<message text>"
    # IMAP isn't latency-sensitive; poll every minute. IMAP IDLE would be
    # the proper push fix but adds deps; out of scope.
    poll_interval_s = 60
    error_backoff_s = 30

    def __init__(self, agent_id: str, config: dict[str, Any]) -> None:
        super().__init__(agent_id, config)
        self.username = str(config.get("username", ""))
        self.imap_host = str(config.get("imap_host", "imap.gmail.com"))
        self.imap_port = int(config.get("imap_port", 993))
        self.smtp_host = str(config.get("smtp_host", "smtp.gmail.com"))
        self.smtp_port = int(config.get("smtp_port", 587))
        self.password = self.secrets["EMAIL_PASSWORD"]
        self.allowed_senders = [
            s.lower() for s in config.get("allowed_senders", [])
        ]

    async def poll(self) -> None:
        for sender, subject, body in await asyncio.to_thread(self._drain):
            self.write_out(sender=sender, body=self._join_subject(subject, body))

    def _drain(self) -> list[tuple[str, str, str]]:
        """Fetch (and thereby mark seen) all unread mail; return the allowed
        ones as (sender, subject, body)."""
        out: list[tuple[str, str, str]] = []
        with self._imap() as imap:
            for msg_id in self._search_ids(imap, "UNSEEN"):
                parsed = self._fetch(imap, msg_id)
                if parsed is None:
                    continue
                sender = email.utils.parseaddr(parsed.get("From", ""))[1].lower()
                if self.allowed_senders and sender not in self.allowed_senders:
                    log.info("ignoring email from non-allowed sender: %s", sender)
                    continue
                out.append((
                    sender,
                    parsed.get("Subject", "") or "",
                    self._extract_body(parsed),
                ))
        return out

    async def handle_in(self, msg: Message) -> None:
        if not msg.address:
            raise RuntimeError("email needs an `address`")
        subject, body = self._split_subject(msg.body)
        await asyncio.to_thread(self._smtp_send, msg.address, subject, body)

    @staticmethod
    def _join_subject(subject: str, body: str) -> str:
        """Inverse of `_split_subject`: inbound mail reaches the model in the
        same shape the model is told to write outbound mail in."""
        return f"Subject: {subject}\n\n{body}" if subject else body

    @staticmethod
    def _split_subject(body: str) -> tuple[str, str]:
        """Peel a leading `Subject:` line off the body. Missing header means
        the whole thing is the message — never a hard failure."""
        head, sep, rest = body.partition("\n")
        if not head.lower().startswith("subject:"):
            return "(no subject)", body
        return head[len("subject:"):].strip() or "(no subject)", rest.lstrip("\n")

    def _smtp_send(self, to: str, subject: str, body: str) -> None:
        mime = StdEmailMessage()
        mime["Subject"] = subject
        mime["From"] = self.username
        mime["To"] = to
        mime.set_content(body)
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
            server.starttls()
            server.login(self.username, self.password)
            server.send_message(mime)
        log.info("sent email to %s: %s", to, subject)

    # --- helpers ---

    def _imap(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        imap.login(self.username, self.password)
        imap.select("INBOX")
        return imap

    @staticmethod
    def _search_ids(imap: imaplib.IMAP4_SSL, criterion: str) -> list[bytes]:
        status, data = imap.search(None, criterion)
        if status != "OK" or not data or data[0] is None:
            return []
        return data[0].split()

    @staticmethod
    def _fetch(imap: imaplib.IMAP4_SSL, msg_id: bytes) -> StdEmailMessage | None:
        status, msg_data = imap.fetch(msg_id.decode(), "(RFC822)")
        if status != "OK" or not msg_data:
            return None
        first = msg_data[0]
        if not isinstance(first, tuple) or len(first) < 2:
            return None
        raw = first[1]
        if not isinstance(raw, (bytes, bytearray)):
            return None
        return email.message_from_bytes(bytes(raw), policy=policy.default)

    @staticmethod
    def _extract_body(parsed: StdEmailMessage) -> str:
        part = parsed.get_body(preferencelist=("plain",))
        if part is None:
            return ""
        content = part.get_content()
        return content if isinstance(content, str) else ""
