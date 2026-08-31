/* kirinuki - interface behaviour.
   Loaded with `defer` from index.html, so the DOM exists by the time this
   runs and $() lookups at the top level are safe. */

const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const genId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);

// File names come from the user's disk and end up inside innerHTML templates
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function warn(context, err) {
  console.warn(`[kirinuki] ${context}:`, err);
}

const ICON_CLOSE = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
const ICON_TRASH = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';
const ICON_WARN = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
const ICON_EXPAND = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
const ICON_CHECK = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

const SWATCHES = [
  { bg: "checker", cls: "checker", title: "Transparent" },
  { bg: "#ffffff", title: "White" }, { bg: "#000000", title: "Black" }, { bg: "#808080", title: "Gray" },
  { bg: "#3ddc97", title: "Green" }, { bg: "#5b8def", title: "Blue" },
];

let MODELS = {}, SIZES = {}, INFO = {}, DOWNLOADED = {}, DEFAULT_MODEL = null;
let PEAK_MB = {}, AVAILABLE_MB = null, LOADED = [];
let PROCESS_MB = 0, HEADROOM_MB = 700, MAX_PROCESS_PX = 1600;
let selectedModel = null;
let resultBg = "checker";
let jobs = [];                 // all jobs across sessions (persisted)
let sessions = [];             // [{id, createdAt}]
let currentSessionId = null;   // session new uploads go to
let viewedSessionId = null;    // session shown in the results area
let processing = false;
let processingJobId = null;   // id of the job currently in flight, or null
function sessionCreatedAt(id) { const s = sessions.find(x => x.id === id); return s ? s.createdAt : Date.now(); }

//  Human-readable time
function timeAgo(ts) {
  const s = Math.floor((Date.now() - ts) / 1000);
  if (s < 10) return "just now";
  if (s < 60) return s + "s ago";
  const m = Math.floor(s / 60); if (m < 60) return m + " min ago";
  const h = Math.floor(m / 60); if (h < 24) return h + " h ago";
  const d = Math.floor(h / 24); if (d < 7) return d + " day" + (d > 1 ? "s" : "") + " ago";
  try { return new Date(ts).toLocaleDateString([], { month: "short", day: "numeric" }); } catch { return ""; }
}
function refreshAgos() { document.querySelectorAll(".ago").forEach(el => { const ts = +el.dataset.ts; if (ts) el.textContent = timeAgo(ts); }); }
setInterval(refreshAgos, 30000);

//  Theme ===
// Three choices: "light", "dark", or "system" (follow the OS). The choice is
// stored in localStorage; "system" stores nothing but the word itself and lets
// the CSS media query decide, so the UI tracks the OS as it changes.
const THEME_KEY = "rmbg-theme";
const THEMES = ["light", "system", "dark"];

function storedTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return THEMES.includes(v) ? v : "system";
  } catch (e) { warn("reading theme preference", e); return "system"; }
}

function applyTheme(choice) {
  if (choice === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", choice);
  for (const btn of document.querySelectorAll(".theme-opt")) {
    btn.classList.toggle("sel", btn.dataset.themeChoice === choice);
    btn.setAttribute("aria-pressed", String(btn.dataset.themeChoice === choice));
  }
}

function setTheme(choice) {
  applyTheme(choice);
  try { localStorage.setItem(THEME_KEY, choice); } catch (e) { warn("saving theme preference", e); }
}

for (const btn of document.querySelectorAll(".theme-opt")) {
  btn.addEventListener("click", () => setTheme(btn.dataset.themeChoice));
}
applyTheme(storedTheme());

//  IndexedDB persistence
const DB_NAME = "removebg-local", STORE = "results";
function idbOpen() {
  return new Promise((res, rej) => {
    let req;
    try {
      req = indexedDB.open(DB_NAME, 1);
    } catch (e) {
      rej(e);
      return;
    }
    req.onupgradeneeded = () => { const db = req.result; if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "id" }); };
    req.onsuccess = () => res(req.result);
    req.onerror = () => rej(req.error || new Error("IndexedDB could not be opened"));
    req.onblocked = () => rej(new Error("IndexedDB is blocked by another tab of this page"));
  });
}

function txDone(tx, res, rej) {
  tx.oncomplete = () => res();
  tx.onerror = () => rej(tx.error || new Error("IndexedDB write failed"));
  tx.onabort = () => rej(tx.error || new Error("IndexedDB transaction aborted"));
}

async function idbPut(rec) { const db = await idbOpen(); return new Promise((res, rej) => { const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).put(rec); txDone(tx, res, rej); }); }

async function idbPatch(id, fields) {
  const db = await idbOpen();
  return new Promise((res, rej) => {
    const tx = db.transaction(STORE, "readwrite");
    const store = tx.objectStore(STORE);
    const get = store.get(id);
    get.onsuccess = () => {
      const rec = get.result;
      if (!rec) { res(); return; }      // nothing saved yet; persistJob will
      store.put(Object.assign(rec, fields));
    };
    txDone(tx, res, rej);
  });
}
async function idbDelete(id) { const db = await idbOpen(); return new Promise((res, rej) => { const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).delete(id); txDone(tx, res, rej); }); }
async function idbClear() { const db = await idbOpen(); return new Promise((res, rej) => { const tx = db.transaction(STORE, "readwrite"); tx.objectStore(STORE).clear(); txDone(tx, res, rej); }); }
async function idbAll() { const db = await idbOpen(); return new Promise((res, rej) => { const tx = db.transaction(STORE, "readonly"); const rq = tx.objectStore(STORE).getAll(); rq.onsuccess = () => res(rq.result || []); rq.onerror = () => rej(rq.error); }); }

