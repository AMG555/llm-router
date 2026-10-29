"use strict";

/* ============================================================
   LLM Router — console
   ============================================================ */
const TOKEN_KEY = "llmrouter.admin";
const THEME_KEY = "llmrouter.theme";
const MAX_FEED = 600;

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const nf = new Intl.NumberFormat();

const ROUTES = ["overview", "models", "providers", "traffic", "settings"];

let STATE = {
  providers: [], keys: [], vmodels: [], gateway_keys: [], logs: [],
  stats: {}, meta: {}, presets: {}, loaded: false,
};
const UI = {
  route: "overview", feed: [], paused: false, online: true, seeded: false,
  expanded: new Set(),
  q: { key: "", model: "", provider: "", level: "", log: "" },
};

/* ---------------- utilities ---------------- */
function esc(value) {
  return String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function debounce(fn, ms = 180) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
function clockTime(ts) {
  return new Date((ts || 0) * 1000).toLocaleTimeString();
}
function relTime(ts) {
  const d = Date.now() / 1000 - (ts || 0);
  if (d < 1) return "just now";
  if (d < 60) return `${Math.floor(d)}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
function msLabel(ms) { return ms === null || ms === undefined ? "—" : `${ms} ms`; }

/* ---------------- api ---------------- */
class ApiError extends Error {}
class AuthError extends ApiError {}

function token() { return localStorage.getItem(TOKEN_KEY) || ""; }

function api(path, opts = {}) {
  const o = { ...opts };
  o.headers = { "content-type": "application/json", "x-admin-token": token(), ...(opts.headers || {}) };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  return fetch(path, o).then(async (r) => {
    let data = {};
    try { data = await r.json(); } catch (_) { /* no body */ }
    if (r.status === 401) throw new AuthError((data && data.detail) || "unauthorized");
    if (!r.ok) throw new ApiError((data && data.detail) || r.statusText || `HTTP ${r.status}`);
    return data;
  });
}

/* ---------------- toast ---------------- */
function toast(title, message = "", kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<div><div class="tt">${esc(title)}</div>${message ? `<div class="tm">${esc(message)}</div>` : ""}</div>`;
  const x = document.createElement("button");
  x.className = "x"; x.setAttribute("aria-label", "Dismiss"); x.textContent = "✕";
  x.addEventListener("click", () => el.remove());
  el.appendChild(x);
  $("#toastRoot").appendChild(el);
  const life = kind === "bad" ? 7000 : 4200;
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, life);
  setTimeout(() => el.remove(), life + 400);
}

/* ---------------- modal system ---------------- */
let modalStack = [];

function paintModal() {
  const root = $("#modalRoot");
  if (!modalStack.length) { root.hidden = true; root.innerHTML = ""; document.body.style.overflow = ""; return; }
  root.hidden = false; document.body.style.overflow = "hidden";
  root.innerHTML = '<div class="backdrop"></div><div class="modal" role="dialog" aria-modal="true" tabindex="-1"></div>';
  const modal = $(".modal", root);
  modalStack[modalStack.length - 1](modal);
  $(".backdrop", root).addEventListener("click", closeModal);
  modal.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const f = $$('a[href],button:not([disabled]),input:not([disabled]),select,textarea,[tabindex]:not([tabindex="-1"])', modal)
      .filter((n) => n.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  const focusable = $("input,select,textarea", modal) || $("button", modal);
  if (focusable) focusable.focus();
}
function openModal(builder) { modalStack.push(builder); paintModal(); }
function closeModal() { modalStack = []; paintModal(); }

function confirmDialog(message, { title = "Are you sure?", confirmLabel = "Confirm", danger = true } = {}) {
  return new Promise((resolve) => {
    openModal((modal) => {
      modal.innerHTML = `
        <h3>${esc(title)}</h3>
        <div class="sub">${esc(message)}</div>
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-no>Cancel</button>
          <button type="button" class="btn ${danger ? "danger" : ""}" data-yes>${esc(confirmLabel)}</button>
        </div>`;
      $("[data-no]", modal).addEventListener("click", () => { closeModal(); resolve(false); });
      $("[data-yes]", modal).addEventListener("click", () => { closeModal(); resolve(true); });
    });
  });
}

function formDialog({ title, sub, body, submitLabel = "Save", onSubmit }) {
  openModal((modal) => {
    modal.innerHTML = `
      <button class="icon-btn modal-close" data-close aria-label="Close">✕</button>
      <h3>${esc(title)}</h3>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}
      <form novalidate>${body}
        <div class="modal-actions">
          <button type="button" class="btn ghost" data-close>Cancel</button>
          <button type="submit" class="btn">${esc(submitLabel)}</button>
        </div>
      </form>`;
    $$("[data-close]", modal).forEach((b) => b.addEventListener("click", closeModal));
    modal.querySelector("form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = modal.querySelector('button[type="submit"]');
      btn.disabled = true;
      try { await onSubmit(modal); closeModal(); await refresh(); }
      catch (err) { toast("Request failed", err.message || "unexpected error", "bad"); }
      finally { btn.disabled = false; }
    });
  });
}

/* ---------------- clipboard ---------------- */
function copy(text, label = "Copied to clipboard") {
  const done = () => toast(label, "", "ok");
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => toast("Copy failed", "", "bad"));
  } else {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (_) { toast("Copy failed", "", "bad"); }
    ta.remove();
  }
}

/* ---------------- command palette ---------------- */
const ACTIONS = {
  "goto-overview": { group: "Navigate", label: "Go to Overview", run: () => go("overview") },
  "goto-models": { group: "Navigate", label: "Go to Virtual models", run: () => go("models") },
  "goto-providers": { group: "Navigate", label: "Go to Providers & keys", run: () => go("providers") },
  "goto-traffic": { group: "Navigate", label: "Go to Traffic", run: () => go("traffic") },
  "goto-settings": { group: "Navigate", label: "Go to Settings", run: () => go("settings") },
  "act-new-model": { group: "Action", label: "Create virtual model", run: () => newModelModal() },
  "act-new-provider": { group: "Action", label: "Add provider", run: () => newProviderModal() },
  "act-bulk": { group: "Action", label: "Bulk add keys", run: () => bulkKeysModal() },
  "act-gwkey": { group: "Action", label: "Generate gateway key", run: () => gwKeyModal(null) },
  "act-export": { group: "Action", label: "Export configuration", run: () => exportConfig() },
  "act-theme": { group: "Action", label: "Toggle color theme", run: () => toggleTheme() },
  "act-refresh": { group: "Action", label: "Refresh now", run: () => refresh() },
};

