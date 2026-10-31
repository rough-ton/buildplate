// Buildplate web UI. Plain ES modules, no build step.
import { Viewer } from "/viewer.js";
import { marked } from "/vendor/marked/marked.esm.js";
import { printerIcon, PRINTER_TYPES, typeLabel } from "/printer-icons.js";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtInt = (n) => Number(n || 0).toLocaleString();
const fmtCost = (n, known = true) => (!known ? "n/a" : n === 0 ? "$0" : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`);
const fmtTokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : fmtInt(n));
const ago = (t) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}

function toast(msg, kind = "") {
  const el = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "bad" ? 6500 : 3200);
}

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers["content-type"] = "application/json";
  }
  const res = await fetch(path, init);
  if (res.status === 401 && !path.startsWith("/api/auth")) { showLogin(); throw new Error("Sign in required"); }
  const type = res.headers.get("content-type") || "";
  const body = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = (body && (body.detail || body.error)) || `Request failed (${res.status})`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.body = body;
    throw err;
  }
  return body;
}

const icons = {
  send: '<svg viewBox="0 0 24 24"><path d="M4 12h14 M12 5l7 7-7 7"/></svg>',
  stop: '<svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>',
  download: '<svg viewBox="0 0 24 24"><path d="M12 4v11 M7 10l5 5 5-5 M5 20h14"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14 M5 12h14"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M7 5l12 7-12 7z"/></svg>',
  save: '<svg viewBox="0 0 24 24"><path d="M5 4h11l3 3v13H5z M8 4v5h7V4 M8 20v-6h8v6"/></svg>',
};

// ---------------------------------------------------------------- global state
const app = { settings: null, cleanup: null };

async function loadSettings() {
  app.settings = await api("/api/settings");
  renderModelChip();
  return app.settings;
}

function providerLabel(id) {
  return app.settings?.providers?.[id]?.meta?.label || id;
}

function renderModelChip() {
  const chip = $("#model-chip");
  const a = app.settings?.agent || {};
  if (a.provider && a.model) {
    chip.className = "model-chip";
    chip.innerHTML = `<strong>${esc(a.model)}</strong>${esc(providerLabel(a.provider).split(" (")[0])}`;
  } else {
    chip.className = "model-chip missing";
    chip.innerHTML = `<strong>No model selected</strong><a href="#/settings">Choose one in Settings</a>`;
  }
}

function modelOptions(selectedProvider, selectedModel) {
  const opts = [];
  for (const [pid, p] of Object.entries(app.settings?.providers || {})) {
    if (!p.enabled) continue;
    const models = new Set(p.models || []);
    if (pid === selectedProvider && selectedModel) models.add(selectedModel);
    for (const m of models) opts.push({ value: `${pid}::${m}`, label: `${m}`, group: p.meta.label, selected: pid === selectedProvider && m === selectedModel });
  }
  return opts;
}

function modelSelect(attrs = {}) {
  const a = app.settings.agent;
  const sel = h("select", attrs);
  const groups = {};
  for (const o of modelOptions(a.provider, a.model)) {
    groups[o.group] ||= h("optgroup", { label: o.group });
    groups[o.group].append(h("option", { value: o.value, selected: o.selected }, o.label));
  }
  Object.values(groups).forEach((g) => sel.append(g));
  if (!sel.options.length) sel.append(h("option", { value: "" }, "No models configured"));
  return sel;
}

// ---------------------------------------------------------------- auth
async function showLogin() {
  $("#login").hidden = false;
  $("#shell").hidden = true;
  $("#login-password").focus();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/auth/login", { method: "POST", json: { password: $("#login-password").value } });
    $("#login").hidden = true;
    $("#shell").hidden = false;
    $("#login-password").value = "";
    $("#login-error").textContent = "";
    await boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  showLogin();
});

// ---------------------------------------------------------------- router
const routes = { studio: Studio, library: Library, usage: Usage, settings: Settings, docs: Docs };

async function route() {
  const [, name = "studio", ...rest] = location.hash.replace(/^#/, "").split("/");
  const view = routes[name] ? name : "studio";
  $$(".nav").forEach((n) => n.classList.toggle("active", n.dataset.route === view));
  if (app.cleanup) { try { app.cleanup(); } catch (_) {} app.cleanup = null; }
  const root = $("#view");
  root.innerHTML = "";
  root.scrollTop = 0;
  try {
    app.cleanup = await routes[view](root, rest);
  } catch (err) {
    if (err.message !== "Sign in required") root.append(h("div", { class: "page" }, h("div", { class: "err-box" }, err.message)));
  }
}

// ================================================================= STUDIO
async function Studio(root, [designId]) {
  const s = {
    designId: designId || null,
    title: "Untitled",
    code: "",
    savedCode: "",
    version: 0,
    versions: [],
    params: [],
    overrides: {},
    stats: null,
    preview: null,
    running: false,
    abort: null,
    sessionTokens: { input: 0, output: 0, cost: 0 },
    tab: "params",
  };

  const settings = app.settings;
  const mobileTabs = h("div", { class: "mobile-tabs" },
    h("button", { "data-m": "chat", class: "on" }, "Chat"),
    h("button", { "data-m": "stage" }, "Model"),
    h("button", { "data-m": "inspect" }, "Inspect"));

  // ---- left: chat
  const titleInput = h("input", { type: "text", class: "title-input", value: s.title, "aria-label": "Design title" });
  const newBtn = h("button", { class: "btn btn-ghost btn-sm", title: "Start a new design", html: icons.plus + "New" });
  const chat = h("div", { class: "chat", "aria-live": "polite" });
  const prompt = h("textarea", { placeholder: "Describe a part, or ask for a change to the current one", rows: 3, "aria-label": "Design request" });
  const picker = modelSelect({ "aria-label": "Model for this request" });
  const sendBtn = h("button", { class: "btn btn-primary", html: icons.send + "Generate" });
  const sessionMeter = h("span", { class: "hint tabular", style: "margin:0" });
  const left = h("section", { class: "pane pane-left on-mobile", "data-m": "chat" },
    h("div", { class: "pane-head" }, titleInput, h("span", { class: "spacer" }), newBtn),
    chat,
    h("div", { class: "composer" }, prompt,
      h("div", { class: "composer-foot" }, picker, h("span", { class: "spacer" }), sendBtn),
      h("div", { class: "row", style: "margin-top:6px" }, sessionMeter)));

  // ---- center: stage
  const viewEl = h("div", { class: "viewer" });
  const emptyEl = h("div", { class: "viewer-empty" }, h("div", {}, h("strong", {}, "Nothing on the plate yet"), "Describe a part on the left, or paste OpenSCAD into the Code tab."));
  const readout = h("div", { class: "readout" });
  const busy = h("div", { class: "busy-veil", hidden: true }, h("span", { class: "pill" }, h("span", { class: "spinner" }), "Rendering"));
  const viewSeg = h("div", { class: "seg", role: "group", "aria-label": "Camera" },
    ...["iso", "front", "right", "top"].map((v) => h("button", { "data-view": v, class: v === "iso" ? "on" : "" }, v[0].toUpperCase() + v.slice(1))));
  const wireBtn = h("button", { class: "btn btn-sm" }, "Wireframe");
  const printerSel = h("select", { class: "printer-select", "aria-label": "Target printer" });
  const printerIconEl = h("span", { class: "printer-chip-icon" });
  function fillPrinters() {
    const st = app.settings;
    printerSel.innerHTML = "";
    for (const pr of st.printers) printerSel.append(h("option", { value: pr.id, selected: pr.id === st.active_printer }, pr.name));
    printerIconEl.innerHTML = printerIcon(st.printer.type);
    printerSel.title = `Target printer: ${st.printer.name}, ${+st.printer.bed_x} x ${+st.printer.bed_y} x ${+st.printer.bed_z} mm`;
    printerSel.append(h("option", { value: "__add" }, "Add printers..."));
    printerSel.append(h("option", { value: "__manage" }, "Manage printers..."));
  }
  fillPrinters();
  const stage = h("section", { class: "stage", "data-m": "stage" },
    h("div", { class: "stage-bar" }, viewSeg, wireBtn, h("span", { class: "spacer" }), h("span", { class: "printer-chip" }, printerIconEl, printerSel)),
    viewEl, emptyEl, readout, busy);

  // ---- right: inspector
  const tabs = h("div", { class: "tabs", role: "tablist" });
  const tabBody = h("div", { class: "tab-body" });
  const exportRow = h("div", { class: "inspector-foot" },
    h("button", { class: "btn btn-sm", "data-fmt": "stl", html: icons.download + "STL" }),
    h("button", { class: "btn btn-sm", "data-fmt": "3mf", html: icons.download + "3MF" }),
    h("button", { class: "btn btn-sm", "data-fmt": "scad", html: icons.download + "SCAD" }));
  const right = h("section", { class: "pane pane-right", "data-m": "inspect" }, tabs, tabBody, exportRow);

  const studio = h("div", { class: "studio" }, left, stage, right);
  root.append(mobileTabs, studio);

  mobileTabs.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", mobileTabs).forEach((x) => x.classList.toggle("on", x === b));
    [left, stage, right].forEach((p) => p.classList.toggle("on-mobile", p.dataset.m === b.dataset.m));
    viewer.resize();
  });

  const viewer = new Viewer(viewEl);
  viewer.setBed(settings.printer.bed_x, settings.printer.bed_y, settings.printer.shape);
  printerSel.addEventListener("change", async () => {
    if (printerSel.value === "__add") {
      fillPrinters();
      const picked = await openPrinterPicker(app.settings.printers.map((x) => x.id));
      if (!picked.length) return;
      try {
        await api("/api/printers/add", { method: "POST", json: { items: picked.map((x) => ({ key: x.preset, nozzle: x.nozzle })), activate: true } });
        await loadSettings();
        fillPrinters();
        const pr = app.settings.printer;
        viewer.setBed(pr.bed_x, pr.bed_y, pr.shape);
        toast(`Designing for ${pr.name}`);
        if (s.code.trim()) renderModel();
      } catch (err) { toast(err.message, "bad"); }
      return;
    }
    if (printerSel.value === "__manage") {
      sessionStorage.setItem("bp-jump", "printer");
      location.hash = "#/settings";
      return;
    }
    try {
      await api("/api/printers/active", { method: "POST", json: { id: printerSel.value } });
      await loadSettings();
      const pr = app.settings.printer;
      viewer.setBed(pr.bed_x, pr.bed_y, pr.shape);
      fillPrinters();
      toast(`Designing for ${pr.name}`);
      if (s.code.trim()) renderModel();
    } catch (err) { toast(err.message, "bad"); fillPrinters(); }
  });

  viewSeg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", viewSeg).forEach((x) => x.classList.toggle("on", x === b));
    viewer.home(b.dataset.view);
  });
  wireBtn.addEventListener("click", () => wireBtn.classList.toggle("on", viewer.toggleWire()));

  // ---- code editor (created once, shown in Code tab)
  CodeMirror.defineSimpleMode("scad", {
    start: [
      { regex: /\/\/.*/, token: "comment" },
      { regex: /\/\*/, token: "comment", next: "comment" },
      { regex: /"(?:[^\\"]|\\.)*?"/, token: "string" },
      { regex: /\b(?:module|function|if|else|for|let|each|include|use|intersection_for|assert|echo)\b/, token: "keyword" },
      { regex: /\b(?:true|false|undef|PI)\b/, token: "atom" },
      { regex: /\b(?:cube|sphere|cylinder|polyhedron|square|circle|polygon|text|translate|rotate|scale|mirror|multmatrix|color|offset|hull|minkowski|union|difference|intersection|linear_extrude|rotate_extrude|projection|surface|import|render|children|resize|len|concat|lookup|str|abs|sign|sin|cos|tan|asin|acos|atan|atan2|floor|ceil|round|ln|log|pow|sqrt|exp|min|max|norm|cross|search|chr|ord|is_num|is_string|is_list|is_bool|is_undef)\b/, token: "builtin" },
      { regex: /\$\w+/, token: "variable-2" },
      { regex: /-?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?/i, token: "number" },
      { regex: /<[^>\n]+>/, token: "meta" },
    ],
    comment: [{ regex: /.*?\*\//, token: "comment", next: "start" }, { regex: /.*/, token: "comment" }],
    meta: { lineComment: "//" },
  });
  const codeWrap = h("div", { class: "code-wrap" });
  const cm = CodeMirror(codeWrap, { value: "", mode: "scad", lineNumbers: true, indentUnit: 2, tabSize: 2, lineWrapping: false, extraKeys: { "Ctrl-Enter": () => runCode(), "Cmd-Enter": () => runCode(), "Ctrl-S": () => saveVersion(), "Cmd-S": () => saveVersion() } });
  const runBtn = h("button", { class: "btn btn-sm", html: icons.play + "Render", title: "Render (Ctrl+Enter)" });
  const saveBtn = h("button", { class: "btn btn-sm btn-primary", html: icons.save + "Save version", title: "Save as a new version (Ctrl+S)" });
  const dirtyPill = h("span", { class: "pill warn", hidden: true }, "Unsaved edits");
  codeWrap.append(h("div", { class: "code-bar" }, runBtn, saveBtn, h("span", { class: "spacer" }), dirtyPill));
  cm.on("change", () => {
    s.code = cm.getValue();
    dirtyPill.hidden = s.code === s.savedCode;
    tabCount();
  });
  runBtn.addEventListener("click", () => runCode());
  saveBtn.addEventListener("click", () => saveVersion());

  // ---- tabs
  const TABS = [["params", "Parameters"], ["code", "Code"], ["checks", "Checks"], ["history", "History"]];
  function tabCount() {
    tabs.innerHTML = "";
    for (const [id, label] of TABS) {
      const count = id === "params" ? s.params.length : id === "checks" && s.stats?.issues?.length ? s.stats.issues.length : id === "history" ? s.versions.length : null;
      tabs.append(h("button", { class: `tab ${s.tab === id ? "on" : ""}`, role: "tab", "aria-selected": s.tab === id, "data-tab": id }, label, count ? h("span", { class: "count" }, count) : null));
    }
  }
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest(".tab"); if (!b) return;
    s.tab = b.dataset.tab;
    renderTab();
  });

  function renderTab() {
    tabCount();
    tabBody.innerHTML = "";
    if (s.tab === "code") { tabBody.append(codeWrap); requestAnimationFrame(() => cm.refresh()); return; }
    if (s.tab === "params") return renderParams();
    if (s.tab === "checks") return renderChecks();
    if (s.tab === "history") return renderHistory();
  }

  function renderParams() {
    if (!s.params.length) {
      tabBody.append(h("div", { class: "empty" }, h("h3", {}, "No parameters"), "Parameters appear here when the design declares Customizer variables at the top of its code."));
      return;
    }
    const wrap = h("div", { class: "params" });
    let section = null;
    for (const p of s.params) {
      if (p.section !== section) { section = p.section; wrap.append(h("div", { class: "param-section" }, section)); }
      const cur = s.overrides[p.name] ?? p.value;
      const row = h("div", { class: "param" });
      const name = h("div", {}, h("div", { class: "param-name" }, p.label || p.name), p.label ? h("div", { class: "param-var" }, p.name) : null);
      if (p.control === "slider" || p.control === "number") {
        const num = h("input", { type: "number", class: "param-value", value: cur, step: p.step ?? "any", "aria-label": p.name });
        row.append(h("div", { class: "param-top" }, name, num));
        if (p.control === "slider") {
          const range = h("input", { type: "range", min: p.min, max: p.max, step: p.step, value: cur, "aria-label": p.name });
          range.addEventListener("input", () => { num.value = range.value; setParam(p, Number(range.value)); });
          num.addEventListener("change", () => { range.value = num.value; setParam(p, Number(num.value)); });
          row.append(range);
        } else {
          num.addEventListener("change", () => setParam(p, Number(num.value)));
        }
      } else if (p.control === "checkbox") {
        const cb = h("input", { type: "checkbox", checked: !!cur });
        cb.addEventListener("change", () => setParam(p, cb.checked));
        row.append(h("div", { class: "param-top" }, name, h("label", { class: "switch" }, cb)));
      } else if (p.control === "select") {
        const sel = h("select", {}, p.options.map((o) => h("option", { value: o.value, selected: String(o.value) === String(cur) }, o.label)));
        sel.addEventListener("change", () => setParam(p, p.type === "number" ? Number(sel.value) : sel.value));
        row.append(h("div", { class: "param-top" }, name), sel);
      } else if (p.control === "text") {
        const t = h("input", { type: "text", value: cur });
        t.addEventListener("change", () => setParam(p, t.value));
        row.append(h("div", { class: "param-top" }, name), t);
      } else {
        row.append(h("div", { class: "param-top" }, name, h("span", { class: "mono" }, JSON.stringify(cur))));
      }
      wrap.append(row);
    }
    const changed = Object.keys(s.overrides).length;
    wrap.append(h("div", { class: "row", style: "margin-top:16px" },
      h("button", { class: "btn btn-sm", disabled: !changed, onclick: () => { s.overrides = {}; renderParams_(); renderModel(); } }, "Reset"),
      h("button", { class: "btn btn-sm", disabled: !changed, title: "Write these values into the code as the new defaults", onclick: bakeParams }, "Apply to code")));
    tabBody.append(wrap);
  }
  const renderParams_ = () => { if (s.tab === "params") renderTab(); };

  const rerender = debounce(() => renderModel(), 300);
  function setParam(p, v) {
    if (v === p.value) delete s.overrides[p.name]; else s.overrides[p.name] = v;
    rerender();
  }

  async function bakeParams() {
    let code = s.code;
    for (const [name, v] of Object.entries(s.overrides)) {
      const lit = typeof v === "string" ? JSON.stringify(v) : typeof v === "boolean" ? String(v) : String(v);
      code = code.replace(new RegExp(`^(\\s*${name}\\s*=\\s*)([^;]+)(;)`, "m"), `$1${lit}$3`);
    }
    s.overrides = {};
    cm.setValue(code);
    s.params = (await api("/api/parameters", { method: "POST", json: { code } })).params;
    toast("Values written into the code. Save a version to keep them.");
    renderTab();
  }

  function renderChecks() {
    const wrap = h("div", { class: "checks" });
    if (!s.stats) {
      wrap.append(h("div", { class: "empty" }, h("h3", {}, "No checks yet"), "Render a design to see its size, volume and printability checks."));
      tabBody.append(wrap); return;
    }
    const st = s.stats;
    const issues = st.issues || [];
    wrap.append(issues.length
      ? h("div", {}, ...issues.map((i) => h("div", { class: "issue", style: "margin-bottom:6px" }, i)))
      : h("div", { class: "issue ok" }, "Ready to print: watertight, on the bed and inside the build volume."));
    wrap.append(h("dl", { class: "stat-list" },
      h("dt", {}, "Checked for"), h("dd", {}, st.printer || app.settings.printer.name),
      h("dt", {}, "Size"), h("dd", {}, `${st.size_mm.join(" x ")} mm`),
      h("dt", {}, "Volume"), h("dd", {}, st.volume_mm3 != null ? `${fmtInt(Math.round(st.volume_mm3))} mm³` : "n/a"),
      h("dt", {}, "PLA estimate"), h("dd", {}, st.est_filament_g_pla != null ? `${st.est_filament_g_pla} g` : "n/a"),
      h("dt", {}, "Watertight"), h("dd", {}, st.watertight ? "Yes" : "No"),
      h("dt", {}, "Bodies"), h("dd", {}, st.bodies ?? "n/a"),
      h("dt", {}, "Triangles"), h("dd", {}, fmtInt(st.triangles))));
    if (st.openscad_warnings?.length) wrap.append(h("div", {}, h("p", { class: "h3" }, "OpenSCAD warnings"), h("pre", { class: "mono", style: "white-space:pre-wrap;margin:0" }, st.openscad_warnings.join("\n"))));
    if (s.preview) wrap.append(h("div", { class: "previews" }, h("p", { class: "h3" }, "What the AI saw"), h("img", { src: `data:image/png;base64,${s.preview}`, alt: "Preview render in four views" })));
    tabBody.append(wrap);
  }

  function renderHistory() {
    const wrap = h("div", { class: "checks" });
    if (!s.versions.length) { wrap.append(h("div", { class: "empty" }, h("h3", {}, "No versions yet"), "Each generation or saved edit becomes a version you can go back to.")); tabBody.append(wrap); return; }
    const list = h("div", { class: "versions" });
    for (const v of [...s.versions].reverse()) {
      list.append(h("button", { class: `version ${v.version === s.version ? "on" : ""}`, onclick: () => openVersion(v.version) },
        h("span", { class: "v" }, `v${v.version}`), h("span", { class: "s", title: v.summary }, v.summary || "No summary", v.stats?.printer ? h("span", { class: "hint", style: "display:block;margin:0" }, v.stats.printer) : null), h("span", { class: "spacer" }), h("span", { class: "hint", style: "margin:0;white-space:nowrap" }, ago(v.created_at))));
    }
    wrap.append(list);
    tabBody.append(wrap);
  }

  async function openVersion(v) {
    const ver = await api(`/api/designs/${s.designId}/versions/${v}`);
    s.version = v;
    setCode(ver.scad, true);
    s.params = ver.params;
    s.overrides = {};
    renderTab();
    renderModel();
  }

  function setCode(code, saved = false) {
    s.code = code;
    if (saved) s.savedCode = code;
    cm.setValue(code);
    dirtyPill.hidden = s.code === s.savedCode;
  }

  // ---- rendering
  let renderSeq = 0;
  async function renderModel() {
    if (!s.code.trim()) return;
    const seq = ++renderSeq;
    busy.hidden = false;
    try {
      const r = await api("/api/render", { method: "POST", json: { code: s.code, params: Object.keys(s.overrides).length ? s.overrides : null } });
      if (seq !== renderSeq) return;
      await viewer.load(r.url);
      emptyEl.hidden = true;
      s.stats = r.stats;
      showReadout();
      if (s.tab === "checks") renderTab(); else tabCount();
    } catch (err) {
      if (seq === renderSeq) toast(err.message, "bad");
    } finally {
      if (seq === renderSeq) busy.hidden = true;
    }
  }

  function showReadout() {
    readout.innerHTML = "";
    if (!s.stats) return;
    const st = s.stats;
    readout.append(h("span", { class: "pill tabular" }, `${st.size_mm.map((n) => +n.toFixed(1)).join(" x ")} mm`));
    if (st.est_filament_g_pla != null) readout.append(h("span", { class: "pill tabular" }, `${st.est_filament_g_pla} g PLA`));
    readout.append(st.issues?.length ? h("span", { class: "pill warn" }, `${st.issues.length} check${st.issues.length > 1 ? "s" : ""} to review`) : h("span", { class: "pill good" }, "Printable"));
  }

  async function runCode() {
    s.params = (await api("/api/parameters", { method: "POST", json: { code: s.code } })).params;
    s.overrides = {};
    tabCount();
    await renderModel();
  }

  async function saveVersion() {
    if (!s.code.trim()) return;
    try {
      if (!s.designId) {
        const r = await api("/api/designs", { method: "POST", json: { code: s.code, title: titleInput.value || "Untitled" } });
        s.designId = r.design_id;
        history.replaceState(null, "", `#/studio/${r.design_id}`);
      } else {
        await api(`/api/designs/${s.designId}/versions`, { method: "POST", json: { code: s.code } });
      }
      s.savedCode = s.code;
      dirtyPill.hidden = true;
      await reloadDesign(false);
      toast(`Saved as v${s.version}`);
    } catch (err) { toast(err.message, "bad"); }
  }

  exportRow.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-fmt]"); if (!b) return;
    const fmt = b.dataset.fmt;
    if (!s.code.trim()) return toast("Nothing to export yet.");
    const params = Object.keys(s.overrides).length ? s.overrides : null;
    if (s.designId && s.code === s.savedCode) {
      const q = new URLSearchParams({ format: fmt, version: s.version });
      if (params) q.set("params", JSON.stringify(params));
      location.href = `/api/designs/${s.designId}/download?${q}`;
      return;
    }
    if (fmt === "scad") {
      const blob = new Blob([s.code], { type: "text/plain" });
      return download(URL.createObjectURL(blob), `${slug(titleInput.value)}.scad`);
    }
    try {
      b.disabled = true;
      const r = await api("/api/render", { method: "POST", json: { code: s.code, params, format: fmt } });
      download(r.url, `${slug(titleInput.value)}.${fmt}`);
    } catch (err) { toast(err.message, "bad"); } finally { b.disabled = false; }
  });

  // ---- chat
  function intro() {
    chat.innerHTML = "";
    const starters = [
      "A wall-mount hook for a 25 mm broom handle with two countersunk screw holes",
      "A 4 x 3 grid of AA battery holders with a finger notch",
      "A parametric project box 80 x 50 x 30 mm with a snap-fit lid",
    ];
    chat.append(h("div", { class: "chat-intro" },
      h("h2", {}, "What are we printing?"),
      h("p", {}, "Describe the part in plain words. Include sizes in millimetres when you know them. The AI writes parametric OpenSCAD, checks it renders and fits your printer, then puts it on the plate."),
      ...starters.map((t) => h("button", { class: "starter", onclick: () => { prompt.value = t; prompt.focus(); } }, t))));
  }

  function addUser(text) {
    chat.querySelector(".chat-intro")?.remove();
    chat.append(h("div", { class: "msg msg-user" }, text));
    chat.scrollTop = chat.scrollHeight;
  }

  function addAI(text, meta) {
    const body = h("div", { class: "body" }, text);
    const el = h("div", { class: "msg msg-ai" }, body);
    if (meta) el.append(meta);
    chat.append(el);
    chat.scrollTop = chat.scrollHeight;
    return el;
  }

  function usagePills(t, steps) {
    return h("div", { class: "msg-meta", title: "Tokens used by this request" },
      h("span", { class: "pill tabular" }, `${fmtInt(t.input_tokens)} in`),
      h("span", { class: "pill tabular" }, `${fmtInt(t.output_tokens)} out`),
      t.cache_read_tokens ? h("span", { class: "pill tabular" }, `${fmtInt(t.cache_read_tokens)} cached`) : null,
      h("span", { class: "pill accent tabular" }, fmtCost(t.cost_usd, t.cost_known)),
      steps ? h("span", { class: "pill" }, `${steps} step${steps > 1 ? "s" : ""}`) : null);
  }

  function updateSessionMeter() {
    const t = s.sessionTokens;
    sessionMeter.textContent = t.input || t.output ? `This session: ${fmtTokens(t.input + t.output)} tokens, ${fmtCost(t.cost)}` : "";
  }

  async function generate() {
    const text = prompt.value.trim();
    if (!text || s.running) return;
    const [provider, model] = (picker.value || "").split("::");
    if (!provider) { toast("Add an AI provider and pick a model in Settings first.", "bad"); return; }
    addUser(text);
    prompt.value = "";
    s.running = true;
    sendBtn.innerHTML = icons.stop + "Stop";
    const trace = h("div", { class: "trace" });
    const status = h("div", { class: "thinking" }, h("span", { class: "spinner" }), h("span", {}, "Starting"));
    const msgEl = addAI("", h("div", {}, status, trace));
    const body = $(".body", msgEl);
    let totals = null;
    let steps = 0;
    s.abort = new AbortController();
    try {
      const res = await fetch("/api/generate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ prompt: text, design_id: s.designId, code: s.code && s.code !== s.savedCode ? s.code : null, provider, model }),
        signal: s.abort.signal,
      });
      if (res.status === 401) { showLogin(); return; }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
          if (!line.trim()) continue;
          const ev = JSON.parse(line);
          if (ev.type === "status") status.lastChild.textContent = ev.message;
          else if (ev.type === "assistant") body.textContent = ev.text;
          else if (ev.type === "tool") trace.append(h("div", {}, ev.name === "submit_design" ? "Saving design" : "Checking design"));
          else if (ev.type === "check") trace.append(h("div", { class: ev.ok ? "ok" : "fail" }, ev.ok ? `Rendered: ${ev.stats.size_mm.join(" x ")} mm${ev.stats.issues.length ? `, ${ev.stats.issues.length} issue(s)` : ""}` : `Render failed: ${ev.error}`));
          else if (ev.type === "code") { setCode(ev.code); }
          else if (ev.type === "preview") { s.preview = ev.image; }
          else if (ev.type === "usage") { totals = ev.totals; steps = ev.step; }
          else if (ev.type === "done") {
            totals = ev.totals;
            s.designId = ev.design_id;
            history.replaceState(null, "", `#/studio/${ev.design_id}`);
            body.textContent = ev.summary || "Done.";
            setCode(ev.code, true);
            s.params = ev.params;
            s.overrides = {};
            s.stats = ev.stats;
            await reloadDesign(false);
            renderModel();
          } else if (ev.type === "error") {
            totals = ev.totals || totals;
            msgEl.append(h("div", { class: "err-box" }, ev.message));
          }
        }
      }
    } catch (err) {
      if (err.name === "AbortError") msgEl.append(h("div", { class: "hint" }, "Stopped."));
      else msgEl.append(h("div", { class: "err-box" }, err.message));
    } finally {
      status.remove();
      if (totals) {
        msgEl.append(usagePills(totals, steps));
        s.sessionTokens.input += totals.input_tokens;
        s.sessionTokens.output += totals.output_tokens;
        s.sessionTokens.cost += totals.cost_usd;
        updateSessionMeter();
      }
      s.running = false;
      s.abort = null;
      sendBtn.innerHTML = icons.send + "Generate";
      chat.scrollTop = chat.scrollHeight;
    }
  }

  sendBtn.addEventListener("click", () => (s.running ? s.abort?.abort() : generate()));
  prompt.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey || !e.shiftKey)) { e.preventDefault(); generate(); }
  });
  titleInput.addEventListener("change", async () => {
    s.title = titleInput.value.trim() || "Untitled";
    if (s.designId) await api(`/api/designs/${s.designId}`, { method: "PATCH", json: { title: s.title } });
  });
  newBtn.addEventListener("click", () => { location.hash = "#/studio"; route(); });

  async function reloadDesign(full = true) {
    if (!s.designId) return;
    const d = await api(`/api/designs/${s.designId}`);
    s.title = d.title;
    titleInput.value = d.title;
    s.versions = d.versions;
    s.version = d.current_version;
    if (full) {
      setCode(d.code, true);
      s.params = d.params;
      chat.innerHTML = "";
      if (!d.messages.length) intro();
      const runs = d.messages.length ? (await api("/api/usage/runs?limit=500")).runs.filter((r) => r.design_id === s.designId) : [];
      const byRun = Object.fromEntries(runs.map((r) => [r.id, r]));
      for (const m of d.messages) {
        if (m.role === "user") addUser(m.content);
        else {
          const r = byRun[m.run_id];
          addAI(m.content, r ? usagePills({ ...r, cost_known: !!r.cost_known }, r.iterations) : null);
        }
      }
      if (d.messages.length) chat.prepend(h("p", { class: "hint", style: "text-align:center" }, `${d.versions.length} version${d.versions.length === 1 ? "" : "s"}, last updated ${ago(d.updated_at)}`));
    }
    renderTab();
  }

  // ---- boot
  renderTab();
  if (s.designId) {
    try { await reloadDesign(true); await renderModel(); }
    catch (err) { toast(err.message, "bad"); intro(); }
  } else intro();
  updateSessionMeter();
  setTimeout(() => prompt.focus(), 0);

  return () => { s.abort?.abort(); viewer.dispose(); };
}