function fmtBytes(n) {
  const mb = n / 1048576;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

let storageWarned = false;

const STORAGE_BLOCKED_ERRORS = ["UnknownError", "SecurityError", "InvalidStateError"];

async function classifyStorageError(err) {
  const name = err && err.name;
  if (name === "QuotaExceededError" || name === "NS_ERROR_DOM_QUOTA_REACHED") return "full";
  if (STORAGE_BLOCKED_ERRORS.includes(name)) return "blocked";
  // A generic abort with the quota nearly gone is a full disk whatever the
  // browser chose to call it -- Chrome on Windows does not always name it.
  const est = await storageUsage();
  if (est && est.quota && est.usage / est.quota > 0.9) return "full";
  return "unknown";
}

async function reportStorageFailure(err, what) {
  const kind = await classifyStorageError(err);
  if (kind === "unknown") {
    toast(`Could not save "${what}" for later — see the console for why`, "err");
    return;
  }
  if (storageWarned) return;
  storageWarned = true;
  if (kind === "blocked") {
    toast("This browser is blocking storage for this page, so results cannot be kept. They stay on screen until you reload. Allow site data for this address, or download what you need first.", "err");
    return;
  }
  const est = await storageUsage();
  const room = est && est.quota ? ` (${fmtBytes(est.usage)} of ${fmtBytes(est.quota)} used)` : "";
  toast(`Out of browser storage${room}. Results stay on screen but will be lost on reload — delete older sessions to free space.`, "err");
}

async function persistJob(job) {
  if (job.state !== "done" || !job.inBlob || !job.outBlob) return;
  try {
    await idbPut({ id: job.id, sessionId: job.sessionId, sessionCreatedAt: sessionCreatedAt(job.sessionId), createdAt: job.createdAt, name: job.name, model: job.model, ms: job.ms, outKB: job.outKB, bg: job.bg, inBlob: job.inBlob, outBlob: job.outBlob, exif: job.exif });
  } catch (e) {
    warn(`saving ${job.name}`, e);
    await reportStorageFailure(e, job.name);
  }
}

// Rough storage usage, shown in the sidebar so filling the quota is visible
// before it bites rather than after a reload loses work.
async function storageUsage() {
  try {
    if (!navigator.storage || !navigator.storage.estimate) return null;
    const { usage, quota } = await navigator.storage.estimate();
    if (!usage) return null;
    return { usage, quota: quota || 0 };
  } catch (e) { warn("reading storage estimate", e); return null; }
}

//  Toast / status
const toastEl = $("toast"); let toastTimer = null;
function toast(msg, kind = "") { toastEl.textContent = msg; toastEl.className = "toast show " + kind; clearTimeout(toastTimer); toastTimer = setTimeout(() => toastEl.className = "toast " + kind, 3500); }
function setStatus(text, busy = false) { $("status-text").textContent = text; $("dot").style.background = busy ? "var(--accent)" : "var(--ok)"; }

//  Model status API
async function getStatus(model) { const r = await fetch("/model_status?model=" + encodeURIComponent(model)); if (!r.ok) throw new Error("status check failed"); return r.json(); }
async function warmup(model) { const fd = new FormData(); fd.append("model", model); await fetch("/warmup", { method: "POST", body: fd }); }
async function pinDefaultModel(model, warm) {
  // Tells the server this is the active default; the server evicts any other
  // loaded model from RAM so we don't pile up BiRefNet + ISNet + U2Net at once.
  const fd = new FormData(); fd.append("model", model); if (warm) fd.append("warmup", "true");
  try { await fetch("/set_default_model", { method: "POST", body: fd }); } catch (e) { warn("pinning default model", e); }
}
async function waitForModel(model, onTick) {
  let s = await getStatus(model); if (s.state === "ready") return s;
  await warmup(model);
  while (true) { s = await getStatus(model); if (onTick) onTick(s); if (s.state === "ready") return s; if (s.state === "error") throw new Error(s.error || "Model failed to load"); await sleep(800); }
}

//  Models
async function loadModels() {
  const data = await (await fetch("/models")).json();
  MODELS = data.available; SIZES = data.sizes_mb || {}; INFO = data.info || {}; DOWNLOADED = data.downloaded || {}; DEFAULT_MODEL = data.default;
  PEAK_MB = data.peak_mb || {}; AVAILABLE_MB = data.available_mb; LOADED = data.loaded || [];
  PROCESS_MB = data.process_mb || 0; HEADROOM_MB = data.headroom_mb != null ? data.headroom_mb : 700;
  if (data.max_process_px != null) MAX_PROCESS_PX = data.max_process_px;
  selectedModel = selectedModel || DEFAULT_MODEL;
  renderModelDropdown(); renderModelsPage(); updateMemoryWarning();
}
function renderModelDropdown() {
  const info = INFO[selectedModel] || {};
  $("t-title").textContent = (info.title || selectedModel) + (SIZES[selectedModel] ? `  ·  ~${SIZES[selectedModel]} MB` : "");
  $("t-tag").textContent = info.tagline || "";
  const menu = $("model-menu"); menu.innerHTML = "";
  for (const [key, info2] of Object.entries(INFO)) {
    const avail = !!DOWNLOADED[key];
    const opt = document.createElement("div");
    opt.className = "dd-opt" + (key === selectedModel ? " sel" : "") + (avail ? "" : " disabled");
    const na = avail ? "" : `<span class="o-na">Not downloaded</span>`;
    opt.innerHTML = `<span class="o-main"><span class="o-title">${info2.title}<span class="o-size">~${SIZES[key]} MB</span>${na}</span><span class="o-tag">${info2.tagline || ""}</span></span><span class="o-check">${ICON_CHECK}</span>`;
    if (avail) opt.addEventListener("click", () => { selectedModel = key; closeDropdown(); renderModelDropdown(); pinDefaultModel(key, false); refreshMemoryWarning(); });
    else opt.title = "Download it on the Models page first";
    menu.appendChild(opt);
  }
}
function openDropdown() { $("model-menu").hidden = false; }
function closeDropdown() { $("model-menu").hidden = true; }
$("model-trigger").addEventListener("click", (e) => { e.stopPropagation(); const m = $("model-menu"); m.hidden ? openDropdown() : closeDropdown(); });
document.addEventListener("click", (e) => { if (!$("model-dd").contains(e.target)) closeDropdown(); });
async function maybeWarm(model) { try { const s = await getStatus(model); if (s.state !== "ready") { toast(`Preparing ${INFO[model]?.title || model} in the background…`); warmup(model); } } catch (e) { warn("warming model", e); } }

function renderModelsPage() {
  const list = $("models-list"); list.innerHTML = "";
  for (const [key, info] of Object.entries(INFO)) {
    const card = document.createElement("div");
    card.className = "model-card" + (key === DEFAULT_MODEL ? " is-default" : "");
    card.innerHTML = `<div class="top"><span class="mtitle">${info.title}</span>${key === DEFAULT_MODEL ? `<span class="tag">Default</span>` : ""}<span class="id">${key}</span></div><div class="tagline">${info.tagline || ""}</div><div class="desc">${info.description}</div><div class="facts"><div class="fact"><b>Speed</b>${info.speed}</div><div class="fact"><b>Quality</b>${info.quality}</div><div class="fact"><b>Best for</b>${info.best_for}</div><div class="fact"><b>Download</b>~${SIZES[key]} MB</div><div class="dl" id="dl-${key}"></div></div>`;
    list.appendChild(card); renderDlArea(key);
  }
}
function renderDlArea(key, statusObj) {
  const el = $("dl-" + key); if (!el) return;
  const downloaded = statusObj ? statusObj.downloaded : DOWNLOADED[key];
  const state = statusObj ? statusObj.state : null;
  if (state === "loading") {
    const pct = Math.round(((statusObj && statusObj.progress != null ? statusObj.progress : 0)) * 100);
    const label = (statusObj && statusObj.progress != null && statusObj.progress < 1) ? `Downloading ${pct}%` : "Initializing…";
    el.innerHTML = `<div class="progress"><span style="width:${pct}%"></span></div><div class="dl-label">${label}</div>`; return;
  }
  el.innerHTML = "";
  if (downloaded) {
    const badge = document.createElement("span"); badge.className = "badge-dl"; badge.textContent = "Downloaded";
    el.appendChild(badge);
    // A loaded model holds its weights in RAM until the idle timer drops it.
    // Offer to free it now, for when the machine is short of memory.
    if (LOADED.includes(key)) {
      const mem = document.createElement("span");
      mem.className = "badge-mem"; mem.textContent = "In memory";
      const unload = document.createElement("button");
      unload.className = "btn-del"; unload.textContent = "Free RAM";
      unload.title = "Drop this model from memory; it reloads from disk next time";
      unload.addEventListener("click", () => unloadModelUI(key));
      el.appendChild(mem); el.appendChild(unload);
    }
    const del = document.createElement("button"); del.className = "btn-del"; del.textContent = "Delete";
    del.addEventListener("click", () => deleteModelUI(key));
    el.appendChild(del);
  } else {
    const btn = document.createElement("button"); btn.textContent = "Download"; btn.addEventListener("click", () => downloadModelUI(key));
    el.appendChild(btn);
  }
}
async function downloadModelUI(key) {
  try {
    await warmup(key);
    while (true) { const s = await getStatus(key); renderDlArea(key, s); if (s.state === "ready" || (s.downloaded && s.state !== "loading")) break; if (s.state === "error") throw new Error(s.error || "download failed"); await sleep(600); }
    DOWNLOADED[key] = true; renderDlArea(key); renderModelDropdown(); toast(`${INFO[key]?.title || key} downloaded`, "ok");
  } catch (e) { toast(e.message || "Download failed", "err"); renderDlArea(key); }
}
async function unloadModelUI(key) {
  try {
    const fd = new FormData(); fd.append("model", key);
    const r = await fetch("/unload_model", { method: "POST", body: fd });
    if (!r.ok) throw new Error("Could not free the model");
    await loadModels();
    toast(`${INFO[key]?.title || key} freed from memory`, "ok");
  } catch (e) { warn("unloading model", e); toast(e.message || "Could not free the model", "err"); }
}

async function deleteModelUI(key) {
  if (!confirm(`Delete the ${INFO[key]?.title || key} model (~${SIZES[key]} MB) from disk?\n\nYou can download it again later.`)) return;
  try {
    const fd = new FormData(); fd.append("model", key);
    const r = await fetch("/delete_model", { method: "POST", body: fd });
    if (!r.ok) { let d = "Delete failed"; try { d = (await r.json()).detail || d; } catch { } throw new Error(d); }
    DOWNLOADED[key] = false;
    if (selectedModel === key) { const firstDl = Object.keys(INFO).find(k => DOWNLOADED[k]); selectedModel = firstDl || DEFAULT_MODEL; }
    renderDlArea(key); renderModelDropdown(); toast(`${INFO[key]?.title || key} deleted`, "ok");
  } catch (e) { toast(e.message || "Delete failed", "err"); }
}

//  View nav 
document.querySelectorAll(".nav-tab").forEach(tab => tab.addEventListener("click", () => switchView(tab.dataset.view)));
function switchView(name) { document.querySelectorAll(".nav-tab").forEach(t => t.classList.toggle("active", t.dataset.view === name)); $("view-editor").hidden = name !== "editor"; $("view-models").hidden = name !== "models"; }

//  Background swatches
function buildSwatches(container, current, onPick, small) {
  container.querySelectorAll(".swatch").forEach(s => s.remove());
  for (const sw of SWATCHES) {
    const b = document.createElement("button");
    b.className = "swatch" + (small ? " sm" : "") + (sw.cls ? " " + sw.cls : "") + (current === sw.bg ? " active" : "");
    if (!sw.cls) b.style.background = sw.bg; b.title = sw.title || sw.bg;
    b.addEventListener("click", () => onPick(sw.bg)); container.appendChild(b);
  }
  const custom = document.createElement("span");
  custom.className = "swatch custom" + (small ? " sm" : "") + (current && !SWATCHES.some(s => s.bg === current) ? " active" : "");
  custom.title = "Custom color";
  const inp = document.createElement("input"); inp.type = "color"; inp.value = (current && current.startsWith && current.startsWith("#")) ? current : "#ff8800";
  inp.addEventListener("input", (e) => onPick(e.target.value)); custom.appendChild(inp); container.appendChild(custom);
}
function buildGlobalSwatches() {
  buildSwatches($("global-bg"), resultBg, (bg) => {
    resultBg = bg;
    buildGlobalSwatches();
    for (const j of jobs) if (j.state === "done") paintCardBackdrop(j);
    updateMetadataNote();
  }, false);
}
function effectiveBg(job) { return job.bg != null ? job.bg : resultBg; }

//  Edge refinement options
function estimatePeakMb(model, vitmatte, decontaminate) {
  const px = MAX_PROCESS_PX > 0 ? MAX_PROCESS_PX * MAX_PROCESS_PX : 3000 * 3000;
  const mp = Math.max(1, px / 1000000);
  let mb = 260 + (PEAK_MB[model] || 2000) + mp * 320;
  if (vitmatte) mb = Math.max(mb, 260 + 2500 + mp * 900);
  if (decontaminate) mb += mp * 550;
  return mb;
}

// EXIF, ICC and DPI ride along on a plain PNG download
function updateMetadataNote() {
  const el = $("meta-note");
  if (!el) return;
  const fmt = $("dl-format").value;
  const serverBg = $("srvbg-enabled").checked;
  const rebuilt = fmt !== "png" || $("trim-enabled").checked
    || (resultBg !== "checker" && !serverBg);
  if (!rebuilt) {
    el.textContent = "Downloads keep the original's EXIF, colour profile and DPI.";
  } else if (fmt === "jpg") {
    el.textContent = "Note: JPG downloads keep the original's EXIF, but not the colour profile or DPI. A plain PNG download keeps all three.";
  } else {
    el.textContent = "Note: this rebuilds the image, so EXIF, colour profile and DPI are not carried over. A plain PNG download keeps them.";
  }
}

// Mirrors the server's budget exactly (see the guard in /remove)
function memoryBudgetMb() {
  if (AVAILABLE_MB == null) return null;
  return AVAILABLE_MB + PROCESS_MB - HEADROOM_MB;
}

// The numbers move as other applications open and close, so a warning drawn
// once at load time goes stale. Refresh before showing it.
async function refreshMemory() {
  try {
    const d = await (await fetch("/models")).json();
    if (d.available_mb != null) AVAILABLE_MB = d.available_mb;
    if (d.process_mb != null) PROCESS_MB = d.process_mb;
    if (d.headroom_mb != null) HEADROOM_MB = d.headroom_mb;
    LOADED = d.loaded || LOADED;
  } catch (e) { warn("refreshing memory figures", e); }
}

function updateMemoryWarning() {
  const el = $("mem-warn");
  if (!el) return;
  const budget = memoryBudgetMb();
  if (budget == null || !selectedModel) { el.hidden = true; return; }
  const need = estimatePeakMb(selectedModel, $("vm-enabled").checked, $("dc-enabled").checked);
  if (need <= budget) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = `Needs about ${(need / 1024).toFixed(1)} GB, but only `
    + `${(budget / 1024).toFixed(1)} GB is usable — this will be refused`;
}

async function refreshMemoryWarning() {
  await refreshMemory();
  updateMemoryWarning();
}

function syncRefinementOptions() {
  const vm = $("vm-enabled").checked;
  const amRow = $("am-enabled").closest(".row");
  $("am-enabled").disabled = vm;
  ["am-fg", "am-bg", "am-erode"].forEach(id => { $(id).disabled = vm || !$("am-enabled").checked; });
  amRow.classList.toggle("disabled", vm);
}
$("vm-enabled").addEventListener("change", () => { if ($("vm-enabled").checked) $("am-enabled").checked = false; syncRefinementOptions(); refreshMemoryWarning(); });
$("am-enabled").addEventListener("change", syncRefinementOptions);
$("dc-enabled").addEventListener("change", refreshMemoryWarning);
$("trim-enabled").addEventListener("change", updateMetadataNote);
$("srvbg-enabled").addEventListener("change", updateMetadataNote);
$("dl-format").addEventListener("change", updateMetadataNote);
syncRefinementOptions();
updateMetadataNote();

//  Drag & drop / paste
const drop = $("drop"), fileInput = $("file");
drop.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", (e) => { if (e.target.files.length) enqueue([...e.target.files]); fileInput.value = ""; });
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("dragover"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("dragover"); }));
drop.addEventListener("drop", (e) => { const f = [...(e.dataTransfer.files || [])]; if (f.length) enqueue(f); });
document.addEventListener("paste", (e) => { const imgs = []; for (const it of e.clipboardData.items) if (it.type.startsWith("image/")) { const f = it.getAsFile(); if (f) imgs.push(f); } if (imgs.length) enqueue(imgs); });

