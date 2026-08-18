"""Standalone HTTP viewer.
"""

from __future__ import annotations

import hmac
import html
import http.cookies
import json
import logging
import mimetypes
import os
import subprocess
import threading
from collections.abc import Callable
from email.message import Message as Headers
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

from lib import settings as cfg
from lib.handle import Handle, describe_tree, parse_line
from lib.message import ADDRESS, Message
from lib.paths import CONFIG_TOML, REPO_DIR, SPACE_DIR
from lib.plugins import load_provider, load_service
from lib.settings import Config

from .templates import LOGIN_HTML, PAGE_HTML, SPACE_EMPTY_HTML

if TYPE_CHECKING:
    from providers.base_provider import Provider
    from services.base_service import Service

log = logging.getLogger(__name__)

COOKIE_NAME = "dash_token"
CHAT_TAIL_MAX = 64 * 1024  # cap how much web_chat/log a poll will read
CHARSET_TYPES = frozenset({"application/json", "application/javascript"})


class DashboardServer(ThreadingHTTPServer):
    """The HTTP server plus the config and tokens its handlers read."""

    def __init__(self, config: Config, address: tuple[str, int] | None = None) -> None:
        self.config = config
        self.owner_token = os.environ.get("DASHBOARD_TOKEN", "")
        chat_enabled = config.services.get("web_chat", {}).get("enabled")
        self.chat = Handle("web_chat") if chat_enabled else None
        if not self.owner_token:
            log.warning(
                "dashboard has no DASHBOARD_TOKEN set; cloudflare requests will be rejected"
            )
        super().__init__(
            address or (config.dashboard_host, config.dashboard_port), _Handler
        )

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        log.info("dashboard listening on %s:%s", *self.server_address[:2])


def _is_via_cloudflare(headers: Headers) -> bool:
    return "CF-Connecting-IP" in headers  # header lookup is case-insensitive


def _get_cookie(headers: Headers, name: str) -> str:
    raw = headers.get("Cookie", "")
    if not raw:
        return ""
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        return jar[name].value if name in jar else ""
    except Exception:
        return ""


def _resolve_space(url_path: str) -> Path | None:
    """Safe file resolve under space/. Rejects traversal, symlink escape,
    non-files. Directory paths resolve to `index.html` inside."""
    rel = unquote(url_path[len("/space"):]).lstrip("/")
    root = SPACE_DIR.resolve()
    try:
        real = (root / rel).resolve()
    except OSError:
        return None
    if not real.is_relative_to(root):
        return None
    if real.is_dir():
        real = real / "index.html"
    return real if real.is_file() else None


def _git_pull(branch: str = "main") -> str:
    """Make the repo dir byte-identical to origin/<branch>. Destructive: drops
    tracked changes (reset --hard) and wipes every untracked or ignored file
    (clean -fdx) — except the microagent home, the venv, and a repo-root .env,
    which hold everything durable."""
    subprocess.run(
        ["git", "-C", REPO_DIR, "fetch", "origin", branch],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", REPO_DIR, "reset", "--hard", f"origin/{branch}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", REPO_DIR, "clean", "-fdx",
         "-e", ".microagent", "-e", ".venv", "-e", ".env", "-e", ".claude"],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", REPO_DIR, "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _required_env(
    loader: Callable[[str], type[Service] | type[Provider]], name: str
) -> list[str]:
    try:
        return list(loader(name).REQUIRED_ENV)
    except ImportError:
        return []