function slug(t) { return (t || "design").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "design"; }
function download(url, name) { const a = h("a", { href: url, download: name }); document.body.append(a); a.click(); a.remove(); }

// ================================================================= LIBRARY
async function Library(root) {
  const page = h("div", { class: "page" });
  const search = h("input", { type: "text", placeholder: "Search designs", style: "width:240px", "aria-label": "Search designs" });
  page.append(h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Library"), h("p", {}, "Every design you generate or save, with its full version history.")),
    h("div", { class: "row" }, search, h("a", { class: "btn btn-primary", href: "#/studio", html: icons.plus + "New design" }))));
  const grid = h("div", { class: "lib-grid" });
  page.append(grid);
  root.append(page);

  async function load() {
    const { designs } = await api(`/api/designs?q=${encodeURIComponent(search.value)}`);
    grid.innerHTML = "";
    if (!designs.length) {
      grid.append(h("div", { class: "empty", style: "grid-column:1/-1" }, h("h3", {}, search.value ? "No designs match" : "Your library is empty"), search.value ? "Try a different search." : h("span", {}, "Designs land here after you generate them in ", h("a", { href: "#/studio" }, "Studio"), ".")));
      return;
    }
    for (const d of designs) {
      const st = d.stats || {};
      const card = h("a", { class: "lib-card", href: `#/studio/${d.id}` },
        h("div", { class: "lib-thumb" }, h("img", { src: `/api/designs/${d.id}/download?format=png&version=${d.current_version}`, alt: "", loading: "lazy" })),
        h("div", { class: "lib-info" },
          h("h3", {}, d.title),
          h("div", { class: "meta" }, `v${d.current_version}, ${ago(d.updated_at)}`),
          h("div", { class: "row" },
            st.size_mm ? h("span", { class: "pill tabular" }, `${st.size_mm.map((n) => Math.round(n)).join(" x ")} mm`) : null,
            d.source === "mcp" ? h("span", { class: "pill" }, "via MCP") : null,
            h("span", { class: "spacer" }),
            h("button", { class: "btn btn-ghost btn-sm btn-danger", title: "Delete design", onclick: async (e) => {
              e.preventDefault();
              if (!confirm(`Delete "${d.title}" and all its versions?`)) return;
              await api(`/api/designs/${d.id}`, { method: "DELETE" });
              toast("Design deleted");
              load();
            } }, "Delete"))));
      grid.append(card);
    }
  }
  search.addEventListener("input", debounce(load, 250));
  await load();
}