//  Queue
function enqueue(files) {
  let added = 0;
  for (const f of files) {
    if (!f.type.startsWith("image/")) { toast(`Skipped "${f.name}" (not an image)`, "err"); continue; }
    jobs.push({ id: genId(), sessionId: currentSessionId, createdAt: Date.now(), file: f, inBlob: f, name: f.name || `image-${added}.png`, model: selectedModel, state: "queued", inUrl: URL.createObjectURL(f), outUrl: null, outBlob: null, ms: null, outKB: null, bg: null, err: null, hidden: false });
    added++;
  }
  if (added) { viewedSessionId = currentSessionId; renderAll(); warnIfStorageNearlyFull(); pump(); }
}

async function warnIfStorageNearlyFull() {
  if (storageWarned) return;
  const est = await storageUsage();
  if (!est || !est.quota) return;
  if (est.usage / est.quota < 0.85) return;
  storageWarned = true;
  toast(`Browser storage is nearly full (${fmtBytes(est.usage)} of ${fmtBytes(est.quota)}). Delete older sessions or new results will not survive a reload.`, "err");
}

async function pump() {
  if (processing) return;
  processing = true;
  try {
    while (true) {
      const job = jobs.find(j => j.state === "queued");
      if (!job) break;
      processingJobId = job.id;
      try {
        await runJob(job);
      } catch (e) {
        if (jobs.includes(job)) { job.state = "error"; job.err = e.message || "Error"; }
        warn(`processing ${job.name}`, e);
        toast(e.message || "Error", "err");
      }
      renderAll();
    }
  } finally {
    processingJobId = null;
    processing = false;
    setStatus("ready");
    renderAll();
  }
}

async function runJob(job) {
  const t0 = performance.now();
  let st = await getStatus(job.model);
  if (st.state !== "ready") {
    // Already on disk means we are only reading it into RAM, which is a very
    // different wait from a first-time download: say which one it is.
    job.state = st.downloaded ? "loading-model" : "downloading-model";
    renderAll();
    setStatus(st.downloaded ? `loading ${job.model} into memory…` : `downloading ${job.model}…`, true);
    await waitForModel(job.model);
  }
  job.state = "processing"; renderAll(); setStatus("processing…", true);
  const fd = new FormData(); fd.append("image", job.file); fd.append("model", job.model);
  if (job.transient || job.model !== selectedModel) fd.append("transient", "true");
  if ($("vm-enabled").checked) fd.append("vitmatte", "true");
  else if ($("am-enabled").checked) { fd.append("alpha_matting", "true"); fd.append("alpha_matting_foreground_threshold", $("am-fg").value); fd.append("alpha_matting_background_threshold", $("am-bg").value); fd.append("alpha_matting_erode_size", $("am-erode").value); }
  if ($("dc-enabled").checked) fd.append("decontaminate", "true");
  if ($("pp-enabled").checked) fd.append("post_process_mask", "true");
  if ($("srvbg-enabled").checked) {
    const bg = effectiveBg(job);
    if (bg !== "checker") fd.append("bgcolor", bg);
  }
  const r = await fetch("/remove", { method: "POST", body: fd });
  if (!r.ok) { let d = "Server error"; try { d = (await r.json()).detail || d; } catch { } throw new Error(d); }
  const blob = await r.blob();
  if (job.outUrl) URL.revokeObjectURL(job.outUrl);
  job.outBlob = blob; job.outUrl = URL.createObjectURL(blob); job.outKB = (blob.size / 1024).toFixed(0);
  if (job.outPreview) URL.revokeObjectURL(job.outPreview);
  job.outPreview = await makePreviewUrl(blob);
  if (!job.inPreview && job.inBlob) job.inPreview = await makePreviewUrl(job.inBlob);
  job.ms = r.headers.get("X-Processing-Time") || ((performance.now() - t0) / 1000).toFixed(2);
  job.exif = decodeExifHeader(r.headers.get("X-Exif"));
  job.createdAt = Date.now(); job.state = "done";
  if (!jobs.includes(job)) {
    if (job.outUrl) URL.revokeObjectURL(job.outUrl);
    return;
  }
  persistJob(job);
}

