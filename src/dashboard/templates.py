"""Inline HTML templates for the dashboard.

Extracted to its own module so server.py stays readable. Moving these to
real static files (one HTML, one JS, one CSS) is the obvious next step —
see plan §12 (out of scope for this pass)."""

LOGIN_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>microagent dashboard</title>
<style>body{font-family:system-ui;max-width:380px;margin:10vh auto;padding:1rem}
input{width:100%;padding:.6rem;font-size:1rem;margin:.5rem 0}
button{padding:.6rem 1rem;font-size:1rem;cursor:pointer}
.err{color:#b00}
.demo{margin-top:1rem;font-size:.9rem;color:#666}
</style></head>
<body><h2>microagent</h2>
<p>Access token required.</p>
<form method="post" action="/login">
<input type="password" name="token" placeholder="DASHBOARD_TOKEN" autofocus>
<button>enter</button>
<div class="err">{{error}}</div>
</form>
{{demo_link}}
</body></html>
"""

PAGE_HTML = r"""<!doctype html>
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
#demo-banner{background:#ffd;padding:.6rem 1rem;border-bottom:1px solid #cc9;margin:-1rem -1rem 1rem -1rem}
.disabled-demo{opacity:.5;pointer-events:none}
#interfaces,#sources{display:flex;flex-direction:column;gap:.4rem}
.iface-row{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;padding:.35rem .5rem;border:1px solid #eee;border-radius:4px}
.iface-row .iname{font-family:ui-monospace,monospace;min-width:6rem}
.iface-row .imiss{color:#b00;font-size:.85rem}
.iface-prompt{display:flex;flex-basis:100%;gap:.4rem;align-items:center;margin-top:.3rem}
.iface-prompt input{flex:1 1 auto;padding:.4rem;font-family:ui-monospace,monospace;border:1px solid #ccc;border-radius:4px}
.iface-prompt label{font-family:ui-monospace,monospace;font-size:.85rem;min-width:9rem}
</style></head>
<body>
<div id="demo-banner" hidden><b>Demo mode</b> — all values are empty and changes are not saved.</div>
<h1>microagent</h1>
<p class="status">Control panel for secrets and config. Changes take effect after restart.</p>

<section>
<h2>Interfaces</h2>
<p class="status" style="margin:.2rem 0 .6rem">Two-way channels (send + receive). Toggle on to enable. If an interface needs secrets you don't have yet, you'll be prompted inline. Restart after changes.</p>
<div id="interfaces"></div>
</section>

<section>
<h2>Sources</h2>
<p class="status" style="margin:.2rem 0 .6rem">Receive-only wake inputs (no send path) — external feeds like iMessage and the agent-schedulable cron.</p>
<div id="sources"></div>
</section>

<section>
<h2>Environment (.env)</h2>
<div id="env"></div>
<div class="row" style="margin-top:.5rem">
<button type="button" data-owner-only onclick="addRow()">+ add</button>
<button type="button" data-owner-only onclick="saveEnv()">save</button>
<button type="button" onclick="toggleReveal()">show values</button>
<span id="env-status" class="status"></span>
</div>
</section>

<section>
<h2>Config (/config/config.toml)</h2>
<textarea id="config" data-owner-only></textarea>
<div class="row" style="margin-top:.5rem">
<button type="button" data-owner-only onclick="saveConfig()">save</button>
<span id="config-status" class="status"></span>
</div>
</section>

<section>
<h2>Chat</h2>
<div id="chat-log" style="border:1px solid #ddd;border-radius:4px;padding:.75rem;height:18rem;overflow-y:auto;background:#fafafa;font-family:ui-monospace,monospace;font-size:.9rem;margin-bottom:.6rem"></div>
<div id="chat-pending" style="font-family:ui-monospace,monospace;font-size:.85rem;color:#888;font-style:italic;min-height:1.2rem;margin:-.3rem 0 .4rem .1rem"></div>
<div class="row">
<input id="chat-input" type="text" placeholder="say something to the agent…" style="flex:1 1 auto;padding:.5rem;border:1px solid #ccc;border-radius:4px" data-owner-only>
<button type="button" data-owner-only onclick="sendChat()">send</button>
</div>
</section>

<section>
<h2>Agent Space</h2>
<p class="status" style="margin:.2rem 0 .6rem">A corner the agent owns. It can write HTML or linked pages here.</p>
<iframe id="space-frame" src="/space/" style="width:100%;height:22rem;border:1px solid #ddd;border-radius:4px;background:#fff" sandbox="allow-same-origin allow-scripts allow-top-navigation-by-user-activation"></iframe>
<div class="row" style="margin-top:.4rem">
<a href="/space/" target="_blank" rel="noopener">open in new tab →</a>
<button type="button" onclick="reloadSpace()">reload</button>
</div>
</section>

<section>
<h2>Usage</h2>
<div id="usage" class="status">loading…</div>
</section>

<section>
<h2>Process</h2>
<button type="button" data-owner-only onclick="update()">update &amp; restart</button>
<button type="button" data-owner-only onclick="restart()">restart agent</button>
<span class="status">update pulls origin/main; docker respawns the container</span>
</section>

<script>
let ROLE = 'owner';

function applyRole() {
  const demo = ROLE === 'demo';
  document.getElementById('demo-banner').hidden = !demo;
  document.querySelectorAll('[data-owner-only]').forEach(el => {
    el.disabled = demo;
    if (demo) el.classList.add('disabled-demo'); else el.classList.remove('disabled-demo');
  });
}

let _env = {};
let _interfaces = [];

// Baseline = enabled/wake-map at page load. The running process booted
// from config.toml, so this matches what it's actually running. A row
// shows "restart to apply" iff current != baseline; toggling back clears it.
let _baselineEnabled = {};
let _baselineWake = {};

function renderInterfaces() {
  const ifaceHost = document.getElementById('interfaces');
  const sourceHost = document.getElementById('sources');
  if (!ifaceHost || !sourceHost) return;
  ifaceHost.innerHTML = '';
  sourceHost.innerHTML = '';
  for (const iface of _interfaces) {
    const row = document.createElement('div');
    row.className = 'iface-row';
    row.dataset.name = iface.name;
    const missing = iface.required_env.filter(k => !_env[k]);
    const missingHtml = (iface.enabled && missing.length)
      ? `<span class="imiss">missing: ${missing.join(', ')}</span>` : '';
    const pendingEnabled = _baselineEnabled[iface.name] !== iface.enabled;
    const pendingWake = iface.kind === 'sources'
      && _baselineWake[iface.name] !== iface.wake_on_event;
    const pendingHtml = (pendingEnabled || pendingWake)
      ? '<span class="imiss" style="color:#a60">restart to apply</span>' : '';
    const wakeHtml = iface.kind === 'sources'
      ? `<label class="status" style="font-size:.8rem;display:inline-flex;align-items:center;gap:.2rem">` +
        `<input type="checkbox" ${iface.wake_on_event?'checked':''} data-owner-only onchange="onWakeToggle('${iface.name}', this)">` +
        `wakes</label>`
      : '';
    row.innerHTML =
      `<input type="checkbox" ${iface.enabled?'checked':''} data-owner-only onchange="onToggle('${iface.name}', this)">` +
      `<span class="iname">${iface.name}</span>` +
      wakeHtml +
      (iface.required_env.length ? `<span class="status" style="font-size:.8rem">needs: ${iface.required_env.join(', ')}</span>` : '') +
      missingHtml +
      pendingHtml;
    const host = iface.kind === 'sources' ? sourceHost : ifaceHost;
    host.appendChild(row);
  }
  applyRole();
}

async function onWakeToggle(name, cb) {
  if (ROLE === 'demo') { cb.checked = !cb.checked; return; }
  const iface = _interfaces.find(i => i.name === name);
  const r = await fetch('/api/source/wake_toggle', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name, enabled: cb.checked})});
  if (!r.ok) { cb.checked = !cb.checked; return; }
  iface.wake_on_event = cb.checked;
  renderInterfaces();
}

async function onToggle(name, cb) {
  if (ROLE === 'demo') { cb.checked = !cb.checked; return; }
  const iface = _interfaces.find(i => i.name === name);
  const row = cb.closest('.iface-row');
  row.querySelectorAll('.iface-prompt').forEach(e => e.remove());
  if (cb.checked) {
    const missing = iface.required_env.filter(k => !_env[k]);
    if (missing.length) {
      cb.checked = false;
      for (const k of missing) {
        const p = document.createElement('div');
        p.className = 'iface-prompt';
        p.innerHTML = `<label>${k}</label><input type="password" placeholder="${k}"><button type="button">save & enable</button><button type="button" class="cancel">cancel</button>`;
        const input = p.querySelector('input');
        const save = p.querySelector('button');
        const cancel = p.querySelector('.cancel');
        save.onclick = async () => {
          const val = input.value.trim();
          if (!val) return;
          save.disabled = true;
          _env[k] = val;
          const entries = Object.entries(_env).map(([key,value]) => ({key,value}));
          let r = await fetch('/api/env', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries})});
          if (!r.ok) { save.disabled = false; return; }
          r = await fetch('/api/interface/toggle', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name, enabled:true})});
          if (!r.ok) { save.disabled = false; return; }
          iface.enabled = true;
          iface.missing_env = iface.required_env.filter(k2 => !_env[k2]);
          renderInterfaces();
        };
        cancel.onclick = () => { p.remove(); };
        row.appendChild(p);
      }
      row.querySelector('.iface-prompt input').focus();
      return;
    }
  }
  const r = await fetch('/api/interface/toggle', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name, enabled: cb.checked})});
  if (!r.ok) { cb.checked = !cb.checked; return; }
  iface.enabled = cb.checked;
  renderInterfaces();
}