function openPalette() {
  const root = $("#paletteRoot");
  root.hidden = false;
  root.innerHTML = `<div class="backdrop"></div><div class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
    <input id="palInput" placeholder="Type a command…" autocomplete="off" aria-label="Command">
    <div class="palette-list" id="palList"></div></div>`;
  const input = $("#palInput");
  let items = [], idx = 0;

  const render = () => {
    const q = input.value.trim().toLowerCase();
    items = Object.entries(ACTIONS)
      .map(([id, a]) => ({ id, ...a }))
      .filter((a) => !q || a.label.toLowerCase().includes(q) || a.group.toLowerCase().includes(q));
    idx = Math.min(idx, Math.max(0, items.length - 1));
    const list = $("#palList");
    list.innerHTML = items.length
      ? items.map((a, i) => `<div class="palette-item ${i === idx ? "active" : ""}" data-i="${i}">
          <span>${esc(a.label)}</span><span class="grp">${esc(a.group)}</span></div>`).join("")
      : `<div class="palette-empty">No matching commands</div>`;
    $$(".palette-item", list).forEach((el) => el.addEventListener("click", () => run(+el.dataset.i)));
  };
  const run = (i) => {
    const a = items[i]; if (!a) return;
    closePalette(); a.run();
  };
  input.addEventListener("input", () => { idx = 0; render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { idx = Math.min(idx + 1, items.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = Math.max(idx - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter") { run(idx); e.preventDefault(); }
    else if (e.key === "Escape") closePalette();
  });
  $(".backdrop", root).addEventListener("click", closePalette);
  render(); input.focus();
}
function closePalette() { const r = $("#paletteRoot"); r.hidden = true; r.innerHTML = ""; }

/* ---------------- theme ---------------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem(THEME_KEY, t);
}
function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
}
function initTheme() {
  const saved = localStorage.getItem(THEME_KEY);
  const prefers = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefers ? "dark" : "light"));
}

/* ---------------- routing ---------------- */
function go(name) { location.hash = "#/" + name; }
function route() {
  const name = (location.hash || "").replace(/^#\//, "").split("?")[0];
  UI.route = ROUTES.includes(name) ? name : "overview";
  $$(".page").forEach((p) => p.classList.toggle("active", p.dataset.page === UI.route));
  $$(".nav-item").forEach((n) => {
    const on = n.dataset.route === UI.route;
    n.classList.toggle("active", on);
    if (on) n.setAttribute("aria-current", "page"); else n.removeAttribute("aria-current");
  });
  document.title = `LLM Router — ${UI.route[0].toUpperCase()}${UI.route.slice(1)}`;
  $("#sidebar").classList.remove("open");
  if (UI.route === "traffic") renderFeed();
}

/* ---------------- event delegation ---------------- */
document.addEventListener("click", (e) => {
  const nav = e.target.closest(".nav-item");
  if (nav) return go(nav.dataset.route);
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const name = el.dataset.action;
  const fn = HANDLERS[name];
  if (!fn) return;
  Promise.resolve(fn(el)).catch(handleError);
});

const HANDLERS = {
  "toggle-nav": () => $("#sidebar").classList.toggle("open"),
  "palette": () => openPalette(),
  "theme": () => toggleTheme(),
  "refresh": () => refresh(),
  "new-model": () => newModelModal(),
  "new-provider": () => newProviderModal(),
  "bulk-keys": () => bulkKeysModal(),
  "new-gw-key": () => gwKeyModal(null),
  "edit-gw": (el) => { const g = (STATE.gateway_keys || []).find((x) => x.id === +el.dataset.id); if (g) gwKeyModal(g); },
  "save-token": () => { localStorage.setItem(TOKEN_KEY, $("#adminToken").value.trim()); toast("Token saved", "", "ok"); refresh(); connectStream(); },
  "export-config": () => exportConfig(),
  "copy-bootstrap": () => api("/admin/api/bootstrap-b64").then((r) => copy(r.b64, "BOOTSTRAP_CONFIG copied")).catch(handleError),
  "import-config": () => $("#importFile").click(),
  "clear-logs": () => clearLogs(),
  "pause-feed": (el) => togglePause(el),
  "toggle-entity": (el) => toggleEntity(el),
  "copy-base": () => copy(location.origin + "/v1", "Base URL copied"),
  "egress": () => probeEgress(),

  "add-key": (el) => newKeyModal(+el.dataset.id),
  "del-key": (el) => confirmDialog("Delete this key? In-flight requests continue.", { confirmLabel: "Delete" }).then((ok) => ok && api(`/admin/api/keys/${el.dataset.id}`, { method: "DELETE" }).then(refresh)),
  "toggle-key": (el) => api(`/admin/api/keys/${el.dataset.id}`, { method: "PATCH", body: { enabled: +el.dataset.next } }).then(refresh),
  "reset-key": (el) => api(`/admin/api/keys/${el.dataset.id}/reset`, { method: "POST" }).then(() => { toast("Key reset", "", "ok"); refresh(); }),
  "toggle-provider": (el) => api(`/admin/api/providers/${el.dataset.id}`, { method: "PATCH", body: { enabled: +el.dataset.next } }).then(refresh),
  "del-provider": (el) => confirmDialog("Delete this provider and all of its keys?", { confirmLabel: "Delete" }).then((ok) => ok && api(`/admin/api/providers/${el.dataset.id}`, { method: "DELETE" }).then(refresh)),
  "add-target": (el) => newTargetModal(+el.dataset.id),
  "del-model": (el) => confirmDialog("Delete this virtual model?", { confirmLabel: "Delete" }).then((ok) => ok && api(`/admin/api/vmodels/${el.dataset.id}`, { method: "DELETE" }).then(refresh)),
  "test-model": (el) => testModel(el.dataset.name),
  "toggle-target": (el) => api(`/admin/api/targets/${el.dataset.id}`, { method: "PATCH", body: { enabled: +el.dataset.next } }).then(refresh),
  "del-target": (el) => confirmDialog("Remove this target?", { confirmLabel: "Remove" }).then((ok) => ok && api(`/admin/api/targets/${el.dataset.id}`, { method: "DELETE" }).then(refresh)),
  "del-gw": (el) => confirmDialog("Revoke this gateway key? Clients using it will stop working.", { confirmLabel: "Revoke" }).then((ok) => ok && api(`/admin/api/gateway-keys/${el.dataset.id}`, { method: "DELETE" }).then(refresh)),
  "new-admin-token": () => adminTokenModal(),
  "del-admin-token": (el) => confirmDialog("Revoke this admin user?", { confirmLabel: "Revoke" }).then((ok) => ok && api(`/admin/api/admin-tokens/${el.dataset.id}`, { method: "DELETE" }).then(refresh)),
  "copy-key": (el) => copy(el.dataset.value, "Key copied"),
};

function handleError(err) {
  if (err instanceof AuthError) { setConn("locked"); openTokenModal(); return; }
  toast("Request failed", err.message || "unexpected error", "bad");
}

/* ---------------- forms ---------------- */
function presetOptions() {
  return Object.entries(STATE.presets || {})
    .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label || k)}</option>`).join("");
}

function newProviderModal() {
  formDialog({
    title: "Add provider",
    sub: "A provider is an upstream base URL and wire format.",
    submitLabel: "Create",
    body: `
      <div class="field"><label for="ppPreset">Preset</label><select id="ppPreset">${presetOptions()}</select></div>
      <div class="field"><label for="ppName">Name (unique)</label><input id="ppName" placeholder="acme-main" required></div>
      <div class="field"><label for="ppAdapter">Adapter</label><select id="ppAdapter"><option>openai</option><option>anthropic</option><option>gemini</option></select></div>
      <div class="field"><label for="ppBase">Base URL</label><input id="ppBase" placeholder="https://host/v1" required></div>
      <div class="field"><label for="ppHeaders">Extra headers (JSON)</label><input id="ppHeaders" value="{}"></div>
      <div class="field"><label for="ppProxy">Egress proxy (optional)</label><input id="ppProxy" placeholder="http://user:pass@fixed-ip:3128"><div class="help">Route this provider's calls through a fixed, allowlisted IP.</div></div>`,
    onSubmit: async (modal) => {
      const headers = JSON.parse($("#ppHeaders", modal).value || "{}");
      await api("/admin/api/providers", { method: "POST", body: {
        preset: $("#ppPreset", modal).value, name: $("#ppName", modal).value.trim(),
        adapter: $("#ppAdapter", modal).value, base_url: $("#ppBase", modal).value.trim(),
        headers, proxy: $("#ppProxy", modal).value.trim(),
      }});
      toast("Provider added", "", "ok");
    },
  });
  const modal = $(".modal");
  const fill = () => {
    const p = (STATE.presets || {})[$("#ppPreset", modal).value] || {};
    $("#ppBase", modal).value = p.base_url || "";
    $("#ppAdapter", modal).value = p.adapter || "openai";
    if (!$("#ppName", modal).value) $("#ppName", modal).value = $("#ppPreset", modal).value;
  };
  $("#ppPreset", modal).addEventListener("change", fill);
  fill();
}

