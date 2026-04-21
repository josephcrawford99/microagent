import hmac
import html
import http.cookies
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from lib.interface import Interface, Message, Trigger

log = logging.getLogger("microagent.dashboard")

ENV_PATH = "/repo/.env"
CONFIG_PATH = "/repo/soul/config.json"
COOKIE_NAME = "dash_token"
# Keys matching any of these substrings render as masked •••• in UI payloads.
SECRET_HINTS = ("TOKEN", "PASSWORD", "SECRET", "KEY", "API")


class Dashboard(Interface):
    """HTTP control panel for editing .env and soul/config.json at runtime.

    Auth model: requests arriving through Cloudflare Tunnel carry CF-Connecting-IP;
    those require a bearer token / cookie matching DASHBOARD_TOKEN. Direct LAN hits
    (no CF headers) are trusted — bind the port to the local network only. `/demo`
    is always public and renders mock data for portfolio viewers.

    Never wakes the agent.
    """

    name = "dashboard"

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8767,
        token_env: str = "DASHBOARD_TOKEN",
        public_url: str = "",
    ) -> None:
        self.host = host
        self.port = port
        self.token = os.environ.get(token_env, "")
        self.public_url = public_url
        if not self.token:
            log.warning(
                "dashboard has no token set (%s); public edits will be rejected",
                token_env,
            )
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()

    # --- Interface contract ---

    def trigger_wake(self) -> Optional[Trigger]:
        return None

    async def receive(self) -> list[Message]:
        return []

    async def send(self, message: Message) -> str:
        del message
        return "dashboard does not send"

    def tools(self) -> list:
        return []

    # --- server ---

    def _serve(self) -> None:
        dash = self
        handler = _make_handler(dash)
        srv = ThreadingHTTPServer((self.host, self.port), handler)
        log.info("dashboard listening on %s:%s", self.host, self.port)
        srv.serve_forever()


def _is_via_cloudflare(headers) -> bool:
    return any(h in headers for h in ("CF-Connecting-IP", "Cf-Connecting-Ip", "cf-connecting-ip"))


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


def _mask(key: str, value: str) -> str:
    if any(h in key.upper() for h in SECRET_HINTS):
        return "••••" if value else ""
    return value


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(ENV_PATH) as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def _write_env(new: dict[str, str]) -> None:
    try:
        with open(ENV_PATH) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(new)
    out: list[str] = []
    for line in lines:
        s = line.lstrip()
        if s.startswith("#") or "=" not in s:
            out.append(line)
            continue
        k = s.split("=", 1)[0].strip()
        if k in remaining:
            out.append(f"{k}={remaining.pop(k)}\n")
        # Keys omitted from `new` are dropped (user removed them in UI).
    for k, v in remaining.items():
        out.append(f"{k}={v}\n")
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.writelines(out)
    os.replace(tmp, ENV_PATH)


def _read_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _write_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def _exit_soon() -> None:
    def _bye() -> None:
        os._exit(0)

    threading.Timer(0.5, _bye).start()


MOCK_ENV = {
    "OPENAI_API_KEY": "sk-demo-••••",
    "EMAIL_PASSWORD": "••••",
    "META_TOKEN": "••••",
    "DASHBOARD_TOKEN": "••••",
}

MOCK_CONFIG = {
    "user": {"name": "Demo User"},
    "interfaces": {
        "email": {"enabled": True, "allowed_senders": ["you@example.com"]},
        "imessage": {"enabled": False, "allowed_senders": []},
    },
}