function renderEnv(entries) {
  const host = document.getElementById('env');
  host.innerHTML = '';
  const hints = ['TOKEN','PASSWORD','SECRET','KEY','API'];
  const isSecret = k => hints.some(h => k.toUpperCase().includes(h));
  for (const [k, v] of Object.entries(entries)) {
    const row = document.createElement('div');
    row.className = 'env-row' + (isSecret(k) ? ' secret' : '');
    const type = isSecret(k) ? 'password' : 'text';
    row.innerHTML =
      `<input class="k" name="k" value="${k.replace(/"/g,'&quot;')}">` +
      `<input class="v" name="v" type="${type}" value="${v.replace(/"/g,'&quot;')}">` +
      `<button type="button" class="del" data-owner-only onclick="softDelete(this)">delete</button>` +
      `<button type="button" class="undo" onclick="undo(this)" hidden>undo</button>`;
    host.appendChild(row);
  }
  applyRole();
}

function addRow(){
  if (ROLE === 'demo') return;
  const row=document.createElement('div');
  row.className='env-row';
  row.innerHTML='<input class="k" name="k" value=""><input class="v" name="v" value="">'+
               '<button type="button" class="del" onclick="this.closest(\'.env-row\').remove()">×</button>';
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
function toggleReveal(e){
  _revealed=!_revealed;
  document.querySelectorAll('#env .env-row.secret input.v').forEach(i=>{
    i.type=_revealed?'text':'password';
  });
  e.target.textContent=_revealed?'hide values':'show values';
}
function undo(btn){
  const row=btn.closest('.env-row');
  row.classList.remove('deleted');
  row.querySelectorAll('input').forEach(i=>i.disabled=false);
  row.querySelector('.del').hidden=false;
  row.querySelector('.undo').hidden=true;
}
async function saveEnv(){
  if (ROLE === 'demo') return;
  const rows=[...document.querySelectorAll('#env .env-row:not(.deleted)')];
  const entries=rows.map(r=>({key:r.querySelector('.k').value,value:r.querySelector('.v').value}));
  const removed=document.querySelectorAll('#env .env-row.deleted').length;
  if(removed>0 && !confirm(`Delete ${removed} entr${removed===1?'y':'ies'}? This removes them from .env.`))return;
  const s=document.getElementById('env-status'); s.textContent='saving…';
  const r=await fetch('/api/env',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries})});
  s.textContent=r.ok?'saved':'error: '+await r.text();
}
async function saveConfig(){
  if (ROLE === 'demo') return;
  const s=document.getElementById('config-status'); s.textContent='saving…';
  const text=document.getElementById('config').value;
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/toml'},body:text});
  const d=await r.json().catch(()=>({}));
  s.textContent=r.ok?'saved':'error: '+(d.error||r.status);
}
function reloadSpace(){
  const f=document.getElementById('space-frame');
  if(f) f.src=f.src;
}
async function restart(){
  if (ROLE === 'demo') return;
  if(!confirm('restart now?'))return;
  await fetch('/api/restart',{method:'POST'});
  alert('restarting…');
}
async function update(){
  if (ROLE === 'demo') return;
  if(!confirm('pull origin/main and restart?'))return;
  const r=await fetch('/api/update',{method:'POST'});
  const d=await r.json().catch(()=>({}));
  if(r.ok) alert('updated to '+(d.sha||'?')+', restarting…');
  else alert('update failed: '+(d.error||r.status));
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
let _pollingChat=false;
async function pollChat(){
  if(_pollingChat) return;  // guard: interval + post-send both call this; concurrent polls double-render
  _pollingChat=true;
  try{
    const r=await fetch('/api/chat/poll?after='+_chatAfter);
    if(r.ok){
      const d=await r.json();
      if(d.messages && d.messages.length){ renderChat(d.messages); _chatAfter=d.latest; }
      renderPending(d.pending);
    }
  }catch(e){}
  finally{ _pollingChat=false; }
}
async function sendChat(){
  if (ROLE === 'demo') return;
  const i=document.getElementById('chat-input');
  const body=i.value.trim();
  if(!body)return;
  i.value='';
  await fetch('/api/chat/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({body})});
  pollChat();
}

function fmtNum(n){ return (n==null)?'–':n.toLocaleString(); }
function fmtResets(ts){
  if(!ts) return '–';
  const d=new Date(ts*1000), now=new Date();
  const mins=Math.round((d-now)/60000);
  const hhmm=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  if(mins<=0) return hhmm+' (now)';
  if(mins<60) return hhmm+' (in '+mins+'m)';
  return hhmm+' (in '+Math.round(mins/60)+'h'+(mins%60)+'m)';
}
function renderUsage(d){
  const el=document.getElementById('usage'); if(!el) return;
  const lw=d.last_wake, rl=d.rate_limit;
  if(!lw && !rl){ el.textContent='no wakes recorded yet'; return; }
  const parts=[];
  if(lw){
    const u=lw.usage||{};
    parts.push('<div><b>Last wake</b> ('+(lw.at||'?')+', '+(lw.num_turns||0)+' turns, $'+((lw.total_cost_usd||0).toFixed(4))+')</div>'
      +'<div style="font-family:ui-monospace,monospace;font-size:.85rem">'
      +'in: '+fmtNum(u.input_tokens)+' · out: '+fmtNum(u.output_tokens)
      +' · cache read: '+fmtNum(u.cache_read_input_tokens)
      +' · cache write: '+fmtNum(u.cache_creation_input_tokens)
      +'</div>');
  }
  if(rl){
    parts.push('<div style="margin-top:.5rem"><b>Rate limit</b> ('+(rl.rate_limit_type||'?')+'): '+(rl.status||'?')
      +(rl.utilization!=null?' · '+Math.round(rl.utilization*100)+'% used':'')
      +' · resets '+fmtResets(rl.resets_at)+'</div>');
  }
  el.innerHTML=parts.join('');
}
async function pollUsage(){
  try{
    const r=await fetch('/api/usage');
    if(r.ok) renderUsage(await r.json());
  }catch(e){}
}

async function bootstrap() {
  const r = await fetch('/api/bootstrap', {cache: 'no-store'});
  if (!r.ok) { document.body.textContent='bootstrap failed: '+r.status; return; }
  const d = await r.json();
  ROLE = d.role || 'owner';
  _env = d.env || {};
  _interfaces = d.interfaces || [];
  _baselineEnabled = Object.fromEntries(_interfaces.map(i => [i.name, i.enabled]));
  _baselineWake = Object.fromEntries(_interfaces.filter(i => i.kind === 'sources').map(i => [i.name, !!i.wake_on_event]));
  document.getElementById('config').value = d.config_toml || '';
  renderEnv(_env);
  renderInterfaces();
  applyRole();
  renderUsage(d.usage || {});
  document.getElementById('chat-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') sendChat();
  });
  pollChat();
  setInterval(pollChat, 1500);
  pollUsage();
  setInterval(pollUsage, 5000);
}
bootstrap();
</script>
</body></html>
"""