function keyFields(idp) {
  return `
    <div class="field"><label for="${idp}Label">Label</label><input id="${idp}Label" placeholder="acct-1"></div>
    <div class="field"><label for="${idp}Key">API key</label><input id="${idp}Key" placeholder="sk-…" required autocomplete="off"></div>
    <div class="grid-3">
      <div class="field"><label for="${idp}Weight">Weight</label><input id="${idp}Weight" type="number" value="1" step="0.1"></div>
      <div class="field"><label for="${idp}Prio">Priority</label><input id="${idp}Prio" type="number" value="100"></div>
      <div class="field"><label for="${idp}Conc">Max concurrency</label><input id="${idp}Conc" type="number" value="0"></div>
    </div>
    <div class="grid-2">
      <div class="field"><label for="${idp}Rpm">Max RPM (0 = none)</label><input id="${idp}Rpm" type="number" value="0"></div>
      <div class="field"><label for="${idp}Tpm">Max TPM (0 = none)</label><input id="${idp}Tpm" type="number" value="0"></div>
    </div>`;
}

function newKeyModal(pid) {
  formDialog({
    title: "Add API key", sub: "Each key carries its own budget, cooldown and weight.", submitLabel: "Add",
    body: keyFields("k"),
    onSubmit: async (modal) => {
      await api("/admin/api/keys", { method: "POST", body: {
        provider_id: pid,
        label: $("#kLabel", modal).value.trim(), api_key: $("#kKey", modal).value.trim(),
        weight: parseFloat($("#kWeight", modal).value || "1"),
        priority: parseInt($("#kPrio", modal).value || "100", 10),
        max_concurrency: parseInt($("#kConc", modal).value || "0", 10),
        max_rpm: parseInt($("#kRpm", modal).value || "0", 10),
        max_tpm: parseInt($("#kTpm", modal).value || "0", 10),
      }});
      toast("Key added", "", "ok");
    },
  });
}