function reprocessJob(job, newModel) {
  if (!confirm(`Reprocess "${job.name}" with ${INFO[newModel]?.title || newModel}?\n\nThis replaces the current result for this image.`)) return;
  job.model = newModel; job.state = "queued"; job.err = null;
  job.transient = newModel !== selectedModel;
  if (job.outUrl) { URL.revokeObjectURL(job.outUrl); job.outUrl = null; }
  if (job.outPreview) { URL.revokeObjectURL(job.outPreview); job.outPreview = null; }
  job.outBlob = null; job.ms = null; job.outKB = null;
  renderAll(); pump();
}

//  Export
// Cards show images at ~320px but hold the full-resolution originals, so the
// browser rescales several megapixels on every repaint.
const CARD_PREVIEW_PX = 640;   // 2x the display size, for high-DPI screens

async function makePreviewUrl(blob) {
  try {
    const bmp = await createImageBitmap(blob);
    const scale = Math.min(1, CARD_PREVIEW_PX / Math.max(bmp.width, bmp.height));
    if (scale === 1) { bmp.close(); return null; }   // already small enough
    const w = Math.max(1, Math.round(bmp.width * scale));
    const h = Math.max(1, Math.round(bmp.height * scale));
    const cv = document.createElement("canvas");
    cv.width = w; cv.height = h;
    cv.getContext("2d").drawImage(bmp, 0, 0, w, h);
    bmp.close();
    const small = await new Promise(r => cv.toBlob(r, "image/png"));
    return small ? URL.createObjectURL(small) : null;
  } catch (e) {
    warn("building a card preview", e);
    return null;   // fall back to the full-resolution image
  }
}

function makeImg(url, alt) {
  const img = document.createElement("img"); img.alt = alt;
  img.loading = "lazy";
  img.decoding = "async";
  img.addEventListener("error", () => { const d = document.createElement("div"); d.className = "errmsg"; d.innerHTML = ICON_WARN + " Asset unavailable"; if (img.parentNode) img.replaceWith(d); });
  img.src = url || ""; if (!url) img.dispatchEvent(new Event("error")); return img;
}
function loadImage(url) { return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = url; }); }
function alphaBounds(ctx, w, h, threshold = 8) {
  let data;
  try {
    data = ctx.getImageData(0, 0, w, h).data;
  } catch (e) {
    warn("reading pixels for trim", e);
    return null;
  }
  let top = -1, left = w, right = -1, bottom = -1;
  for (let y = 0; y < h; y++) {
    const row = y * w * 4;
    for (let x = 0; x < w; x++) {
      if (data[row + x * 4 + 3] <= threshold) continue;
      if (top === -1) top = y;
      bottom = y;
      if (x < left) left = x;
      if (x > right) right = x;
    }
  }
  if (top === -1) return null;
  return { x: left, y: top, w: right - left + 1, h: bottom - top + 1 };
}

async function renderJobCanvas(job, format, opts = {}) {
  const eff = effectiveBg(job);
  const img = await loadImage(job.outUrl);
  const transparent = eff === "checker";

  let sx = 0, sy = 0, sw = img.naturalWidth, sh = img.naturalHeight;
  if (opts.trim) {
    const probe = document.createElement("canvas");
    probe.width = sw; probe.height = sh;
    probe.getContext("2d", { willReadFrequently: true }).drawImage(img, 0, 0);
    const box = alphaBounds(probe.getContext("2d", { willReadFrequently: true }), sw, sh);
    if (box) {
      const pad = Math.max(0, opts.padding | 0);
      sx = Math.max(0, box.x - pad);
      sy = Math.max(0, box.y - pad);
      sw = Math.min(img.naturalWidth - sx, box.w + pad * 2);
      sh = Math.min(img.naturalHeight - sy, box.h + pad * 2);
    }
  }

  const cv = document.createElement("canvas");
  cv.width = sw; cv.height = sh;
  const ctx = cv.getContext("2d");
  if (!transparent) { ctx.fillStyle = eff; ctx.fillRect(0, 0, sw, sh); }
  else if (format === "jpg") { ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, sw, sh); }
  ctx.drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
  return cv;
}

function exportMime(format) {
  return format === "png" ? "image/png" : format === "webp" ? "image/webp" : "image/jpeg";
}
function exportExt(format) { return format === "jpg" ? "jpg" : format; }
function exportName(job, format) {
  return job.name.replace(/\.[^.]+$/, "") + "_nobg." + exportExt(format);
}

// A canvas round-trip discards everything the server attached
function canUseServerBlob(job, format, opts) {
  return format === "png"
    && !opts.trim
    && effectiveBg(job) === "checker"
    && !!job.outBlob;
}

function withJpegExif(bytes, exif) {
  if (!exif || bytes[0] !== 0xFF || bytes[1] !== 0xD8) return bytes;
  let at = 2;
  while (at + 3 < bytes.length && bytes[at] === 0xFF &&
    (bytes[at + 1] === 0xE0 || bytes[at + 1] === 0xE1)) {
    at += 2 + ((bytes[at + 2] << 8) | bytes[at + 3]);
  }

  const size = exif.length + 2 + 6;          // segment length + "Exif\0\0"
  if (size > 0xFFFF) return bytes;           // too large for one APP1 segment
  const seg = new Uint8Array(4 + 6 + exif.length);
  seg[0] = 0xFF; seg[1] = 0xE1;
  seg[2] = ((exif.length + 8) >> 8) & 0xFF;
  seg[3] = (exif.length + 8) & 0xFF;
  seg.set([0x45, 0x78, 0x69, 0x66, 0x00, 0x00], 4);   // "Exif\0\0"
  seg.set(exif, 10);

  const out = new Uint8Array(2 + seg.length + (bytes.length - at));
  out.set(bytes.subarray(0, 2), 0);
  out.set(seg, 2);
  out.set(bytes.subarray(at), 2 + seg.length);
  return out;
}

