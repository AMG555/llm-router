"use strict";

const TOKEN_KEY = "llmrouter.admin";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let STATE = {};
let stream = null;

/* ---------------- helpers ---------------- */

function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

function api(path, opts = {}) {
  const o = { ...opts };
  o.headers = { "content-type": "application/json", "x-admin-token": token(), ...(opts.headers || {}) };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  return fetch(path, o).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) throw new AuthError(data.detail || "unauthorized");
    if (!r.ok) throw new Error(data.detail || r.statusText);
    return data;
  });
}

class AuthError extends Error {}

/* ---------------- toast ---------------- */
function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  $("#toastRoot").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3800);
  setTimeout(() => el.remove(), 4200);
}

/* ---------------- modal stack ---------------- */
let stack = [];

function paintModal() {
  const root = $("#modalRoot");
  if (!stack.length) { root.hidden = true; root.innerHTML = ""; return; }
  root.hidden = false;
  root.innerHTML = '<div class="modal-backdrop"></div><div class="modal" role="dialog" aria-modal="true"></div>';
  const modal = $(".modal", root);
  stack[stack.length - 1](modal);
  $(".modal-backdrop", root).onclick = () => dismissModal();
  const focusable = $("input,select,button", modal);
  if (focusable) focusable.focus();
}
function openModal(builder) { stack.push(builder); paintModal(); }
function dismissModal() { stack = []; paintModal(); }

function dialog(title, sub, fields, onOk, okLabel = "Save") {
  openModal((modal) => {
    modal.innerHTML = `
      <h3>${esc(title)}</h3>
      ${sub ? `<div class="modal-sub">${esc(sub)}</div>` : ""}
      <form id="dlg" autocomplete="off">
        ${fields}
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-cancel>Cancel</button>
          <button type="submit" class="btn">${esc(okLabel)}</button>
        </div>
      </form>`;
    $("[data-cancel]", modal).onclick = dismissModal;
    $("#dlg", modal).onsubmit = async (e) => {
      e.preventDefault();
      try { await onOk(modal); dismissModal(); refresh(); }
      catch (err) { toast(err.message || "failed", "bad"); }
    };
  });
}

function confirmDialog(message, okLabel = "Confirm", danger = false) {
  return new Promise((resolve) => {
    openModal((modal) => {
      modal.innerHTML = `
        <h3>Are you sure?</h3>
        <div class="modal-sub">${esc(message)}</div>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-no>Cancel</button>
          <button type="button" class="btn ${danger ? "danger" : ""}" data-yes>${esc(okLabel)}</button>
        </div>`;
      $("[data-no]", modal).onclick = () => { dismissModal(); resolve(false); };
      $("[data-yes]", modal).onclick = () => { dismissModal(); resolve(true); };
    });
  });
}

/* ---------------- navigation ---------------- */
function switchTab(name) {
  $$(".nav-item").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("is-active", t.id === "tab-" + name));
}

/* ---------------- action dispatch ---------------- */
document.addEventListener("click", (e) => {
  const nav = e.target.closest(".nav-item");
  if (nav) return switchTab(nav.dataset.tab);
  const act = e.target.closest("[data-action]");
  if (!act) return;
  handle(act.dataset.action, act);
});