function bulkKeysModal() {
  if (!(STATE.providers || []).length) return toast("Add a provider first", "Keys must belong to a provider.", "warn");
  const provs = (STATE.providers || []).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  formDialog({
    title: "Bulk add keys",
    sub: "One per line. Use \"label | key\" to set a label, or just the key.",
    submitLabel: "Add keys",
    body: `
      <div class="field"><label for="bkProv">Provider</label><select id="bkProv">${provs}</select></div>
      <div class="field"><label for="bkList">Keys</label><textarea id="bkList" placeholder="acct-1 | sk-...&#10;sk-...&#10;acct-2 | sk-..."></textarea></div>`,
    onSubmit: async (modal) => {
      const pid = parseInt($("#bkProv", modal).value, 10);
      const lines = $("#bkList", modal).value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
      if (!lines.length) throw new Error("no keys provided");
      let ok = 0;
      for (const line of lines) {
        const [label, key] = line.includes("|") ? line.split("|").map((s) => s.trim()) : ["", line];
        if (!key) continue;
        await api("/admin/api/keys", { method: "POST", body: { provider_id: pid, label, api_key: key } });
        ok++;
      }
      toast(`${ok} key(s) added`, "", "ok");
    },
  });
}

function newModelModal() {
  formDialog({
    title: "New virtual model", sub: "The model name your application sends.", submitLabel: "Create",
    body: `
      <div class="field"><label for="vmName">Model name</label><input id="vmName" placeholder="fast-chat" required></div>
      <div class="field"><label for="vmDesc">Description</label><input id="vmDesc" placeholder="optional"></div>
      <div class="field"><label for="vmStrat">Strategy</label><select id="vmStrat">
        <option value="balanced">balanced — priority tiers + weighted round robin</option>
        <option value="priority">priority — strict failover order</option>
        <option value="round_robin">round robin</option>
        <option value="least_latency">least latency</option>
        <option value="random">random</option></select></div>
      <div class="field"><label for="vmSticky">Sticky sessions</label><select id="vmSticky">
        <option value="1">on — pin a conversation to one key (better cache hits)</option>
        <option value="0">off — rotate freely</option></select></div>`,
    onSubmit: async (modal) => {
      await api("/admin/api/vmodels", { method: "POST", body: {
        name: $("#vmName", modal).value.trim(), description: $("#vmDesc", modal).value.trim(),
        strategy: $("#vmStrat", modal).value, sticky: $("#vmSticky", modal).value === "1",
      }});
      toast("Model created", "", "ok");
    },
  });
}

function newTargetModal(vid) {
  if (!(STATE.providers || []).length) return toast("Add a provider first", "A target points at a provider.", "warn");
  const provs = (STATE.providers || []).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  formDialog({
    title: "Add target", sub: "Point the model at an upstream model on a provider.", submitLabel: "Add",
    body: `
      <div class="field"><label for="tgProv">Provider</label><select id="tgProv">${provs}</select></div>
      <div class="field"><label for="tgKey">Key</label><select id="tgKey"><option value="">any key in provider pool</option></select></div>
      <div class="field"><label for="tgModel">Upstream model</label>
        <div style="display:flex;gap:8px"><input id="tgModel" list="tgModels" placeholder="upstream model id" required>
        <button type="button" class="btn ghost" id="tgLoad" style="flex:none">Load list</button></div>
        <datalist id="tgModels"></datalist></div>
      <div class="grid-2">
        <div class="field"><label for="tgWeight">Weight</label><input id="tgWeight" type="number" value="1" step="0.1"></div>
        <div class="field"><label for="tgPrio">Priority</label><input id="tgPrio" type="number" value="100"></div>
      </div>
      <div class="grid-2">
        <div class="field"><label for="tgRpm">Target RPM cap</label><input id="tgRpm" type="number" value="0"></div>
        <div class="field"><label for="tgTpm">Target TPM cap</label><input id="tgTpm" type="number" value="0"></div>
      </div>`,
    onSubmit: async (modal) => {
      await api("/admin/api/targets", { method: "POST", body: {
        vmodel_id: vid, provider_id: parseInt($("#tgProv", modal).value, 10),
        key_id: $("#tgKey", modal).value || null, upstream_model: $("#tgModel", modal).value.trim(),
        weight: parseFloat($("#tgWeight", modal).value || "1"),
        priority: parseInt($("#tgPrio", modal).value || "100", 10),
        max_rpm: parseInt($("#tgRpm", modal).value || "0", 10),
        max_tpm: parseInt($("#tgTpm", modal).value || "0", 10),
      }});
      toast("Target added", "", "ok");
    },
  });
  const modal = $(".modal");
  const fillKeys = () => {
    const pid = parseInt($("#tgProv", modal).value, 10);
    const keys = (STATE.keys || []).filter((k) => k.provider_id === pid);
    $("#tgKey", modal).innerHTML = '<option value="">any key in provider pool</option>' +
      keys.map((k) => `<option value="${k.id}">${esc(k.label || "key#" + k.id)} (${esc(k.masked)})</option>`).join("");
  };
  $("#tgProv", modal).addEventListener("change", fillKeys);
  fillKeys();
  $("#tgLoad", modal).addEventListener("click", async () => {
    try {
      const r = await api(`/admin/api/providers/${$("#tgProv", modal).value}/models`);
      if (!r.models || !r.models.length) return toast("No model list returned", "Provider may not expose /models", "warn");
      $("#tgModels", modal).innerHTML = r.models.map((m) => `<option value="${esc(m)}"></option>`).join("");
      $("#tgModel", modal).focus();
      toast(`${r.models.length} models loaded`, "", "ok");
    } catch (err) { handleError(err); }
  });
}

/* ---------------- actions ---------------- */
function adminTokenModal() {
  formDialog({
    title: "Add admin user",
    sub: "Viewers can read configuration; admins can change it.",
    submitLabel: "Create",
    body: `
      <div class="field"><label for="atLabel">Label</label><input id="atLabel" placeholder="ops@example"></div>
      <div class="field"><label for="atRole">Role</label><select id="atRole">
        <option value="viewer">viewer — read only</option>
        <option value="admin">admin — full access</option></select></div>`,
    onSubmit: async (modal) => {
      const r = await api("/admin/api/admin-tokens", { method: "POST", body: {
        label: $("#atLabel", modal).value.trim(), role: $("#atRole", modal).value }});
      toast("Admin user created", "", "ok");
      copy(r.token, "Token copied to clipboard");
    },
  });
}