function decodeExifHeader(value) {
  if (!value) return null;
  try {
    const bin = atob(value);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch (e) { warn("decoding EXIF header", e); return null; }
}

async function jobBlob(job, format, opts) {
  if (canUseServerBlob(job, format, opts)) return job.outBlob;
  const cv = await renderJobCanvas(job, format, opts);
  const blob = await new Promise(r => cv.toBlob(r, exportMime(format), format === "png" ? undefined : 0.92));
  if (format !== "jpg" || !job.exif) return blob;
  try {
    const patched = withJpegExif(new Uint8Array(await blob.arrayBuffer()), job.exif);
    return new Blob([patched], { type: "image/jpeg" });
  } catch (e) {
    warn("attaching EXIF to the JPEG", e);
    return blob;   // the image matters more than its metadata
  }
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

async function downloadMask(job) {
  if (!job.inBlob) return;
  toast("Rendering the mask…");
  try {
    const fd = new FormData();
    fd.append("image", job.inBlob, job.name);
    fd.append("model", job.model);
    fd.append("only_mask", "true");
    fd.append("transient", "true");
    const r = await fetch("/remove", { method: "POST", body: fd });
    if (!r.ok) { let d = "Server error"; try { d = (await r.json()).detail || d; } catch { } throw new Error(d); }
    saveBlob(await r.blob(), job.name.replace(/\.[^.]+$/, "") + "_mask.png");
  } catch (e) {
    warn(`rendering the mask for ${job.name}`, e);
    toast(e.message || "Could not render the mask", "err");
  }
}

async function exportJob(job, format) {
  if (!job.outUrl) return;
  try {
    const blob = await jobBlob(job, format, exportOptions());
    saveBlob(blob, exportName(job, format));
  } catch (e) {
    warn(`exporting ${job.name}`, e);
    toast(`Could not export "${job.name}"`, "err");
  }
}

function exportOptions() {
  return { trim: $("trim-enabled").checked, padding: parseInt($("trim-pad").value, 10) || 0 };
}

// ZIP

function crc32(bytes) {
  let c, crc = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) {
    c = (crc ^ bytes[i]) & 0xFF;
    for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0xEDB88320 : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function zipDosTime(d) {
  const time = ((d.getHours() & 31) << 11) | ((d.getMinutes() & 63) << 5) | ((d.getSeconds() / 2) & 31);
  const date = (((d.getFullYear() - 1980) & 127) << 9) | (((d.getMonth() + 1) & 15) << 5) | (d.getDate() & 31);
  return { time, date };
}

function buildZip(files) {
  const enc = new TextEncoder();
  const chunks = [], central = [];
  let offset = 0;
  const { time, date } = zipDosTime(new Date());

  for (const f of files) {
    const nameBytes = enc.encode(f.name);
    const crc = crc32(f.data);
    const local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034b50, true);
    local.setUint16(4, 20, true);          // version needed
    local.setUint16(6, 0x0800, true);      // UTF-8 filename flag
    local.setUint16(8, 0, true);           // stored, no compression
    local.setUint16(10, time, true); local.setUint16(12, date, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, f.data.length, true);
    local.setUint32(22, f.data.length, true);
    local.setUint16(26, nameBytes.length, true);
    local.setUint16(28, 0, true);
    chunks.push(new Uint8Array(local.buffer), nameBytes, f.data);

    const cen = new DataView(new ArrayBuffer(46));
    cen.setUint32(0, 0x02014b50, true);
    cen.setUint16(4, 20, true); cen.setUint16(6, 20, true);
    cen.setUint16(8, 0x0800, true);
    cen.setUint16(10, 0, true);
    cen.setUint16(12, time, true); cen.setUint16(14, date, true);
    cen.setUint32(16, crc, true);
    cen.setUint32(20, f.data.length, true);
    cen.setUint32(24, f.data.length, true);
    cen.setUint16(28, nameBytes.length, true);
    cen.setUint32(42, offset, true);
    central.push(new Uint8Array(cen.buffer), nameBytes);
    offset += 30 + nameBytes.length + f.data.length;
  }

  let centralSize = 0;
  for (const c of central) centralSize += c.length;
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);

  return new Blob([...chunks, ...central, new Uint8Array(end.buffer)], { type: "application/zip" });
}

// Two files from the same batch can share a name (the compare feature makes
// that likely, since every model produces the same filename).
function uniqueName(taken, name) {
  if (!taken.has(name)) { taken.add(name); return name; }
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  let i = 2;
  while (taken.has(`${stem}-${i}${ext}`)) i++;
  const out = `${stem}-${i}${ext}`;
  taken.add(out);
  return out;
}

async function downloadAll(asZip) {
  const fmt = $("dl-format").value;
  const opts = exportOptions();
  const batch = jobs.filter(j => j.sessionId === viewedSessionId && j.state === "done" && !j.hidden);
  if (!batch.length) { toast("Nothing to download in this session"); return; }

  const btn = $("download-all");
  const label = btn.textContent;
  btn.disabled = true;

  try {
    if (!asZip) {
      for (const job of batch) { await exportJob(job, fmt); await sleep(250); }
      return;
    }
    const taken = new Set();
    const files = [];
    for (let i = 0; i < batch.length; i++) {
      btn.textContent = `Packing ${i + 1}/${batch.length}…`;
      const job = batch[i];
      const base = exportName(job, fmt);
      const withModel = base.replace(/_nobg\./, `_${job.model}.`);
      const blob = await jobBlob(job, fmt, opts);
      files.push({ name: uniqueName(taken, withModel), data: new Uint8Array(await blob.arrayBuffer()) });
    }
    btn.textContent = "Building ZIP…";
    const stamp = new Date().toISOString().slice(0, 10);
    saveBlob(buildZip(files), `nobg-${stamp}.zip`);
    toast(`${files.length} image${files.length > 1 ? "s" : ""} packed`, "ok");
  } catch (e) {
    warn("downloading batch", e);
    toast("Could not finish the download", "err");
  } finally {
    btn.textContent = label;
    btn.disabled = false;
    renderResults();
  }
}

$("download-all").addEventListener("click", () => downloadAll($("dl-zip").checked));

//  Render
const BADGE_TEXT = { queued: "Queued", "downloading-model": "Downloading model…", "loading-model": "Loading model…", processing: "Processing…", done: "Done", error: "Error" };
function renderAll() { renderResults(); renderSidebar(); }

function renderResults() {
  const el = $("results");
  el.querySelectorAll(".card").forEach(c => c.remove());
  const inView = jobs.filter(j => j.sessionId === viewedSessionId);
  const visible = inView.filter(j => !j.hidden);
  for (let i = jobs.length - 1; i >= 0; i--) { const j = jobs[i]; if (j.sessionId === viewedSessionId && !j.hidden) el.appendChild(renderCard(j)); }
  const empty = $("results-empty");
  empty.style.display = visible.length ? "none" : "flex";
  $("empty-text").textContent = inView.length ? "All cards closed. Click an image in the sidebar to reopen it." : "Nothing here yet. Drop an image to remove its background.";
  const done = inView.filter(j => j.state === "done").length;
  const pending = inView.filter(j => ["queued", "processing", "loading-model", "downloading-model"].includes(j.state)).length;
  $("download-all").disabled = done === 0;
  const closeAll = $("close-all");
  closeAll.disabled = inView.length === 0;
  closeAll.textContent = (inView.length && visible.length === 0) ? "Show all" : "Close all";
  $("queue-info").textContent = (done ? `${done} processed` : "") + (pending ? `${done ? " · " : ""}${pending} in queue` : "");
}
$("close-all").addEventListener("click", () => {
  const inView = jobs.filter(j => j.sessionId === viewedSessionId);
  if (!inView.length) return;
  const anyVisible = inView.some(j => !j.hidden);
  inView.forEach(j => j.hidden = anyVisible);
  renderAll();
});

// Overlay button that opens the full-size viewer for a finished job.
function zoomButton(job, mode, label) {
  const b = document.createElement("button");
  b.type = "button"; b.className = "zoom-btn";
  b.innerHTML = ICON_EXPAND + esc(label);
  b.title = label === "Compare" ? "Compare original and result" : "View full size";
  b.addEventListener("click", (e) => { e.stopPropagation(); openViewer(job, mode); });
  return b;
}

function swapCardPreviews(job) {
  const card = $("card-" + job.id);
  if (!card) return;
  const cells = card.querySelectorAll(".cell img");
  if (job.inPreview && cells[0]) cells[0].src = job.inPreview;
  if (job.outPreview && cells[1]) cells[1].src = job.outPreview;
}

function paintCardBackdrop(job) {
  const card = $("card-" + job.id);
  if (!card) return;
  const cell = card.querySelector(".cell.out");
  if (!cell) return;
  const eff = effectiveBg(job);
  cell.classList.toggle("checker", eff === "checker");
  cell.style.background = eff === "checker" ? "" : eff;
}

function renderCard(job) {
  const card = document.createElement("div"); card.className = "card"; card.id = "card-" + job.id;
  const head = document.createElement("div"); head.className = "card-head";
  const modelTitle = INFO[job.model]?.title || job.model;
  const metaLine = job.state === "done" ? `${modelTitle} · processed <span class="ago" data-ts="${job.createdAt}">${timeAgo(job.createdAt)}</span>` : modelTitle;
  const metaRight = job.state === "done" ? `<span class="meta">${job.outKB} KB · ${job.ms}s</span>` : "";
  head.innerHTML = `<span class="ci-left"><span class="name" title="${esc(job.name)}">${esc(job.name)}</span><span class="cmeta">${metaLine}</span></span><span class="right">${metaRight}<span class="badge ${job.state}">${BADGE_TEXT[job.state]}</span><button class="card-close icon-btn" title="Close from view">${ICON_CLOSE}</button></span>`;
  head.querySelector(".card-close").addEventListener("click", () => hideJob(job.id));
  card.appendChild(head);

  const pair = document.createElement("div"); pair.className = "pair";
  const inCell = document.createElement("div"); inCell.className = "cell checker";
  inCell.innerHTML = `<span class="label">Original</span>`; inCell.appendChild(makeImg(job.inPreview || job.inUrl, "Original")); pair.appendChild(inCell);
  const outCell = document.createElement("div"); outCell.className = "cell out";
  const eff = effectiveBg(job); if (eff === "checker") outCell.classList.add("checker"); else outCell.style.background = eff;
  outCell.innerHTML = `<span class="label">No background</span>`;
  if (job.state === "done") {
    outCell.appendChild(makeImg(job.outPreview || job.outUrl, "No background"));
    inCell.appendChild(zoomButton(job, "split", "Compare"));
    outCell.appendChild(zoomButton(job, "result", "View"));
    inCell.addEventListener("dblclick", () => openViewer(job, "split"));
    outCell.addEventListener("dblclick", () => openViewer(job, "result"));
  }
  else if (job.state === "error") { const d = document.createElement("div"); d.className = "errmsg"; d.textContent = job.err || "Error"; outCell.appendChild(d); }
  else if (job.state === "queued") { const d = document.createElement("div"); d.className = "placeholder"; d.textContent = "Waiting…"; outCell.appendChild(d); }
  else { const s = document.createElement("div"); s.className = "spinner"; outCell.appendChild(s); }
  pair.appendChild(outCell); card.appendChild(pair);

  if (job.state === "done") {
    const foot = document.createElement("div"); foot.className = "card-foot";
    const bgGrp = document.createElement("div"); bgGrp.className = "grp";
    const lbl = document.createElement("span"); lbl.className = "glbl"; lbl.textContent = "Background:"; bgGrp.appendChild(lbl);
    const pickBg = (bg) => {
      job.bg = bg;
      idbPatch(job.id, { bg }).catch(e => warn("saving the backdrop", e));
      paintCardBackdrop(job);
      buildSwatches(bgGrp, job.bg != null ? job.bg : "__none__", pickBg, true);
    };
    buildSwatches(bgGrp, job.bg != null ? job.bg : "__none__", pickBg, true);
    foot.appendChild(bgGrp);

    const mGrp = document.createElement("div"); mGrp.className = "grp";
    const mLbl = document.createElement("span"); mLbl.className = "glbl"; mLbl.textContent = "Model:"; mGrp.appendChild(mLbl);
    const mSel = document.createElement("select");
    for (const [k, inf] of Object.entries(INFO)) { const o = document.createElement("option"); o.value = k; o.textContent = inf.title; if (k === job.model) o.selected = true; mSel.appendChild(o); }
    const reBtn = document.createElement("button"); reBtn.textContent = "Reprocess";
    reBtn.title = "Replace this result using the selected model";
    reBtn.addEventListener("click", () => reprocessJob(job, mSel.value));
    const cmpBtn = document.createElement("button"); cmpBtn.textContent = "Compare…";
    cmpBtn.title = "Run several models on this image and keep every result";
    cmpBtn.addEventListener("click", () => openCompare(job));
    mGrp.appendChild(mSel); mGrp.appendChild(reBtn); mGrp.appendChild(cmpBtn); foot.appendChild(mGrp);

    const dlGrp = document.createElement("div"); dlGrp.className = "grp";
    const sel = document.createElement("select"); sel.innerHTML = `<option value="png">PNG</option><option value="webp">WEBP</option><option value="jpg">JPG</option>`;
    const maskBtn = document.createElement("button"); maskBtn.textContent = "Mask";
    maskBtn.title = "Download the alpha channel as a greyscale image, for retouching the original by hand";
    maskBtn.addEventListener("click", () => downloadMask(job));
    dlGrp.appendChild(maskBtn);
    const btn = document.createElement("button"); btn.className = "primary"; btn.textContent = "Download";
    btn.addEventListener("click", () => exportJob(job, sel.value));
    dlGrp.appendChild(sel); dlGrp.appendChild(btn); foot.appendChild(dlGrp);
    card.appendChild(foot);
  }
  return card;
}

function renderSidebar() {
  const list = $("sidebar-list");
  list.innerHTML = "";
  if (!jobs.length) {
    const e = document.createElement("div"); e.className = "sidebar-empty"; e.innerHTML = "No images yet.<br>Drop some to start.";
    list.appendChild(e); $("sess-leg").textContent = ""; return;
  }
  $("sess-leg").textContent = "* open session";
  const ordered = [...sessions].sort((a, b) => b.createdAt - a.createdAt);
  for (const sess of ordered) {
    const sjobs = jobs.filter(j => j.sessionId === sess.id);
    if (!sjobs.length) continue;                       // hide empty sessions
    const group = document.createElement("div"); group.className = "sess-group";
    const head = document.createElement("div"); head.className = "sess-head" + (sess.id === viewedSessionId ? " viewed" : "");
    const isCur = sess.id === currentSessionId;
    head.innerHTML = `<span class="sess-name"><b>${isCur ? "Current" : "Session"}</b> · <span class="ago" data-ts="${sess.createdAt}">${timeAgo(sess.createdAt)}</span> · ${sjobs.length}</span>`;
    const sdel = document.createElement("button"); sdel.className = "sess-del icon-btn"; sdel.title = "Delete session"; sdel.innerHTML = ICON_TRASH;
    sdel.addEventListener("click", (e) => { e.stopPropagation(); deleteSession(sess.id); });
    head.appendChild(sdel);
    head.addEventListener("click", () => { viewedSessionId = sess.id; renderAll(); });
    group.appendChild(head);
    for (let i = sjobs.length - 1; i >= 0; i--) {
      const job = sjobs[i];
      const item = document.createElement("div"); item.className = "side-item" + (sess.id === viewedSessionId ? " active" : "");
      const thumb = document.createElement("div"); thumb.className = "side-thumb";
      if (job.inUrl) { const im = document.createElement("img"); im.src = job.inUrl; im.alt = ""; im.addEventListener("error", () => thumb.innerHTML = ICON_WARN); thumb.appendChild(im); }
      else thumb.innerHTML = ICON_WARN;
      const meta = document.createElement("div"); meta.className = "side-meta";
      const sub = job.state === "done" ? `<span class="ago" data-ts="${job.createdAt}">${timeAgo(job.createdAt)}</span>` : BADGE_TEXT[job.state];
      meta.innerHTML = `<div class="side-name" title="${esc(job.name)}">${esc(job.name)}</div><div class="side-sub ${job.state}">${sub}</div>`;
      const d = document.createElement("button"); d.className = "side-del icon-btn"; d.title = "Delete image"; d.innerHTML = ICON_TRASH;
      d.addEventListener("click", (e) => { e.stopPropagation(); deleteJob(job.id); });
      item.appendChild(thumb); item.appendChild(meta); item.appendChild(d);
      item.addEventListener("click", () => revealJob(job.id));
      group.appendChild(item);
    }
    list.appendChild(group);
  }
  renderStorage();
}

// Re-reads the storage figure once the given deletes have committed.
async function refreshStorageAfter(deletions) {
  try {
    await Promise.allSettled(deletions);
  } catch (e) { warn("waiting for deletes", e); }
  storageWarned = false;
  renderStorage();
}

// Shows how much of the browser's storage quota the saved results take up.
async function renderStorage() {
  const foot = $("storage-foot");
  const est = await storageUsage();
  if (!est) { foot.hidden = true; return; }
  const mb = est.usage / 1048576;
  const pct = est.quota ? Math.min(100, (est.usage / est.quota) * 100) : 0;
  if (mb < 50 && pct < 60) { foot.hidden = true; return; }
  const label = fmtBytes(est.usage);
  foot.hidden = false;
  foot.classList.toggle("warn", pct >= 80);
  foot.innerHTML = `Saved results: ${label}${est.quota ? ` · ${Math.round(pct)}% of quota` : ""}${pct >= 80 ? " — delete old sessions" : ""}<div class="bar"><span style="width:${pct}%"></span></div>`;
}

//  Compare models
let cmpJob = null;

function openCompare(job) {
  if (!job || !job.inBlob) return;
  cmpJob = job;
  $("cmp-sub").textContent = job.name;

  const list = $("cmp-list");
  list.innerHTML = "";
  for (const [key, info] of Object.entries(INFO)) {
    const row = document.createElement("label");
    row.className = "cmp-row" + (key === job.model ? " current" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = key;
    cb.disabled = !DOWNLOADED[key];
    cb.addEventListener("change", updateCompareEstimate);
    const main = document.createElement("div");
    main.className = "cm-main";
    main.innerHTML = `<div class="cm-title">${esc(info.title)}</div><div class="cm-tag">${esc(info.tagline || "")}</div>`;
    const right = document.createElement("span");
    if (DOWNLOADED[key]) { right.className = "cm-size"; right.textContent = info.speed || ""; }
    else { right.className = "cm-na"; right.textContent = "Not downloaded"; }
    row.appendChild(cb); row.appendChild(main); row.appendChild(right);
    list.appendChild(row);
  }
  $("cmp-note").textContent =
    "Each selected model processes the image again and is added as its own result, "
    + "so nothing you already have is replaced. Models that are not downloaded yet "
    + "can be fetched from the Models page.";
  updateCompareEstimate();
  $("cmp-back").hidden = false;
}

function closeCompare() { $("cmp-back").hidden = true; cmpJob = null; }

function selectedCompareModels() {
  return [...$("cmp-list").querySelectorAll("input:checked")].map(i => i.value);
}

function updateCompareEstimate() {
  const picks = selectedCompareModels();
  const btn = $("cmp-run");
  btn.disabled = picks.length === 0;
  if (!picks.length) { $("cmp-est").textContent = "Select one or more models"; return; }
  let secs = 0;
  for (const k of picks) {
    const m = /([\d.]+)\s*s/.exec(INFO[k]?.speed || "");
    secs += m ? parseFloat(m[1]) : 5;
  }
  const label = secs >= 60 ? `about ${Math.round(secs / 60)} min` : `about ${Math.round(secs)}s`;
  $("cmp-est").textContent = `${picks.length} model${picks.length > 1 ? "s" : ""} · ${label} on CPU`;
}

function runCompare() {
  const picks = selectedCompareModels();
  if (!cmpJob || !picks.length) return;
  const src = cmpJob;
  closeCompare();
  for (const model of picks) {
    jobs.push({
      id: genId(), sessionId: src.sessionId, createdAt: Date.now(),
      file: src.inBlob, inBlob: src.inBlob, name: src.name, model,
      state: "queued", inUrl: URL.createObjectURL(src.inBlob),
      outUrl: null, outBlob: null, ms: null, outKB: null,
      bg: src.bg, err: null, hidden: false,
      transient: true,
    });
  }
  viewedSessionId = src.sessionId;
  toast(`Comparing ${picks.length} model${picks.length > 1 ? "s" : ""} on "${src.name}"`);
  renderAll();
  pump();
}

$("cmp-close").addEventListener("click", closeCompare);
$("cmp-cancel").addEventListener("click", closeCompare);
$("cmp-run").addEventListener("click", runCompare);
$("cmp-back").addEventListener("click", (e) => { if (e.target === $("cmp-back")) closeCompare(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("cmp-back").hidden) closeCompare();
});

//  Full-size viewer
const VIEW_MODES = ["result", "split", "side"];
const MIN_ZOOM = 0.1, MAX_ZOOM = 8;

const viewer = {
  job: null, mode: "split", zoom: 1, fitZoom: 1,
  panX: 0, panY: 0, split: 0.5, bg: "checker",
  natural: { w: 0, h: 0 },
};

function openViewer(job, mode) {
  if (!job || job.state !== "done" || !job.outUrl) return;
  viewer.job = job;
  viewer.mode = VIEW_MODES.includes(mode) ? mode : "split";
  viewer.split = 0.5;
  viewer.bg = effectiveBg(job);

  $("v-name").textContent = job.name;
  $("v-orig-img").src = job.inUrl || "";
  $("v-out-img").src = job.outUrl;
  $("viewer").hidden = false;

  buildViewerSwatches();
  const img = $("v-out-img");
  const ready = () => {
    viewer.natural = { w: img.naturalWidth || 0, h: img.naturalHeight || 0 };
    const t = INFO[job.model]?.title || job.model;
    $("v-meta").textContent = `${t} · ${viewer.natural.w}x${viewer.natural.h} · ${job.outKB} KB · ${job.ms}s`;
    fitViewer();
  };
  if (img.complete && img.naturalWidth) ready();
  else img.addEventListener("load", ready, { once: true });

  setViewerMode(viewer.mode);
}

function closeViewer() {
  $("viewer").hidden = true;
  viewer.job = null;
  $("v-orig-img").removeAttribute("src");
  $("v-out-img").removeAttribute("src");
}

function setViewerMode(mode) {
  viewer.mode = mode;
  const el = $("viewer");
  for (const m of VIEW_MODES) el.classList.toggle("mode-" + m, m === mode);
  for (const b of document.querySelectorAll(".v-mode")) {
    const on = b.dataset.mode === mode;
    b.classList.toggle("sel", on);
    b.setAttribute("aria-pressed", String(on));
  }
  $("v-handle").hidden = mode !== "split";
  $("v-hint").textContent = mode === "split"
    ? "Drag the divider to wipe between original and result · scroll to zoom · drag the image to pan · Esc to close"
    : "Scroll to zoom · drag to pan · 1 fits to window, 2 sets 100% · Esc to close";
  fitViewer();
}

function layoutViewer() {
  const { w, h } = viewer.natural;
  if (!w || !h) return;
  const pane = $("v-pane");
  const side = viewer.mode === "side";
  pane.style.width = (side ? w * 2 + 1 : w) + "px";
  pane.style.height = h + "px";
  for (const id of ["v-orig", "v-out"]) {
    const layer = $(id);
    layer.style.width = w + "px";
    layer.style.height = h + "px";
  }
  applyViewerTransform();
  applySplit();
}

function applyViewerTransform() {
  const pane = $("v-pane");
  pane.style.transform =
    `translate(${viewer.panX}px, ${viewer.panY}px) scale(${viewer.zoom})`;
  pane.style.setProperty("--inv-zoom", String(1 / viewer.zoom));
  $("v-zoom").textContent = Math.round(viewer.zoom * 100) + "%";
}

function applySplit() {
  if (viewer.mode !== "split") {
    $("v-out").style.clipPath = "";
    return;
  }
  const pct = Math.round(viewer.split * 10000) / 100;
  $("v-out").style.clipPath = `inset(0 0 0 ${pct}%)`;
  $("v-handle").style.left = pct + "%";
}

function fitViewer() {
  const { w, h } = viewer.natural;
  if (!w || !h) return;
  const stage = $("v-stage").getBoundingClientRect();
  const pad = 48;
  const contentW = viewer.mode === "side" ? w * 2 + 1 : w;
  const scale = Math.min((stage.width - pad) / contentW, (stage.height - pad) / h);
  viewer.fitZoom = Math.max(MIN_ZOOM, Math.min(1, scale));
  viewer.zoom = viewer.fitZoom;
  viewer.panX = 0; viewer.panY = 0;
  layoutViewer();
}

function setZoom(next, originX, originY) {
  const prev = viewer.zoom;
  const z = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, next));
  if (z === prev) return;
  if (originX != null) {
    const stage = $("v-stage").getBoundingClientRect();
    const cx = originX - stage.left - stage.width / 2 - viewer.panX;
    const cy = originY - stage.top - stage.height / 2 - viewer.panY;
    const k = z / prev;
    viewer.panX -= cx * (k - 1);
    viewer.panY -= cy * (k - 1);
  }
  viewer.zoom = z;
  applyViewerTransform();
}