const ACTIONS = {
  "new-provider": () => newProviderModal(),
  "new-model": () => newModelModal(),
  "new-gw-key": async () => {
    const r = await api("/admin/api/gateway-keys", { method: "POST", body: { label: "client" } });
    await refresh();
    toast("New gateway key: " + r.key, "ok");
  },
  "save-token": () => { localStorage.setItem(TOKEN_KEY, $("#adminToken").value.trim()); toast("Token saved", "ok"); refresh(); startStream(); },
  "export-config": async () => {
    const data = await api("/admin/api/export?reveal=1");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "llm-router-config.json";
    a.click();
    URL.revokeObjectURL(a.href);
  },
  "import-config": () => $("#importFile").click(),
  "clear-logs": async () => { await api("/admin/api/logs", { method: "DELETE" }); refresh(); },

  "del-key": (el) => withConfirm("Delete this key?", () => api(`/admin/api/keys/${el.dataset.id}`, { method: "DELETE" })),
  "toggle-key": (el) => api(`/admin/api/keys/${el.dataset.id}`, { method: "PATCH", body: { enabled: +el.dataset.next } }).then(refresh),
  "reset-key": (el) => api(`/admin/api/keys/${el.dataset.id}/reset`, { method: "POST" }).then(refresh),
  "add-key": (el) => newKeyModal(+el.dataset.id),
  "toggle-provider": (el) => api(`/admin/api/providers/${el.dataset.id}`, { method: "PATCH", body: { enabled: +el.dataset.next } }).then(refresh),
  "del-provider": (el) => withConfirm("Delete this provider and all of its keys?", () => api(`/admin/api/providers/${el.dataset.id}`, { method: "DELETE" })),
  "add-target": (el) => newTargetModal(+el.dataset.id),
  "del-model": (el) => withConfirm("Delete this virtual model?", () => api(`/admin/api/vmodels/${el.dataset.id}`, { method: "DELETE" })),
  "test-model": (el) => testModel(el.dataset.name),
  "toggle-target": (el) => api(`/admin/api/targets/${el.dataset.id}`, { method: "PATCH", body: { enabled: +el.dataset.next } }).then(refresh),
  "del-target": (el) => withConfirm("Remove this target?", () => api(`/admin/api/targets/${el.dataset.id}`, { method: "DELETE" })),
  "del-gw": (el) => withConfirm("Revoke this gateway key?", () => api(`/admin/api/gateway-keys/${el.dataset.id}`, { method: "DELETE" })),
};

function handle(name, el) {
  const fn = ACTIONS[name];
  if (!fn) return;
  Promise.resolve(fn(el)).catch((err) => toast(err.message || "request failed", "bad"));
}
function withConfirm(msg, fn) { return confirmDialog(msg, "Delete", true).then((ok) => (ok ? fn().then(refresh) : null)); }

/* ---------------- provider / key modals ---------------- */
function presetOptions() {
  return Object.entries(STATE.presets || {})
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join("");
}

function newProviderModal() {
  dialog("Add provider", "A provider is an upstream base URL and wire format.", `
    <div class="field"><label>Preset</label><select id="prPreset">${presetOptions()}</select></div>
    <div class="field"><label>Name (unique)</label><input id="prName" placeholder="groq-main" required></div>
    <div class="field"><label>Adapter</label><select id="prAdapter"><option>openai</option><option>anthropic</option><option>gemini</option></select></div>
    <div class="field"><label>Base URL</label><input id="prBase" placeholder="https://host/v1" required></div>
    <div class="field"><label>Extra headers (JSON)</label><input id="prHeaders" value="{}"></div>`,
    async (modal) => {
      const headers = JSON.parse($("#prHeaders", modal).value || "{}");
      await api("/admin/api/providers", { method: "POST", body: {
        preset: $("#prPreset", modal).value,
        name: $("#prName", modal).value.trim(),
        adapter: $("#prAdapter", modal).value,
        base_url: $("#prBase", modal).value.trim(),
        headers,
      }});
      toast("Provider added", "ok");
    });
  const modal = $(".modal");
  const fill = () => {
    const p = (STATE.presets || {})[$("#prPreset", modal).value] || {};
    $("#prBase", modal).value = p.base_url || "";
    $("#prAdapter", modal).value = p.adapter || "openai";
    if (!$("#prName", modal).value) $("#prName", modal).value = $("#prPreset", modal).value;
  };
  $("#prPreset", modal).onchange = fill;
  fill();
}

function newKeyModal(pid) {
  dialog("Add API key", "Each key carries its own budget and cooldown.", `
    <div class="field"><label>Label</label><input id="kLabel" placeholder="acct-1"></div>
    <div class="field"><label>API key</label><input id="kKey" placeholder="sk-…" required></div>
    <div class="grid3">
      <div class="field"><label>Weight</label><input id="kWeight" type="number" value="1" step="0.1"></div>
      <div class="field"><label>Priority</label><input id="kPrio" type="number" value="100"></div>
      <div class="field"><label>Max concurrency</label><input id="kConc" type="number" value="0"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>Max RPM (0 = none)</label><input id="kRpm" type="number" value="0"></div>
      <div class="field"><label>Max TPM (0 = none)</label><input id="kTpm" type="number" value="0"></div>
    </div>`,
    async (modal) => {
      await api("/admin/api/keys", { method: "POST", body: {
        provider_id: pid,
        label: $("#kLabel", modal).value.trim(),
        api_key: $("#kKey", modal).value.trim(),
        weight: parseFloat($("#kWeight", modal).value || "1"),
        priority: parseInt($("#kPrio", modal).value || "100", 10),
        max_concurrency: parseInt($("#kConc", modal).value || "0", 10),
        max_rpm: parseInt($("#kRpm", modal).value || "0", 10),
        max_tpm: parseInt($("#kTpm", modal).value || "0", 10),
      }});
      toast("Key added", "ok");
    }, "Add");
}

