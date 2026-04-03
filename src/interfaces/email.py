import email
import email.mime.text
import imaplib
import json
import logging
import os
import shutil
import smtplib
import time

from lib.base import Interface
from lib.messages import make_message, write_message

log = logging.getLogger("microagent.email")


class Email(Interface):
    """IMAP/SMTP email interface."""

    name = "email"

    def __init__(self, config, data_dir):
        super().__init__(config, data_dir)
        self.imap_host = config["imap_host"]
        self.imap_port = config.get("imap_port", 993)
        self.smtp_host = config["smtp_host"]
        self.smtp_port = config.get("smtp_port", 587)
        self.username = config["username"]
        self.password = os.environ.get(config.get("password_env", "EMAIL_PASSWORD"), "")
        self.poll_interval = config.get("poll_interval", 30)
        self.allowed_senders = [s.lower() for s in config.get("allowed_senders", [])]

    def poll(self):
        """Fetch unseen emails via IMAP, write to inbox as JSON."""
        count = 0
        try:
            imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            imap.login(self.username, self.password)
            imap.select("INBOX")

            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                log.warning("IMAP search failed: %s", status)
                return 0

            msg_ids = data[0].split()
            for msg_id in msg_ids:
                status, msg_data = imap.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw = msg_data[0][1]
                parsed = email.message_from_bytes(raw)

                sender = email.utils.parseaddr(parsed["From"])[1].lower()
                if self.allowed_senders and sender not in self.allowed_senders:
                    log.info("ignoring email from non-allowed sender: %s", sender)
                    continue

                body = self._extract_body(parsed)
                subject = parsed.get("Subject", "")
                thread = f"email_{sender.split('@')[0]}_{time.strftime('%Y%m%d')}"

                msg = make_message(
                    channel="email",
                    sender=sender,
                    recipient="agent",
                    body=body,
                    subject=subject,
                    thread=thread,
                    extra={"email_message_id": parsed.get("Message-ID", "")},
                )
                write_message(self.inbox_dir, msg)
                count += 1
                log.info("fetched email from %s: %s", sender, subject)

            imap.close()
            imap.logout()

        except Exception:
            log.exception("error polling email")

        return count

    def send(self, message_path):
        """Send an outbox message via SMTP."""
        try:
            with open(message_path) as f:
                msg = json.load(f)

            recipient = msg.get("to", "")
            subject = msg.get("subject", "")
            body = msg.get("body", "")

            mime = email.mime.text.MIMEText(body)
            mime["Subject"] = subject
            mime["From"] = self.username
            mime["To"] = recipient

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                server.send_message(mime)

            log.info("sent email to %s: %s", recipient, subject)

            sent_path = os.path.join(self.sent_dir, os.path.basename(message_path))
            shutil.move(message_path, sent_path)

        except Exception:
            log.exception("error sending email: %s", message_path)

    def _extract_body(self, parsed):
        """Extract plain text body from a parsed email message."""
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="replace")
            return ""
        else:
            payload = parsed.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="replace")
            return ""