// ================================================================= USAGE
async function Usage(root) {
  const page = h("div", { class: "page" });
  const range = h("div", { class: "seg", role: "group", "aria-label": "Period" }, ...[7, 30, 90, 365].map((d) => h("button", { "data-days": d, class: d === 30 ? "on" : "" }, d === 365 ? "1 year" : `${d} days`)));
  page.append(h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, "Usage"), h("p", {}, "Tokens and estimated cost for every request, broken down by model step.")),
    h("div", { class: "row" }, range, h("a", { class: "btn btn-sm", href: "/api/usage/export.csv", html: icons.download + "Export CSV" }))));
  const kpis = h("div", { class: "kpis" });
  const chartSec = h("div", { class: "section" });
  const modelSec = h("div", { class: "section" });
  const runsSec = h("div", { class: "section" });
  page.append(kpis, chartSec, modelSec, runsSec);
  root.append(page);
  let days = 30;

  async function load() {
    const sum = await api(`/api/usage/summary?days=${days}`);
    const t = sum.totals;
    kpis.innerHTML = "";
    const kpi = (k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), sub ? h("div", { class: "sub" }, sub) : null);
    kpis.append(
      kpi("Requests", fmtInt(t.runs), `avg ${fmtTokens(Math.round(t.avg_tokens))} tokens each`),
      kpi("Input tokens", fmtTokens(t.input_tokens), t.cache_read_tokens ? `${fmtTokens(t.cache_read_tokens)} from cache` : "sent to the model"),
      kpi("Output tokens", fmtTokens(t.output_tokens), "generated by the model"),
      kpi("Estimated cost", fmtCost(t.cost_usd), "at list prices"));

    chartSec.innerHTML = "";
    chartSec.append(h("div", { class: "section-head" }, h("h2", {}, "Tokens per day")),
      h("div", { class: "section-body" }, sum.daily.length ? chart(sum.daily, days) : h("p", { class: "hint" }, "No requests in this period."),
        h("div", { class: "legend", style: "margin-top:8px" }, h("span", {}, h("i", { style: "background:var(--accent)" }), "Input"), h("span", {}, h("i", { style: "background:var(--part)" }), "Output"))));

    modelSec.innerHTML = "";
    const mt = h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", {}, "Provider"), h("th", { class: "r" }, "Requests"), h("th", { class: "r" }, "Input"), h("th", { class: "r" }, "Output"), h("th", { class: "r" }, "Cost"))),
      h("tbody", {}, sum.by_model.map((m) => h("tr", {}, h("td", {}, m.model), h("td", {}, providerLabel(m.provider)), h("td", { class: "r" }, fmtInt(m.runs)), h("td", { class: "r" }, fmtInt(m.input_tokens)), h("td", { class: "r" }, fmtInt(m.output_tokens)), h("td", { class: "r" }, fmtCost(m.cost_usd))))));
    modelSec.append(h("div", { class: "section-head" }, h("h2", {}, "By model")), h("div", { class: "section-body table-scroll" }, sum.by_model.length ? mt : h("p", { class: "hint" }, "No requests in this period.")));
    await loadRuns();
  }

  let offset = 0;
  async function loadRuns() {
    const { runs, total } = await api(`/api/usage/runs?limit=25&offset=${offset}`);
    runsSec.innerHTML = "";
    const tbody = h("tbody");
    for (const r of runs) {
      const statusPill = r.status === "ok" ? h("span", { class: "pill good" }, "Done") : r.status === "running" ? h("span", { class: "pill" }, "Running") : r.status === "cancelled" ? h("span", { class: "pill" }, "Stopped") : h("span", { class: "pill bad", title: r.error || "" }, "Failed");
      const tr = h("tr", { class: "clickable", tabindex: 0, title: "Show steps" },
        h("td", {}, new Date(r.started_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })),
        h("td", { class: "prompt", title: r.prompt }, r.source === "mcp" ? h("span", { class: "pill", style: "margin-right:6px" }, "MCP") : null, r.prompt),
        h("td", {}, r.model),
        h("td", {}, statusPill),
        h("td", { class: "r" }, fmtInt(r.iterations)),
        h("td", { class: "r" }, fmtInt(r.input_tokens)),
        h("td", { class: "r" }, fmtInt(r.output_tokens)),
        h("td", { class: "r" }, fmtCost(r.cost_usd, !!r.cost_known)),
        h("td", {}, r.design_id ? h("a", { href: `#/studio/${r.design_id}`, onclick: (e) => e.stopPropagation() }, "Open") : ""));
      const toggle = async () => {
        if (tr.nextSibling?.classList?.contains("steps")) { tr.nextSibling.remove(); return; }
        const d = await api(`/api/usage/runs/${r.id}`);
        const inner = h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, "Step"), h("th", {}, "Tools called"), h("th", { class: "r" }, "Input"), h("th", { class: "r" }, "Output"), h("th", { class: "r" }, "Cached"), h("th", { class: "r" }, "Cost"), h("th", { class: "r" }, "Time"))),
          h("tbody", {}, d.steps.map((st) => h("tr", {}, h("td", {}, st.step), h("td", {}, st.tool_calls.join(", ") || "none"), h("td", { class: "r" }, fmtInt(st.input_tokens)), h("td", { class: "r" }, fmtInt(st.output_tokens)), h("td", { class: "r" }, fmtInt(st.cache_read_tokens)), h("td", { class: "r" }, fmtCost(st.cost_usd)), h("td", { class: "r" }, `${(st.duration_ms / 1000).toFixed(1)} s`)))));
        const row = h("tr", { class: "steps" }, h("td", { colspan: 9 }, d.error ? h("div", { class: "err-box", style: "margin-bottom:8px" }, d.error) : null, inner));
        tr.after(row);
      };
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", (e) => e.key === "Enter" && toggle());
      tbody.append(tr);
    }
    const table = h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Prompt"), h("th", {}, "Model"), h("th", {}, "Status"), h("th", { class: "r" }, "Steps"), h("th", { class: "r" }, "Input"), h("th", { class: "r" }, "Output"), h("th", { class: "r" }, "Cost"), h("th", {}, ""))), tbody);
    const pager = h("div", { class: "row", style: "margin-top:12px" },
      h("span", { class: "hint", style: "margin:0" }, total ? `${offset + 1} to ${Math.min(offset + 25, total)} of ${fmtInt(total)}` : ""),
      h("span", { class: "spacer" }),
      h("button", { class: "btn btn-sm", disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - 25); loadRuns(); } }, "Newer"),
      h("button", { class: "btn btn-sm", disabled: offset + 25 >= total, onclick: () => { offset += 25; loadRuns(); } }, "Older"));
    runsSec.append(h("div", { class: "section-head" }, h("h2", {}, "Requests"), h("p", {}, "Select a request to see the tokens used by each step of the design loop.")),
      h("div", { class: "section-body" }, total ? h("div", { class: "table-scroll" }, table) : h("p", { class: "hint" }, "No requests yet."), total ? pager : null));
  }

  range.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", range).forEach((x) => x.classList.toggle("on", x === b));
    days = Number(b.dataset.days);
    load();
  });
  await load();
}