/* ---------------- model / target modals ---------------- */
function newModelModal() {
  dialog("New virtual model", "This is the model name your app sends.", `
    <div class="field"><label>Model name</label><input id="mName" placeholder="fast-chat" required></div>
    <div class="field"><label>Description</label><input id="mDesc" placeholder="optional"></div>
    <div class="field"><label>Strategy</label><select id="mStrat">
      <option value="balanced">balanced — priority tiering + weighted round robin</option>
      <option value="priority">priority — strict failover order</option>
      <option value="round_robin">round robin</option>
      <option value="least_latency">least latency</option>
      <option value="random">random</option>
    </select></div>
    <div class="field"><label>Sticky sessions</label>
      <select id="mSticky"><option value="1">on — pin a conversation to one key (better cache hits)</option>
      <option value="0">off — rotate freely</option></select></div>`,
    async (modal) => {
      await api("/admin/api/vmodels", { method: "POST", body: {
        name: $("#mName", modal).value.trim(),
        description: $("#mDesc", modal).value.trim(),
        strategy: $("#mStrat", modal).value,
        sticky: $("#mSticky", modal).value === "1",
      }});
      toast("Model created", "ok");
    }, "Create");
}

function newTargetModal(vid) {
  const provs = (STATE.providers || [])
    .map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  dialog("Add target", "Point the model at an upstream model on one provider.", `
    <div class="field"><label>Provider</label><select id="tProv">${provs}</select></div>
    <div class="field"><label>Key</label><select id="tKey"><option value="">any key in provider pool</option></select></div>
    <div class="field"><label>Upstream model</label>
      <div style="display:flex;gap:8px">
        <input id="tModel" list="tModelList" placeholder="e.g. a model id on that provider" required>
        <button type="button" class="btn ghost" data-load>Load list</button>
      </div>
      <datalist id="tModelList"></datalist>
    </div>
    <div class="grid2">
      <div class="field"><label>Weight</label><input id="tWeight" type="number" value="1" step="0.1"></div>
      <div class="field"><label>Priority</label><input id="tPrio" type="number" value="100"></div>
    </div>
    <div class="grid2">
      <div class="field"><label>Target RPM cap</label><input id="tRpm" type="number" value="0"></div>
      <div class="field"><label>Target TPM cap</label><input id="tTpm" type="number" value="0"></div>
    </div>`,
    async (modal) => {
      await api("/admin/api/targets", { method: "POST", body: {
        vmodel_id: vid,
        provider_id: parseInt($("#tProv", modal).value, 10),
        key_id: $("#tKey", modal).value || null,
        upstream_model: $("#tModel", modal).value.trim(),
        weight: parseFloat($("#tWeight", modal).value || "1"),
        priority: parseInt($("#tPrio", modal).value || "100", 10),
        max_rpm: parseInt($("#tRpm", modal).value || "0", 10),
        max_tpm: parseInt($("#tTpm", modal).value || "0", 10),
      }});
      toast("Target added", "ok");
    }, "Add");

  const modal = $(".modal");
  const fillKeys = () => {
    const pid = parseInt($("#tProv", modal).value, 10);
    const keys = (STATE.keys || []).filter((k) => k.provider_id === pid);
    $("#tKey", modal).innerHTML = '<option value="">any key in provider pool</option>' +
      keys.map((k) => `<option value="${k.id}">${esc(k.label || "key#" + k.id)} (${esc(k.masked)})</option>`).join("");
  };
  $("#tProv", modal).onchange = fillKeys;
  fillKeys();
  $("[data-load]", modal).onclick = async () => {
    try {
      const r = await api(`/admin/api/providers/${$("#tProv", modal).value}/models`);
      if (!r.models || !r.models.length) return toast("No model list from this provider", "warn");
      $("#tModelList", modal).innerHTML = r.models.map((m) => `<option value="${esc(m)}"></option>`).join("");
      $("#tModel", modal).focus();
      toast(r.models.length + " models loaded", "ok");
    } catch (err) { toast(err.message || "failed", "bad"); }
  };
}