class _Handler(BaseHTTPRequestHandler):
    server: DashboardServer  # pyright: ignore[reportIncompatibleVariableOverride]  # always constructed by DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        msg = format % args
        if "/api/chat/poll" in msg or "/api/handles" in msg:
            return  # ~1/s per open tab, drowns everything else
        log.info("%s - %s", self.address_string(), msg)

    def _authorized(self) -> bool:
        """LAN requests are trusted; cloudflare requests need the owner token
        (cookie from /login, or a bearer header for the API)."""
        if not _is_via_cloudflare(self.headers):
            return True
        supplied = _get_cookie(self.headers, COOKIE_NAME)
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer "):]
        token = self.server.owner_token
        return bool(token and supplied and hmac.compare_digest(token, supplied))

    # --- response helpers ---

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # --- routing ---

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if not self._authorized():
            if path.startswith("/api/"):
                return self._json(401, {"error": "unauthorized"})
            return self._login_page()
        if path in ("/", "/login"):
            return self._send(200, PAGE_HTML.encode())
        if path == "/api/bootstrap":
            return self._bootstrap()
        if path == "/api/handles":
            return self._json(200, describe_tree())
        if path == "/api/chat/poll":
            return self._chat_poll()
        if path == "/space" or path.startswith("/space/"):
            return self._serve_space(path)
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            return self._login_submit()
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        body = self._read_body()
        if path == "/api/env":
            return self._env_save(body)
        if path == "/api/restart":
            self._json(200, {"ok": True})
            return self._exit_soon()
        if path == "/api/update":
            return self._update()
        if path == "/api/chat/send":
            return self._chat_send(body)
        self._send(404, b"not found", "text/plain")

    # --- route impls ---

    @staticmethod
    def _exit_soon() -> None:
        # Docker's restart policy respawns the process.
        threading.Timer(0.5, os._exit, args=(0,)).start()

    def _env_save(self, body: bytes) -> None:
        try:
            entries = json.loads(body).get("entries", [])
            cfg.write_env({
                k: row.get("value", "")
                for row in entries
                if (k := row.get("key", "").strip())
            })
            self._json(200, {"ok": True})
        except Exception as e:
            log.exception("env save failed")
            self._json(400, {"error": str(e)})

    def _update(self) -> None:
        try:
            sha = _git_pull()
            self._json(200, {"ok": True, "sha": sha})
            self._exit_soon()
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode(errors="replace")
            log.error("update failed: %s", stderr)
            self._json(500, {"error": stderr or str(e)})
        except Exception as e:
            log.exception("update failed")
            self._json(500, {"error": str(e)})

    def _bootstrap(self) -> None:
        env = cfg.read_env()
        config = self.server.config
        services: list[dict[str, Any]] = []
        for name, section in config.services.items():
            required = _required_env(load_service, name)
            services.append({
                "name": name,
                "enabled": bool(section.get("enabled", False)),
                "wake": bool(section.get("wake", True)),
                "required_env": required,
                "missing_env": [k for k in required if not env.get(k)],
            })
        required = _required_env(load_provider, config.provider)
        agent_env = env | {k: v for k, v in os.environ.items() if k in required}
        self._json(200, {
            "env": env,
            "config_toml": CONFIG_TOML.read_text() if CONFIG_TOML.exists() else "",
            "services": services,
            "agent": {
                "agent_id": config.agent_id,
                "provider": config.provider,
                "required_env": required,
                "missing_env": [k for k in required if not agent_env.get(k)],
            },
        })

    # --- chat over the web_chat handles ---

    def _chat_poll(self) -> None:
        """Tail web_chat/log from a byte offset. Lines with `from` are user
        messages; lines with `address` are agent replies."""
        chat = self.server.chat
        if chat is None:
            return self._json(200, {"messages": [], "offset": 0})
        try:
            after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        messages: list[dict[str, Any]] = []
        offset = after
        try:
            if chat.log_path.stat().st_size < after:
                after = 0  # log was truncated/recreated; start over
            with chat.log_path.open("rb") as f:
                f.seek(after)
                data = f.read(CHAT_TAIL_MAX)
            offset = after + len(data)
            for line in data.splitlines():
                if not line.strip():
                    continue
                m = parse_line(line)
                messages.append({
                    "role": "agent" if ADDRESS in m else "user",
                    "body": m.get("body", ""),
                    "ts": m.get("ts", ""),
                })
        except FileNotFoundError:
            offset = 0
        self._json(200, {"messages": messages, "offset": offset})

    def _chat_send(self, body: bytes) -> None:
        """Play the remote user: write the line to web_chat/out, mirrored to
        the log, exactly like an external echo would."""
        chat = self.server.chat
        if chat is None:
            return self._json(400, {"error": "web_chat service not enabled"})
        try:
            text = str(json.loads(body).get("body", "")).strip()
            if text:
                chat.write("out", Message("web_chat", "web", text))
            self._json(200, {"ok": True})
        except Exception as e:
            self._json(400, {"error": str(e)})

    def _serve_space(self, path: str) -> None:
        if path == "/space":
            path = "/space/"
        resolved = _resolve_space(path)
        if resolved is None:
            if path == "/space/":
                return self._send(200, SPACE_EMPTY_HTML.encode())
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in CHARSET_TYPES:
            ctype += "; charset=utf-8"
        try:
            body = resolved.read_bytes()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        self._send(200, body, ctype)

    # --- login page ---

    def _login_page(self, error: str = "") -> None:
        self._send(200, LOGIN_HTML.replace("{{error}}", html.escape(error)).encode())

    def _login_submit(self) -> None:
        supplied = (parse_qs(self._read_body().decode()).get("token") or [""])[0]
        token = self.server.owner_token
        if token and hmac.compare_digest(token, supplied):
            cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict"
            if _is_via_cloudflare(self.headers):
                cookie += "; Secure"
            return self._send(
                302, b"", headers={"Location": "/", "Set-Cookie": cookie}
            )
        self._login_page(error="invalid token")