function buildViewerSwatches() {
  const box = $("v-bgs"); box.innerHTML = "";
  buildSwatches(box, viewer.bg, (bg) => {
    viewer.bg = bg;
    if (viewer.job) {
      viewer.job.bg = bg;
      idbPatch(viewer.job.id, { bg }).catch(e => warn("saving the backdrop", e));
      paintCardBackdrop(viewer.job);
    }
    paintViewerBackdrop();
  }, true);
  paintViewerBackdrop();
}

function paintViewerBackdrop() {
  const out = $("v-out");
  out.classList.toggle("checker", viewer.bg === "checker");
  out.style.background = viewer.bg === "checker" ? "" : viewer.bg;
  const orig = $("v-orig");
  orig.classList.add("checker");
}

// viewer events
for (const b of document.querySelectorAll(".v-mode")) {
  b.addEventListener("click", () => setViewerMode(b.dataset.mode));
}
$("v-close").addEventListener("click", closeViewer);
$("v-fit").addEventListener("click", fitViewer);
$("v-zoom-in").addEventListener("click", () => setZoom(viewer.zoom * 1.25));
$("v-zoom-out").addEventListener("click", () => setZoom(viewer.zoom / 1.25));

$("v-stage").addEventListener("wheel", (e) => {
  if ($("viewer").hidden) return;
  e.preventDefault();
  setZoom(viewer.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12), e.clientX, e.clientY);
}, { passive: false });