async function testModel(name) {
  toast("Testing " + name + "…");
  try {
    const r = await api("/admin/api/test", { method: "POST", body: { model: name } });
    if (r.ok) toast(`OK via ${r.provider} / ${r.key} · ${r.attempts} attempt(s) · ${r.ms} ms · ${r.reply}`, "ok");
    else toast("Failed: " + r.error, "bad");
  } catch (err) { toast(err.message || "failed", "bad"); }
}

/* ---------------- import ---------------- */
document.addEventListener("DOMContentLoaded", () => {
  const file = $("#importFile");
  if (file) file.onchange = async () => {
    const f = file.files[0];
    if (!f) return;
    const text = await f.text();
    const wipe = await confirmDialog("Replace existing config? Cancel will merge instead.", "Replace", false);
    try {
      await api("/admin/api/import?wipe=" + (wipe ? 1 : 0), { method: "POST", body: JSON.parse(text) });
      toast("Config imported", "ok");
      refresh();
    } catch (err) { toast(err.message || "import failed", "bad"); }
    file.value = "";
  };
});

/* ---------------- renderers ---------------- */
function providerName(id) {
  const p = (STATE.providers || []).find((x) => x.id === id);
  return p ? p.name : "—";
}

function statusPill(enabled, cooldown) {
  if (!enabled) return '<span class="pill off">disabled</span>';
  if (cooldown > 0) return `<span class="pill warn">cooling ${Math.ceil(cooldown)}s</span>`;
  return '<span class="pill ok">ready</span>';
}

function renderDash() {
  const s = STATE.stats || {};
  const tiles = [
    ["Requests · 5m", s.requests_5m ?? 0, "rolling window"],
    ["Success", (s.success_rate ?? 100) + "%", "of recent calls"],
    ["Avg latency", (s.avg_latency ?? 0) + " ms", "end to end"],
    ["Active keys", `${s.keys_enabled ?? 0}/${s.keys_total ?? 0}`, "enabled / total"],
    ["Models", s.models ?? 0, "virtual"],
  ];
  $("#statCards").innerHTML = tiles.map(([k, v, d]) =>
    `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d">${esc(d)}</div></div>`).join("");

  const rows = (STATE.keys || []).map((k) => {
    const h = k.health || {};
    const pct = k.max_rpm ? Math.min(100, Math.round(100 * (h.rpm_used || 0) / k.max_rpm)) : 0;
    const cls = pct > 85 ? "bad" : pct > 60 ? "warn" : "";
    return `<tr>
      <td>${esc(providerName(k.provider_id))}</td>
      <td class="mono">${esc(k.label || "key#" + k.id)}</td>
      <td class="mono">${esc(k.masked)}</td>
      <td>${statusPill(k.enabled, h.cooldown || 0)}</td>
      <td>${k.max_rpm ? `<div class="bar"><i class="${cls}" style="width:${pct}%"></i></div>` : '<span class="mono">—</span>'}
          <span class="mono">${h.rpm_used || 0}${k.max_rpm ? "/" + k.max_rpm : ""} rpm</span></td>
      <td class="num">${h.latency_ema || "—"}</td>
      <td class="mono">${esc(h.last_error || "")}</td>
    </tr>`;
  }).join("");
  $("#keyHealth").innerHTML = `<table><thead><tr>
      <th>Provider</th><th>Key</th><th>Value</th><th>State</th><th>RPM window</th><th>Latency</th><th>Last note</th>
    </tr></thead><tbody>${rows || '<tr><td colspan="7">No keys configured yet.</td></tr>'}</tbody></table>`;
  $("#healthNote").textContent = `${(STATE.keys || []).length} key(s) tracked`;
}