function chart(daily, days) {
  const W = 900, H = 180, P = { l: 44, r: 8, t: 8, b: 22 };
  const map = Object.fromEntries(daily.map((d) => [d.day, d]));
  const n = Math.min(days, 90);
  const series = [];
  for (let i = n - 1; i >= 0; i--) {
    const dt = new Date(Date.now() - i * 86400000);
    const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
    series.push({ key, ...(map[key] || { input_tokens: 0, output_tokens: 0 }) });
  }
  const max = Math.max(1, ...series.map((d) => d.input_tokens + d.output_tokens));
  const bw = (W - P.l - P.r) / series.length;
  const y = (v) => P.t + (H - P.t - P.b) * (1 - v / max);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Tokens per day">`;
  for (const f of [0, 0.5, 1]) svg += `<line class="axis" x1="${P.l}" x2="${W - P.r}" y1="${y(max * f)}" y2="${y(max * f)}"/><text x="${P.l - 6}" y="${y(max * f) + 4}" text-anchor="end">${fmtTokens(Math.round(max * f))}</text>`;
  series.forEach((d, i) => {
    const x = P.l + i * bw + bw * 0.15, w = Math.max(1, bw * 0.7);
    const yi = y(d.input_tokens), yo = y(d.input_tokens + d.output_tokens);
    svg += `<rect class="bar-in" x="${x}" y="${yi}" width="${w}" height="${H - P.b - yi}" rx="1"><title>${d.key}: ${fmtInt(d.input_tokens)} input</title></rect>`;
    svg += `<rect class="bar-out" x="${x}" y="${yo}" width="${w}" height="${yi - yo}" rx="1"><title>${d.key}: ${fmtInt(d.output_tokens)} output</title></rect>`;
    if (i % Math.ceil(series.length / 8) === 0) svg += `<text x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${d.key.slice(5)}</text>`;
  });
  return h("div", { html: svg + "</svg>" });
}