let vDrag = null;

$("viewer").addEventListener("dragstart", (e) => e.preventDefault());

$("v-stage").addEventListener("pointerdown", (e) => {
  if ($("viewer").hidden) return;
  if (e.button !== 0) return;
  const onHandle = e.target.closest(".v-handle");
  vDrag = onHandle
    ? { kind: "split", id: e.pointerId }
    : { kind: "pan", id: e.pointerId, x: e.clientX, y: e.clientY, panX: viewer.panX, panY: viewer.panY };
  if (vDrag.kind === "pan") $("v-stage").classList.add("panning");
  try { $("v-stage").setPointerCapture(e.pointerId); } catch (err) { warn("capturing pointer", err); }
  if (onHandle) moveSplitTo(e.clientX);
  e.preventDefault();
});

$("v-stage").addEventListener("pointermove", (e) => {
  if (!vDrag || e.pointerId !== vDrag.id) return;
  if (vDrag.kind === "split") { moveSplitTo(e.clientX); return; }
  viewer.panX = vDrag.panX + (e.clientX - vDrag.x);
  viewer.panY = vDrag.panY + (e.clientY - vDrag.y);
  applyViewerTransform();
});

function endVDrag(e) {
  if (!vDrag || (e && e.pointerId !== vDrag.id)) return;
  const id = vDrag.id;
  vDrag = null;
  $("v-stage").classList.remove("panning");
  try { $("v-stage").releasePointerCapture(id); } catch (err) { /* already released */ }
  const sel = window.getSelection && window.getSelection();
  if (sel && !sel.isCollapsed) sel.removeAllRanges();
}
$("v-stage").addEventListener("pointerup", endVDrag);
$("v-stage").addEventListener("pointercancel", endVDrag);