function renderProviders() {
  const host = $("#providers");
  if (!(STATE.providers || []).length) {
    host.innerHTML = '<div class="empty">No providers yet — add one to start routing.</div>';
    return;
  }
  host.innerHTML = (STATE.providers || []).map((p) => {
    const keys = (STATE.keys || []).filter((k) => k.provider_id === p.id);
    const keyRows = keys.length ? keys.map((k) => {
      const h = k.health || {};
      return `<div class="row-item">
        <span class="mono">${esc(k.label || "key#" + k.id)}</span>
        <span class="mono" style="color:var(--muted)">${esc(k.masked)}</span>
        ${statusPill(k.enabled, h.cooldown || 0)}
        <span class="mono" style="color:var(--muted)">w${esc(k.weight)} · p${esc(k.priority)}${k.max_rpm ? " · " + esc(k.max_rpm) + "rpm" : ""}${k.max_tpm ? " · " + esc(k.max_tpm) + "tpm" : ""}</span>
        <span class="spacer"></span>
        <button class="btn sm ghost" data-action="reset-key" data-id="${k.id}">reset</button>
        <button class="btn sm ghost" data-action="toggle-key" data-id="${k.id}" data-next="${k.enabled ? 0 : 1}">${k.enabled ? "disable" : "enable"}</button>
        <button class="btn sm danger" data-action="del-key" data-id="${k.id}">remove</button>
      </div>`;
    }).join("") : '<div class="hint">No keys yet — this provider will be skipped.</div>';
    return `<div class="panel">
      <div class="panel-head">
        <h3>${esc(p.name)}</h3>
        <span class="tag">${esc(p.adapter)}</span>
        ${p.enabled ? "" : '<span class="pill off">disabled</span>'}
        <span class="spacer"></span>
        <button class="btn sm" data-action="add-key" data-id="${p.id}">Add key</button>
        <button class="btn sm ghost" data-action="toggle-provider" data-id="${p.id}" data-next="${p.enabled ? 0 : 1}">${p.enabled ? "disable" : "enable"}</button>
        <button class="btn sm danger" data-action="del-provider" data-id="${p.id}">delete</button>
      </div>
      <div class="panel-sub">${esc(p.base_url)}</div>
      ${keyRows}
    </div>`;
  }).join("");
}

function renderModels() {
  const host = $("#models");
  if (!(STATE.vmodels || []).length) {
    host.innerHTML = '<div class="empty">No virtual models yet — create one and point it at a target.</div>';
    return;
  }
  host.innerHTML = (STATE.vmodels || []).map((v) => {
    const targets = (v.targets || []);
    const rows = targets.length ? targets.map((t) => `<div class="row-item">
        <span>${esc(providerName(t.provider_id))}</span>
        <span class="mono">${esc(t.upstream_model)}</span>
        <span class="mono" style="color:var(--muted)">${t.key_id ? "key#" + esc(t.key_id) : "any key"}</span>
        <span class="mono" style="color:var(--muted)">w${esc(t.weight)} · p${esc(t.priority)}</span>
        ${t.enabled ? "" : '<span class="pill off">off</span>'}
        <span class="spacer"></span>
        <button class="btn sm ghost" data-action="toggle-target" data-id="${t.id}" data-next="${t.enabled ? 0 : 1}">${t.enabled ? "disable" : "enable"}</button>
        <button class="btn sm danger" data-action="del-target" data-id="${t.id}">remove</button>
      </div>`).join("") : '<div class="hint">No targets yet — this model will return 429 until one is added.</div>';
    return `<div class="panel">
      <div class="panel-head">
        <h3 class="mono">${esc(v.name)}</h3>
        <span class="tag">${esc(v.strategy)}</span>
        ${v.sticky ? '<span class="tag">sticky</span>' : ""}
        ${v.enabled ? "" : '<span class="pill off">disabled</span>'}
        <span class="spacer"></span>
        <button class="btn sm" data-action="add-target" data-id="${v.id}">Add target</button>
        <button class="btn sm ghost" data-action="test-model" data-name="${esc(v.name)}">Test</button>
        <button class="btn sm danger" data-action="del-model" data-id="${v.id}">delete</button>
      </div>
      ${v.description ? `<div class="panel-sub">${esc(v.description)}</div>` : ""}
      ${rows}
    </div>`;
  }).join("");
}