function renderTeam() {
  const toks = STATE.admin_tokens || [];
  const rows = toks.map((t) => `<tr>
      <td>${esc(t.label)}</td><td>${esc(t.role)}</td>
      <td>${t.enabled ? '<span class="pill ok">enabled</span>' : '<span class="pill off">disabled</span>'}</td>
      <td class="r"><button class="btn sm danger" data-action="del-admin-token" data-id="${t.id}">Revoke</button></td>
    </tr>`).join("");
  $("#adminTokens").innerHTML = `<table><thead><tr><th>Label</th><th>Role</th><th>State</th><th></th></tr></thead>
    <tbody>${rows || emptyRow(4, "Only the shared ADMIN_TOKEN is configured.")}</tbody></table>`;

  const audit = STATE.audit || [];
  const arows = audit.map((a) => `<tr>
      <td class="mono">${esc(clockTime(a.ts))}</td><td>${esc(a.actor)}</td>
      <td class="muted">${esc(a.role)}</td>
      <td class="mono">${esc(a.action)} ${esc(a.target)}</td>
      <td class="mono muted">${esc(a.ip)}</td></tr>`).join("");
  $("#audit").innerHTML = `<table><thead><tr><th>Time</th><th>Actor</th><th>Role</th><th>Action</th><th>IP</th></tr></thead>
    <tbody>${arows || emptyRow(5, "No admin activity yet.")}</tbody></table>`;
}

async function gwKeyModal(g) {
  const editing = !!g;
  formDialog({
    title: editing ? "Edit gateway key" : "Generate gateway key",
    sub: editing ? "Update the scope and quota for this key."
                 : "Optionally restrict which models it may call and cap its rate.",
    submitLabel: editing ? "Save" : "Generate",
    body: `
      <div class="field"><label for="gkLabel">Label</label><input id="gkLabel" value="${esc(g ? g.label : "")}" placeholder="client"></div>
      <div class="field"><label for="gkModels">Allowed models</label><input id="gkModels" value="${esc(g ? g.allowed_models : "")}" placeholder="empty = all models"><div class="help">Comma-separated virtual model names.</div></div>
      <div class="field"><label for="gkRpm">Max requests / minute</label><input id="gkRpm" type="number" value="${esc(g ? g.max_rpm : 0)}"><div class="help">0 = unlimited.</div></div>`,
    onSubmit: async (modal) => {
      const body = {
        label: $("#gkLabel", modal).value.trim(),
        allowed_models: $("#gkModels", modal).value.trim(),
        max_rpm: parseInt($("#gkRpm", modal).value || "0", 10),
      };
      if (editing) {
        await api(`/admin/api/gateway-keys/${g.id}`, { method: "PATCH", body });
        toast("Gateway key updated", "", "ok");
      } else {
        const r = await api("/admin/api/gateway-keys", { method: "POST", body });
        toast("Gateway key generated", "", "ok");
        copy(r.key, "Key copied to clipboard");
      }
    },
  });
}
async function exportConfig() {
  const data = await api("/admin/api/export?reveal=1");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "llm-router-config.json"; a.click();
  URL.revokeObjectURL(a.href);
}
async function clearLogs() {
  await api("/admin/api/logs", { method: "DELETE" });
  UI.feed = [];
  toast("Log cleared", "", "ok");
  refresh();
}
async function testModel(name) {
  toast("Testing " + name + "…");
  try {
    const r = await api("/admin/api/test", { method: "POST", body: { model: name } });
    if (r.ok) toast(`OK · ${r.provider} / ${r.key}`, `${r.attempts} attempt(s) · ${r.ms} ms · ${r.reply}`, "ok");
    else toast("Test failed", r.error, "bad");
  } catch (err) { handleError(err); }
}
async function probeEgress() {
  try {
    const r = await api("/admin/api/egress");
    const proxies = Object.keys(r.providers || {}).length;
    toast("Egress probe", `local IP ${r.local_ip} · global proxy ${r.global_proxy_set ? "set" : "not set"} · ${proxies} provider override(s)`, "ok");
  } catch (err) { handleError(err); }
}
function toggleEntity(el) {
  const ent = el.closest(".entity");
  const body = $(".entity-body", ent);
  const open = !ent.classList.contains("open");
  ent.classList.toggle("open", open);
  body.hidden = !open;
  el.setAttribute("aria-expanded", open ? "true" : "false");
  const key = ent.dataset.key;
  if (key) { if (open) UI.expanded.add(key); else UI.expanded.delete(key); }
}
function togglePause(el) {
  UI.paused = !UI.paused;
  el.innerHTML = UI.paused
    ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5l12 7-12 7z"/></svg>Resume'
    : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14"/></svg>Pause';
  if (!UI.paused) renderFeed();
}
function openTokenModal() {
  formDialog({
    title: "Admin token", sub: "Enter the ADMIN_TOKEN configured on the server.",
    submitLabel: "Save",
    body: `<div class="field"><input id="tok" type="password" placeholder="ADMIN_TOKEN" value="${esc(token())}"></div>`,
    onSubmit: async (modal) => {
      localStorage.setItem(TOKEN_KEY, $("#tok", modal).value.trim());
      await refresh();
      connectStream();
    },
  });
}

/* ---------------- renderers ---------------- */
function providerName(id) {
  const p = (STATE.providers || []).find((x) => x.id === id);
  return p ? p.name : "—";
}
function keyStatePill(enabled, cooldown) {
  if (!enabled) return '<span class="pill off">disabled</span>';
  if (cooldown > 0) return `<span class="pill warn">cooling ${Math.ceil(cooldown)}s</span>`;
  return '<span class="pill ok">ready</span>';
}
function levelOf(e) { return e.level || "ok"; }