// ================================================================= SETTINGS
async function Settings(root) {
  const st = structuredClone(await loadSettings());
  const sys = await api("/api/system");
  const page = h("div", { class: "page" });
  const draft = structuredClone(st);
  let dirty = false;
  const saveState = h("span", { class: "state" }, "All changes saved");
  const saveBtn = h("button", { class: "btn btn-primary", disabled: true }, "Save changes");
  const markDirty = () => { dirty = true; saveBtn.disabled = false; saveState.textContent = "Unsaved changes"; };

  const field = (label, input, hint) => h("div", {}, h("label", { for: input.id }, label), input, hint ? h("p", { class: "hint" }, hint) : null);
  let uid = 0;
  const bind = (path, type = "text", attrs = {}) => {
    const [sec, key] = path.split(".");
    const id = `f${++uid}`;
    const el = type === "select"
      ? h("select", { id }, attrs.options.map(([v, l]) => h("option", { value: v, selected: String(draft[sec][key]) === String(v) }, l)))
      : type === "textarea" ? h("textarea", { id, rows: attrs.rows || 4 }, draft[sec][key])
      : h("input", { id, type, value: draft[sec][key], ...attrs });
    el.addEventListener("input", () => { draft[sec][key] = type === "number" ? Number(el.value) : el.value; markDirty(); });
    return el;
  };

  page.append(h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Settings"), h("p", {}, "Changes apply immediately. Nothing needs a restart or rebuild."))));
  const toc = h("nav", { class: "toc", "aria-label": "Settings sections" },
    ...[["providers", "AI providers"], ["agent", "Design agent"], ["printer", "Printers"], ["render", "Rendering"], ["access", "Access and MCP"], ["pricing", "Pricing"], ["system", "System"]].map(([id, l]) => h("a", { href: `#/settings`, onclick: (e) => { e.preventDefault(); $(`#sec-${id}`).scrollIntoView({ behavior: "smooth", block: "start" }); } }, l)));
  const body = h("div", {});
  page.append(h("div", { class: "settings-layout" }, toc, body));
  root.append(page);

  const section = (id, title, desc, ...content) => h("section", { class: "section", id: `sec-${id}` }, h("div", { class: "section-head" }, h("h2", {}, title), desc ? h("p", {}, desc) : null), h("div", { class: "section-body" }, ...content));

  // ---- providers (saved individually, since connecting needs a live test)
  const provWrap = h("div", {});
  function renderProviders() {
    provWrap.innerHTML = "";
    for (const [pid, p] of Object.entries(app.settings.providers)) {
      const meta = p.meta;
      const enabled = h("input", { type: "checkbox", checked: p.enabled, "aria-label": `Enable ${meta.label}` });
      const keyInput = meta.needs_key || pid === "openai_compatible"
        ? h("input", { type: "password", placeholder: p.api_key_set ? `Saved key ${p.api_key_hint}. Enter a new one to replace it.` : meta.needs_key ? "Paste your API key" : "Optional API key", autocomplete: "off", id: `k-${pid}` })
        : null;
      const urlInput = meta.needs_base_url ? h("input", { type: "url", value: p.base_url, id: `u-${pid}` }) : null;
      const testBtn = h("button", { class: "btn" }, p.api_key_set || !meta.needs_key ? "Test and load models" : "Connect");
      const status = h("span", { class: "hint", style: "margin:0" }, p.models?.length ? `${p.models.length} models available` : "");
      const box = h("div", { class: `provider ${p.enabled ? "on" : ""}` },
        h("div", { class: "provider-head" },
          h("label", { class: "switch" }, enabled),
          h("h3", {}, meta.label),
          p.api_key_set ? h("span", { class: "pill good" }, "Key saved") : null,
          pid === app.settings.agent.provider ? h("span", { class: "pill accent" }, "Active") : null,
          h("span", { class: "spacer" }),
          meta.key_url ? h("a", { href: meta.key_url, target: "_blank", rel: "noopener", class: "hint", style: "margin:0" }, "Get a key") : null),
        h("div", { class: "provider-body" },
          urlInput ? h("div", { class: "full" }, h("label", { for: urlInput.id }, "Base URL"), urlInput, h("p", { class: "hint" }, pid === "ollama" ? "Where Ollama listens. From inside Docker, use your host's LAN IP or host.docker.internal." : "The server's OpenAI-compatible /v1 endpoint.")) : null,
          keyInput ? h("div", {}, h("label", { for: keyInput.id }, "API key"), keyInput) : h("div", {}),
          h("div", { class: "row" }, testBtn, p.api_key_set ? h("button", { class: "btn btn-ghost btn-danger", onclick: async () => {
            if (!confirm(`Remove the saved ${meta.label} key?`)) return;
            await api("/api/settings", { method: "PATCH", json: { providers: { [pid]: { api_key: "", enabled: false } } } });
            await loadSettings(); renderProviders(); toast("Key removed");
          } }, "Remove key") : null),
          h("div", { class: "full" }, status)));
      enabled.addEventListener("change", async () => {
        await api("/api/settings", { method: "PATCH", json: { providers: { [pid]: { enabled: enabled.checked } } } });
        await loadSettings(); renderProviders(); renderAgent();
      });
      testBtn.addEventListener("click", async () => {
        testBtn.disabled = true; status.textContent = "Connecting";
        try {
          const r = await api(`/api/providers/${pid}/models`, { method: "POST", json: { api_key: keyInput?.value || null, base_url: urlInput?.value || null } });
          await loadSettings();
          if (!app.settings.agent.provider || !app.settings.agent.model) {
            const pick = r.models[0];
            if (pick) await api("/api/settings", { method: "PATCH", json: { agent: { provider: pid, model: pick } } });
            await loadSettings();
            Object.assign(draft.agent, app.settings.agent);
          }
          toast(`${meta.label} connected: ${r.count} models`);
          renderProviders(); renderAgent();
        } catch (err) {
          status.textContent = err.message; status.style.color = "var(--bad)";
        } finally { testBtn.disabled = false; }
      });
      provWrap.append(box);
    }
  }

  // ---- agent
  const agentWrap = h("div", {});
  function renderAgent() {
    agentWrap.innerHTML = "";
    const sel = modelSelect({ id: "agent-model" });
    sel.addEventListener("change", () => { const [p, m] = sel.value.split("::"); draft.agent.provider = p; draft.agent.model = m; markDirty(); });
    const custom = h("input", { type: "text", id: "agent-custom", placeholder: "or type a model ID" });
    custom.addEventListener("input", () => { if (custom.value.trim()) { const [p] = (sel.value || "::").split("::"); draft.agent.provider = p || draft.agent.provider; draft.agent.model = custom.value.trim(); markDirty(); } });
    agentWrap.append(
      h("div", { class: "grid-2" }, field("Default model", sel, "Used for new requests. You can override it per request in Studio."), field("Custom model ID", custom, "For a model that is not listed, on the provider selected to the left.")),
      h("div", { class: "grid-4", style: "margin-top:16px" },
        field("Max steps", bind("agent.max_iterations", "number", { min: 1, max: 25 }), "Model calls per request"),
        field("Temperature", bind("agent.temperature", "number", { min: 0, max: 2, step: 0.1 })),
        field("Max output tokens", bind("agent.max_output_tokens", "number", { min: 512, step: 512 })),
        field("Image previews", bind("agent.vision", "select", { options: [["auto", "Automatic"], ["on", "Always send"], ["off", "Never send"]] }), "Images cost tokens")),
      h("div", { class: "grid-2", style: "margin-top:16px" }, field("Preview size (px)", bind("agent.preview_size", "number", { min: 256, max: 2048, step: 64 }), "Smaller previews use fewer tokens.")),
      h("div", { style: "margin-top:16px" }, field("Extra instructions", bind("agent.extra_instructions", "textarea", { rows: 3 }), "Added to every request. For example: prefer BOSL2, always add 1 mm chamfers, design for PETG.")));
  }

  // ---- access & MCP
  const accessWrap = h("div", {});
  function renderAccess() {
    accessWrap.innerHTML = "";
    const sec = app.settings.security;
    const cur = h("input", { type: "password", id: "pw-cur", autocomplete: "current-password" });
    const nw = h("input", { type: "password", id: "pw-new", autocomplete: "new-password" });
    const mcpUrl = `${location.origin}/mcp`;
    const reveal = h("div", {});
    accessWrap.append(
      h("h3", { class: "h3" }, "Web interface password"),
      h("p", { class: "hint", style: "margin:0 0 10px" }, sec.password_enabled ? "A password is required to open Buildplate." : "Anyone who can reach this address can use Buildplate and your API keys. Set a password unless it sits behind an authenticating proxy."),
      h("div", { class: "grid-3", style: "align-items:end" },
        sec.password_enabled ? field("Current password", cur) : null,
        field(sec.password_enabled ? "New password" : "Password", nw, "At least 8 characters. Leave empty and save to remove."),
        h("div", {}, h("button", { class: "btn", onclick: async () => {
          try {
            const r = await api("/api/settings/password", { method: "POST", json: { current: cur.value, new: nw.value } });
            toast(r.password_enabled ? "Password saved" : "Password removed");
            await loadSettings(); renderAccess();
            $("#logout").hidden = !r.password_enabled;
          } catch (err) { toast(err.message, "bad"); }
        } }, sec.password_enabled ? "Change password" : "Set password"))),
      h("h3", { class: "h3", style: "margin-top:24px" }, "MCP endpoint"),
      h("p", { class: "hint", style: "margin:0 0 10px" }, "Connect Claude Desktop, Claude Code or any MCP client to use Buildplate's renderer, validator, library and design agent."),
      h("dl", { class: "kv" }, h("dt", {}, "Server URL"), h("dd", { class: "mono" }, mcpUrl), h("dt", {}, "Bearer token"), h("dd", {}, sec.mcp_token_set ? h("span", { class: "pill good" }, `Required (${sec.mcp_token_hint})`) : h("span", { class: "pill warn" }, "Not set: the endpoint is open"))),
      h("div", { class: "row", style: "margin-top:12px" },
        h("button", { class: "btn", onclick: async () => {
          if (sec.mcp_token_set && !confirm("Replace the token? Clients using the old one will stop working.")) return;
          const r = await api("/api/settings/mcp-token", { method: "POST" });
          await loadSettings(); renderAccess();
          $("#mcp-reveal").replaceChildren(h("p", { class: "hint" }, "Copy it now. It will not be shown again."), h("div", { class: "token-reveal" }, r.token),
            h("pre", { class: "mono", style: "margin-top:10px;white-space:pre-wrap" }, `claude mcp add --transport http buildplate ${mcpUrl} --header "Authorization: Bearer ${r.token}"`));
        } }, sec.mcp_token_set ? "Generate new token" : "Generate token"),
        sec.mcp_token_set ? h("button", { class: "btn btn-ghost btn-danger", onclick: async () => {
          if (!confirm("Remove the token and leave the MCP endpoint open?")) return;
          await api("/api/settings/mcp-token", { method: "DELETE" }); await loadSettings(); renderAccess();
        } }, "Remove token") : null),
      h("div", { id: "mcp-reveal", style: "margin-top:12px" }),
      h("div", { class: "grid-2", style: "margin-top:16px" }, field("Public URL", bind("server.public_url", "url", { placeholder: "https://buildplate.example.com" }), "Used to build download links returned to MCP clients.")));
  }

  // ---- pricing overrides
  const priceWrap = h("div", {});
  function renderPricing() {
    priceWrap.innerHTML = "";
    const ov = draft.pricing.overrides;
    const rows = Object.entries(ov);
    const tbody = h("tbody", {}, rows.map(([k, v]) => h("tr", {},
      h("td", { class: "mono" }, k),
      h("td", { class: "r" }, `$${v.input}`),
      h("td", { class: "r" }, `$${v.output}`),
      h("td", { class: "r" }, h("button", { class: "btn btn-ghost btn-sm btn-danger", onclick: () => { delete ov[k]; markDirty(); renderPricing(); } }, "Remove")))));
    const m = h("input", { type: "text", placeholder: "provider/model, e.g. openrouter/some-model", id: "po-m" });
    const i = h("input", { type: "number", step: "0.01", placeholder: "0.00", id: "po-i" });
    const o = h("input", { type: "number", step: "0.01", placeholder: "0.00", id: "po-o" });
    priceWrap.append(
      rows.length ? h("table", { class: "data", style: "margin-bottom:16px" }, h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", { class: "r" }, "Input per 1M"), h("th", { class: "r" }, "Output per 1M"), h("th", {}))), tbody) : null,
      h("div", { class: "grid-4", style: "align-items:end" }, field("Model", m), field("Input $ per 1M tokens", i), field("Output $ per 1M tokens", o),
        h("div", {}, h("button", { class: "btn", onclick: () => { if (!m.value.includes("/")) return toast("Use the form provider/model", "bad"); ov[m.value.trim()] = { input: Number(i.value || 0), output: Number(o.value || 0) }; markDirty(); renderPricing(); } }, "Add price"))));
  }

  // ---- printers
  const printerWrap = h("div", {});
  let editId = draft.active_printer;
  function renderPrinters() {
    printerWrap.innerHTML = "";
    const list = h("div", { class: "printer-list", role: "list" });
    for (const pr of draft.printers) {
      const active = pr.id === draft.active_printer;
      list.append(h("button", { class: `pcard ${pr.id === editId ? "on" : ""}`, role: "listitem", onclick: () => { editId = pr.id; renderPrinters(); } },
        h("span", { class: "pcard-icon", html: printerIcon(pr.type) }),
        h("span", { class: "pcard-text" },
          h("span", { class: "pcard-name" }, pr.name, active ? h("span", { class: "pill accent", style: "margin-left:8px" }, "Active") : null),
          h("span", { class: "pcard-dims tabular" }, `${+pr.bed_x} x ${+pr.bed_y} x ${+pr.bed_z} mm, ${+pr.nozzle} mm nozzle`))));
    }
    const pr = draft.printers.find((x) => x.id === editId) || draft.printers[0];
    editId = pr.id;
    const pf = (label, key, type = "number", attrs = {}, hint) => {
      const id = `pr-${key}`;
      const el = h("input", { id, type, value: pr[key], ...attrs });
      el.addEventListener("input", () => {
        pr[key] = type === "number" ? Number(el.value) : el.value;
        markDirty();
        if (key === "name" || key.startsWith("bed_") || key === "nozzle") {
          const card = $(".pcard.on", list);
          if (card) { $(".pcard-name", card).firstChild.textContent = pr.name; $(".pcard-dims", card).textContent = `${+pr.bed_x} x ${+pr.bed_y} x ${+pr.bed_z} mm, ${+pr.nozzle} mm nozzle`; }
        }
      });
      return field(label, el, hint);
    };
    const addBtn = h("button", { class: "btn btn-primary", html: icons.plus + "Add printers" });
    addBtn.addEventListener("click", async () => {
      const picked = await openPrinterPicker(draft.printers.map((x) => x.id));
      if (!picked.length) return;
      for (const prof of picked) if (!draft.printers.some((x) => x.id === prof.id)) draft.printers.push(prof);
      editId = picked[0].id;
      markDirty();
      renderPrinters();
      toast(`${picked.length} printer${picked.length > 1 ? "s" : ""} added. Save changes to keep ${picked.length > 1 ? "them" : "it"}.`);
    });
    const blankBtn = h("button", { class: "btn btn-ghost", onclick: () => {
      const id = `p${Date.now().toString(36)}`;
      draft.printers.push({ id, name: "Custom printer", shape: "rect", type: "generic", bed_x: 220, bed_y: 220, bed_z: 250, nozzle: 0.4, layer_height: 0.2, clearance: 0.25, max_overhang: 45 });
      editId = id; markDirty(); renderPrinters(); $("#pr-name")?.focus();
    } }, "Add custom printer");
    const isActive = pr.id === draft.active_printer;
    printerWrap.append(
      list,
      h("div", { class: "printer-editor" },
        h("div", { class: "row", style: "margin-bottom:12px" },
          h("h3", { class: "h3", style: "margin:0" }, `Editing ${pr.name}`),
          h("span", { class: "spacer" }),
          isActive ? h("span", { class: "hint", style: "margin:0" }, "This is the active printer") : h("button", { class: "btn btn-sm", onclick: () => { draft.active_printer = pr.id; markDirty(); renderPrinters(); } }, "Make active"),
          h("button", { class: "btn btn-sm btn-ghost", onclick: () => { const id = `p${Date.now().toString(36)}`; draft.printers.push({ ...structuredClone(pr), id, name: `${pr.name} copy` }); editId = id; markDirty(); renderPrinters(); } }, "Duplicate"),
          draft.printers.length > 1 ? h("button", { class: "btn btn-sm btn-ghost btn-danger", onclick: () => {
            if (!confirm(`Remove the ${pr.name} profile?`)) return;
            draft.printers = draft.printers.filter((x) => x.id !== pr.id);
            if (draft.active_printer === pr.id) draft.active_printer = draft.printers[0].id;
            editId = draft.active_printer;
            markDirty(); renderPrinters();
          } }, "Remove") : null),
        h("div", { class: "grid-4" },
          pf("Name", "name", "text"),
          pf("Bed X (mm)", "bed_x", "number", { min: 20 }),
          pf("Bed Y (mm)", "bed_y", "number", { min: 20 }),
          pf("Max height (mm)", "bed_z", "number", { min: 20 })),
        h("div", { class: "grid-4", style: "margin-top:16px" },
          pf("Nozzle (mm)", "nozzle", "number", { step: 0.05 }),
          pf("Layer height (mm)", "layer_height", "number", { step: 0.02 }),
          pf("Fit clearance (mm)", "clearance", "number", { step: 0.05 }, "Gap for parts that mate"),
          pf("Max overhang (deg)", "max_overhang", "number")),
        h("div", { class: "grid-4", style: "margin-top:16px" },
          field("Printer type", (() => { const sel = h("select", { id: "pr-type" }, PRINTER_TYPES.map(([v, l]) => h("option", { value: v, selected: (pr.type || "generic") === v }, l))); sel.addEventListener("change", () => { pr.type = sel.value; markDirty(); const ic = $(".pcard.on .pcard-icon", list); if (ic) ic.innerHTML = printerIcon(pr.type); }); return sel; })(), "Sets the illustration"),
          field("Bed shape", (() => { const sel = h("select", { id: "pr-shape" }, h("option", { value: "rect", selected: pr.shape !== "round" }, "Rectangular"), h("option", { value: "round", selected: pr.shape === "round" }, "Round (delta)")); sel.addEventListener("change", () => { pr.shape = sel.value; markDirty(); }); return sel; })(), "For round beds, Bed X is the diameter")),
        pr.preset ? h("p", { class: "hint" }, "Values come from the printer catalog, which uses slicer printable areas. Adjust them if your machine differs.") : null),
      h("div", { class: "row printer-add" }, addBtn, blankBtn));
  }

  const p = sys.openscad;
  body.append(
    section("providers", "AI providers", "Connect one or more providers. Keys are encrypted on disk and never sent back to the browser.", provWrap),
    section("agent", "Design agent", "How the AI works on a request.", agentWrap),
    section("printer", "Printers", "Save a profile for each printer you own. The agent designs to the active printer's limits, and the viewer draws its build plate. Switch printers here or from the menu above the viewer in Studio.", printerWrap),
    section("render", "Rendering", null,
      h("div", { class: "grid-4" },
        field("Geometry engine", bind("render.backend", "select", { options: [["auto", "Automatic"], ["manifold", "Manifold (fast)"], ["cgal", "CGAL (classic)"]] }), p.manifold ? "Manifold is available" : "This OpenSCAD build only has CGAL"),
        field("Timeout (s)", bind("render.timeout", "number", { min: 10 })),
        field("Parallel renders", bind("render.max_parallel", "number", { min: 1, max: 16 })),
        field("Preview colours", bind("render.colorscheme", "select", { options: ["Tomorrow", "Cornfield", "Metallic", "Sunset", "Starnight", "BeforeDawn", "Nature", "DeepOcean", "Solarized", "Tomorrow Night", "Monotone"].map((c) => [c, c]) })))),
    section("access", "Access and MCP", null, accessWrap),
    section("pricing", "Pricing overrides", "Costs come from LiteLLM's price list. Add a price here for models it does not know, or to match a negotiated rate. Local models are free.", priceWrap),
    section("system", "System", null,
      h("dl", { class: "kv" },
        h("dt", {}, "Buildplate"), h("dd", {}, sys.version),
        h("dt", {}, "OpenSCAD"), h("dd", {}, p.available ? `${p.version}${p.manifold ? " with Manifold" : ""}` : h("span", { style: "color:var(--bad)" }, `Not working: ${p.error || "not found"}`)),
        h("dt", {}, "Libraries"), h("dd", {}, sys.libraries.map((l) => l.name).join(", ") || "none"),
        h("dt", {}, "Your libraries folder"), h("dd", { class: "mono" }, sys.user_library_dir),
        h("dt", {}, "Data folder"), h("dd", { class: "mono" }, sys.data_dir),
        h("dt", {}, "Render cache"), h("dd", {}, `${(sys.cache_bytes / 1048576).toFixed(1)} MB`),
        h("dt", {}, "Components"), h("dd", {}, Object.entries(sys.packages).map(([k, v]) => `${k} ${v}`).join(", "))),
      h("div", { class: "row", style: "margin-top:14px" },
        h("button", { class: "btn", onclick: async () => { const r = await api("/api/system/clear-cache", { method: "POST" }); toast(`Cleared ${r.removed} cached files`); } }, "Clear render cache"),
        h("a", { class: "btn btn-ghost", href: "/api/docs", target: "_blank" }, "REST API reference"))),
    h("div", { class: "save-bar" }, saveState, saveBtn));

  renderProviders(); renderAgent(); renderAccess(); renderPricing(); renderPrinters();
  if (location.hash.endsWith("#printers") || sessionStorage.getItem("bp-jump") === "printer") {
    sessionStorage.removeItem("bp-jump");
    requestAnimationFrame(() => $("#sec-printer")?.scrollIntoView({ block: "start" }));
  }

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const { providers, security, printer, ...rest } = draft;
      rest.security = { session_hours: draft.security.session_hours };
      await api("/api/settings", { method: "PATCH", json: rest });
      // Overrides are replaced as a whole so removals stick.
      await loadSettings();
      dirty = false;
      saveState.textContent = "Saved. Changes are live.";
      toast("Settings saved");
      Object.assign(draft, structuredClone(app.settings));
      editId = draft.printers.some((x) => x.id === editId) ? editId : draft.active_printer;
      renderProviders(); renderAgent(); renderAccess(); renderPrinters();
    } catch (err) {
      saveBtn.disabled = false;
      toast(err.message, "bad");
    }
  });
  const beforeUnload = (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", beforeUnload);
  return () => window.removeEventListener("beforeunload", beforeUnload);
}


