"""
console_app.py — Supi admin console (separate port, operator-only).

A small standalone FastAPI app that serves a minimal black-and-white web UI for tenant management
and mounts the admin API ([admin.py]) on the SAME (separate) port. It deliberately does NOT load the
TTS model and is NOT part of the public API (port 8000), so the whole admin surface can be exposed on
its own port and locked down independently.

Run it with: uvicorn console_app:console_app --host 0.0.0.0 --port ${ADMIN_PORT:-8001}
(start.sh launches it automatically when ADMIN_API_KEY is set.)

Security model: every action requires the operator's ADMIN_API_KEY, entered in the UI and sent as the
`X-Admin-Key` header on same-origin requests (so no CORS and no cross-origin exposure). The static
page holds no secrets. Prefer reaching this port over the RunPod HTTPS proxy and restrict who can
reach it.
"""

import logging

from fastapi import FastAPI, status
from fastapi.responses import HTMLResponse

import admin
import security

logger = logging.getLogger("supi.console")

console_app = FastAPI(
    title="Supi Admin Console",
    description="Operator-only tenant & API-key management UI. Runs on a separate port.",
    version="1.0.0",
    docs_url="/swagger",   # keep Swagger off '/', which serves the console UI
    redoc_url=None,
)

# Strict, self-contained CSP: the page is fully inline (no external JS/CSS), only ever talks to its
# own origin, must never be framed, and only needs to load voice-preview audio from HTTPS sources.
# no_store keeps the authenticated operator page out of any shared/browser cache.
_CONSOLE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src https: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)
security.install_security_headers(console_app, csp=_CONSOLE_CSP, no_store=True)

# The admin API lives on this port only (X-Admin-Key protected, see admin.py).
console_app.include_router(admin.router)


@console_app.get("/healthz", status_code=status.HTTP_200_OK)
async def healthz():
    return {"status": "ok", "service": "supi-admin-console"}


@console_app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_PAGE)