function renderKpis() {
  const s = STATE.stats || {};
  const rate = s.success_rate ?? 100;
  const tiles = [
    { label: "Requests · 5m", value: nf.format(s.requests_5m ?? 0) },
    { label: "Success rate", value: rate + "%", cls: rate < 95 ? "down" : "up" },
    { label: "Avg latency", value: (s.avg_latency ?? 0) + " ms" },
    { label: "Active keys", value: `${s.keys_enabled ?? 0}/${s.keys_total ?? 0}` },
    { label: "Models", value: nf.format(s.models ?? 0) },
  ];
  const spark = sparkBars();
  $("#kpis").innerHTML = tiles.map((t, i) => `
    <div class="kpi"><div class="label">${esc(t.label)}</div>
      <div class="value ${t.cls ? "trend " + t.cls : ""}">${esc(t.value)}</div>
      ${i === 0 ? `<div class="spark">${spark}</div>` : `<div class="sub">${esc(subLabel(i))}</div>`}
    </div>`).join("");
}
function subLabel(i) {
  return ["last 5 minutes", "recent calls", "end to end", "enabled / total", "virtual"][i] || "";
}
function sparkBars() {
  const now = Date.now() / 1000;
  const buckets = new Array(12).fill(0);
  (UI.feed || []).forEach((e) => {
    const age = now - e.ts;
    if (age < 60) buckets[11 - Math.min(11, Math.floor(age / 5))]++;
  });
  const max = Math.max(1, ...buckets);
  return buckets.map((b) => `<i style="height:${Math.round((b / max) * 100)}%"></i>`).join("");
}

function renderKeyHealth() {
  const q = UI.q.key.toLowerCase();
  const keys = (STATE.keys || []).filter((k) =>
    !q || (k.label || "").toLowerCase().includes(q) || provNameOf(k.provider_id).toLowerCase().includes(q) || (k.masked || "").includes(q));
  const rows = keys.map((k) => {
    const h = k.health || {};
    const pct = k.max_rpm ? Math.min(100, Math.round((100 * (h.rpm_used || 0)) / k.max_rpm)) : 0;
    const cls = pct > 85 ? "bad" : pct > 60 ? "warn" : "";
    return `<tr>
      <td>${esc(provNameOf(k.provider_id))}</td>
      <td class="mono">${esc(k.label || "key#" + k.id)}</td>
      <td class="mono">${esc(k.masked)}</td>
      <td>${keyStatePill(k.enabled, h.cooldown || 0)}</td>
      <td>${k.max_rpm ? `<div class="bar"><i class="${cls}" style="width:${pct}%"></i></div>` : ""}<span class="mono muted">${h.rpm_used || 0}${k.max_rpm ? "/" + k.max_rpm : ""} rpm</span></td>
      <td class="num">${h.latency_ema || "—"}</td>
      <td class="muted">${esc(h.last_error || "")}</td></tr>`;
  }).join("");
  $("#keyHealth").innerHTML = `<table><thead><tr>
    <th>Provider</th><th>Key</th><th>Value</th><th>State</th><th>RPM window</th><th class="num">Latency</th><th>Last note</th>
    </tr></thead><tbody>${rows || emptyRow(7, "No keys configured yet.")}</tbody></table>`;
}
function provNameOf(id) { return providerName(id); }
function emptyRow(cols, msg) { return `<tr><td colspan="${cols}" class="muted" style="text-align:center;padding:22px">${esc(msg)}</td></tr>`; }

function renderRecent() {
  const rows = (UI.feed || []).slice(0, 8).map((e) => `
    <tr><td class="mono muted">${esc(clockTime(e.ts))}</td>
      <td><span class="log-level ${esc(levelOf(e))}">${esc(levelOf(e))}</span></td>
      <td>${esc(e.model || "")}</td><td class="mono">${esc(e.target || "")}</td>
      <td class="num">${esc(e.status ?? "—")}</td><td class="num">${esc(msLabel(e.ms))}</td></tr>`).join("");
  $("#recent").innerHTML = `<table><thead><tr><th>Time</th><th>Level</th><th>Model</th><th>Target</th><th class="num">Status</th><th class="num">Latency</th></tr></thead>
    <tbody>${rows || emptyRow(6, "No activity yet.")}</tbody></table>`;
}

function renderModels() {
  const q = UI.q.model.toLowerCase();
  const models = (STATE.vmodels || []).filter((v) => !q || v.name.toLowerCase().includes(q));
  const host = $("#models");
  if (!models.length) {
    host.innerHTML = empty(`<h3>No virtual models</h3><p>Create a model, then point it at one or more targets.</p>
      <button class="btn" data-action="new-model"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>New model</button>`);
    return;
  }
  host.innerHTML = models.map((v) => {
    const targets = v.targets || [];
    const body = targets.length ? targets.map((t) => `
      <div class="mini">
        <span>${esc(providerName(t.provider_id))}</span>
        <span class="mono">${esc(t.upstream_model)}</span>
        <span class="mono muted">${t.key_id ? "key#" + esc(t.key_id) : "any key"}</span>
        <span class="mono muted">w${esc(t.weight)} · p${esc(t.priority)}</span>
        ${t.enabled ? "" : '<span class="pill off">off</span>'}
        <span class="spacer"></span>
        <button class="btn sm ghost" data-action="toggle-target" data-id="${t.id}" data-next="${t.enabled ? 0 : 1}">${t.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-action="del-target" data-id="${t.id}">Remove</button>
      </div>`).join("") : `<div class="mini muted">No targets yet — this model returns 429 until one is added.</div>`;
    const ekey = "model:" + v.id, eopen = UI.expanded.has(ekey);
    return `<div class="entity${eopen ? " open" : ""}" data-key="${ekey}">
      <div class="entity-head" data-action="toggle-entity" role="button" tabindex="0" aria-expanded="${eopen}">
        <svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
        <h3 class="mono">${esc(v.name)}</h3>
        <span class="tag">${esc(v.strategy)}</span>${v.sticky ? '<span class="tag">sticky</span>' : ""}
        ${v.enabled ? "" : '<span class="pill off">disabled</span>'}
        <span class="spacer"></span>
        <button class="btn sm" data-action="add-target" data-id="${v.id}">Add target</button>
        <button class="btn sm ghost" data-action="test-model" data-name="${esc(v.name)}">Test</button>
        <button class="btn sm danger" data-action="del-model" data-id="${v.id}">Delete</button>
      </div>
      ${v.description ? `<div class="entity-meta">${esc(v.description)}</div>` : ""}
      <div class="entity-body"${eopen ? "" : " hidden"}>${body}</div>
    </div>`;
  }).join("");
}

