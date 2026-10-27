const TOKEN_KEY = "llmrouter.admin";
let STATE = {};
let stream = null;

function token(){ return localStorage.getItem(TOKEN_KEY) || ""; }
function saveToken(){ localStorage.setItem(TOKEN_KEY, document.getElementById("adminToken").value.trim()); refresh(); }

async function api(path, opts = {}){
  const o = {...opts, headers: {"content-type":"application/json","x-admin-token":token(),...(opts.headers||{})}};
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  if (r.status === 401) { openModal(`<h3>Admin token</h3><div class="field"><label>ADMIN_TOKEN</label><input id="t" value="${token()}"></div><button class="btn" onclick="saveToken();closeModal()">Save</button>`); throw new Error("unauth"); }
  return r.json();
}

function openModal(html){ document.getElementById("modalRoot").innerHTML = `<div class="overlay" onclick="if(event.target===this)closeModal()"><div class="modal">${html}</div></div>`; }
function closeModal(){ document.getElementById("modalRoot").innerHTML = ""; }

document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  document.getElementById("tab-" + b.dataset.tab).classList.add("active");
});

/* ---------------- dashboard ---------------- */
function renderDash(){
  const s = STATE.stats || {};
  document.getElementById("statCards").innerHTML = [
    ["Requests / 5 min", s.requests_5m ?? 0],
    ["Success rate", (s.success_rate ?? 100) + "%"],
    ["Avg latency", (s.avg_latency ?? 0) + " ms"],
    ["Active keys", `${s.keys_enabled ?? 0} / ${s.keys_total ?? 0}`],
    ["Virtual models", s.models ?? 0],
  ].map(([k,v]) => `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const rows = (STATE.keys || []).map(k => {
    const h = k.health || {};
    const cooling = (h.cooldown || 0) > 0;
    const state = !k.enabled ? '<span class="pill off">disabled</span>'
      : cooling ? `<span class="pill warn">cooling ${Math.ceil(h.cooldown)}s</span>`
      : '<span class="pill ok">ready</span>';
    const rpmPct = k.max_rpm ? Math.min(100, 100 * (h.rpm_used||0) / k.max_rpm) : 0;
    return `<tr>
      <td>${provName(k.provider_id)}</td>
      <td class="mono">${k.label || "key#" + k.id}</td>
      <td class="mono muted">${k.masked}</td>
      <td>${state}</td>
      <td><div class="bar"><i class="${rpmPct>85?'bad':rpmPct>60?'warn':''}" style="width:${rpmPct}%"></i></div>
          <span class="muted">${h.rpm_used||0}${k.max_rpm?"/"+k.max_rpm:""} rpm</span></td>
      <td>${h.latency_ema || "–"} ms</td>
      <td class="muted">${h.last_error || ""}</td>
    </tr>`;
  }).join("");
  document.getElementById("keyHealth").innerHTML = `<table><thead><tr>
    <th>Provider</th><th>Key</th><th>Value</th><th>State</th><th>RPM window</th><th>Latency</th><th>Last note</th>
  </tr></thead><tbody>${rows || '<tr><td colspan="7" class="muted">No keys yet</td></tr>'}</tbody></table>`;
}

function provName(id){ const p = (STATE.providers||[]).find(x => x.id === id); return p ? p.name : "?"; }

/* ---------------- providers ---------------- */
function renderProviders(){
  document.getElementById("providers").innerHTML = (STATE.providers || []).map(p => {
    const keys = (STATE.keys || []).filter(k => k.provider_id === p.id);
    return `<div class="panel">
      <h3>${p.name} <span class="pill off">${p.adapter}</span>
        <span class="grow"></span>
        <button class="btn sm" onclick="modalNewKey(${p.id})">+ key</button>
        <button class="btn sm ghost" onclick="toggleProvider(${p.id},${p.enabled?0:1})">${p.enabled?"disable":"enable"}</button>
        <button class="btn sm danger" onclick="delProvider(${p.id})">delete</button>
      </h3>
      <div class="muted mono">${p.base_url}</div>
      ${keys.length ? keys.map(k => {
        const h = k.health || {};
        return `<div class="target">
          <span class="mono">${k.label || "key#" + k.id}</span>
          <span class="mono muted">${k.masked}</span>
          ${h.cooldown > 0 ? `<span class="pill warn">${Math.ceil(h.cooldown)}s</span>` : '<span class="pill ok">ready</span>'}
          <span class="muted">w${k.weight} p${k.priority} ${k.max_rpm?k.max_rpm+"rpm":""} ${k.max_tpm?k.max_tpm+"tpm":""}</span>
          <span class="grow"></span>
          <button class="btn sm ghost" onclick="resetKey(${k.id})">reset</button>
          <button class="btn sm ghost" onclick="toggleKey(${k.id},${k.enabled?0:1})">${k.enabled?"off":"on"}</button>
          <button class="btn sm danger" onclick="delKey(${k.id})">x</button>
        </div>`;
      }).join("") : '<div class="muted">No keys — add at least one.</div>'}
    </div>`;
  }).join("") || '<div class="muted">No providers configured yet.</div>';
}

function modalNewProvider(){
  const opts = Object.entries(STATE.presets || {}).map(([k,v]) => `<option value="${k}">${v.label}</option>`).join("");
  openModal(`<h3>Add provider</h3>
    <div class="field"><label>Preset</label><select id="pPreset" onchange="presetFill()">${opts}</select></div>
    <div class="field"><label>Name (unique)</label><input id="pName" placeholder="groq-main"></div>
    <div class="field"><label>Adapter</label><select id="pAdapter"><option>openai</option><option>anthropic</option><option>gemini</option></select></div>
    <div class="field"><label>Base URL</label><input id="pBase"></div>
    <div class="field"><label>Extra headers (JSON)</label><input id="pHeaders" value="{}"></div>
    <div class="row"><button class="btn" onclick="createProvider()">Create</button><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
  presetFill();
}
function presetFill(){
  const k = document.getElementById("pPreset").value;
  const p = (STATE.presets||{})[k] || {};
  document.getElementById("pBase").value = p.base_url || "";
  document.getElementById("pAdapter").value = p.adapter || "openai";
  if (!document.getElementById("pName").value) document.getElementById("pName").value = k;
}
async function createProvider(){
  const body = {
    preset: document.getElementById("pPreset").value,
    name: document.getElementById("pName").value.trim(),
    adapter: document.getElementById("pAdapter").value,
    base_url: document.getElementById("pBase").value.trim(),
    headers: JSON.parse(document.getElementById("pHeaders").value || "{}"),
  };
  const r = await api("/admin/api/providers", {method:"POST", body});
  if (r.detail) return alert(r.detail);
  closeModal(); refresh();
}

function modalNewKey(pid){
  openModal(`<h3>Add API key</h3>
    <div class="field"><label>Label</label><input id="kLabel" placeholder="acct-1"></div>
    <div class="field"><label>API key</label><input id="kKey" placeholder="sk-..."></div>
    <div class="grid3">
      <div class="field"><label>Weight</label><input id="kWeight" type="number" value="1" step="0.1"></div>
      <div class="field"><label>Priority</label><input id="kPrio" type="number" value="100"></div>
      <div class="field"><label>Max concurrency</label><input id="kConc" type="number" value="0"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>Max RPM (0 = provider limit)</label><input id="kRpm" type="number" value="0"></div>
      <div class="field"><label>Max TPM (0 = provider limit)</label><input id="kTpm" type="number" value="0"></div>
    </div>
    <div class="row"><button class="btn" onclick="createKey(${pid})">Add</button><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
}
async function createKey(pid){
  await api("/admin/api/keys", {method:"POST", body:{
    provider_id: pid,
    label: document.getElementById("kLabel").value.trim(),
    api_key: document.getElementById("kKey").value.trim(),
    weight: parseFloat(document.getElementById("kWeight").value || 1),
    priority: parseInt(document.getElementById("kPrio").value || 100),
    max_concurrency: parseInt(document.getElementById("kConc").value || 0),
    max_rpm: parseInt(document.getElementById("kRpm").value || 0),
    max_tpm: parseInt(document.getElementById("kTpm").value || 0),
  }});
  closeModal(); refresh();
}
const toggleKey = (id, en) => api(`/admin/api/keys/${id}`, {method:"PATCH", body:{enabled:en}}).then(refresh);
const delKey = id => api(`/admin/api/keys/${id}`, {method:"DELETE"}).then(refresh);
const resetKey = id => api(`/admin/api/keys/${id}/reset`, {method:"POST"}).then(refresh);
const toggleProvider = (id, en) => api(`/admin/api/providers/${id}`, {method:"PATCH", body:{enabled:en}}).then(refresh);
const delProvider = id => confirm("Delete provider and its keys?") && api(`/admin/api/providers/${id}`, {method:"DELETE"}).then(refresh);

/* ---------------- models ---------------- */
function renderModels(){
  document.getElementById("models").innerHTML = (STATE.vmodels || []).map(v => `
    <div class="panel">
      <h3><code>${v.name}</code> <span class="pill off">${v.strategy}</span>
        ${v.sticky ? '<span class="pill ok">sticky</span>' : ''}
        <span class="grow"></span>
        <button class="btn sm" onclick="modalNewTarget(${v.id})">+ target</button>
        <button class="btn sm ghost" onclick="testModel('${v.name}')">test</button>
        <button class="btn sm danger" onclick="delModel(${v.id})">delete</button>
      </h3>
      <div class="muted">${v.description || ""}</div>
      ${(v.targets||[]).length ? v.targets.map(t => `<div class="target">
          <span>${provName(t.provider_id)}</span>
          <span class="mono">${t.upstream_model}</span>
          <span class="muted">${t.key_id ? "key#"+t.key_id : "any key"}</span>
          <span class="muted">w${t.weight} p${t.priority}</span>
          <span class="grow"></span>
          <button class="btn sm ghost" onclick="toggleTarget(${t.id},${t.enabled?0:1})">${t.enabled?"off":"on"}</button>
          <button class="btn sm danger" onclick="delTarget(${t.id})">x</button>
        </div>`).join("") : '<div class="muted">No targets yet — this model will 429.</div>'}
    </div>`).join("") || '<div class="muted">No virtual models yet.</div>';
}

function modalNewModel(){
  openModal(`<h3>New virtual model</h3>
    <div class="field"><label>Public model name</label><input id="mName" placeholder="fast-chat"></div>
    <div class="field"><label>Description</label><input id="mDesc"></div>
    <div class="field"><label>Strategy</label><select id="mStrat">
      <option value="balanced">balanced (priority + weighted round robin)</option>
      <option value="priority">priority (strict failover order)</option>
      <option value="round_robin">round robin</option>
      <option value="least_latency">least latency</option>
      <option value="random">random</option>
    </select></div>
    <div class="field"><label><input type="checkbox" id="mSticky" checked style="width:auto"> sticky sessions (stable key per conversation → better prompt cache hits)</label></div>
    <div class="row"><button class="btn" onclick="createModel()">Create</button><button class="btn ghost" onclick="closeModal()">Cancel</button></div>`);
}
async function createModel(){
  const r = await api("/admin/api/vmodels", {method:"POST", body:{
    name: document.getElementById("mName").value.trim(),
    description: document.getElementById("mDesc").value.trim(),
    strategy: document.getElementById("mStrat").value,
    sticky: document.getElementById("mSticky").checked,
  }});
  if (r.detail) return alert(r.detail);
  closeModal(); refresh();
}
const delModel = id => confirm("Delete model?") && api(`/admin/api/vmodels/${id}`, {method:"DELETE"}).then(refresh);
const toggleTarget = (id, en) => api(`/admin/api/targets/${id}`, {method:"PATCH", body:{enabled:en}}).then(refresh);
const delTarget = id => api(`/admin/api/targets/${id}`, {method:"DELETE"}).then(refresh);

function modalNewTarget(vid){
  const provs = (STATE.providers||[]).map(p => `<option value="${p.id}">${p.name}</option>`).join("");
  openModal(`<h3>Add target</h3>
    <div class="field"><label>Provider</label><select id="tProv" onchange="fillKeys()">${provs}</select></div>
    <div class="field"><label>Key</label><select id="tKey"><option value="">any key in provider pool</option></select></div>
    <div class="field"><label>Upstream model</label><input id="tModel" placeholder="llama-3.3-70b-versatile"></div>
    <div class="grid2">
      <div class="field"><label>Weight</label><input id="tWeight" type="number" value="1" step="0.1"></div>
      <div class="field"><label>Priority</label><input id="tPrio" type="number" value="100"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>Target RPM cap</label><input id="tRpm" type="number" value="0"></div>
      <div class="field"><label>Target TPM cap</label><input id="tTpm" type="number" value="0"></div>
    </div>
    <div class="row"><button class="btn" onclick="createTarget(${vid})">Add</button>
      <button class="btn ghost" onclick="loadUpstreamModels()">fetch model list</button></div>`);
  fillKeys();
}
function fillKeys(){
  const pid = parseInt(document.getElementById("tProv").value);
  const keys = (STATE.keys||[]).filter(k => k.provider_id === pid);
  document.getElementById("tKey").innerHTML = '<option value="">any key in provider pool</option>' +
    keys.map(k => `<option value="${k.id}">${k.label || "key#"+k.id} (${k.masked})</option>`).join("");
}
async function loadUpstreamModels(){
  const pid = parseInt(document.getElementById("tProv").value);
  const r = await api(`/admin/api/providers/${pid}/models`);
  if (!r.models || !r.models.length) return alert("Provider returned no model list (type it manually).");
  const m = r.models.join("\n");
  const pick = prompt("Available upstream models:\n\n" + m + "\n\nType the exact model id:", r.models[0]);
  if (pick) document.getElementById("tModel").value = pick;
}
async function createTarget(vid){
  await api("/admin/api/targets", {method:"POST", body:{
    vmodel_id: vid,
    provider_id: parseInt(document.getElementById("tProv").value),
    key_id: document.getElementById("tKey").value || null,
    upstream_model: document.getElementById("tModel").value.trim(),
    weight: parseFloat(document.getElementById("tWeight").value || 1),
    priority: parseInt(document.getElementById("tPrio").value || 100),
    max_rpm: parseInt(document.getElementById("tRpm").value || 0),
    max_tpm: parseInt(document.getElementById("tTpm").value || 0),
  }});
  closeModal(); refresh();
}
async function testModel(name){
  const r = await api("/admin/api/test", {method:"POST", body:{model:name}});
  alert(r.ok ? `OK via ${r.provider} / ${r.key} after ${r.attempts} attempt(s) in ${r.ms}ms\n\n${r.reply}`
             : `FAILED: ${r.error}`);
}

/* ---------------- logs ---------------- */
function renderLogs(){
  const rows = (STATE.logs || []).slice().reverse();
  document.getElementById("logFeed").innerHTML = rows.map(l =>
    `<div>[${new Date(l.ts*1000).toLocaleTimeString()}] ${l.vmodel} → ${l.provider}/${l.key_label} ${l.status} ${l.latency_ms}ms a${l.attempts} ${l.error?("⚠ "+l.error):""}</div>`
  ).join("");
  const f = document.getElementById("logFeed");
  f.scrollTop = f.scrollHeight;
}
function startStream(){
  if (stream) stream.close();
  stream = new EventSource("/admin/api/stream?token=" + encodeURIComponent(token()));
  stream.onmessage = e => {
    const item = JSON.parse(e.data);
    const line = document.createElement("div");
    line.textContent = `[${new Date(item.ts*1000).toLocaleTimeString()}] ${item.kind.toUpperCase()} ${item.model||""} ${item.provider}/${item.key} ${item.status||""} ${item.ms?item.ms+"ms":""} ${item.seconds?("cool "+item.seconds+"s"):""} ${item.reason||""}`;
    const f = document.getElementById("logFeed");
    f.appendChild(line);
    if (f.childElementCount > 400) f.removeChild(f.firstChild);
    f.scrollTop = f.scrollHeight;
  };
}

/* ---------------- settings ---------------- */
function renderSettings(){
  document.getElementById("gwKeys").innerHTML = (STATE.gateway_keys||[]).map(g => `
    <div class="target"><span class="mono">${g.masked}</span><span class="muted">${g.label}</span>
    <span class="grow"></span>
    <button class="btn sm ghost" onclick="copyKey('${g.key}')">copy</button>
    <button class="btn sm danger" onclick="delGw(${g.id})">x</button></div>`).join("")
    || '<div class="muted">none</div>';
  document.getElementById("baseUrl").textContent = location.origin + "/v1";
  document.getElementById("adminToken").value = token();
}
const delGw = id => api(`/admin/api/gateway-keys/${id}`, {method:"DELETE"}).then(refresh);
function copyKey(k){ navigator.clipboard.writeText(k); }
async function newGwKey(){ const r = await api("/admin/api/gateway-keys", {method:"POST", body:{label:"client"}}); refresh(); alert("New gateway key:\n\n" + r.key); }
async function exportCfg(){
  const d = await api("/admin/api/export");
  const blob = new Blob([JSON.stringify(d, null, 2)], {type:"application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "llm-router-config.json"; a.click();
}
async function importCfg(input){
  const text = await input.files[0].text();
  const wipe = confirm("Wipe existing config before import? OK = replace, Cancel = merge");
  await api("/admin/api/import?wipe=" + (wipe?1:0), {method:"POST", body: JSON.parse(text)});
  refresh();
}

/* ---------------- boot ---------------- */
async function refresh(){
  try {
    STATE = await api("/admin/api/state");
    renderDash(); renderProviders(); renderModels(); renderLogs(); renderSettings();
  } catch(e){}
}
refresh();
setInterval(refresh, 4000);
startStream();