// ================================================================= PRINTER PICKER
let catalogCache = null;
async function printerCatalog() {
  catalogCache ||= await api("/api/printers/catalog");
  return catalogCache;
}

function profileFromCatalog(p, nozzle) {
  const nz = String(+nozzle);
  return {
    id: `${p.key}-${nz.replace(".", "")}`,
    name: `${p.display} ${nz} nozzle`,
    preset: p.key,
    shape: p.shape || "rect",
    type: p.type || "generic",
    bed_x: p.bed_x, bed_y: p.bed_y, bed_z: p.bed_z,
    nozzle: +nozzle,
    layer_height: Math.round(Math.min(Math.max(nozzle * 0.5, 0.08), 0.4) * 100) / 100,
    clearance: nozzle <= 0.25 ? 0.2 : nozzle <= 0.5 ? 0.25 : 0.35,
    max_overhang: 45,
  };
}

function bedSketch(p) {
  const S = 64, m = 4;
  // Drawn to a shared scale so a bigger printer looks bigger (400 mm fills the box).
  const k = (S - 2 * m) / Math.max(400, p.bed_x, p.bed_y);
  const w = p.bed_x * k, hgt = p.bed_y * k;
  const x = (S - w) / 2, y = (S - hgt) / 2;
  const shape = p.shape === "round"
    ? `<circle cx="${S / 2}" cy="${S / 2}" r="${w / 2}" class="bed"/>`
    : `<rect x="${x}" y="${y}" width="${w}" height="${hgt}" rx="3" class="bed"/>`;
  let grid = "";
  for (let i = 1; i < 4; i++) {
    grid += `<line x1="${x + (w * i) / 4}" y1="${y}" x2="${x + (w * i) / 4}" y2="${y + hgt}"/><line x1="${x}" y1="${y + (hgt * i) / 4}" x2="${x + w}" y2="${y + (hgt * i) / 4}"/>`;
  }
  const clip = p.shape === "round" ? `<clipPath id="c-${p.key}"><circle cx="${S / 2}" cy="${S / 2}" r="${w / 2}"/></clipPath>` : "";
  return `<svg viewBox="0 0 ${S} ${S}" class="bed-sketch" aria-hidden="true">${clip}${shape}<g class="grid" ${clip ? `clip-path="url(#c-${p.key})"` : ""}>${grid}</g></svg>`;
}