function renderProviders() {
  const q = UI.q.provider.toLowerCase();
  const provs = (STATE.providers || []).filter((p) => !q || p.name.toLowerCase().includes(q) || (p.base_url || "").toLowerCase().includes(q));
  const host = $("#providers");
  if (!provs.length) {
    host.innerHTML = empty(`<h3>No providers</h3><p>Add a provider, then attach one or more API keys.</p>
      <button class="btn" data-action="new-provider"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>Add provider</button>`);
    return;
  }
  host.innerHTML = provs.map((p) => {
    const keys = (STATE.keys || []).filter((k) => k.provider_id === p.id);
    const body = keys.length ? keys.map((k) => {
      const h = k.health || {};
      return `<div class="mini">
        <span class="mono">${esc(k.label || "key#" + k.id)}</span>
        <span class="mono muted">${esc(k.masked)}</span>
        ${keyStatePill(k.enabled, h.cooldown || 0)}
        <span class="mono muted">w${esc(k.weight)} · p${esc(k.priority)}${k.max_rpm ? " · " + esc(k.max_rpm) + "rpm" : ""}${k.max_tpm ? " · " + esc(k.max_tpm) + "tpm" : ""}</span>
        <span class="spacer"></span>
        <button class="btn sm ghost" data-action="reset-key" data-id="${k.id}">Reset</button>
        <button class="btn sm ghost" data-action="toggle-key" data-id="${k.id}" data-next="${k.enabled ? 0 : 1}">${k.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-action="del-key" data-id="${k.id}">Remove</button>
      </div>`;
    }).join("") : `<div class="mini muted">No keys yet — this provider is skipped.</div>`;
    const ekey = "provider:" + p.id, eopen = UI.expanded.has(ekey);
    return `<div class="entity${eopen ? " open" : ""}" data-key="${ekey}">
      <div class="entity-head" data-action="toggle-entity" role="button" tabindex="0" aria-expanded="${eopen}">
        <svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>
        <h3>${esc(p.name)}</h3>
        <span class="tag">${esc(p.adapter)}</span>${p.proxy_set ? '<span class="tag">egress proxy</span>' : ""}
        ${p.enabled ? "" : '<span class="pill off">disabled</span>'}
        <span class="spacer"></span>
        <button class="btn sm" data-action="add-key" data-id="${p.id}">Add key</button>
        <button class="btn sm ghost" data-action="toggle-provider" data-id="${p.id}" data-next="${p.enabled ? 0 : 1}">${p.enabled ? "Disable" : "Enable"}</button>
        <button class="btn sm danger" data-action="del-provider" data-id="${p.id}">Delete</button>
      </div>
      <div class="entity-meta">${esc(p.base_url)}</div>
      <div class="entity-body"${eopen ? "" : " hidden"}>${body}</div>
    </div>`;
  }).join("");
}

function renderGwKeys() {
  const rows = (STATE.gateway_keys || []).map((g) => {
    const models = (g.allowed_models || "").trim();
    const scope = `${models ? "models: " + esc(models) : "all models"} · ${g.max_rpm ? esc(g.max_rpm) + " rpm" : "unlimited"}`;
    return `<tr><td class="mono">${esc(g.masked)}</td><td>${esc(g.label)}</td>
      <td class="muted">${scope}</td>
      <td class="r"><div class="row-actions">
        <button class="btn sm ghost" data-action="copy-key" data-value="${esc(g.key)}">Copy</button>
        <button class="btn sm ghost" data-action="edit-gw" data-id="${g.id}">Edit</button>
        <button class="btn sm danger" data-action="del-gw" data-id="${g.id}">Revoke</button>
      </div></td></tr>`;
  }).join("");
  $("#gwKeys").innerHTML = `<table><thead><tr><th>Key</th><th>Label</th><th>Scope</th><th></th></tr></thead>
    <tbody>${rows || emptyRow(4, "No gateway keys.")}</tbody></table>`;
}

function renderSettings() {
  const meta = STATE.meta || {};
  $("#envChip").innerHTML = `env <b>${esc(meta.env || "—")}</b>`;
  $("#encChip").innerHTML = `at-rest <b>${meta.encryption ? "encrypted" : "plaintext"}</b>`;
  $("#roleChip").innerHTML = `role <b>${esc(STATE.role || "admin")}</b>`;
  $("#diagEnv").textContent = meta.env || "—";
  $("#diagEnc").textContent = meta.encryption ? "AES-256-GCM" : "plaintext (SECRET_KEY unset)";
  $("#diagRpm").textContent = meta.gateway_rpm ? meta.gateway_rpm + " rpm" : "unlimited";
  $("#diagIp").textContent = "click probe";
  const base = location.origin + "/v1";
  $("#baseUrl").textContent = base;
  $("#curlSample").textContent =
    `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer <gateway key>" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"<virtual model>","messages":[{"role":"user","content":"hello"}]}'`;
  const adminInput = $("#adminToken");
  if (document.activeElement !== adminInput) adminInput.value = token();
  $("#footUrl").textContent = joinHost();
}
function joinHost() { try { return location.host; } catch (_) { return "—"; } }

function renderFoot() {
  const s = STATE.stats || {};
  $("#footKeys").textContent = `${s.keys_enabled ?? 0}/${s.keys_total ?? 0}`;
  $("#footModels").textContent = nf.format(s.models ?? 0);
}