# ==============================================================================
# Minimal black-and-white single-page UI (no framework, no external assets).
# ==============================================================================
_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Supi Admin Console</title>
<style>
  :root { --line:#000; }
  * { box-sizing: border-box; }
  body { margin:0; background:#fff; color:#000; font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size:14px; line-height:1.5; }
  header { border-bottom:2px solid var(--line); padding:14px 18px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; letter-spacing:1px; text-transform:uppercase; }
  main { padding:18px; max-width:1000px; margin:0 auto; }
  section { border:1px solid var(--line); margin-bottom:18px; }
  section > h2 { font-size:13px; margin:0; padding:8px 12px; border-bottom:1px solid var(--line); text-transform:uppercase; letter-spacing:1px; }
  .body { padding:12px; }
  input, button { font-family:inherit; font-size:14px; background:#fff; color:#000; border:1px solid var(--line); padding:6px 8px; }
  input { width:180px; }
  button { cursor:pointer; }
  button:hover { background:#000; color:#fff; }
  button.danger:hover { background:#000; }
  table { width:100%; border-collapse:collapse; }
  th, td { border:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
  th { text-transform:uppercase; font-size:11px; letter-spacing:1px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:8px; }
  .muted { opacity:.6; }
  .pill { border:1px solid var(--line); padding:1px 6px; font-size:11px; }
  #status { padding:8px 12px; border:1px solid var(--line); margin-bottom:18px; white-space:pre-wrap; }
  #status.err { background:#000; color:#fff; }
  .keybox { border:2px solid var(--line); padding:10px; margin-top:8px; word-break:break-all; background:#fff; }
  .actions button { padding:3px 6px; font-size:12px; }
  details > summary { cursor:pointer; }
  .overlay { position:fixed; inset:0; background:rgba(0,0,0,.4); display:flex; align-items:flex-start; justify-content:center; padding:40px 16px; z-index:10; }
  .modal { background:#fff; border:2px solid var(--line); max-width:560px; width:100%; max-height:80vh; overflow:auto; }
  .modal h3 { margin:0; padding:10px 12px; border-bottom:1px solid var(--line); font-size:13px; text-transform:uppercase; letter-spacing:1px; display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .modal .mbody { padding:12px; }
  .modal .breakid { word-break:break-all; }
  .tlist td { vertical-align:middle; }
  label { display:inline-flex; gap:4px; align-items:center; }
  .over { background:#000; color:#fff; padding:1px 6px; }
  tr.picked > td:first-child { background:#000; color:#fff; }
  #swResults table { margin-top:10px; }
  #swResults pre { white-space:pre-wrap; margin:6px 0; }
  #swResults audio { height:32px; }
</style>
</head>
<body>
<header>
  <h1>Supi&nbsp;Admin</h1>
  <input id="adminKey" type="password" placeholder="ADMIN_API_KEY" autocomplete="off">
  <button onclick="connect()">Connect</button>
  <button onclick="logout()">Forget key</button>
  <span id="conn" class="muted">not connected</span>
</header>
<main>
  <div id="status" style="display:none"></div>

  <section>
    <h2>Create tenant</h2>
    <div class="body">
      <div class="row">
        <input id="ntid" placeholder="tenant_id (e.g. acme)">
        <input id="nname" placeholder="name (optional)">
        <input id="ncredits" type="number" placeholder="starting credits" value="0">
        <button onclick="createTenant()">Create</button>
      </div>
      <div class="muted">Then "Mint key" on the row below to generate that tenant's API key.</div>
    </div>
  </section>

  <section>
    <h2>Tenants</h2>
    <div class="body">
      <div class="row"><button onclick="loadTenants()">Refresh</button></div>
      <div id="tenants"><span class="muted">Connect with your admin key to load tenants.</span></div>
    </div>
  </section>

  <section id="detailSection" style="display:none">
    <h2>Tenant detail</h2>
    <div class="body" id="detail"></div>
  </section>

  <section>
    <h2>Credit requests <span id="creqBadge" class="pill" style="display:none"></span></h2>
    <div class="body">
      <div class="row">
        <button onclick="loadCreditRequests()">Refresh</button>
        <label class="muted"><input type="checkbox" id="creqPendingOnly" checked onchange="loadCreditRequests()"> pending only</label>
        <span class="muted">Tenants request top-ups via the API; approve to grant the credits.</span>
      </div>
      <div id="creqs"><span class="muted">Connect with your admin key to load credit requests.</span></div>
    </div>
  </section>

  <section>
    <h2>Voice profiles</h2>
    <div class="body">
      <div class="row">
        <button onclick="loadVoices()">Refresh</button>
        <span class="muted">Cloned/cached voices — publish to all tenants, assign to specific tenants, or mark as a default.</span>
      </div>
      <div id="voices"><span class="muted">Connect with your admin key to load voice profiles.</span></div>
    </div>
  </section>

  <section>
    <h2>Voice sweep / A-B testing</h2>
    <div class="body">
      <div class="muted" style="margin-bottom:10px">Render one fixed text across a grid of params, compare by ear, then ★ pick the winner to copy its exact /tts params. Operator-only; cloning happens once and is reused for every cell. Requires ENABLE_SWEEP on the model service.</div>
      <div class="row">
        <input id="swText" placeholder="text to synthesize" style="width:100%" value="नमस्ते, यो परीक्षण हो।">
      </div>
      <div class="row">
        <label>voice <select id="swVoice"><option value="">(default voice)</option></select></label>
        <label>seed <input id="swSeed" type="number" value="1234" style="width:110px"></label>
        <label>instruct <input id="swInstruct" placeholder="optional" style="width:150px"></label>
      </div>
      <div class="row">
        <label>num_step <input id="swNumStep" value="32, 64" style="width:150px" oninput="updateSweepCount()"></label>
        <label>guidance_scale <input id="swGuidance" value="1.5, 2.0, 2.5, 3.0" style="width:200px" oninput="updateSweepCount()"></label>
      </div>
      <div class="row">
        <label>class_temperature <input id="swClassTemp" value="0.20, 0.25, 0.30" style="width:170px" oninput="updateSweepCount()"></label>
        <label>position_temperature <input id="swPosTemp" value="5.0" style="width:120px" oninput="updateSweepCount()"></label>
        <label>speed <input id="swSpeed" value="1.0" style="width:90px" oninput="updateSweepCount()"></label>
      </div>
      <div class="row">
        <button id="swRun" onclick="runSweep()">Run sweep</button>
        <span id="swCount" class="muted"></span>
      </div>
      <div id="swResults"></div>
    </div>
  </section>
</main>

<div id="overlay" class="overlay" style="display:none" onclick="if(event.target===this)closeModal()">
  <div class="modal"><h3><span id="modalTitle">Assign voice</span><button onclick="closeModal()">Close</button></h3>
  <div class="mbody" id="modalBody"></div></div>
</div>

<script>
const KEY = "supi_admin_key";
function getKey(){ return sessionStorage.getItem(KEY) || ""; }
function setStatus(msg, isErr){
  const s = document.getElementById("status");
  s.style.display = "block"; s.textContent = msg; s.className = isErr ? "err" : "";
}
function hdrs(){ return { "X-Admin-Key": getKey(), "Content-Type": "application/json" }; }

async function api(method, path, body){
  const opt = { method, headers: hdrs() };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch(e) {}
  if (!r.ok){
    const detail = (data && (data.detail || JSON.stringify(data))) || (r.status + " " + r.statusText);
    throw new Error(detail);
  }
  return data;
}

function connect(){
  const k = document.getElementById("adminKey").value.trim();
  if (!k){ setStatus("Enter your ADMIN_API_KEY first.", true); return; }
  sessionStorage.setItem(KEY, k);
  loadTenants();
  loadCreditRequests();
  loadVoices();
}
function logout(){
  sessionStorage.removeItem(KEY);
  document.getElementById("adminKey").value = "";
  document.getElementById("conn").textContent = "not connected";
  document.getElementById("tenants").innerHTML = '<span class="muted">Forgotten. Connect again.</span>';
  document.getElementById("detailSection").style.display = "none";
  document.getElementById("creqs").innerHTML = '<span class="muted">Forgotten. Connect again.</span>';
  document.getElementById("creqBadge").style.display = "none";
  setStatus("Key cleared from this browser tab.", false);
}

async function loadTenants(){
  try {
    const data = await api("GET", "/admin/tenants");
    document.getElementById("conn").textContent = "connected";
    const rows = (data.tenants || []).map(t => `
      <tr>
        <td>${esc(t.tenant_id)}</td>
        <td>${esc(t.name || "")}</td>
        <td>${planCell(t.metered)}</td>
        <td>${t.metered === false ? '<span class="muted">∞</span>' : fmt(t.credits_remaining)}</td>
        <td>${fmt(t.credits_used)}</td>
        <td>${t.active_keys}</td>
        <td class="actions">
          <button onclick="viewTenant('${js(t.tenant_id)}')">View</button>
          <button onclick="toggleMetered('${js(t.tenant_id)}', ${t.metered === false ? 'true' : 'false'})">${t.metered === false ? 'Meter' : 'Unmeter'}</button>
          <button onclick="mintKey('${js(t.tenant_id)}')">Mint key</button>
          <button onclick="addCredits('${js(t.tenant_id)}')">+ credits</button>
          <button class="danger" onclick="delTenant('${js(t.tenant_id)}')">Delete</button>
        </td>
      </tr>`).join("");
    document.getElementById("tenants").innerHTML = rows
      ? `<table><thead><tr><th>tenant_id</th><th>name</th><th>plan</th><th>remaining</th><th>used</th><th>keys</th><th>actions</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<span class="muted">No tenants yet. Create one above.</span>';
    setStatus("Loaded " + (data.tenants ? data.tenants.length : 0) + " tenant(s).", false);
  } catch(e){ fail(e); }
}

async function createTenant(){
  const tenant_id = val("ntid"), name = val("nname"), credits = parseFloat(val("ncredits") || "0");
  if (!tenant_id){ setStatus("tenant_id is required.", true); return; }
  try {
    await api("POST", "/admin/tenants", { tenant_id, name, credits: isNaN(credits) ? 0 : credits });
    document.getElementById("ntid").value = ""; document.getElementById("nname").value = "";
    setStatus("Created tenant '" + tenant_id + "'.", false);
    loadTenants();
  } catch(e){ fail(e); }
}

async function viewTenant(id){
  try {
    const t = await api("GET", "/admin/tenants/" + encodeURIComponent(id));
    const keys = (t.keys || []).map(k => `
      <tr>
        <td>${esc(k.key_id)}</td>
        <td>${esc(k.key_prefix)}</td>
        <td>${k.revoked ? '<span class="pill">revoked</span>' : 'active'}</td>
        <td class="actions">${k.revoked ? "" : `<button class="danger" onclick="revokeKey('${js(k.key_id)}','${js(id)}')">Revoke</button>`}</td>
      </tr>`).join("");
    document.getElementById("detail").innerHTML = `
      <div class="row"><strong>${esc(t.tenant_id)}</strong> <span class="muted">${esc(t.name||"")}</span> ${planCell(t.metered)}</div>
      <div class="row">granted ${fmt(t.credits_granted)} &nbsp;|&nbsp; used ${fmt(t.credits_used)} &nbsp;|&nbsp; remaining ${t.metered === false ? '∞ (unmetered)' : fmt(t.credits_remaining)}</div>
      <div class="row">
        <button onclick="toggleMetered('${js(id)}', ${t.metered === false ? 'true' : 'false'})">${t.metered === false ? 'Switch to metered' : 'Switch to unmetered'}</button>
        <button onclick="mintKey('${js(id)}')">Mint key</button>
        <button onclick="addCredits('${js(id)}')">+ credits</button>
      </div>
      <table><thead><tr><th>key_id</th><th>prefix</th><th>status</th><th>actions</th></tr></thead>
      <tbody>${keys || '<tr><td colspan="4" class="muted">No keys. Mint one.</td></tr>'}</tbody></table>`;
    document.getElementById("detailSection").style.display = "block";
    document.getElementById("detailSection").scrollIntoView({behavior:"smooth"});
  } catch(e){ fail(e); }
}

async function mintKey(id){
  try {
    const r = await api("POST", "/admin/tenants/" + encodeURIComponent(id) + "/keys", {});
    document.getElementById("detailSection").style.display = "block";
    document.getElementById("detail").innerHTML = `
      <div><strong>New API key for ${esc(id)}</strong> — copy it now, it is shown only once:</div>
      <div class="keybox">${esc(r.api_key)}</div>
      <div class="row" style="margin-top:8px">
        <button onclick="copyText('${js(r.api_key)}')">Copy</button>
        <button onclick="viewTenant('${js(id)}')">Back to tenant</button>
      </div>`;
    document.getElementById("detailSection").scrollIntoView({behavior:"smooth"});
    setStatus("Minted a key for '" + id + "'. Store it now.", false);
    loadTenants();
  } catch(e){ fail(e); }
}

async function addCredits(id){
  const amount = prompt("Add how many credits to '" + id + "'?");
  if (amount === null) return;
  const n = parseFloat(amount);
  if (isNaN(n) || n <= 0){ setStatus("Enter a positive number.", true); return; }
  try {
    const t = await api("POST", "/admin/tenants/" + encodeURIComponent(id) + "/credits", { amount: n });
    setStatus("Added " + n + " credits to '" + id + "' (now " + fmt(t.credits_remaining) + ").", false);
    loadTenants();
    if (document.getElementById("detailSection").style.display !== "none") viewTenant(id);
  } catch(e){ fail(e); }
}

function planCell(metered){
  return metered === false
    ? '<span class="pill">unmetered</span>'
    : '<span class="muted">metered</span>';
}

async function toggleMetered(id, metered){
  const msg = metered
    ? "Switch '" + id + "' to METERED?\n\nThis tenant will be charged credits per request again."
    : "Switch '" + id + "' to UNMETERED?\n\nThis tenant will generate unlimited TTS with NO credit charges. Its balance is preserved.";
  if (!confirm(msg)) return;
  try {
    await api("POST", "/admin/tenants/" + encodeURIComponent(id) + "/metered", { metered });
    setStatus("'" + id + "' is now " + (metered ? "metered" : "unmetered") + ".", false);
    loadTenants();
    if (document.getElementById("detailSection").style.display !== "none") viewTenant(id);
  } catch(e){ fail(e); }
}

async function revokeKey(keyId, tenantId){
  if (!confirm("Revoke key " + keyId + "? Clients using it will get 401.")) return;
  try { await api("DELETE", "/admin/keys/" + encodeURIComponent(keyId)); setStatus("Revoked " + keyId + ".", false); viewTenant(tenantId); loadTenants(); }
  catch(e){ fail(e); }
}

async function delTenant(id){
  if (!confirm("Delete tenant '" + id + "' and ALL its keys? This cannot be undone.")) return;
  try { await api("DELETE", "/admin/tenants/" + encodeURIComponent(id)); setStatus("Deleted '" + id + "'.", false);
    document.getElementById("detailSection").style.display = "none"; loadTenants(); }
  catch(e){ fail(e); }
}

async function loadCreditRequests(){
  try {
    const pendingOnly = document.getElementById("creqPendingOnly").checked;
    const q = pendingOnly ? "?status=pending" : "";
    const data = await api("GET", "/admin/credit-requests" + q);
    document.getElementById("conn").textContent = "connected";
    const list = data.requests || [];
    const pendingCount = list.filter(r => r.status === "pending").length;
    const badge = document.getElementById("creqBadge");
    if (pendingCount > 0){ badge.style.display = "inline"; badge.textContent = pendingCount + " pending"; }
    else { badge.style.display = "none"; }
    const rows = list.map(r => `
      <tr>
        <td>${esc(r.tenant_id)}${r.tenant_name ? ' <span class="muted">' + esc(r.tenant_name) + '</span>' : ''}</td>
        <td>${fmt(r.amount)}</td>
        <td>${r.note ? esc(r.note) : '<span class="muted">—</span>'}</td>
        <td>${r.status === "pending" ? r.status : '<span class="pill">' + esc(r.status) + '</span>'}</td>
        <td class="muted">${esc(r.created_at || "")}</td>
        <td class="actions">${r.status === "pending"
          ? `<button onclick="approveRequest('${js(r.request_id)}','${js(r.tenant_id)}',${r.amount})">Approve</button>
             <button class="danger" onclick="rejectRequest('${js(r.request_id)}','${js(r.tenant_id)}')">Reject</button>`
          : `<span class="muted">${esc(r.resolved_note || r.resolved_at || "")}</span>`}</td>
      </tr>`).join("");
    document.getElementById("creqs").innerHTML = rows
      ? `<table><thead><tr><th>tenant</th><th>amount</th><th>note</th><th>status</th><th>requested</th><th>actions</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<span class="muted">No credit requests' + (pendingOnly ? ' pending' : '') + '. Tenants submit them via POST /credits/request.</span>';
  } catch(e){ fail(e); }
}

async function approveRequest(reqId, tenantId, amount){
  if (!confirm("Approve and grant " + Number(amount).toLocaleString() + " credits to '" + tenantId + "'?")) return;
  try {
    await api("POST", "/admin/credit-requests/" + encodeURIComponent(reqId) + "/approve", { note: "" });
    setStatus("Approved request for '" + tenantId + "' (+" + Number(amount).toLocaleString() + " credits).", false);
    loadCreditRequests(); loadTenants();
  } catch(e){ fail(e); }
}

async function rejectRequest(reqId, tenantId){
  const note = prompt("Reject this request from '" + tenantId + "'? Optional reason:");
  if (note === null) return;
  try {
    await api("POST", "/admin/credit-requests/" + encodeURIComponent(reqId) + "/reject", { note });
    setStatus("Rejected request from '" + tenantId + "'.", false);
    loadCreditRequests();
  } catch(e){ fail(e); }
}

let voicesById = {};
async function loadVoices(){
  try {
    const data = await api("GET", "/admin/voices");
    document.getElementById("conn").textContent = "connected";
    voicesById = {};
    (data.voices || []).forEach(v => { voicesById[v.profile_id] = v; });
    fillVoiceSelect();   // keep the sweep voice picker in sync with the loaded voices
    const rows = (data.voices || []).map(v => `
      <tr>
        <td class="breakid">${esc(v.profile_id)}</td>
        <td>${(v.name && v.name !== v.profile_id) ? esc(v.name) : '<span class="muted">unnamed — Rename</span>'}</td>
        <td>${esc(v.owner_tenant_id||"—")}</td>
        <td>${v.is_default ? '<span class="pill">default</span> ' : ''}${esc(v.visibility)}</td>
        <td>${v.ready ? 'ready' : '<span class="muted">not cloned</span>'}</td>
        <td>${v.persistent ? '<span class="pill">persistent</span>' : '<span class="muted">—</span>'}</td>
        <td>${(v.grants && v.grants.length) ? v.grants.map(esc).join(", ") : '<span class="muted">—</span>'}</td>
        <td class="actions">
          ${v.preview_url ? `<button onclick="previewVoice('${js(v.profile_id)}')">▶ Preview</button>` : ''}
          <button onclick="renameVoice('${js(v.profile_id)}')">Rename</button>
          ${v.visibility === 'public'
            ? `<button onclick="setVis('${js(v.profile_id)}','private')">Make private</button>`
            : `<button onclick="setVis('${js(v.profile_id)}','public')">Make public</button>`}
          <button onclick="setVoiceDefault('${js(v.profile_id)}',${v.is_default ? 'false' : 'true'})">${v.is_default ? 'Unset default' : 'Set default'}</button>
          <button onclick="setPersist('${js(v.profile_id)}',${v.persistent ? 'false' : 'true'})">${v.persistent ? 'Unpersist' : 'Persist'}</button>
          <button onclick="assignVoice('${js(v.profile_id)}')">Assign…</button>
          <button class="danger" onclick="delVoice('${js(v.profile_id)}')">Delete</button>
        </td>
      </tr>`).join("");
    document.getElementById("voices").innerHTML = rows
      ? `<table><thead><tr><th>id</th><th>name</th><th>owner</th><th>visibility</th><th>status</th><th>persistent</th><th>assigned to</th><th>actions</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<span class="muted">No voice profiles yet. They appear here once a tenant clones one via the API (POST /tts with a voice_profile_id).</span>';
  } catch(e){ fail(e); }
}

async function setVis(id, visibility){
  try { await api("POST", "/admin/voices/visibility", { profile_id: id, visibility });
    setStatus("'" + id + "' is now " + visibility + ".", false); loadVoices(); }
  catch(e){ fail(e); }
}
async function setVoiceDefault(id, isDefault){
  try { await api("POST", "/admin/voices/default", { profile_id: id, is_default: isDefault });
    setStatus("'" + id + "' default = " + isDefault + ".", false); loadVoices(); }
  catch(e){ fail(e); }
}
async function setPersist(id, persistent){
  try { await api("POST", "/admin/voices/persist", { profile_id: id, persistent });
    setStatus("'" + id + "' persistent = " + persistent + " (warm-loads on restart).", false); loadVoices(); }
  catch(e){ fail(e); }
}

// Modal helpers (shared by the assign picker, preview player, and rename).
let assignVoiceId = null;
function closeModal(){
  document.getElementById("overlay").style.display = "none";
  document.getElementById("modalBody").innerHTML = "";   // also stops any playing preview audio
  assignVoiceId = null;
}

// Preview — play the voice's reference clip in the console (admins only).
function previewVoice(id){
  const v = voicesById[id];
  const url = v && v.preview_url;
  if (!url){ setStatus("No preview audio on file for '" + id + "'.", true); return; }
  document.getElementById("modalTitle").textContent = "Preview voice";
  document.getElementById("modalBody").innerHTML = `
    <div class="row breakid"><strong>${esc((v && v.name && v.name !== id) ? v.name : id)}</strong></div>
    <div class="row"><audio controls autoplay src="${esc(url)}" style="width:100%"></audio></div>
    <div class="row muted breakid">source: <a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a></div>`;
  document.getElementById("overlay").style.display = "flex";
}

// Rename — set the display name tenants receive from GET /voices (instead of the raw id).
async function renameVoice(id){
  const v = voicesById[id];
  const current = (v && v.name && v.name !== id) ? v.name : "";
  const name = prompt("Display name for this voice (this is what tenants see in GET /voices):", current);
  if (name === null) return;
  if (!name.trim()){ setStatus("Name cannot be empty.", true); return; }
  try { await api("PATCH", "/admin/voices", { profile_id: id, name: name.trim() });
    setStatus("Renamed voice to '" + name.trim() + "'.", false); loadVoices(); }
  catch(e){ fail(e); }
}
async function assignVoice(id){
  assignVoiceId = id;
  document.getElementById("modalTitle").textContent = "Assign voice";
  document.getElementById("modalBody").innerHTML = '<span class="muted">Loading tenants…</span>';
  document.getElementById("overlay").style.display = "flex";
  try {
    const data = await api("GET", "/admin/tenants");
    renderAssign(id, data.tenants || []);
  } catch(e){
    // Tenant listing needs a persistent store; fall back to a free-text id so assign still works.
    closeModal();
    if (/503/.test((e && e.message) || "")) { assignByPrompt(id); return; }
    fail(e);
  }
}
function renderAssign(id, tenants){
  if (assignVoiceId !== id) return;
  const granted = new Set((voicesById[id] && voicesById[id].grants) || []);
  const rows = tenants.map(t => {
    const has = granted.has(t.tenant_id);
    return `<tr>
      <td>${esc(t.tenant_id)}</td>
      <td class="muted">${esc(t.name || "")}</td>
      <td>${has ? '<span class="pill">assigned</span>' : ''}</td>
      <td class="actions">${has
        ? `<button class="danger" onclick="toggleGrant('${js(id)}','${js(t.tenant_id)}',false)">Remove</button>`
        : `<button onclick="toggleGrant('${js(id)}','${js(t.tenant_id)}',true)">Assign</button>`}</td>
    </tr>`;
  }).join("");
  document.getElementById("modalBody").innerHTML = `
    <div class="row breakid"><strong>${esc((voicesById[id] && voicesById[id].name) || id)}</strong></div>
    <div class="row muted breakid">${esc(id)}</div>
    <table class="tlist"><thead><tr><th>tenant_id</th><th>name</th><th>status</th><th>action</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">No tenants. Create one first.</td></tr>'}</tbody></table>`;
}
async function toggleGrant(id, tenantId, assign){
  try {
    if (assign) await api("POST", "/admin/voices/grants", { profile_id: id, tenant_id: tenantId });
    else await api("DELETE", "/admin/voices/grants?profile_id=" + encodeURIComponent(id)
                            + "&tenant_id=" + encodeURIComponent(tenantId));
    setStatus((assign ? "Assigned '" : "Removed '") + tenantId + "' " + (assign ? "to" : "from") + " voice.", false);
    await loadVoices();          // refresh the table + cached grants
    const data = await api("GET", "/admin/tenants");
    renderAssign(id, data.tenants || []);
  } catch(e){ fail(e); }
}
async function assignByPrompt(id){
  const t = prompt("Assign voice to which tenant_id?");
  if (t === null || !t.trim()) return;
  try { await api("POST", "/admin/voices/grants", { profile_id: id, tenant_id: t.trim() });
    setStatus("Assigned '" + id + "' to '" + t.trim() + "'.", false); loadVoices(); }
  catch(e){ fail(e); }
}
async function delVoice(id){
  if (!confirm("Delete voice profile '" + id + "' and its cached audio? This cannot be undone.")) return;
  try { await api("DELETE", "/admin/voices?profile_id=" + encodeURIComponent(id));
    setStatus("Deleted voice '" + id + "'.", false); loadVoices(); }
  catch(e){ fail(e); }
}

function copyText(t){ navigator.clipboard.writeText(t).then(()=>setStatus("Copied to clipboard.", false)); }
function fail(e){
  const m = (e && e.message) || String(e);
  if (/401|invalid admin/i.test(m)) setStatus("Auth failed: check your ADMIN_API_KEY.", true);
  else if (/503/.test(m)) setStatus("Admin API unavailable: is TENANT_DB_PATH set (persistent store)?", true);
  else setStatus("Error: " + m, true);
}
function val(id){ return document.getElementById(id).value.trim(); }
function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function js(s){ return String(s).replace(/['\\]/g, "\\$&"); }
function fmt(n){ return (n === null || n === undefined) ? "—" : Number(n).toLocaleString(); }

// ----- Voice sweep / A-B testing -------------------------------------------------
const SWEEP_MAX_CELLS = 24;
let lastSweep = null, sweepUrls = [];

// Mirror the loaded voices into the sweep picker (keeps current selection if still present).
function fillVoiceSelect(){
  const sel = document.getElementById("swVoice");
  if (!sel) return;
  const cur = sel.value;
  const opts = ['<option value="">(default voice)</option>'];
  Object.values(voicesById).forEach(v => {
    const label = (v.name && v.name !== v.profile_id) ? v.name : v.profile_id;
    opts.push('<option value="' + esc(v.profile_id) + '">' + esc(label) + '</option>');
  });
  sel.innerHTML = opts.join("");
  sel.value = cur;
}

// Parse a comma-separated axis field into a clean list of numbers.
function parseAxis(id){
  return document.getElementById(id).value.split(",")
    .map(s => s.trim()).filter(s => s !== "")
    .map(Number).filter(n => !isNaN(n));
}

// Build the request body + the cell count (Cartesian product of the five axes).
function buildSweep(){
  const num_step = parseAxis("swNumStep").map(n => Math.round(n));
  const guidance_scale = parseAxis("swGuidance");
  const class_temperature = parseAxis("swClassTemp");
  const position_temperature = parseAxis("swPosTemp");
  const speed = parseAxis("swSpeed");
  const req = { text: document.getElementById("swText").value,
                num_step, guidance_scale, class_temperature, position_temperature, speed };
  const voice = document.getElementById("swVoice").value;
  if (voice) req.voice_profile_id = voice;
  const seed = document.getElementById("swSeed").value.trim();
  if (seed !== "") req.seed = parseInt(seed, 10);
  const instruct = document.getElementById("swInstruct").value.trim();
  if (instruct) req.instruct = instruct;
  const count = [num_step, guidance_scale, class_temperature, position_temperature, speed]
    .reduce((acc, a) => acc * a.length, 1);
  return { req, count };
}

function updateSweepCount(){
  const { count } = buildSweep();
  const el = document.getElementById("swCount");
  const blocked = count === 0 || count > SWEEP_MAX_CELLS;
  el.textContent = "≈ " + count + " cells (max " + SWEEP_MAX_CELLS + ")";
  el.className = blocked ? "over" : "muted";
  document.getElementById("swRun").disabled = blocked;
}

// base64 -> Blob -> object URL so <audio> can play it (blob: is allowed by the console's media-src).
function b64ToBlobUrl(b64, mime){
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([arr], { type: mime || "audio/wav" }));
  sweepUrls.push(url);
  return url;
}
function clearSweepUrls(){ sweepUrls.forEach(u => URL.revokeObjectURL(u)); sweepUrls = []; }

function cellLabel(p){
  let s = "step" + p.num_step + " g" + p.guidance_scale + " c" + p.class_temperature;
  if (p.position_temperature !== 5) s += " p" + p.position_temperature;
  if (p.speed !== 1) s += " s" + p.speed;
  return s;
}

async function runSweep(){
  const { req, count } = buildSweep();
  if (count === 0){ setStatus("Add at least one value to each axis.", true); return; }
  if (count > SWEEP_MAX_CELLS){ setStatus("Too many cells (" + count + "). Max " + SWEEP_MAX_CELLS + ".", true); return; }
  const btn = document.getElementById("swRun");
  btn.disabled = true; btn.textContent = "Running… (" + count + " cells)";
  setStatus("Running sweep of " + count + " cell(s)… this can take a while on a single GPU.", false);
  try {
    const data = await api("POST", "/admin/sweep", req);
    renderSweep(data);
    const ok = (data.cells || []).filter(c => !c.error).length;
    setStatus("Sweep complete: " + ok + "/" + (data.count || 0) + " cell(s) succeeded.", false);
  } catch(e){ fail(e); }
  finally { btn.textContent = "Run sweep"; updateSweepCount(); }
}

function renderSweep(data){
  clearSweepUrls();
  lastSweep = data;
  const rows = (data.cells || []).map(c => {
    const label = cellLabel(c.params || {});
    if (c.error){
      return '<tr id="swrow-' + c.cell_id + '"><td>' + esc(label) + '</td>'
           + '<td colspan="3">error: ' + esc(c.error) + '</td><td></td></tr>';
    }
    const url = b64ToBlobUrl(c.audio_base64, "audio/wav");
    return '<tr id="swrow-' + c.cell_id + '">'
      + '<td>' + esc(label) + '</td>'
      + '<td>' + (c.gen_ms != null ? c.gen_ms + " ms" : "—") + '</td>'
      + '<td>' + (c.rtf != null ? c.rtf + "×" : "—") + '</td>'
      + '<td><audio controls src="' + url + '"></audio></td>'
      + '<td class="actions"><button onclick="pickCell(' + c.cell_id + ')">★ pick</button></td>'
      + '</tr>';
  }).join("");
  const b = data.base || {};
  document.getElementById("swResults").innerHTML =
    '<div class="muted" style="margin:10px 0 4px">base: “' + esc(b.text || "") + '” · voice '
      + esc(b.voice || "(default)") + ' · seed ' + esc(String(b.seed)) + '</div>'
    + '<table><thead><tr><th>combo</th><th>gen</th><th>rtf</th><th>audio</th><th>pick</th></tr></thead>'
    + '<tbody>' + rows + '</tbody></table><div id="swPick"></div>';
}

let pickedParams = null;
function pickCell(cellId){
  if (!lastSweep) return;
  const cell = (lastSweep.cells || []).find(c => c.cell_id === cellId);
  if (!cell) return;
  document.querySelectorAll('tr[id^="swrow-"]').forEach(r => r.classList.remove("picked"));
  const row = document.getElementById("swrow-" + cellId);
  if (row) row.classList.add("picked");
  pickedParams = JSON.stringify(cell.params, null, 2);
  document.getElementById("swPick").innerHTML =
    '<div class="keybox"><strong>Picked ' + esc(cellLabel(cell.params)) + '</strong> — params for POST /tts:'
    + '<pre>' + esc(pickedParams) + '</pre><button onclick="copyPick()">Copy params</button></div>';
}
function copyPick(){ if (pickedParams) copyText(pickedParams); }

// Restore key for this tab if present.
if (getKey()){ document.getElementById("adminKey").value = getKey(); loadTenants(); loadCreditRequests(); loadVoices(); }
updateSweepCount();   // show the cell counter immediately (inputs are prefilled with the Tier-1 sweep)
</script>
</body>
</html>
"""
