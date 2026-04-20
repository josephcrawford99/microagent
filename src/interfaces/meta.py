import hmac
import logging
import os
import re
import subprocess
import threading
from typing import Any, Optional

from lib.interface import Interface, Message, Trigger

log = logging.getLogger("microagent.meta")

_COMMAND_RE = re.compile(r"^!(\w+)\s+(\S+)(?:\s+(.*))?$", re.DOTALL)
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class Meta(Interface):
    """Control-plane interface.

    Listens on one or more *child* interfaces (its own instances, separate from
    any agent-facing ones) for authenticated commands:

        !update  <token>                    -> git reset --hard origin/<branch>, restart
        !restart <token>                    -> restart
        !env     <token> KEY=VAL [KEY=VAL]  -> upsert into $REPO_DIR/.env, restart

    Never wakes the agent: trigger_wake() always returns None, tools() returns
    [], so the agent has zero visibility into this channel.
    """

    name = "meta"

    def __init__(
        self,
        sources: dict[str, dict[str, Any]],
        token_env: str = "META_TOKEN",
        branch: str = "main",
    ) -> None:
        # Late import to avoid circular load (interfaces/__init__ imports this module).
        from interfaces import INTERFACES

        self.token = os.environ.get(token_env, "")
        self.branch = branch
        self.repo_dir = os.environ.get("REPO_DIR", "/repo")
        self.children: list[Interface] = []
        for child_name, cfg in sources.items():
            if child_name not in INTERFACES:
                raise RuntimeError(f"meta: unknown source interface '{child_name}'")
            if child_name == "meta":
                raise RuntimeError("meta: cannot wrap itself")
            self.children.append(INTERFACES[child_name](**cfg))

        if not self.token:
            log.warning("meta interface has no token set (%s); commands will be rejected", token_env)

        self._handlers = {
            "update": self._handle_update,
            "restart": self._handle_restart,
            "env": self._handle_env,
        }

    # --- Interface contract ---

    def trigger_wake(self) -> Optional[Trigger]:
        for child in self.children:
            try:
                if child.trigger_wake() is None:
                    continue
                # Child has inbox ready — drain it here, don't let the agent see it.
                # receive() is async; schedule it on a throwaway loop.
                messages = self._drain(child)
            except Exception:
                log.exception("meta: child %s poll failed", child.name)
                continue
            for msg in messages:
                self._dispatch(child, msg)
        return None

    async def receive(self) -> list[Message]:
        return []

    async def send(self, message: Message) -> str:
        del message
        return "meta interface does not send directly"

    def tools(self) -> list:
        return []

    # --- helpers ---

    @staticmethod
    def _run_async(coro):
        """Run an async call from the sync poll path.

        We're invoked inside main's running event loop (via a sync gen
        expression), so `asyncio.run` would error. Spin up a short-lived loop
        on a worker thread instead.
        """
        import asyncio

        result: list = []
        error: list = []

        def _work() -> None:
            loop = asyncio.new_event_loop()
            try:
                result.append(loop.run_until_complete(coro))
            except Exception as e:
                error.append(e)
            finally:
                loop.close()

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join()
        if error:
            raise error[0]
        return result[0]

    def _drain(self, child: Interface) -> list[Message]:
        return self._run_async(child.receive())

    def _dispatch(self, child: Interface, msg: Message) -> None:
        body = (msg.body or "").strip()
        if not body.startswith("!"):
            log.info("meta: dropping non-command message from %s", child.name)
            return

        match = _COMMAND_RE.match(body)
        if not match:
            log.info("meta: malformed command from %s", child.name)
            self._reply(child, msg, "malformed command")
            return

        cmd, token, rest = match.group(1), match.group(2), (match.group(3) or "").strip()

        if not self._auth_ok(token):
            log.warning("meta: unauthorized %s via %s (sender=%s)", cmd, child.name, msg.sender)
            self._reply(child, msg, "unauthorized")
            return

        handler = self._handlers.get(cmd)
        if handler is None:
            self._reply(child, msg, f"unknown command: {cmd}")
            return

        try:
            handler(child, msg, rest)
        except Exception as e:
            log.exception("meta: handler %s failed", cmd)
            self._reply(child, msg, f"{cmd} failed: {e}")

    def _auth_ok(self, token: str) -> bool:
        if not self.token:
            return False
        return hmac.compare_digest(self.token, token)

    def _reply(self, child: Interface, origin: Message, body: str) -> None:
        reply = child.message_class(
            body=body,
            to=origin.sender or "",
            sender=origin.to or "meta",
        )
        # Carry subject through for email replies.
        if hasattr(origin, "subject") and hasattr(reply, "subject"):
            setattr(reply, "subject", f"re: {getattr(origin, 'subject', '')}")
        try:
            self._run_async(child.send(reply))
        except Exception:
            log.exception("meta: reply send via %s failed", child.name)

    # --- handlers ---

    def _handle_restart(self, child: Interface, msg: Message, rest: str) -> None:
        del rest
        self._reply(child, msg, "restarting")
        self._exit_soon()

    def _handle_update(self, child: Interface, msg: Message, rest: str) -> None:
        del rest
        subprocess.run(
            ["git", "-C", self.repo_dir, "fetch", "origin", self.branch],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", self.repo_dir, "reset", "--hard", f"origin/{self.branch}"],
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "-C", self.repo_dir, "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._reply(child, msg, f"updated to {sha}, restarting")
        self._exit_soon()

    def _handle_env(self, child: Interface, msg: Message, rest: str) -> None:
        if not rest:
            self._reply(child, msg, "env: need KEY=VALUE")
            return

        updates: dict[str, str] = {}
        for pair in rest.split():
            if "=" not in pair:
                self._reply(child, msg, f"env: bad pair '{pair}'")
                return
            key, value = pair.split("=", 1)
            if not _ENV_KEY_RE.match(key):
                self._reply(child, msg, f"env: invalid key '{key}'")
                return
            updates[key] = value

        self._upsert_env(updates)
        self._reply(child, msg, f"env updated: {sorted(updates)}, restarting")
        self._exit_soon()

    def _upsert_env(self, updates: dict[str, str]) -> None:
        env_path = os.path.join(self.repo_dir, ".env")
        try:
            with open(env_path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        remaining = dict(updates)
        out_lines: list[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("#") or "=" not in stripped:
                out_lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out_lines.append(f"{key}={remaining.pop(key)}\n")
            else:
                out_lines.append(line)
        for key, value in remaining.items():
            out_lines.append(f"{key}={value}\n")

        tmp_path = env_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.writelines(out_lines)
        os.replace(tmp_path, env_path)

    def _exit_soon(self) -> None:
        # Give reply I/O a moment to flush before pulling the plug.
        # Docker's `restart: unless-stopped` brings us back with fresh code/env.
        def _bye() -> None:
            os._exit(0)

        threading.Timer(0.5, _bye).start()