/* ---------------- feed ---------------- */
function seedFeed() {
  UI.feed = (STATE.logs || []).map((l) => ({
    ts: l.ts, level: (l.status || 500) < 400 ? "ok" : "retry",
    model: l.vmodel, target: `${l.provider || ""}/${l.key_label || ""}`.replace(/^\/|\/$/g, ""),
    status: l.status, ms: l.latency_ms, attempts: l.attempts, note: l.error || "",
  }));
}
function pushEvent(item) {
  const e = {
    ts: item.ts || Date.now() / 1000,
    level: item.kind || "ok",
    model: item.model || "",
    target: `${item.provider || ""}/${item.key || ""}`.replace(/^\/|\/$/g, ""),
    status: item.status, ms: item.ms,
    note: item.reason || (item.seconds ? `cooldown ${item.seconds}s` : ""),
  };
  UI.feed.unshift(e);
  if (UI.feed.length > MAX_FEED) UI.feed.length = MAX_FEED;
  if (UI.route === "traffic" && !UI.paused) scheduleFeedRender();
  if (UI.route === "overview") { renderRecent(); }
}
let feedTimer = null;
function scheduleFeedRender() {
  if (feedTimer) return;
  feedTimer = setTimeout(() => { feedTimer = null; if (UI.route === "traffic" && !UI.paused) renderFeed(); }, 200);
}
function renderFeed() {
  const lvl = UI.q.level, q = UI.q.log.toLowerCase();
  const rows = (UI.feed || []).filter((e) => {
    if (lvl && e.level !== lvl) return false;
    if (q && !(`${e.model} ${e.target} ${e.note}`.toLowerCase().includes(q))) return false;
    return true;
  }).slice(0, 300);
  $("#logFeed").innerHTML = rows.map((e) => `
    <tr><td>${esc(clockTime(e.ts))}</td>
      <td><span class="log-level ${esc(e.level)}">${esc(e.level)}</span></td>
      <td>${esc(e.model || "")}</td><td class="mono">${esc(e.target || "")}</td>
      <td class="num">${esc(e.status ?? "—")}</td><td class="num">${esc(msLabel(e.ms))}</td>
      <td class="muted">${esc(e.note || "")}</td></tr>`).join("")
    || emptyRow(7, UI.feed.length ? "No events match the filter." : "Waiting for traffic…");
  $("#feedCount").textContent = `${rows.length} shown · ${UI.feed.length} total`;
}

/* ---------------- empty helper ---------------- */
function empty(inner) { return `<div class="empty">${inner}</div>`; }

/* ---------------- data + stream ---------------- */
let stream = null;
let streamTimer = null;
function setConn(state) {
  const b = $("#connBadge");
  b.classList.toggle("on", state === "on");
  b.classList.toggle("off", state === "off" || state === "locked");
  $("#connText").textContent = state === "on" ? "live" : state === "locked" ? "locked" : "offline";
}
async function connectStream() {
  if (stream) stream.close();
  if (streamTimer) { clearTimeout(streamTimer); streamTimer = null; }
  // Never put the long-lived admin token in the URL: use a short-lived ticket.
  let url = "/admin/api/stream";
  try {
    const r = await api("/admin/api/stream-ticket");
    url += "?ticket=" + encodeURIComponent(r.ticket);
  } catch (_) {
    url += "?token=" + encodeURIComponent(token());
  }
  stream = new EventSource(url);
  stream.onopen = () => { setConn("on"); hideOffline(); };
  stream.onerror = () => {
    setConn("off"); showOffline();
    if (stream) stream.close();
    streamTimer = setTimeout(connectStream, 5000);
  };
  stream.onmessage = (e) => { try { pushEvent(JSON.parse(e.data)); } catch (_) {} };
}
function showOffline() { const b = $("#offline"); if (b) b.hidden = false; }
function hideOffline() { const b = $("#offline"); if (b) b.hidden = true; }

let refreshing = false;
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    STATE = Object.assign(STATE, await api("/admin/api/state"));
    STATE.loaded = true;
    hideOffline();
    if (!UI.seeded) { seedFeed(); UI.seeded = true; }
    renderKpis(); renderKeyHealth(); renderRecent(); renderModels();
    renderProviders(); renderGwKeys(); renderSettings(); renderFoot(); renderTeam();
    if (UI.route === "traffic") renderFeed();
  } catch (err) {
    if (err instanceof AuthError) { setConn("locked"); if ($("#modalRoot").hidden) openTokenModal(); }
    else { showOffline(); console.error("refresh failed:", err); }
  } finally { refreshing = false; }
}

/* ---------------- wiring ---------------- */
function wireFilters() {
  const bind = (id, key) => {
    const el = $(id); if (!el) return;
    el.addEventListener("input", debounce(() => { UI.q[key] = el.value; rerenderFor(key); }, 160));
  };
  bind("#keyFilter", "key"); bind("#modelFilter", "model");
  bind("#providerFilter", "provider"); bind("#logFilter", "log");
  const lvl = $("#levelFilter");
  if (lvl) lvl.addEventListener("change", () => { UI.q.level = lvl.value; renderFeed(); });
}
function rerenderFor(key) {
  if (key === "key") renderKeyHealth();
  else if (key === "model") renderModels();
  else if (key === "provider") renderProviders();
  else if (key === "log") renderFeed();
}

function boot() {
  initTheme();
  route();
  window.addEventListener("hashchange", route);
  wireFilters();

  const file = $("#importFile");
  if (file) file.addEventListener("change", async () => {
    const f = file.files[0]; if (!f) return;
    const text = await f.text();
    const wipe = await confirmDialog("Replace existing configuration? Cancel merges instead.", { title: "Import configuration", confirmLabel: "Replace", danger: false });
    try {
      await api("/admin/api/import?wipe=" + (wipe ? 1 : 0), { method: "POST", body: JSON.parse(text) });
      toast("Configuration imported", "", "ok");
      refresh();
    } catch (err) { handleError(err); }
    file.value = "";
  });

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
    else if (e.key === "Escape") { if (!$("#paletteRoot").hidden) closePalette(); else if (!$("#modalRoot").hidden) closeModal(); }
    else if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("entity-head")) {
      e.preventDefault(); toggleEntity(e.target);
    }
  });

  refresh();
  setInterval(refresh, 5000);
  setInterval(() => { if (UI.route === "overview") renderRecent(); }, 15000);
  connectStream();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
