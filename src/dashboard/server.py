"""Standalone HTTP control panel.

Separate from the Interface abstraction — the dashboard is not a channel the
agent talks through. It delegates all config I/O to `lib.settings` (imported
as `cfg`), surfaces agent usage stats, and proxies chat to the WebChat
interface when enabled.

Auth model:
  - Direct LAN hits (no CF-Connecting-IP header) are trusted as "owner".
  - Requests via Cloudflare Tunnel require a token. The cookie / bearer value
    is compared against two configured tokens to derive a role:
      DASHBOARD_TOKEN          → role=owner (full read/write)
      DASHBOARD_DEMO_TOKEN     → role=demo  (empty reads, writes are no-ops)
"""

from __future__ import annotations

import hmac
import html
import http.cookies
import json
import logging
import mimetypes
import os
import posixpath
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from lib import settings as cfg
from lib.settings import Settings

from .templates import LOGIN_HTML, PAGE_HTML

log = logging.getLogger("microagent.dashboard")

COOKIE_NAME = "dash_token"
SPACE_DIR = "/space"


class DashboardServer:
    """Owns the HTTP server thread. Not an Interface."""

    def __init__(
        self,
        settings: Settings,
        agent,  # AgentType — avoids a cycle-y import
        web_chat=None,  # Optional[WebChat]
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.web_chat = web_chat
        self.owner_token = settings.dashboard_token.get_secret_value()
        self.demo_token = settings.dashboard_demo_token.get_secret_value()
        self.public_url = settings.dashboard.public_url
        if not self.owner_token:
            log.warning(
                "dashboard has no DASHBOARD_TOKEN set; cloudflare requests will be rejected"
            )

    def start(self) -> None:
        host = self.settings.dashboard.host
        port = self.settings.dashboard.port
        srv = _DashboardHTTPServer((host, port), _Handler)
        srv.dashboard = self  # type: ignore[attr-defined]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log.info("dashboard listening on %s:%s", host, port)


class _DashboardHTTPServer(ThreadingHTTPServer):
    dashboard: DashboardServer  # set right after construction


# --- auth helpers ----------------------------------------------------------


def _is_via_cloudflare(headers) -> bool:
    return any(
        h in headers
        for h in ("CF-Connecting-IP", "Cf-Connecting-Ip", "cf-connecting-ip")
    )


def _get_cookie(headers, name: str) -> str:
    raw = headers.get("Cookie", "")
    if not raw:
        return ""
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        return jar[name].value if name in jar else ""
    except Exception:
        return ""


def _resolve_space(url_path: str) -> Optional[str]:
    """Safe file resolve under SPACE_DIR. Rejects traversal, symlink escape,
    non-files. Directory paths resolve to `index.html` inside."""
    rel = unquote(url_path[len("/space"):]).lstrip("/")
    rel = posixpath.normpath(rel) if rel else ""
    if rel.startswith("..") or rel.startswith("/"):
        return None
    if rel == ".":
        rel = ""
    candidate = os.path.join(SPACE_DIR, rel) if rel else SPACE_DIR
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return None
    root = os.path.realpath(SPACE_DIR)
    if real != root and not real.startswith(root + os.sep):
        return None
    if os.path.isdir(real):
        real = os.path.join(real, "index.html")
    if not os.path.isfile(real):
        return None
    return real


_SPACE_EMPTY_HTML = """<!doctype html><meta charset="utf-8">
<title>agent space</title>
<style>body{font-family:system-ui;max-width:36rem;margin:4rem auto;padding:1rem;color:#555}</style>
<h2>empty</h2>
<p>The agent hasn't written anything here yet. Ask it to — this is its space to fill.</p>
<p style="color:#888;font-size:.9rem">Path: <code>/space/index.html</code></p>
""".encode("utf-8")


def _exit_soon() -> None:
    def _bye() -> None:
        os._exit(0)

    threading.Timer(0.5, _bye).start()


def _git_pull(branch: str = "main") -> str:
    """Make /repo byte-identical to origin/<branch>. Destructive: drops
    tracked changes (reset --hard) and wipes every untracked or ignored
    file (clean -fdx), including `__pycache__/`, `.env`, `.DS_Store`, and
    any stray files the agent may have written. The canonical .env lives
    in /config/ and is never touched."""
    subprocess.run(
        ["git", "-C", "/repo", "fetch", "origin", branch],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", "/repo", "reset", "--hard", f"origin/{branch}"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", "/repo", "clean", "-fdx"],
        check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", "/repo", "rev-parse", "--short", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# --- request handler -------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server: _DashboardHTTPServer  # for type narrowing

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/chat/poll" in msg:
            return  # ~1/s per open tab, drowns everything else
        log.info("%s - %s", self.address_string(), msg)

    @property
    def dash(self) -> DashboardServer:
        return self.server.dashboard

    # --- auth ---

    def _role(self) -> Optional[str]:
        """Return "owner" | "demo" | None (unauth)."""
        if not _is_via_cloudflare(self.headers):
            return "owner"  # LAN trusted
        supplied = _get_cookie(self.headers, COOKIE_NAME)
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[len("Bearer ") :]
        if not supplied:
            return None
        if self.dash.owner_token and hmac.compare_digest(
            self.dash.owner_token, supplied
        ):
            return "owner"
        if self.dash.demo_token and hmac.compare_digest(
            self.dash.demo_token, supplied
        ):
            return "demo"
        return None

    # --- response helpers ---

    def _send(
        self,
        status: int,
        body: bytes,
        ctype: str = "text/html; charset=utf-8",
        extra: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Dashboard HTML/JSON is dynamic by nature; cached copies after a
        # server rebuild leave tabs out of sync with the running process.
        self.send_header("Cache-Control", "no-store")
        for k, v in extra or []:
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
            self._json(200, {"ok": True})
            return
        role = self._role()
        if path in ("/", "/login"):
            if role is None:
                self._login_page()
                return
            self._send(200, PAGE_HTML.encode())
            return
        if role is None:
            if path.startswith("/api/"):
                self._json(401, {"error": "unauthorized"})
            else:
                self._login_page()
            return
        if path == "/api/bootstrap":
            self._bootstrap(role)
            return
        if path == "/api/chat/poll":
            self._chat_poll(role)
            return
        if path == "/api/usage":
            self._json(200, {} if role == "demo" else self.dash.agent.get_usage())
            return
        if path == "/space" or path.startswith("/space/"):
            self._serve_space(path)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self._login_submit()
            return
        role = self._role()
        if role is None:
            self._json(401, {"error": "unauthorized"})
            return
        if role == "demo":
            # Every write endpoint is a no-op in demo mode.
            self._json(200, {"ok": True, "demo": True})
            return

        body = self._read_body()
        if path == "/api/env":
            try:
                payload = json.loads(body)
                new: dict[str, str] = {}
                for row in payload.get("entries", []):
                    k = row.get("key", "").strip()
                    if not k:
                        continue
                    new[k] = row.get("value", "")
                cfg.write_env(new)
                self._json(200, {"ok": True})
            except Exception as e:
                log.exception("env save failed")
                self._json(400, {"error": str(e)})
            return
        if path == "/api/config":
            try:
                cfg.write_toml_text(body.decode("utf-8"))
                self._json(200, {"ok": True})
            except Exception as e:
                log.exception("config save failed")
                self._json(400, {"error": str(e)})
            return
        if path == "/api/interface/toggle":
            try:
                payload = json.loads(body)
                cfg.toggle(payload.get("name", ""), bool(payload.get("enabled", False)))
                self._json(200, {"ok": True})
            except Exception as e:
                log.exception("interface toggle failed")
                self._json(400, {"error": str(e)})
            return
        if path == "/api/source/wake_toggle":
            try:
                payload = json.loads(body)
                cfg.set_wake(payload.get("name", ""), bool(payload.get("enabled", False)))
                self._json(200, {"ok": True})
            except Exception as e:
                log.exception("source wake toggle failed")
                self._json(400, {"error": str(e)})
            return
        if path == "/api/interface/field":
            try:
                payload = json.loads(body)
                coerced = cfg.set_field(
                    payload.get("name", ""),
                    payload.get("field", ""),
                    payload.get("value", []),
                )
                self._json(200, {"ok": True, "value": coerced})
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                log.exception("editable field write failed")
                self._json(400, {"error": str(e)})
            return
        if path == "/api/restart":
            self._json(200, {"ok": True})
            _exit_soon()
            return
        if path == "/api/update":
            try:
                sha = _git_pull()
                self._json(200, {"ok": True, "sha": sha})
                _exit_soon()
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode(errors="replace")
                log.error("update failed: %s", stderr)
                self._json(500, {"error": stderr or str(e)})
            except Exception as e:
                log.exception("update failed")
                self._json(500, {"error": str(e)})
            return
        if path == "/api/chat/send":
            try:
                payload = json.loads(body)
                if self.dash.web_chat is None:
                    self._json(400, {"error": "web_chat interface not enabled"})
                    return
                self.dash.web_chat.submit(payload.get("body", ""))
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(400, {"error": str(e)})
            return
        self._send(404, b"not found", "text/plain")

    # --- route impls ---

    def _bootstrap(self, role: str) -> None:
        if role == "demo":
            self._json(200, {
                "role": "demo",
                "env": {},
                "config_toml": "",
                "interfaces": [],
                "usage": {},
                "public_url": self.dash.public_url,
            })
            return
        self._json(200, {
            "role": "owner",
            "env": cfg.read_env(),
            "config_toml": cfg.read_toml_text(),
            "interfaces": cfg.inputs_status(),
            "usage": self.dash.agent.get_usage(),
            "public_url": self.dash.public_url,
        })

    def _chat_poll(self, role: str) -> None:
        if role == "demo" or self.dash.web_chat is None:
            self._json(200, {"messages": [], "latest": 0, "pending": {"note": None, "id": 0}})
            return
        try:
            after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
        except ValueError:
            after = 0
        self._json(200, self.dash.web_chat.get_log(after))

    def _serve_space(self, path: str) -> None:
        if path == "/space":
            self._send(302, b"", extra=[("Location", "/space/")])
            return
        if path == "/space/" and not os.path.isfile(
            os.path.join(SPACE_DIR, "index.html")
        ):
            self._send(200, _SPACE_EMPTY_HTML)
            return
        resolved = _resolve_space(path)
        if not resolved:
            self._send(404, b"not found", "text/plain")
            return
        ctype, _ = mimetypes.guess_type(resolved)
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/json",
            "application/javascript",
        ):
            ctype += "; charset=utf-8"
        try:
            body = Path(resolved).read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, body, ctype)

    # --- login page ---

    def _login_page(self, error: str = "") -> None:
        demo_link = (
            '<p class="demo">Or <a href="/login?demo=1">view the demo →</a></p>'
            if self.dash.demo_token
            else ""
        )
        body = (
            LOGIN_HTML
            .replace("{{error}}", html.escape(error))
            .replace("{{demo_link}}", demo_link)
            .encode()
        )
        # /login?demo=1 — a GET that auto-sets the demo cookie and redirects.
        if urlparse(self.path).query == "demo=1" and self.dash.demo_token:
            self._set_cookie_and_redirect(self.dash.demo_token)
            return
        self._send(200, body)

    def _login_submit(self) -> None:
        body = self._read_body().decode()
        fields = parse_qs(body)
        supplied = (fields.get("token") or [""])[0]
        tokens = [
            t for t in (self.dash.owner_token, self.dash.demo_token) if t
        ]
        if any(hmac.compare_digest(t, supplied) for t in tokens):
            self._set_cookie_and_redirect(supplied)
            return
        self._login_page(error="invalid token")

    def _set_cookie_and_redirect(self, token: str) -> None:
        cookie = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict"
        if _is_via_cloudflare(self.headers):
            cookie += "; Secure"
        self._send(302, b"", extra=[("Location", "/"), ("Set-Cookie", cookie)])