def _make_handler(dash: "Dashboard"):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s - %s", self.address_string(), fmt % args)

        # --- helpers ---

        def _authed(self) -> bool:
            if not _is_via_cloudflare(self.headers):
                return True  # local network
            if not dash.token:
                return False
            supplied = _get_cookie(self.headers, COOKIE_NAME)
            if not supplied:
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    supplied = auth[len("Bearer ") :]
            return bool(supplied) and hmac.compare_digest(dash.token, supplied)

        def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: Optional[list[tuple[str, str]]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in extra or []:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json")

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        # --- routing ---

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/demo":
                self._render(MOCK_ENV, MOCK_CONFIG, demo=True)
                return
            if path == "/healthz":
                self._json(200, {"ok": True})
                return
            if path in ("/", "/login"):
                if not self._authed():
                    self._login_page()
                    return
                env = _read_env()
                self._render({k: _mask(k, v) for k, v in env.items()}, _read_config(), demo=False)
                return
            if path == "/api/config":
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                self._json(200, _read_config())
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/login":
                self._login_submit()
                return
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            body = self._read_body()
            if path == "/api/env":
                try:
                    payload = json.loads(body)
                    entries = payload.get("entries", [])
                    existing = _read_env()
                    new: dict[str, str] = {}
                    for row in entries:
                        k, v = row.get("key", "").strip(), row.get("value", "")
                        if not k:
                            continue
                        # Keep existing value when UI sends the mask sentinel.
                        if v == "••••" and k in existing:
                            v = existing[k]
                        new[k] = v
                    _write_env(new)
                    self._json(200, {"ok": True})
                except Exception as e:
                    log.exception("env save failed")
                    self._json(400, {"error": str(e)})
                return
            if path == "/api/config":
                try:
                    cfg = json.loads(body)
                    _write_config(cfg)
                    self._json(200, {"ok": True})
                except Exception as e:
                    log.exception("config save failed")
                    self._json(400, {"error": str(e)})
                return
            if path == "/api/restart":
                self._json(200, {"ok": True})
                _exit_soon()
                return
            self._send(404, b"not found", "text/plain")

        def _login_page(self, error: str = "") -> None:
            body = _LOGIN_HTML.replace("{{error}}", html.escape(error)).encode()
            self._send(200, body)

        def _login_submit(self) -> None:
            body = self._read_body().decode()
            fields = parse_qs(body)
            supplied = (fields.get("token") or [""])[0]
            if dash.token and hmac.compare_digest(dash.token, supplied):
                cookie = f"{COOKIE_NAME}={supplied}; Path=/; HttpOnly; SameSite=Strict"
                if _is_via_cloudflare(self.headers):
                    cookie += "; Secure"
                self._send(
                    302,
                    b"",
                    extra=[("Location", "/"), ("Set-Cookie", cookie)],
                )
                return
            self._login_page(error="invalid token")

        def _render(self, env: dict[str, str], cfg: dict, demo: bool) -> None:
            env_rows = "".join(
                f'<tr><td><input name="k" value="{html.escape(k)}"></td>'
                f'<td><input name="v" value="{html.escape(v)}"></td>'
                f'<td><button type="button" onclick="this.closest(\'tr\').remove()">×</button></td></tr>'
                for k, v in sorted(env.items())
            )
            page = (
                _PAGE_HTML
                .replace("{{banner}}", _DEMO_BANNER if demo else "")
                .replace("{{env_rows}}", env_rows)
                .replace("{{config_json}}", html.escape(json.dumps(cfg, indent=2)))
                .replace("{{readonly}}", "disabled" if demo else "")
                .replace("{{public_url}}", html.escape(dash.public_url or ""))
            )
            self._send(200, page.encode())

    return Handler


_LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>microagent dashboard</title>
<style>body{font-family:system-ui;max-width:380px;margin:10vh auto;padding:1rem}
input{width:100%;padding:.6rem;font-size:1rem;margin:.5rem 0}
button{padding:.6rem 1rem;font-size:1rem;cursor:pointer}
.err{color:#b00}</style></head>
<body><h2>microagent</h2>
<p>Access token required. <a href="/demo">see demo →</a></p>
<form method="post" action="/login">
<input type="password" name="token" placeholder="DASHBOARD_TOKEN" autofocus>
<button>enter</button>
<div class="err">{{error}}</div>
</form></body></html>
"""

_DEMO_BANNER = """<div style="background:#ffd;padding:.6rem 1rem;border-bottom:1px solid #cc9">
<b>Demo mode</b> — all values are mocked. Real dashboard requires auth.
</div>"""

_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>microagent dashboard</title>
<style>
body{font-family:system-ui;max-width:860px;margin:0 auto;padding:1rem}
h1{margin-top:0}
section{border:1px solid #ddd;border-radius:6px;padding:1rem;margin:1rem 0}
table{width:100%;border-collapse:collapse}
td{padding:.25rem}
input[type=text],input:not([type]){width:100%;padding:.4rem;font-family:ui-monospace,monospace}
textarea{width:100%;height:24rem;font-family:ui-monospace,monospace;font-size:.85rem}
button{padding:.5rem 1rem;cursor:pointer;margin-right:.5rem}
.row{display:flex;gap:.5rem;align-items:center}
.status{color:#666;font-size:.9rem}
</style></head>
<body>
{{banner}}
<h1>microagent</h1>
<p class="status">Control panel for secrets and soul config. Changes take effect after restart.
{{public_url}}</p>

<section>
<h2>Environment (.env)</h2>
<table id="env"><tbody>{{env_rows}}</tbody></table>
<div class="row" style="margin-top:.5rem">
<button type="button" onclick="addRow()" {{readonly}}>+ add</button>
<button type="button" onclick="saveEnv()" {{readonly}}>save</button>
<span id="env-status" class="status"></span>
</div>
</section>

<section>
<h2>Soul config (soul/config.json)</h2>
<textarea id="config" {{readonly}}>{{config_json}}</textarea>
<div class="row" style="margin-top:.5rem">
<button type="button" onclick="saveConfig()" {{readonly}}>save</button>
<span id="config-status" class="status"></span>
</div>
</section>

<section>
<h2>Process</h2>
<button type="button" onclick="restart()" {{readonly}}>restart agent</button>
<span class="status">docker restarts the container automatically</span>
</section>

<script>
function addRow(){
  const tr=document.createElement('tr');
  tr.innerHTML='<td><input name="k" value=""></td><td><input name="v" value=""></td>'+
               '<td><button type="button" onclick="this.closest(\\'tr\\').remove()">×</button></td>';
  document.querySelector('#env tbody').appendChild(tr);
}
async function saveEnv(){
  const rows=[...document.querySelectorAll('#env tbody tr')];
  const entries=rows.map(r=>{const i=r.querySelectorAll('input');return{key:i[0].value,value:i[1].value}});
  const s=document.getElementById('env-status'); s.textContent='saving…';
  const r=await fetch('/api/env',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries})});
  s.textContent=r.ok?'saved':'error: '+await r.text();
}
async function saveConfig(){
  const s=document.getElementById('config-status'); s.textContent='saving…';
  try{
    const cfg=JSON.parse(document.getElementById('config').value);
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    s.textContent=r.ok?'saved':'error: '+await r.text();
  }catch(e){ s.textContent='invalid JSON: '+e.message; }
}
async function restart(){
  if(!confirm('restart now?'))return;
  await fetch('/api/restart',{method:'POST'});
  alert('restarting…');
}
</script>
</body></html>
"""