// Opens the "Add printers" dialog. Resolves with the profiles the user picked
// (one per printer and nozzle size), or [] if cancelled.
async function openPrinterPicker(existingIds = []) {
  const cat = await printerCatalog();
  const existing = new Set(existingIds);
  const chosen = new Map(); // id -> profile
  return new Promise((resolve) => {
    let vendor = "Bambu Lab";
    if (!cat.vendors.some((v) => v.name === vendor)) vendor = cat.vendors[0].name;
    const search = h("input", { type: "text", placeholder: `Search ${cat.printers.length} printers`, "aria-label": "Search printers" });
    const vendorList = h("div", { class: "pp-vendors", role: "listbox", "aria-label": "Manufacturers" });
    const grid = h("div", { class: "pp-grid" });
    const count = h("span", { class: "pp-count" });
    const addBtn = h("button", { class: "btn btn-primary", disabled: true }, "Add printers");
    const cancelBtn = h("button", { class: "btn" }, "Cancel");
    const dialog = h("dialog", { class: "pp", "aria-label": "Add printers" },
      h("div", { class: "pp-head" },
        h("div", {}, h("h2", {}, "Add printers"), h("p", {}, "Pick your printers and the nozzle sizes you use. Each choice becomes a profile.")),
        search),
      h("div", { class: "pp-body" }, vendorList, grid),
      h("div", { class: "pp-foot" }, count, h("span", { class: "spacer" }), h("span", { class: "hint", style: "margin:0" }, `Catalog from ${cat.source.split(" (")[0]}, ${cat.generated}`), cancelBtn, addBtn));
    document.body.append(dialog);

    const updateFoot = () => {
      const n = chosen.size;
      count.textContent = n ? `${n} selected` : "Nothing selected";
      addBtn.disabled = !n;
      addBtn.textContent = n ? `Add ${n} printer${n > 1 ? "s" : ""}` : "Add printers";
      $$(".pp-vendor", vendorList).forEach((el) => {
        const v = el.dataset.vendor;
        const k = [...chosen.values()].filter((x) => cat.printers.find((p) => p.key === x.preset)?.vendor === v).length;
        $(".pp-vsel", el).textContent = k ? String(k) : "";
      });
    };

    const renderVendors = () => {
      vendorList.innerHTML = "";
      for (const v of cat.vendors) {
        vendorList.append(h("button", { class: `pp-vendor ${v.name === vendor && !search.value ? "on" : ""}`, role: "option", "aria-selected": v.name === vendor, "data-vendor": v.name, onclick: () => { vendor = v.name; search.value = ""; renderVendors(); renderGrid(); grid.scrollTop = 0; } },
          h("span", {}, v.name), h("span", { class: "pp-vsel" }), h("span", { class: "pp-vn" }, v.count)));
      }
      updateFoot();
    };

    const renderGrid = () => {
      grid.innerHTML = "";
      const q = search.value.trim().toLowerCase();
      const list = q
        ? cat.printers.filter((p) => `${p.vendor} ${p.model}`.toLowerCase().includes(q)).slice(0, 120)
        : cat.printers.filter((p) => p.vendor === vendor);
      if (!list.length) { grid.append(h("div", { class: "empty", style: "grid-column:1/-1" }, h("h3", {}, "No printers match"), "Try a shorter search, or add a custom printer in Settings.")); return; }
      for (const p of list) {
        const nozzles = h("div", { class: "pp-nozzles", role: "group", "aria-label": `Nozzle sizes for ${p.display}` });
        for (const nz of p.nozzles) {
          const prof = profileFromCatalog(p, nz);
          const added = existing.has(prof.id);
          const on = added || chosen.has(prof.id);
          const b = h("button", { class: `pp-nz ${on ? "on" : ""} ${added ? "added" : ""}`, "aria-pressed": on, disabled: added, title: added ? "Already added" : `${p.display} with a ${nz} mm nozzle` }, `${+nz} mm`);
          b.addEventListener("click", () => {
            if (chosen.has(prof.id)) chosen.delete(prof.id); else chosen.set(prof.id, prof);
            const now = chosen.has(prof.id);
            b.classList.toggle("on", now); b.setAttribute("aria-pressed", now);
            card.classList.toggle("picked", [...chosen.values()].some((x) => x.preset === p.key));
            updateFoot();
          });
          nozzles.append(b);
        }
        const dims = p.shape === "round" ? `Round ${+p.bed_x} mm, ${+p.bed_z} mm tall` : `${+p.bed_x} x ${+p.bed_y} x ${+p.bed_z} mm`;
        const card = h("div", { class: `pp-card ${[...chosen.values()].some((x) => x.preset === p.key) ? "picked" : ""}` },
          h("div", { class: "pp-art", title: typeLabel(p.type) }, h("div", { class: "pp-illo", html: printerIcon(p.type) }), h("div", { class: "pp-bed", title: "Build plate to scale", html: bedSketch(p) })),
          h("div", { class: "pp-name" }, q ? p.display : p.model),
          h("div", { class: "pp-dims tabular" }, dims),
          nozzles);
        grid.append(card);
      }
    };

    const close = (result) => { dialog.close(); dialog.remove(); resolve(result); };
    search.addEventListener("input", debounce(() => { renderVendors(); renderGrid(); }, 150));
    cancelBtn.addEventListener("click", () => close([]));
    addBtn.addEventListener("click", () => close([...chosen.values()]));
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); close([]); });
    renderVendors();
    renderGrid();
    dialog.showModal();
    $(".pp-vendor.on", vendorList)?.scrollIntoView({ block: "center" });
    search.focus();
  });
}

