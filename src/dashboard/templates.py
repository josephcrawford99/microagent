"""Inline HTML for the dashboard viewer"""

LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>microagent</title>
<style>
  body{font-family:system-ui;background:#0f1115;color:#d7dae0;display:flex;
       align-items:center;justify-content:center;min-height:100vh;margin:0}
  form{background:#171a21;padding:2rem;border-radius:12px;width:20rem}
  h1{font-size:1.1rem;margin:0 0 1rem}
  input{width:100%;box-sizing:border-box;padding:.6rem;border-radius:8px;
        border:1px solid #2a2f3a;background:#0f1115;color:#d7dae0}
  button{margin-top:.8rem;width:100%;padding:.6rem;border-radius:8px;border:0;
         background:#3b82f6;color:#fff;font-weight:600;cursor:pointer}
  .err{color:#f87171;font-size:.85rem;min-height:1.2em}
  a{color:#60a5fa}
</style></head><body>
<form method="POST" action="/login">
  <h1>microagent</h1>
  <input type="password" name="token" placeholder="dashboard token" autofocus>
  <p class="err">{{error}}</p>
  <button>enter</button>
</form>
</body></html>"""


SPACE_EMPTY_HTML = """<!doctype html><meta charset="utf-8">
<title>agent space</title>
<style>body{font-family:system-ui;max-width:36rem;margin:4rem auto;padding:1rem;color:#555}</style>
<h2>empty</h2>
<p>The agent hasn't written anything here yet. Ask it to — this is its space to fill.</p>
<p style="color:#888;font-size:.9rem">Path: <code>space/index.html</code></p>
"""


PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>microagent</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--line:#2a2f3a;--fg:#d7dae0;--dim:#8b93a3;
        --accent:#3b82f6;--ok:#34d399;--bad:#f87171}
  *{box-sizing:border-box}
  body{font-family:system-ui;background:var(--bg);color:var(--fg);margin:0;
       padding:1rem;max-width:72rem;margin-inline:auto}
  h1{font-size:1.1rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  h2{font-size:.85rem;color:var(--dim);text-transform:uppercase;
     letter-spacing:.06em;margin:0 0 .6rem}
  .pill{font-size:.75rem;background:var(--card);border:1px solid var(--line);
        padding:.15rem .55rem;border-radius:99px;color:var(--dim)}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
  @media(max-width:64rem){.grid{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:1rem;margin-bottom:1rem}
  button{padding:.4rem .8rem;border-radius:8px;border:1px solid var(--line);
         background:var(--bg);color:var(--fg);cursor:pointer;font-size:.85rem}
  button:hover{border-color:var(--accent)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  #chatlog{height:22rem;overflow-y:auto;display:flex;flex-direction:column;
           gap:.5rem;padding:.4rem 0}
  .msg{max-width:85%;padding:.5rem .7rem;border-radius:10px;white-space:pre-wrap;
       overflow-wrap:anywhere;font-size:.9rem}
  .msg.user{align-self:flex-end;background:var(--accent);color:#fff}
  .msg.agent{align-self:flex-start;background:var(--bg);border:1px solid var(--line)}
  #chatrow{display:flex;gap:.5rem;margin-top:.5rem}
  #chatrow input{flex:1;padding:.55rem;border-radius:8px;border:1px solid var(--line);
                 background:var(--bg);color:var(--fg)}
  pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;
      padding:.7rem;overflow-x:auto;font-size:.78rem;line-height:1.45;
      white-space:pre-wrap;overflow-wrap:anywhere}
  .tree{font-family:ui-monospace,monospace;font-size:.8rem;line-height:1.6}
  .tree .dir{color:var(--fg);font-weight:600;margin-top:.4rem}
  .tree .h{color:var(--dim);padding-left:1.2rem}
  .tree .fifo::after{content:" (fifo)";color:var(--accent)}
  .tree .tail{color:var(--dim);padding-left:2.4rem;font-size:.72rem;
              white-space:pre-wrap;overflow-wrap:anywhere}
  table{width:100%;border-collapse:collapse;font-size:.85rem}
  td,th{text-align:left;padding:.3rem .4rem;border-bottom:1px solid var(--line)}
  .on{color:var(--ok)} .off{color:var(--dim)} .miss{color:var(--bad)}
  #envrows input{width:100%;padding:.35rem;border-radius:6px;
                 border:1px solid var(--line);background:var(--bg);color:var(--fg);
                 font-family:ui-monospace,monospace;font-size:.78rem}
  .toast{position:fixed;bottom:1rem;right:1rem;background:var(--card);
         border:1px solid var(--line);border-radius:8px;padding:.6rem .9rem;
         font-size:.85rem;display:none}
</style></head><body>
<h1>microagent
  <span class="pill" id="agentpill">…</span>
  <span style="flex:1"></span>
  <button onclick="location.href='/space/'">space</button>
  <button onclick="act('restart')">restart</button>
  <button onclick="act('update')">update</button>
</h1>

<div class="grid">
  <div>
    <div class="card">
      <h2>chat</h2>
      <div id="chatlog"></div>
      <div id="chatrow">
        <input id="chatinput" placeholder="message the agent…"
               onkeydown="if(event.key==='Enter')sendChat()">
        <button class="primary" onclick="sendChat()">send</button>
      </div>
    </div>
    <div class="card">
      <h2>services</h2>
      <table id="services"></table>
    </div>
    <div class="card">
      <h2>secrets (.env)</h2>
      <table id="envrows"></table>
      <div style="margin-top:.6rem;display:flex;gap:.5rem">
        <button onclick="addEnvRow()">add</button>
        <button class="primary" onclick="saveEnv()">save + restart</button>
      </div>
    </div>
  </div>
  <div>
    <div class="card">
      <h2>handle tree — run/</h2>
      <div class="tree" id="tree"></div>
    </div>
    <div class="card">
      <h2>config.toml (read-only — edit on disk)</h2>
      <pre id="configtoml"></pre>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let chatOffset = 0;

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function toast(t){const el=document.getElementById('toast');el.textContent=t;
  el.style.display='block';setTimeout(()=>el.style.display='none',2500)}

async function api(path, opts){const r=await fetch(path,opts);return r.json()}

async function bootstrap(){
  const b = await api('/api/bootstrap');
  const miss = (b.agent.missing_env||[]).length
    ? ` — missing: ${b.agent.missing_env.join(', ')}` : '';
  document.getElementById('agentpill').textContent =
    `${b.agent.agent_id||'?'} · ${b.agent.provider||'?'}${miss}`;
  document.getElementById('configtoml').textContent = b.config_toml || '(empty)';
  const rows = (b.services||[]).map(s=>{
    const cls = s.enabled?'on':'off';
    const missing = (s.missing_env||[]).length
      ? `<span class="miss">missing ${s.missing_env.join(', ')}</span>` : '';
    return `<tr><td>${esc(s.name)}</td>
      <td class="${cls}">${s.enabled?'enabled':'off'}</td>
      <td>${s.enabled&&!s.wake?'passive':''}</td><td>${missing}</td></tr>`;
  }).join('');
  document.getElementById('services').innerHTML = rows;
  const env = Object.entries(b.env||{});
  document.getElementById('envrows').innerHTML = env.map(([k,v])=>
    `<tr><td><input value="${esc(k)}" class="ek"></td>
     <td><input value="${esc(v)}" type="password" class="ev"
         onfocus="this.type='text'" onblur="this.type='password'"></td></tr>`
  ).join('');
}

function addEnvRow(){
  document.getElementById('envrows').insertAdjacentHTML('beforeend',
    `<tr><td><input class="ek" placeholder="KEY"></td>
     <td><input class="ev" placeholder="value"></td></tr>`);
}

async function saveEnv(){
  const entries=[...document.querySelectorAll('#envrows tr')].map(tr=>({
    key:tr.querySelector('.ek').value.trim(),
    value:tr.querySelector('.ev').value})).filter(e=>e.key);
  const r = await api('/api/env',{method:'POST',body:JSON.stringify({entries})});
  toast(r.ok?'saved — restarting…':(r.error||'failed'));
}

async function act(kind){
  if(!confirm(kind==='update'?'git reset --hard origin/main + restart?':'restart the daemon?'))return;
  const r = await api('/api/'+kind,{method:'POST'});
  toast(r.ok?(kind+' ok'+(r.sha?' @ '+r.sha:'')):(r.error||'failed'));
}

async function pollChat(){
  try{
    const r = await api('/api/chat/poll?after='+chatOffset);
    if(r.messages && r.messages.length){
      const log=document.getElementById('chatlog');
      for(const m of r.messages){
        const el=document.createElement('div');
        el.className='msg '+(m.role==='user'?'user':'agent');
        el.textContent=m.body;
        log.appendChild(el);
      }
      log.scrollTop=log.scrollHeight;
    }
    chatOffset = r.offset||chatOffset;
  }catch(e){}
}

async function sendChat(){
  const input=document.getElementById('chatinput');
  const body=input.value.trim();
  if(!body)return;
  input.value='';
  await api('/api/chat/send',{method:'POST',body:JSON.stringify({body})});
  pollChat();
}

async function pollTree(){
  try{
    const dirs = await api('/api/handles');
    document.getElementById('tree').innerHTML = dirs.map(d=>
      `<div class="dir">${esc(d.name)}/</div>` +
      d.handles.map(h=>
        `<div class="h ${h.type}">${esc(h.name)} · ${h.size}b</div>` +
        (h.tail?`<div class="tail">${esc(h.tail.slice(-4).join('\\n'))}</div>`:'')
      ).join('')
    ).join('');
  }catch(e){}
}

bootstrap();
pollChat(); setInterval(pollChat, 1500);
pollTree(); setInterval(pollTree, 4000);
</script>
</body></html>"""