window.addEventListener("pointerup", endVDrag);
window.addEventListener("blur", () => endVDrag());

function moveSplitTo(clientX) {
  const pane = $("v-pane").getBoundingClientRect();
  if (!pane.width) return;
  viewer.split = Math.max(0, Math.min(1, (clientX - pane.left) / pane.width));
  applySplit();
}

window.addEventListener("resize", () => { if (!$("viewer").hidden) fitViewer(); });

document.addEventListener("keydown", (e) => {
  if ($("viewer").hidden) return;
  const k = e.key;
  if (k === "Escape") { closeViewer(); return; }
  if (k === "1") { fitViewer(); return; }
  if (k === "2") { setZoom(1); return; }
  if (k === "+" || k === "=") { setZoom(viewer.zoom * 1.25); return; }
  if (k === "-") { setZoom(viewer.zoom / 1.25); return; }
  const i = VIEW_MODES.indexOf(viewer.mode);
  if (k === "ArrowRight" && e.shiftKey) { setViewerMode(VIEW_MODES[(i + 1) % 3]); return; }
  if (k === "ArrowLeft" && e.shiftKey) { setViewerMode(VIEW_MODES[(i + 2) % 3]); return; }
});

// Item actions
function hideJob(id) { const j = jobs.find(x => x.id === id); if (j) { j.hidden = true; renderAll(); } }
function revealJob(id) {
  const j = jobs.find(x => x.id === id); if (!j) return;
  j.hidden = false; viewedSessionId = j.sessionId; renderAll();
  const card = $("card-" + id);
  if (card) { card.scrollIntoView({ behavior: "smooth", block: "center" }); card.classList.add("flash"); setTimeout(() => card.classList.remove("flash"), 1000); }
}
function deleteJob(id) {
  const i = jobs.findIndex(x => x.id === id); if (i < 0) return;
  const j = jobs[i];
  if (j.inPreview) URL.revokeObjectURL(j.inPreview);
  if (j.outPreview) URL.revokeObjectURL(j.outPreview);
  if (id === processingJobId) toast("Removed; the image in progress will be discarded when it finishes");
  if (j.inUrl) URL.revokeObjectURL(j.inUrl); if (j.outUrl) URL.revokeObjectURL(j.outUrl);
  jobs.splice(i, 1);
  refreshStorageAfter([idbDelete(id)]);
  if (!jobs.some(x => x.sessionId === j.sessionId) && j.sessionId !== currentSessionId) sessions = sessions.filter(s => s.id !== j.sessionId);
  if (viewedSessionId === j.sessionId && !jobs.some(x => x.sessionId === viewedSessionId)) viewedSessionId = currentSessionId;
  renderAll();
}
function deleteSession(id) {
  const victims = jobs.filter(j => j.sessionId === id);
  if (victims.some(j => j.id === processingJobId)) toast("Removed; the image in progress will be discarded when it finishes");
  const deletions = [];
  for (const j of victims) { if (j.inUrl) URL.revokeObjectURL(j.inUrl); if (j.outUrl) URL.revokeObjectURL(j.outUrl); if (j.inPreview) URL.revokeObjectURL(j.inPreview); if (j.outPreview) URL.revokeObjectURL(j.outPreview); deletions.push(idbDelete(j.id)); }
  jobs = jobs.filter(j => j.sessionId !== id);
  sessions = sessions.filter(s => s.id !== id);
  if (currentSessionId === id) { const s = { id: genId(), createdAt: Date.now() }; sessions.push(s); currentSessionId = s.id; }
  if (viewedSessionId === id) viewedSessionId = currentSessionId;
  renderAll();
  // The storage figure comes from the browser, which only reflects the freed
  // space once the deletes have actually committed. Without waiting, the
  // footer kept showing the old total until a reload.
  refreshStorageAfter(deletions);
}
function startNewSession() { const s = { id: genId(), createdAt: Date.now() }; sessions.push(s); currentSessionId = s.id; viewedSessionId = s.id; renderAll(); }
$("new-session").addEventListener("click", () => {
  const curEmpty = !jobs.some(j => j.sessionId === currentSessionId);
  if (curEmpty) { viewedSessionId = currentSessionId; renderAll(); toast("Current session is already empty"); return; }
  startNewSession(); toast("New session started");
});

// Install overlay
const overlay = $("overlay"), installBox = $("install-box");
function showOverlay(model) { installBox.classList.remove("error"); $("install-retry").style.display = "none"; $("install-title").textContent = "Setting up Kirinuki"; $("install-msg").innerHTML = `Downloading the <span class="em">${INFO[model]?.title || model}</span> model (~<span class="em">${SIZES[model]} MB</span>) for the first time…`; overlay.classList.add("show"); }
function hideOverlay() { overlay.classList.remove("show"); }
function updateOverlay(s) { const pct = Math.round(((s.progress != null ? s.progress : 0)) * 100); $("install-bar").style.width = pct + "%"; $("install-sub").textContent = (s.progress != null && s.progress < 1) ? `Downloading… ${pct}%` : "Initializing model…"; }
function overlayError(msg) { installBox.classList.add("error"); $("install-title").textContent = "Could not download the model"; $("install-msg").textContent = msg + ". Check your internet connection and try again."; $("install-retry").style.display = "inline-block"; }
$("install-retry").addEventListener("click", () => initInstall());
async function initInstall() {
  try {
    const s = await getStatus(DEFAULT_MODEL);
    if (s.state === "ready") { hideOverlay(); setStatus("ready"); return; }
    showOverlay(DEFAULT_MODEL); setStatus("downloading model…", true);
    await waitForModel(DEFAULT_MODEL, updateOverlay);
    DOWNLOADED[DEFAULT_MODEL] = true; renderModelDropdown(); renderModelsPage();
    hideOverlay(); setStatus("ready"); toast("Model ready — drop an image to start", "ok");
  } catch (e) { overlayError(e.message || "Download error"); setStatus("model error"); }
}

// Boot
async function restoreFromIDB() {
  let records = [];
  try {
    records = await idbAll();
  } catch (e) {
    warn("reading saved results", e);
    records = [];
    await reportStorageFailure(e, "saved results");
  }
  records.sort((a, b) => a.createdAt - b.createdAt);
  const sessMap = {};
  for (const rec of records) {
    try {
      const sid = rec.sessionId || "legacy";
      if (!sessMap[sid]) sessMap[sid] = rec.sessionCreatedAt || rec.createdAt;
      jobs.push({ id: rec.id, sessionId: sid, createdAt: rec.createdAt, name: rec.name, model: rec.model, state: "done", ms: rec.ms, outKB: rec.outKB, bg: rec.bg != null ? rec.bg : null, inBlob: rec.inBlob, outBlob: rec.outBlob, exif: rec.exif || null, file: rec.inBlob, inUrl: URL.createObjectURL(rec.inBlob), outUrl: URL.createObjectURL(rec.outBlob), err: null, hidden: false });
    } catch (e) {
      warn(`restoring saved result ${rec && rec.id}`, e);
    }
  }
  sessions = Object.entries(sessMap).map(([id, createdAt]) => ({ id, createdAt }));

  (async () => {
    for (const job of jobs) {
      if (job.state !== "done") continue;
      if (!job.outPreview && job.outBlob) job.outPreview = await makePreviewUrl(job.outBlob);
      if (!job.inPreview && job.inBlob) job.inPreview = await makePreviewUrl(job.inBlob);
      swapCardPreviews(job);
    }
  })();
}
(async function boot() {
  try {
    buildGlobalSwatches();
    await loadModels();
    try { const h = await (await fetch("/health")).json(); if (h.version) $("ver").textContent = "v" + h.version; } catch (e) { warn("reading /health", e); }
    await restoreFromIDB();
    startNewSession();          // fresh empty working session; past sessions stay in the sidebar
    await initInstall();
  } catch (e) { setStatus("server error"); toast("Could not reach the server", "err"); }
})();