// ================================================================= DOCS
const DOCS = [
  ["getting-started", "Getting started"],
  ["providers", "AI providers"],
  ["using-studio", "Using Studio"],
  ["token-usage", "Token usage and cost"],
  ["mcp", "MCP integration"],
  ["unraid", "Unraid"],
  ["configuration", "Configuration reference"],
  ["security", "Security"],
  ["api", "REST API"],
  ["troubleshooting", "Troubleshooting"],
];

async function Docs(root, [slug = "getting-started"]) {
  const page = h("div", { class: "page" });
  const toc = h("nav", { class: "toc", "aria-label": "Documentation" }, DOCS.map(([id, t]) => h("a", { href: `#/docs/${id}`, style: id === slug ? "background:var(--panel);color:var(--ink)" : "" }, t)));
  const prose = h("article", { class: "prose" });
  page.append(h("div", { class: "docs-layout" }, toc, prose));
  root.append(page);
  const res = await fetch(`/docs-content/${encodeURIComponent(slug)}.md`);
  const md = res.ok ? await res.text() : "# Not found\n\nThat page does not exist.";
  prose.innerHTML = marked.parse(md.replace(/\]\(\.\/([\w-]+)\.md(#[\w-]+)?\)/g, "](#/docs/$1)"));
}

// ---------------------------------------------------------------- boot
async function boot() {
  const st = await api("/api/auth/status");
  if (!st.authenticated) return showLogin();
  $("#logout").hidden = !st.password_enabled;
  await loadSettings();
  route();
}

window.addEventListener("hashchange", route);
boot();