function renderLogs() {
  const feed = $("#logFeed");
  const rows = (STATE.logs || []).slice().reverse();
  feed.innerHTML = rows.map((l) => {
    const cls = (l.status || 500) < 400 ? "ok" : "bad";
    const err = l.error ? "  ⚠ " + esc(l.error) : "";
    return `<div class="ln"><span class="t">${esc(new Date(l.ts * 1000).toLocaleTimeString())}</span>  ` +
      `<span class="sys">${esc(l.vmodel)}</span> → ${esc(l.provider)}/${esc(l.key_label)}  ` +
      `<span class="${cls}">${esc(l.status)}</span>  ${esc(l.latency_ms)}ms  a${esc(l.attempts)}${err}</div>`;
  }).join("");
  feed.scrollTop = feed.scrollHeight;
}

function renderSettings() {
  $("#baseUrl").textContent = location.origin + "/v1";
  const adminInput = $("#adminToken");
  if (document.activeElement !== adminInput) adminInput.value = token();
  $("#envChip").innerHTML = `env:&nbsp;<b>${esc(STATE.meta?.env || "—")}</b>`;
  $("#encChip").innerHTML = `at-rest:&nbsp;<b>${STATE.meta?.encryption ? "encrypted" : "plaintext"}</b>`;

  const gws = (STATE.gateway_keys || []);
  const host = $("#gwKeys");
  host.innerHTML = gws.length ? gws.map((g) => `<div class="row-item">
      <span class="mono">${esc(g.masked)}</span>
      <span class="mono" style="color:var(--muted)">${esc(g.label)}</span>
      <span class="spacer"></span>
      <button class="btn sm ghost" data-copy="${esc(g.key)}">copy</button>
      <button class="btn sm danger" data-action="del-gw" data-id="${g.id}">revoke</button>
    </div>`).join("") : '<div class="empty">No gateway keys.</div>';
  $$("[data-copy]", host).forEach((b) => {
    b.addEventListener("click", () => {
      navigator.clipboard.writeText(b.dataset.copy).then(
        () => toast("Copied to clipboard", "ok"),
        () => toast("Copy failed", "bad"));
    });
  });
}

/* ---------------- live stream ---------------- */
function startStream() {
  if (stream) stream.close();
  stream = new EventSource("/admin/api/stream?token=" + encodeURIComponent(token()));
  const setStatus = (on) => {
    const s = $("#statusDot");
    s.classList.toggle("on", on);
    s.classList.toggle("off", !on);
    $("#statusText").textContent = on ? "live" : "offline";
  };
  stream.onopen = () => setStatus(true);
  stream.onerror = () => setStatus(false);
  stream.onmessage = (e) => {
    let item;
    try { item = JSON.parse(e.data); } catch { return; }
    const feed = $("#logFeed");
    const line = document.createElement("div");
    line.className = "ln";
    const kindCls = item.kind === "cooldown" ? "warn" : item.kind === "ok" ? "ok" : "sys";
    line.innerHTML = `<span class="t">${esc(new Date(item.ts * 1000).toLocaleTimeString())}</span>  ` +
      `<span class="${kindCls}">${esc((item.kind || "").toUpperCase())}</span>  ` +
      `${esc(item.model || "")} ${esc(item.provider || "")}/${esc(item.key || "")} ` +
      `${item.ms ? esc(item.ms) + "ms" : ""} ${item.seconds ? "cool " + esc(item.seconds) + "s" : ""} ${esc(item.reason || "")}`;
    feed.appendChild(line);
    while (feed.childElementCount > 500) feed.removeChild(feed.firstChild);
    feed.scrollTop = feed.scrollHeight;
  };
}

/* ---------------- refresh ---------------- */
async function refresh() {
  try {
    STATE = await api("/admin/api/state");
    renderDash();
    renderProviders();
    renderModels();
    renderLogs();
    renderSettings();
  } catch (err) {
    if (err instanceof AuthError) {
      const s = $("#statusDot");
      s.classList.remove("on"); s.classList.add("off");
      $("#statusText").textContent = "locked";
    }
  }
}

refresh();
setInterval(refresh, 5000);
startStream();
