import hmac
import html
import http.cookies
import json
import logging
import mimetypes
import os
import posixpath
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from lib.interface import Interface, Message, Trigger

log = logging.getLogger("microagent.dashboard")

ENV_PATH = "/repo/.env"
CONFIG_PATH = "/repo/soul/config.json"
SPACE_DIR = "/data/space"
COOKIE_NAME = "dash_token"
# Keys matching any of these substrings get rendered as type=password inputs
# (hidden by default, revealable with the "show values" toggle).
SECRET_HINTS = ("TOKEN", "PASSWORD", "SECRET", "KEY", "API")


class Dashboard(Interface):
    """HTTP control panel for editing .env and soul/config.json at runtime.

    Auth model: requests arriving through Cloudflare Tunnel carry CF-Connecting-IP;
    those require a bearer token / cookie matching DASHBOARD_TOKEN. Direct LAN hits
    (no CF headers) are trusted — bind the port to the local network only. `/demo`
    is always public and renders mock data for portfolio viewers.

    Also a full chat interface: user messages typed in the UI land in an inbox
    that wakes the agent; agent replies via dashboard_send and surface in the UI.
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
        # Agent-facing inbox (drained by receive()).
        self._inbox: "queue.Queue[Message]" = queue.Queue()
        # UI-facing chat log (user + agent messages, browser polls for updates).
        self._chat_lock = threading.Lock()
        self._chat_log: list[dict] = []
        self._chat_next_id = 1
        # Transient "agent is thinking / using X" indicator. Cleared when the
        # next real send() lands. Monotonic pending_id lets the UI detect
        # clears even if the text happens to repeat.
        self._pending: Optional[str] = None
        self._pending_id = 0
        threading.Thread(target=self._serve, daemon=True).start()

    # --- Interface contract ---

    def trigger_wake(self) -> Optional[Trigger]:
        if self._inbox.empty():
            return None
        return Trigger(interface=self)

    async def receive(self) -> list[Message]:
        out: list[Message] = []
        while True:
            try:
                out.append(self._inbox.get_nowait())
            except queue.Empty:
                break
        return out

    async def send(self, message: Message) -> str:
        with self._chat_lock:
            self._pending = None
            self._pending_id += 1
        self._chat_append("agent", message.body or "")
        return "delivered to dashboard"

    async def indicate_pending(self, note: str) -> None:
        with self._chat_lock:
            self._pending = note
            self._pending_id += 1

    # --- chat plumbing ---

    def _chat_append(self, role: str, body: str) -> None:
        with self._chat_lock:
            self._chat_log.append({
                "id": self._chat_next_id,
                "ts": time.time(),
                "role": role,
                "body": body,
            })
            self._chat_next_id += 1
            if len(self._chat_log) > 500:
                self._chat_log = self._chat_log[-500:]

    def chat_send(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._chat_append("user", text)
        self._inbox.put(Message(body=text, sender="dashboard", to="agent"))

    def chat_poll(self, after: int) -> dict:
        with self._chat_lock:
            msgs = [m for m in self._chat_log if m["id"] > after]
            latest = self._chat_next_id - 1
            pending = {"note": self._pending, "id": self._pending_id}
        return {"messages": msgs, "latest": latest, "pending": pending}

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


def _is_secret(key: str) -> bool:
    return any(h in key.upper() for h in SECRET_HINTS)


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


def _resolve_space(url_path: str) -> Optional[str]:
    """Resolve a /space/... URL to an absolute path under SPACE_DIR, or None if unsafe/missing.

    Rejects traversal, symlinks escaping the root, and non-files. A directory
    resolves to its index.html.
    """
    rel = unquote(url_path[len("/space"):]).lstrip("/")
    # Normalize, then guard against absolute paths and parent segments.
    rel = posixpath.normpath(rel) if rel else ""
    if rel.startswith("..") or rel.startswith("/") or rel == ".":
        rel = "" if rel in (".", "") else None
        if rel is None:
            return None
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


_SPACE_EMPTY_HTML = b"""<!doctype html><meta charset="utf-8">
<title>agent space</title>
<style>body{font-family:system-ui;max-width:36rem;margin:4rem auto;padding:1rem;color:#555}</style>
<h2>empty</h2>
<p>The agent hasn't written anything here yet. Ask it to - this is its space to fill.</p>
<p style="color:#888;font-size:.9rem">Path: <code>/data/space/index.html</code></p>
"""


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
            msg = fmt % args
            # Chat poll fires every ~1s per open tab — logging it buries everything else.
            if "/api/chat/poll" in msg:
                return
            log.info("%s - %s", self.address_string(), msg)

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
                self._render(_read_env(), _read_config(), demo=False)
                return
            if path == "/api/config":
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                self._json(200, _read_config())
                return
            if path == "/space" or path.startswith("/space/"):
                if not self._authed():
                    self._login_page()
                    return
                # Bare /space -> canonical /space/ so relative links resolve.
                if path == "/space":
                    self._send(302, b"", extra=[("Location", "/space/")])
                    return
                # Root with no index yet: show placeholder instead of 404.
                if path == "/space/" and not os.path.isfile(os.path.join(SPACE_DIR, "index.html")):
                    self._send(200, _SPACE_EMPTY_HTML)
                    return
                resolved = _resolve_space(path)
                if not resolved:
                    self._send(404, b"not found", "text/plain")
                    return
                ctype, _ = mimetypes.guess_type(resolved)
                ctype = ctype or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
                    ctype += "; charset=utf-8"
                try:
                    with open(resolved, "rb") as f:
                        body = f.read()
                except OSError:
                    self._send(404, b"not found", "text/plain")
                    return
                self._send(200, body, ctype)
                return
            if path == "/api/chat/poll":
                if not self._authed():
                    self._json(401, {"error": "unauthorized"})
                    return
                try:
                    after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
                except ValueError:
                    after = 0
                self._json(200, dash.chat_poll(after))
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
                    new: dict[str, str] = {}
                    for row in entries:
                        k, v = row.get("key", "").strip(), row.get("value", "")
                        if not k:
                            continue
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
            if path == "/api/chat/send":
                try:
                    payload = json.loads(body)
                    dash.chat_send(payload.get("body", ""))
                    self._json(200, {"ok": True})
                except Exception as e:
                    self._json(400, {"error": str(e)})
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
            def _row(k: str, v: str) -> str:
                vtype = "password" if _is_secret(k) else "text"
                secret_cls = " secret" if _is_secret(k) else ""
                return (
                    f'<div class="env-row{secret_cls}">'
                    f'<input class="k" name="k" value="{html.escape(k)}">'
                    f'<input class="v" name="v" type="{vtype}" value="{html.escape(v)}">'
                    f'<button type="button" class="del" onclick="softDelete(this)">delete</button>'
                    f'<button type="button" class="undo" onclick="undo(this)" hidden>undo</button>'
                    f'</div>'
                )

            env_rows = "".join(_row(k, v) for k, v in sorted(env.items()))
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
#env{display:flex;flex-direction:column;gap:.5rem}
.env-row{display:flex;gap:.6rem;align-items:center}
.env-row input{padding:.45rem;font-family:ui-monospace,monospace;border:1px solid #ccc;border-radius:4px;min-width:0}
.env-row input.k{flex:0 0 16rem}
.env-row input.v{flex:1 1 auto}
.env-row.deleted input{text-decoration:line-through;opacity:.4;background:#fee}
.del{color:#a00;background:none;border:1px solid #ddd;border-radius:4px;padding:.25rem .5rem;font-size:.85rem}
.del:hover{background:#fee;border-color:#a00}
.undo{background:#ffd;border:1px solid #cc9;border-radius:4px;padding:.25rem .5rem;font-size:.85rem}
textarea{width:100%;height:24rem;font-family:ui-monospace,monospace;font-size:.85rem}
button{padding:.5rem 1rem;cursor:pointer}
.row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.status{color:#666;font-size:.9rem}
</style></head>
<body>
{{banner}}
<h1>microagent</h1>
<p class="status">Control panel for secrets and soul config. Changes take effect after restart.
{{public_url}}</p>

<section>
<h2>Environment (.env)</h2>
<div id="env">{{env_rows}}</div>
<div class="row" style="margin-top:.5rem">
<button type="button" onclick="addRow()" {{readonly}}>+ add</button>
<button type="button" onclick="saveEnv()" {{readonly}}>save</button>
<button type="button" onclick="toggleReveal()">show values</button>
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
<h2>Chat</h2>
<div id="chat-log" style="border:1px solid #ddd;border-radius:4px;padding:.75rem;height:18rem;overflow-y:auto;background:#fafafa;font-family:ui-monospace,monospace;font-size:.9rem;margin-bottom:.6rem"></div>
<div id="chat-pending" style="font-family:ui-monospace,monospace;font-size:.85rem;color:#888;font-style:italic;min-height:1.2rem;margin:-.3rem 0 .4rem .1rem"></div>
<div class="row">
<input id="chat-input" type="text" placeholder="say something to the agent…" style="flex:1 1 auto;padding:.5rem;border:1px solid #ccc;border-radius:4px" {{readonly}}>
<button type="button" onclick="sendChat()" {{readonly}}>send</button>
</div>
</section>

<section>
<h2>Agent Space</h2>
<p class="status" style="margin:.2rem 0 .6rem">A corner the agent owns. It can write any HTML / linked pages here and check its own work.</p>
<iframe id="space-frame" src="/space/" style="width:100%;height:22rem;border:1px solid #ddd;border-radius:4px;background:#fff" sandbox="allow-same-origin allow-top-navigation-by-user-activation"></iframe>
<div class="row" style="margin-top:.4rem">
<a href="/space/" target="_blank" rel="noopener">open in new tab →</a>
<button type="button" onclick="reloadSpace()">reload</button>
</div>
</section>

<section>
<h2>Process</h2>
<button type="button" onclick="restart()" {{readonly}}>restart agent</button>
<span class="status">docker restarts the container automatically</span>
</section>

<script>
function addRow(){
  const row=document.createElement('div');
  row.className='env-row';
  row.innerHTML='<input class="k" name="k" value=""><input class="v" name="v" value="">'+
               '<button type="button" class="del" onclick="this.closest(\\'.env-row\\').remove()">×</button>';
  document.getElementById('env').appendChild(row);
}
function softDelete(btn){
  const row=btn.closest('.env-row');
  row.classList.add('deleted');
  row.querySelectorAll('input').forEach(i=>i.disabled=true);
  row.querySelector('.del').hidden=true;
  row.querySelector('.undo').hidden=false;
}
let _revealed=false;
function toggleReveal(){
  _revealed=!_revealed;
  document.querySelectorAll('#env .env-row.secret input.v').forEach(i=>{
    i.type=_revealed?'text':'password';
  });
  event.target.textContent=_revealed?'hide values':'show values';
}
function undo(btn){
  const row=btn.closest('.env-row');
  row.classList.remove('deleted');
  row.querySelectorAll('input').forEach(i=>i.disabled=false);
  row.querySelector('.del').hidden=false;
  row.querySelector('.undo').hidden=true;
}
async function saveEnv(){
  const rows=[...document.querySelectorAll('#env .env-row:not(.deleted)')];
  const entries=rows.map(r=>({key:r.querySelector('.k').value,value:r.querySelector('.v').value}));
  const removed=document.querySelectorAll('#env .env-row.deleted').length;
  if(removed>0 && !confirm(`Delete ${removed} entr${removed===1?'y':'ies'}? This removes them from .env.`))return;
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
function reloadSpace(){
  const f=document.getElementById('space-frame');
  if(f) f.src=f.src;
}
async function restart(){
  if(!confirm('restart now?'))return;
  await fetch('/api/restart',{method:'POST'});
  alert('restarting…');
}

let _chatAfter=0;
const _roleColors={user:'#036',agent:'#060',system:'#a60'};
function renderChat(msgs){
  const log=document.getElementById('chat-log');
  if(!log)return;
  const stick=log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  for(const m of msgs){
    const d=document.createElement('div');
    d.style.marginBottom='.4rem';
    const who=document.createElement('b');
    who.textContent=m.role+': ';
    who.style.color=_roleColors[m.role]||'#333';
    d.appendChild(who);
    d.appendChild(document.createTextNode(m.body));
    log.appendChild(d);
  }
  if(stick) log.scrollTop=log.scrollHeight;
}
function renderPending(p){
  const el=document.getElementById('chat-pending');
  if(!el)return;
  el.textContent=p && p.note ? 'agent is '+p.note+'…' : '';
}
async function pollChat(){
  try{
    const r=await fetch('/api/chat/poll?after='+_chatAfter);
    if(r.ok){
      const d=await r.json();
      if(d.messages.length){ renderChat(d.messages); _chatAfter=d.latest; }
      renderPending(d.pending);
    }
  }catch(e){}
}
async function sendChat(){
  const i=document.getElementById('chat-input');
  const body=i.value.trim();
  if(!body)return;
  i.value='';
  await fetch('/api/chat/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({body})});
  pollChat();
}
if(document.getElementById('chat-log')){
  document.getElementById('chat-input').addEventListener('keydown',e=>{if(e.key==='Enter')sendChat()});
  pollChat();
  setInterval(pollChat, 1500);
}
</script>
</body></html>
"""
