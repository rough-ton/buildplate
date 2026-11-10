// Buildplate web UI. Plain ES modules, no build step.
import { Viewer } from "/viewer.js";
import { marked } from "/vendor/marked/marked.esm.js";
import { printerIcon, PRINTER_TYPES, typeLabel } from "/printer-icons.js";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtInt = (n) => Number(n || 0).toLocaleString();
// Request-sized costs are often a few cents, so keep four decimals below $1.
const fmtCost = (n, known = true) => (!known || n == null ? "n/a" : n === 0 ? "$0" : n < 1 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`);
const fmtTokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : fmtInt(n));
const ago = (t) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
};
// Cost for display. Plan (subscription) usage costs nothing extra, so show what it
// would cost at API list prices, tagged "plan".
function costView(billing, cost, known = true, est = null) {
  if (billing !== "subscription") return h("span", { class: "tabular" }, fmtCost(cost, known));
  const title = est == null
    ? "Included in your Claude plan. This model has no API price on file, so there is no estimate."
    : `Included in your Claude plan, so you are not charged for it. At API list prices it would cost about ${fmtCost(est)}.`;
  return h("span", { class: "plan-cost", title }, h("span", { class: "tabular" }, est == null ? "n/a" : `~${fmtCost(est)}`), h("span", { class: "pill plan-tag" }, "plan"));
}
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
// Short provider name: "Anthropic" rather than "Anthropic (Claude API)"; unknown ids are capitalised.
function providerName(id) {
  if (!id) return "";
  const label = app.settings?.providers?.[id]?.meta?.label;
  return label ? label.split(" (")[0] : id.charAt(0).toUpperCase() + id.slice(1);
}

// Who made a design version: {label, title} for a pill and its tooltip.
function madeBy(v) {
  const run = v.run;
  const usage = run ? `\n${fmtTokens(run.input_tokens)} in, ${fmtTokens(run.output_tokens)} out tokens, ${fmtCost(run.cost_usd, run.cost_known)}` : "";
  if (v.origin === "mcp") {
    const who = v.client || "an external MCP client";
    const what = [providerName(v.provider), v.model].filter(Boolean).join(" \u00b7 ");
    // lead with the client so external designs never look like Buildplate's own agent runs
    const label = v.model ? `${v.client || "External"} \u00b7 ${v.model}` : v.client ? `${v.client} (MCP)` : "External (MCP)";
    return { label, title: `Saved over MCP by ${who}${what ? `, written by ${what}` : ""}`, kind: "external" };
  }
  if (v.model) {
    const label = `${providerName(v.provider)} \u00b7 ${v.model}`;
    return { label, title: `Generated by ${label}${usage}`, kind: "agent" };
  }
  if (v.origin === "agent") return { label: "AI generated", title: `Generated by the design agent${usage}`, kind: "agent" };
  return { label: "Manual edit", title: "Code written or edited by hand", kind: "manual" };
}

const BILLING_LABELS = { api: "Pay per use", subscription: "Uses your plan", cloud: "Cloud account", local: "Local" };
function connectionById(id) { return (app.settings?.connections || []).find((c) => c.id === id); }

function renderModelChip() {
  const chip = $("#model-chip");
  const a = app.settings?.agent || {};
  const conn = connectionById(a.connection);
  if (conn && a.model) {
    chip.className = "model-chip";
    chip.innerHTML = `<strong>${esc(a.model)}</strong>${esc(conn.label)}`;
  } else {
    chip.className = "model-chip missing";
    chip.innerHTML = `<strong>No model selected</strong><a href="#/settings">Choose one in Settings</a>`;
  }
}

// Picker values are "<connection id>::<model>", grouped by connection.
function modelSelect(attrs = {}) {
  const a = app.settings.agent;
  const sel = h("select", attrs);
  for (const c of app.settings?.connections || []) {
    if (!c.enabled) continue;
    const models = new Set(c.models || []);
    if (c.id === a.connection && a.model) models.add(a.model);
    if (!models.size) continue;
    const group = h("optgroup", { label: `${c.label} (${BILLING_LABELS[c.billing] || c.billing})` });
    for (const m of models) group.append(h("option", { value: `${c.id}::${m}`, selected: c.id === a.connection && m === a.model }, m));
    sel.append(group);
  }
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
const routes = { studio: Studio, library: Library, parts: Parts, usage: Usage, settings: Settings, docs: Docs };

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
    bom: null,
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
  // How to handle screws, inserts, magnets and other hardware for the next request.
  const hwSel = h("select", { class: "hw-select", "aria-label": "Hardware for this request", title: "How to handle screws, inserts, magnets and other hardware" },
    ...Object.entries(HARDWARE_MODES).map(([v, l]) => h("option", { value: v }, `Hardware: ${l}`)));
  hwSel.value = app.settings.agent.hardware_mode || "ask";
  const tags = tagEditor(async (next) => (await api(`/api/designs/${s.designId}/tags`, { method: "PUT", json: { tags: next } })).tags);
  tags.el.hidden = true;
  const left = h("section", { class: "pane pane-left on-mobile", "data-m": "chat" },
    h("div", { class: "pane-head" }, titleInput, h("span", { class: "spacer" }), newBtn),
    tags.el,
    chat,
    h("div", { class: "composer" }, prompt,
      h("div", { class: "composer-foot" }, picker, h("span", { class: "spacer" }), sendBtn),
      h("div", { class: "row composer-meta" }, hwSel, h("span", { class: "spacer" }), sessionMeter)));

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
  const TABS = [["params", "Parameters"], ["code", "Code"], ["checks", "Checks"], ["parts", "Parts"], ["history", "History"]];
  function tabCount() {
    tabs.innerHTML = "";
    for (const [id, label] of TABS) {
      const count = id === "params" ? s.params.length : id === "checks" && s.stats?.issues?.length ? s.stats.issues.length : id === "history" ? s.versions.length : id === "parts" ? s.bom?.parts?.length || null : null;
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
    if (s.tab === "parts") return renderParts();
  }

  async function loadBom() {
    s.bom = s.designId ? await api(`/api/designs/${s.designId}/bom?version=${s.version}`).catch(() => null) : null;
    if (s.tab === "parts") renderTab(); else tabCount();
  }

  // Parts used by the current version: what is on hand, what to buy, and where.
  function renderParts() {
    const wrap = h("div", { class: "checks" });
    const b = s.bom;
    if (!s.designId || !b) {
      wrap.append(h("div", { class: "empty" }, h("h3", {}, "No parts yet"), "When a design uses screws, inserts, magnets or other hardware, the parts list appears here with what you have and where to buy the rest."));
      tabBody.append(wrap); return;
    }
    const mode = b.hardware_mode && b.hardware_mode !== "ask" ? HARDWARE_MODES[b.hardware_mode] : null;
    if (!b.parts.length) {
      wrap.append(h("div", { class: "empty" }, h("h3", {}, "No hardware"), mode === HARDWARE_MODES.none ? "This version was designed without hardware." : "This version does not use any hardware."));
      tabBody.append(wrap); return;
    }
    const toBuy = b.parts.filter((p) => p.to_buy);
    wrap.append(h("div", { class: "row" },
      h("strong", {}, `${b.parts.reduce((n, p) => n + p.quantity, 0)} parts`),
      toBuy.length ? h("span", { class: "pill warn" }, `${toBuy.length} to buy`) : h("span", { class: "pill good" }, "All on hand"),
      mode ? h("span", { class: "hint", style: "margin:0" }, mode) : null));
    if (b.hardware_note) wrap.append(h("div", { class: "issue" }, b.hardware_note));
    const list = h("div", { class: "bom" });
    for (const p of b.parts) {
      const status = p.status === "have" ? h("span", { class: "pill good" }, "Have") : p.status === "short" ? h("span", { class: "pill warn", title: `${p.on_hand} on hand` }, `Buy ${p.to_buy}`) : h("span", { class: "pill warn" }, `Buy ${p.to_buy}`);
      list.append(h("div", { class: "bom-row" },
        h("div", { class: "bom-qty tabular" }, `${p.quantity}x`),
        h("div", { class: "bom-name" }, h("div", {}, p.name), p.use ? h("div", { class: "hint", style: "margin:0" }, p.use) : null,
          p.status !== "have" ? h("div", { class: "bom-links" }, h("a", { href: p.links.bambu, target: "_blank", rel: "noopener noreferrer" }, "Bambu Lab Store"),
            h("a", { href: p.links.amazon, target: "_blank", rel: "noopener noreferrer sponsored" }, "Amazon"),
            p.links.other ? h("a", { href: p.links.other, target: "_blank", rel: "noopener noreferrer" }, "My link") : null) : null),
        status));
    }
    wrap.append(list);
    const fromStock = b.parts.some((p) => p.source === "inventory" && p.on_hand);
    if (b.built) {
      wrap.append(h("div", { class: "row" }, h("span", { class: "hint", style: "margin:0" }, `Marked as built ${ago(b.built.at)}; ${b.built.taken.reduce((n, t) => n + t.quantity, 0)} parts taken from your inventory.`),
        h("button", { class: "btn btn-sm btn-ghost", onclick: async () => { await api(`/api/designs/${s.designId}/versions/${b.version}/built`, { method: "DELETE" }); toast("Parts returned to your inventory"); loadBom(); } }, "Undo")));
    } else if (fromStock) {
      wrap.append(h("div", { class: "row" }, h("button", { class: "btn btn-sm", onclick: async () => {
        try {
          const r = await api(`/api/designs/${s.designId}/versions/${b.version}/built`, { method: "POST" });
          toast(`Took ${r.built.taken.reduce((n, t) => n + t.quantity, 0)} parts from your inventory`); loadBom();
        } catch (err) { toast(err.message, "bad"); }
      } }, "Mark as built"), h("span", { class: "hint", style: "margin:0" }, "Takes the parts you have out of your inventory.")));
    }
    wrap.append(h("p", { class: "hint" }, h("a", { href: "#/parts" }, "Manage your parts inventory")));
    tabBody.append(wrap);
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
        h("span", { class: "v" }, `v${v.version}`), h("span", { class: "s", title: v.summary }, v.summary || "No summary", (() => { const by = madeBy(v); return h("span", { class: "hint", style: "display:block;margin:0", title: by.title }, [by.label, v.stats?.printer].filter(Boolean).join(", ")); })()), h("span", { class: "spacer" }), h("span", { class: "hint", style: "margin:0;white-space:nowrap" }, ago(v.created_at))));
    }
    wrap.append(list);
    tabBody.append(wrap);
  }

  async function openVersion(v) {
    const ver = await api(`/api/designs/${s.designId}/versions/${v}`);
    s.version = v;
    loadBom();
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
      t.billing === "subscription"
        ? h("span", { class: "pill accent" }, costView("subscription", 0, true, t.est_cost_usd))
        : h("span", { class: "pill accent tabular" }, fmtCost(t.cost_usd, t.cost_known)),
      steps ? h("span", { class: "pill" }, `${steps} step${steps > 1 ? "s" : ""}`) : null);
  }

  function updateSessionMeter() {
    const t = s.sessionTokens;
    sessionMeter.textContent = t.input || t.output ? `This session: ${fmtTokens(t.input + t.output)} tokens, ${fmtCost(t.cost)}` : "";
  }

  // Show the hardware question as choices; picking one re-sends the request with it.
  function askHardware(msgEl, ev, text) {
    const box = h("div", { class: "question" }, h("p", {}, ev.message));
    for (const o of ev.options) {
      box.append(h("button", { class: "q-option", disabled: !!o.disabled, onclick: () => {
        hwSel.value = o.value;
        box.replaceChildren(h("p", { class: "hint", style: "margin:0" }, `${o.label}.`));
        generate(text, true);
      } }, h("strong", {}, o.label), h("span", {}, o.detail)));
    }
    msgEl.querySelector(".body").replaceWith(box);
  }

  async function generate(resend = null, isResend = false) {
    const text = isResend ? resend : prompt.value.trim();
    if (!text || s.running) return;
    const [connection, model] = (picker.value || "").split("::");
    if (!connection) { toast("Add an AI connection and pick a model in Settings first.", "bad"); return; }
    if (!isResend) addUser(text);
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
        body: JSON.stringify({ prompt: text, design_id: s.designId, code: s.code && s.code !== s.savedCode ? s.code : null, connection, model, hardware_mode: hwSel.value }),
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
          else if (ev.type === "tool") trace.append(h("div", {}, ev.name === "submit_design" ? "Saving design" : ev.name === "list_parts" ? "Looking up parts" : "Checking design"));
          else if (ev.type === "question") askHardware(msgEl, ev, text);
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
            if (ev.hardware_mode && ev.hardware_mode !== "ask") hwSel.value = ev.hardware_mode;
            await reloadDesign(false);
            renderModel();
            if (ev.bom?.some((p) => p.to_buy)) msgEl.append(h("p", { class: "hint" }, `This design needs parts you don't have. `, h("a", { href: "#", onclick: (e) => { e.preventDefault(); s.tab = "parts"; renderTab(); } }, "See the parts list")));
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
  hwSel.addEventListener("change", () => hwSel.blur());
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
    tags.set(d.tags);
    tags.el.hidden = false;
    s.versions = d.versions;
    s.version = d.current_version;
    const mode = d.versions.at(-1)?.hardware_mode;
    if (mode && mode !== "ask") hwSel.value = mode;
    loadBom();
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

// ================================================================= TAGS
// Editable tag chips. save(tags) persists and returns the stored (normalised) tags.
function tagEditor(save) {
  let tags = [];
  const list = h("span", { class: "tag-list" });
  const input = h("input", { type: "text", class: "tag-input", placeholder: "Add tag", "aria-label": "Add tag", maxlength: 40 });
  const el = h("div", { class: "tag-editor" }, list, input);
  function render() {
    list.innerHTML = "";
    for (const t of tags) {
      list.append(h("span", { class: "tag" }, t,
        h("button", { class: "tag-x", title: `Remove tag ${t}`, "aria-label": `Remove tag ${t}`, onclick: () => commit(tags.filter((x) => x !== t)) }, "×")));
    }
  }
  async function commit(next) {
    const prev = tags;
    tags = next;
    render();
    try { tags = await save(next); render(); }
    catch (err) { tags = prev; render(); toast(err.message, "bad"); }
  }
  function addFromInput() {
    const add = input.value.split(",").map((t) => t.trim().toLowerCase().replace(/\s+/g, " ")).filter(Boolean);
    input.value = "";
    if (add.length) commit([...new Set([...tags, ...add])]);
  }
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addFromInput(); }
    else if (e.key === "Backspace" && !input.value && tags.length) commit(tags.slice(0, -1));
  });
  input.addEventListener("blur", addFromInput);
  return { el, set(next) { tags = [...(next || [])]; render(); } };
}

// ================================================================= CLAUDE CODE SIGN-IN
let xtermLoading = null;
function loadXterm() {
  xtermLoading ||= new Promise((resolve, reject) => {
    document.head.append(h("link", { rel: "stylesheet", href: "/vendor/xterm/xterm.css" }));
    const sc = h("script", { src: "/vendor/xterm/xterm.js" });
    sc.onload = () => resolve(window.Terminal);
    sc.onerror = () => reject(new Error("Could not load the terminal"));
    document.head.append(sc);
  });
  return xtermLoading;
}

// Runs Claude Code's own `claude auth login` (or logout) in a browser terminal.
// Resolves with the connection status reported when it finishes.
async function openClaudeTerminal(cid, action = "login") {
  const Terminal = await loadXterm();
  return new Promise((resolve) => {
    const screen = h("div", { class: "term-screen" });
    const linkRow = h("div", { class: "row term-link", hidden: true });
    const state = h("span", { class: "hint", style: "margin:0" }, "Starting Claude Code");
    const line = h("input", { type: "text", placeholder: "Paste the code here if typing into the terminal does not work", "aria-label": "Send text to Claude Code", autocomplete: "off" });
    const sendBtn = h("button", { class: "btn btn-sm" }, "Send");
    const closeBtn = h("button", { class: "btn" }, "Close");
    const dialog = h("dialog", { class: "pp term", "aria-label": "Sign in with Claude" },
      h("div", { class: "pp-head" }, h("div", {},
        h("h2", {}, action === "login" ? "Sign in with Claude" : "Sign out of Claude"),
        h("p", {}, action === "login"
          ? "This is Claude Code's own sign-in. Open the link it shows, sign in to your Claude account, then paste the code back here."
          : "Claude Code is signing this connection out."))),
      h("div", { class: "term-body" }, linkRow, screen,
        h("p", { class: "hint" }, "Buildplate passes your keystrokes to Claude Code and does not see or store your Claude credentials.")),
      h("div", { class: "pp-foot" }, state, h("span", { class: "spacer" }), line, sendBtn, closeBtn));
    document.body.append(dialog);
    dialog.showModal();
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    const term = new Terminal({
      cols: 100, rows: 22, convertEol: false, cursorBlink: true, fontFamily: getComputedStyle(document.body).getPropertyValue("--mono") || "monospace", fontSize: 13,
      theme: dark ? { background: "#13181E", foreground: "#E6EAF0" } : { background: "#F6F8FA", foreground: "#1C2430", cursor: "#1C2430", selectionBackground: "#B9C2CC" },
    });
    term.open(screen);
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/api/connections/${encodeURIComponent(cid)}/terminal?action=${action}`);
    let result = null;
    let raw = "";
    // Surface Claude Code's sign-in link as a button: long URLs are hard to select in a
    // terminal, especially on a phone. The link opens Anthropic's own sign-in page.
    const findLink = (text) => {
      raw = (raw + text.replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07]*\x07|[\r\n]/g, "")).slice(-8000);
      // only once Claude Code's prompt follows the link, so the URL is complete
      const m = raw.match(/https:\/\/[a-z0-9.-]*claude\.(?:ai|com)\/[^\s"'<>]*oauth[^\s"'<>]*?(?=Paste code)/i);
      if (m && linkRow.hidden) {
        linkRow.append(h("a", { class: "btn btn-primary btn-sm", href: m[0], target: "_blank", rel: "noopener noreferrer" }, "Open the Claude sign-in page"),
          h("span", { class: "hint", style: "margin:0" }, "Then copy the code it shows and paste it below."));
        linkRow.hidden = false;
      }
    };
    const send = (data) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "input", data })); };
    term.onData(send);
    ws.onopen = () => { state.textContent = "Connected to Claude Code"; ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows })); term.focus(); };
    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);
      if (msg.type === "output") { term.write(msg.data); findLink(msg.data); }
      else if (msg.type === "error") { state.textContent = msg.message; state.style.color = "var(--bad)"; }
      else if (msg.type === "exit") {
        result = msg.status || null;
        const ok = result?.state === "connected";
        state.textContent = action === "login" ? (ok ? result.detail || "Signed in" : "Not signed in. You can try again.") : "Signed out.";
        state.style.color = action === "login" ? (ok ? "var(--good)" : "var(--bad)") : "";
        closeBtn.textContent = "Done";
      }
    };
    ws.onclose = (e) => {
      if (e.code === 4403) { state.textContent = "Not allowed. Sign in to Buildplate with your password and try again."; state.style.color = "var(--bad)"; }
      else if (!result) state.textContent = "Claude Code has finished.";
      line.disabled = sendBtn.disabled = true;
    };
    const sendLine = () => { if (line.value) { send(line.value + "\r"); line.value = ""; term.focus(); } };
    sendBtn.addEventListener("click", sendLine);
    line.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendLine(); } });
    const finish = async () => {
      try { ws.close(); } catch (_) {}
      term.dispose(); dialog.remove();
      // closed before Claude Code finished: ask for the status rather than guess
      if (!result) { try { result = (await api(`/api/connections/${cid}/test`, { method: "POST" })).connection.status; } catch (_) {} }
      resolve(result);
    };
    closeBtn.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", finish, { once: true });
  });
}

// Installs the pinned Claude Code release, reporting progress in statusEl.
async function installClaudeCode(statusEl) {
  let info = await api("/api/claude-code/install", { method: "POST" });
  while (info.install.running) {
    const { done, total } = info.install;
    statusEl.textContent = total ? `Installing Claude Code ${info.version}: ${Math.round((done / total) * 100)}%` : `Installing Claude Code ${info.version}`;
    await new Promise((r) => setTimeout(r, 800));
    info = await api("/api/claude-code");
  }
  if (!info.installed) throw new Error(info.install.error || "Claude Code could not be installed.");
  statusEl.textContent = `Claude Code ${info.version} installed`;
  return info;
}

// ================================================================= PARTS
const HARDWARE_MODES = { ask: "Ask me", inventory: "Use parts I have", order: "Buy parts as needed", none: "No hardware" };
const AMAZON_SITES = ["amazon.com", "amazon.ca", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it", "amazon.es", "amazon.nl", "amazon.se", "amazon.pl", "amazon.com.au", "amazon.co.jp", "amazon.in", "amazon.com.mx", "amazon.com.br"];

function buyLinks(p) {
  return h("span", { class: "bom-links" },
    h("a", { href: p.links.bambu, target: "_blank", rel: "noopener noreferrer" }, "Bambu"),
    h("a", { href: p.links.amazon, target: "_blank", rel: "noopener noreferrer sponsored" }, "Amazon"),
    p.links.other ? h("a", { href: p.links.other, target: "_blank", rel: "noopener noreferrer" }, "My link") : null);
}

async function Parts(root) {
  let filter = { q: "", category: "", low: false };
  const page = h("div", { class: "page" });
  const search = h("input", { type: "search", placeholder: "Search your parts", "aria-label": "Search parts" });
  const catSel = h("select", { "aria-label": "Category" });
  const lowOnly = h("input", { type: "checkbox", "aria-label": "Low stock only" });
  const fileIn = h("input", { type: "file", accept: ".csv,text/csv", hidden: true });
  const body = h("div", {});
  page.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Parts"), h("p", {}, "The hardware you have on hand. When a design needs screws, inserts, magnets or bearings, the agent can design around these and tell you what to buy.")),
      h("div", { class: "row" },
        h("button", { class: "btn btn-primary", html: icons.plus + "Add from catalog", onclick: () => openCatalog() }),
        h("button", { class: "btn", onclick: () => editPart(null) }, "Custom part"))),
    h("div", { class: "lib-filters", style: "margin-bottom:16px" }, search, catSel,
      h("label", { class: "switch" }, lowOnly, "Low stock only"),
      h("span", { class: "spacer" }),
      h("button", { class: "btn btn-sm btn-ghost", onclick: () => fileIn.click() }, "Import CSV"),
      h("a", { class: "btn btn-sm btn-ghost", href: "/api/parts/export.csv", html: icons.download + "Export CSV" }), fileIn),
    body);
  root.append(page);
  const catalog = await api("/api/parts/catalog");
  catSel.append(h("option", { value: "" }, "All categories"), ...catalog.categories.map((c) => h("option", { value: c }, c[0].toUpperCase() + c.slice(1))));

  async function load() {
    const { parts } = await api(`/api/parts?q=${encodeURIComponent(filter.q)}&category=${encodeURIComponent(filter.category)}`);
    const rows = filter.low ? parts.filter((p) => p.low) : parts;
    body.innerHTML = "";
    if (!rows.length) {
      body.append(h("div", { class: "empty" }, h("h3", {}, filter.q || filter.category || filter.low ? "No parts match" : "No parts yet"),
        filter.q || filter.category || filter.low ? "Try another search." : h("span", {}, "Add the screws, inserts and magnets you keep, and the agent will design around them. ",
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); openCatalog(); } }, "Add from the catalog"), ".")));
      return;
    }
    const tbody = h("tbody");
    for (const p of rows) {
      const qty = h("input", { type: "number", min: 0, value: p.quantity, class: "qty-input", "aria-label": `Quantity of ${p.name}` });
      qty.addEventListener("change", async () => { try { await api(`/api/parts/${p.id}`, { method: "PATCH", json: { quantity: Number(qty.value || 0) } }); load(); } catch (err) { toast(err.message, "bad"); } });
      const step = (d) => async () => { await api(`/api/parts/${p.id}/adjust`, { method: "POST", json: { delta: d } }); load(); };
      tbody.append(h("tr", { class: p.low ? "low" : "" },
        h("td", {}, h("div", {}, p.name), h("div", { class: "hint", style: "margin:0" }, [p.type, p.material].filter(Boolean).join(", "))),
        h("td", {}, p.category),
        h("td", {}, h("div", { class: "qty" },
          h("button", { class: "btn btn-sm btn-ghost", "aria-label": `One fewer ${p.name}`, onclick: step(-1) }, "\u2212"), qty,
          h("button", { class: "btn btn-sm btn-ghost", "aria-label": `One more ${p.name}`, onclick: step(1) }, "+"),
          p.low ? h("span", { class: "pill warn", title: `At or below ${p.low_stock}` }, "Low") : null)),
        h("td", {}, buyLinks(p)),
        h("td", { class: "r" }, h("button", { class: "btn btn-sm btn-ghost", onclick: () => editPart(p) }, "Edit"))));
    }
    body.append(h("div", { class: "section" }, h("div", { class: "section-body table-scroll" },
      h("table", { class: "data parts-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Part"), h("th", {}, "Category"), h("th", {}, "On hand"), h("th", {}, "Buy"), h("th", {}))), tbody))));
  }

  // Pick parts from the catalog and enter how many you have.
  function openCatalog() {
    let cat = "screw";
    const q = h("input", { type: "search", placeholder: `Search ${catalog.parts.length} parts`, "aria-label": "Search the catalog" });
    const cats = h("div", { class: "pp-vendors", role: "listbox", "aria-label": "Categories" });
    const list = h("div", { class: "catalog-list" });
    const close = h("button", { class: "btn" }, "Done");
    const dialog = h("dialog", { class: "pp", "aria-label": "Add parts from the catalog" },
      h("div", { class: "pp-head" }, h("div", {}, h("h2", {}, "Add parts"), h("p", {}, "Enter how many you have and select Add. Hole sizes for each part are included for the design agent.")), q),
      h("div", { class: "pp-body" }, cats, list),
      h("div", { class: "pp-foot" }, h("span", { class: "hint", style: "margin:0" }, "Missing something? Close this and add a custom part."), h("span", { class: "spacer" }), close));
    document.body.append(dialog);
    const render = () => {
      cats.innerHTML = "";
      for (const c of catalog.categories) {
        cats.append(h("button", { class: `pp-vendor ${c === cat && !q.value ? "on" : ""}`, onclick: () => { cat = c; q.value = ""; render(); } },
          h("span", {}, c[0].toUpperCase() + c.slice(1)), h("span", { class: "spacer" }), h("span", { class: "hint", style: "margin:0" }, catalog.parts.filter((p) => p.category === c).length)));
      }
      const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
      const items = catalog.parts.filter((p) => words.length ? words.every((w) => `${p.name} ${p.size} ${p.type} ${p.category}`.toLowerCase().includes(w)) : p.category === cat);
      list.innerHTML = "";
      for (const p of items.slice(0, 200)) {
        const n = h("input", { type: "number", min: 1, value: p.category === "screw" ? 20 : 10, class: "qty-input", "aria-label": `How many ${p.name}` });
        const add = h("button", { class: "btn btn-sm" }, "Add");
        add.addEventListener("click", async () => {
          try {
            const r = await api("/api/parts", { method: "POST", json: { catalog_key: p.key, quantity: Number(n.value || 0) } });
            add.textContent = `${r.quantity} on hand`; add.disabled = true; load();
          } catch (err) { toast(err.message, "bad"); }
        });
        const holes = Object.entries(p.holes).filter(([k]) => k !== "note").slice(0, 3).map(([k, v]) => `${k.replace(/_/g, " ").replace(/ d$/, " \u2300")} ${v}`).join(" \u00b7 ");
        list.append(h("div", { class: "catalog-row" }, h("div", {}, h("div", {}, p.name), h("div", { class: "hint", style: "margin:0" }, holes)), n, add));
      }
      if (!items.length) list.append(h("p", { class: "hint", style: "padding:16px" }, "No parts match."));
    };
    q.addEventListener("input", debounce(render, 150));
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove());
    render();
    dialog.showModal();
  }

  // Add a custom part, or edit or remove one.
  function editPart(p) {
    const f = {};
    const input = (name, label, attrs = {}) => {
      const el = h("input", { id: `pe-${name}`, value: p?.[name] ?? "", ...attrs });
      f[name] = el;
      return h("div", { class: attrs.wide ? "full" : "" }, h("label", { for: el.id }, label), el);
    };
    const dl = h("datalist", { id: "pe-cats" }, ...catalog.categories.map((c) => h("option", { value: c })));
    const save = h("button", { class: "btn btn-primary", type: "submit", form: "pe-form" }, p ? "Save" : "Add part");
    const del = p ? h("button", { class: "btn btn-ghost btn-danger", type: "button" }, "Delete") : null;
    const form = h("form", { class: "conn-fields", id: "pe-form" },
      input("name", "Name", { type: "text", required: true, maxlength: 200, wide: true }),
      input("category", "Category", { type: "text", list: "pe-cats", placeholder: "screw, magnet, bearing..." }),
      input("size", "Size", { type: "text", placeholder: "M3 x 8, 6 x 3 mm..." }),
      input("type", "Type", { type: "text", placeholder: "socket head, nyloc..." }),
      input("material", "Material", { type: "text" }),
      input("quantity", "On hand", { type: "number", min: 0 }),
      input("low_stock", "Warn when at or below", { type: "number", min: 0, placeholder: "optional" }),
      input("bambu_url", "Bambu Lab Store link", { type: "url", placeholder: "optional: replaces the store search", wide: true }),
      input("amazon_url", "Amazon link", { type: "url", placeholder: "optional: replaces the Amazon search", wide: true }),
      input("other_url", "Other shop link", { type: "url", placeholder: "optional", wide: true }),
      input("notes", "Notes", { type: "text", wide: true }), dl);
    const dialog = h("dialog", { class: "pp part-dialog", "aria-label": p ? `Edit ${p.name}` : "Add a custom part" },
      h("div", { class: "pp-head" }, h("div", {}, h("h2", {}, p ? "Edit part" : "Add a custom part"), p?.catalog_key ? h("p", {}, "From the catalog. Hole sizes for the design agent come with it.") : h("p", {}, "For hardware that is not in the catalog."))),
      h("div", { class: "term-body" }, form),
      h("div", { class: "pp-foot" }, del, h("span", { class: "spacer" }), h("button", { class: "btn", type: "button", onclick: () => dialog.close() }, "Cancel"), save));
    if (!p) f.quantity.value = 0;
    document.body.append(dialog);
    const submit = async (e) => {
      e?.preventDefault();
      const data = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
      data.quantity = Number(data.quantity || 0);
      data.low_stock = data.low_stock === "" ? null : Number(data.low_stock);
      try {
        await api(p ? `/api/parts/${p.id}` : "/api/parts", { method: p ? "PATCH" : "POST", json: data });
        toast(p ? "Part saved" : "Part added"); dialog.close(); load();
      } catch (err) { toast(err.message, "bad"); }
    };
    form.addEventListener("submit", submit);  // Enter in any field, or the Save button
    del?.addEventListener("click", async () => {
      if (!confirm(`Delete ${p.name} from your inventory?`)) return;
      await api(`/api/parts/${p.id}`, { method: "DELETE" }); toast("Part deleted"); dialog.close(); load();
    });
    dialog.addEventListener("close", () => dialog.remove());
    dialog.showModal();
    f.name.focus();
  }

  fileIn.addEventListener("change", async () => {
    const file = fileIn.files[0]; fileIn.value = "";
    if (!file) return;
    try {
      const r = await api("/api/parts/import", { method: "POST", json: { csv: await file.text() } });
      toast(`Imported: ${r.added} added, ${r.updated} updated${r.errors.length ? `, ${r.errors.length} skipped` : ""}`, r.errors.length ? "bad" : "");
      if (r.errors.length) console.warn(r.errors);
      load();
    } catch (err) { toast(err.message, "bad"); }
  });
  search.addEventListener("input", debounce(() => { filter.q = search.value.trim(); load(); }, 200));
  catSel.addEventListener("change", () => { filter.category = catSel.value; load(); });
  lowOnly.addEventListener("change", () => { filter.low = lowOnly.checked; load(); });
  await load();
}

// ================================================================= LIBRARY
const SORT_LABELS = { "": "Best match", updated: "Recently updated", created: "Newest", title: "Title A to Z", largest: "Largest first", smallest: "Smallest first" };
const SOURCE_LABELS = { studio: "Studio", mcp: "MCP", manual: "Manual" };

async function Library(root) {
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem("bp-lib") || "{}"); } catch (_) {}
  const f = { q: "", source: "", model: "", tags: [], minSize: "", maxSize: "", sort: "", ...saved };

  const page = h("div", { class: "page" });
  const search = h("input", { type: "search", class: "lib-search", value: f.q, placeholder: "Search titles, descriptions, tags, prompts and models", "aria-label": "Search designs" });
  const sourceSel = h("select", { "aria-label": "Source" });
  const modelSel = h("select", { "aria-label": "Model" });
  const tagSel = h("select", { "aria-label": "Add tag filter" });
  const minIn = h("input", { type: "number", min: 0, step: 1, placeholder: "Min", value: f.minSize, "aria-label": "Minimum size in mm" });
  const maxIn = h("input", { type: "number", min: 0, step: 1, placeholder: "Max", value: f.maxSize, "aria-label": "Maximum size in mm" });
  const sortSel = h("select", { "aria-label": "Sort" }, ...Object.entries(SORT_LABELS).map(([v, l]) => h("option", { value: v, selected: v === f.sort }, l)));
  const chips = h("div", { class: "lib-chips" });
  const count = h("span", { class: "hint", style: "margin:0" });
  const grid = h("div", { class: "lib-grid" });
  page.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Library"), h("p", {}, "Every design you generate or save, with its full version history.")),
      h("a", { class: "btn btn-primary", href: "#/studio", html: icons.plus + "New design" })),
    h("div", { class: "lib-toolbar" },
      search,
      h("div", { class: "lib-filters" },
        sourceSel, modelSel, tagSel,
        h("span", { class: "lib-size", title: "Largest dimension, in mm" }, h("span", {}, "Size"), minIn, h("span", {}, "to"), maxIn, h("span", {}, "mm")),
        sortSel)),
    h("div", { class: "row lib-status" }, chips, h("span", { class: "spacer" }), count,
      h("details", { class: "lib-help" }, h("summary", {}, "Search tips"),
        h("div", { html: `<p>Words match anywhere: title, description, tags, the prompt, provider, model and printer. Quote a phrase for an exact match.</p>
<p><code>tag:garage</code> <code>model:qwen</code> <code>provider:ollama</code> <code>source:mcp</code> <code>printer:x1c</code></p>
<p><code>size:&lt;100</code> largest side, <code>w:&lt;50</code> <code>d:&gt;20</code> <code>h:20..40</code> width, depth, height in mm</p>
<p><code>after:2026-10-01</code> <code>before:2026-11</code> by date created</p>` }))),
    grid);
  root.append(page);

  function fillFilters(facets) {
    sourceSel.innerHTML = "";
    sourceSel.append(h("option", { value: "" }, "Any source"));
    for (const { source, count: n } of facets.sources) sourceSel.append(h("option", { value: source, selected: source === f.source }, `${SOURCE_LABELS[source] || source} (${n})`));
    modelSel.innerHTML = "";
    modelSel.append(h("option", { value: "" }, "Any model"));
    const byProvider = {};
    for (const m of facets.models) (byProvider[m.provider] ||= []).push(m);
    for (const [prov, models] of Object.entries(byProvider)) {
      const label = providerName(prov) || "External";
      const group = h("optgroup", { label });
      const all = JSON.stringify([prov, ""]);
      if (prov) group.append(h("option", { value: all, selected: all === f.model }, `All ${label}`));
      for (const m of models) {
        const v = JSON.stringify([prov, m.model]);
        group.append(h("option", { value: v, selected: v === f.model }, `${m.model} (${m.count})`));
      }
      modelSel.append(group);
    }
    tagSel.innerHTML = "";
    tagSel.append(h("option", { value: "" }, facets.tags.length ? "Filter by tag" : "No tags yet"));
    for (const { tag, count: n } of facets.tags) if (!f.tags.includes(tag)) tagSel.append(h("option", { value: tag }, `${tag} (${n})`));
    tagSel.disabled = !facets.tags.length;
  }

  function renderChips() {
    chips.innerHTML = "";
    const chip = (label, clear) => chips.append(h("span", { class: "tag tag-filter" }, label,
      h("button", { class: "tag-x", title: `Remove filter ${label}`, "aria-label": `Remove filter ${label}`, onclick: () => { clear(); sync(); } }, "×")));
    if (f.source) chip(`Source: ${SOURCE_LABELS[f.source] || f.source}`, () => (f.source = ""));
    if (f.model) {
      const [prov, model] = JSON.parse(f.model);
      chip(model ? `Model: ${model}` : `Provider: ${providerName(prov)}`, () => (f.model = ""));
    }
    for (const t of f.tags) chip(`Tag: ${t}`, () => (f.tags = f.tags.filter((x) => x !== t)));
    if (f.minSize !== "" || f.maxSize !== "") {
      const label = f.minSize !== "" && f.maxSize !== "" ? `${f.minSize} to ${f.maxSize} mm` : f.minSize !== "" ? `at least ${f.minSize} mm` : `up to ${f.maxSize} mm`;
      chip(`Size: ${label}`, () => { f.minSize = ""; f.maxSize = ""; });
    }
    if (chips.childElementCount > 1) chips.append(h("button", { class: "btn btn-ghost btn-sm", onclick: () => { Object.assign(f, { source: "", model: "", tags: [], minSize: "", maxSize: "" }); sync(); } }, "Clear filters"));
  }

  // pull control values into f, refresh controls and results
  function sync() {
    sourceSel.value = f.source;
    modelSel.value = f.model;
    minIn.value = f.minSize;
    maxIn.value = f.maxSize;
    try { sessionStorage.setItem("bp-lib", JSON.stringify(f)); } catch (_) {}
    renderChips();
    loadFacets();
    load();
  }

  async function loadFacets() {
    try { fillFilters(await api("/api/designs/facets")); } catch (_) {}
  }

  let seq = 0;
  async function load() {
    const my = ++seq;
    const p = new URLSearchParams({ q: f.q });
    if (f.sort) p.set("sort", f.sort);
    if (f.source) p.set("source", f.source);
    if (f.model) {
      const [prov, model] = JSON.parse(f.model);
      if (prov) p.set("provider", prov);
      if (model) p.set("model", model);
    }
    if (f.tags.length) p.set("tags", f.tags.join(","));
    if (f.minSize !== "") p.set("min_size", f.minSize);
    if (f.maxSize !== "") p.set("max_size", f.maxSize);
    let designs;
    try {
      ({ designs } = await api(`/api/designs?${p}`));
    } catch (err) {
      if (my !== seq) return;
      grid.innerHTML = "";
      count.textContent = "";
      grid.append(h("div", { class: "empty", style: "grid-column:1/-1" }, h("h3", {}, "Check the search"), err.message));
      return;
    }
    if (my !== seq) return;
    const filtered = f.q.trim() || chips.childElementCount;
    count.textContent = filtered ? `${designs.length} match${designs.length === 1 ? "" : "es"}` : `${designs.length} design${designs.length === 1 ? "" : "s"}`;
    grid.innerHTML = "";
    if (!designs.length) {
      grid.append(h("div", { class: "empty", style: "grid-column:1/-1" }, h("h3", {}, filtered ? "No designs match" : "Your library is empty"), filtered ? "Try fewer words or remove a filter." : h("span", {}, "Designs land here after you generate them in ", h("a", { href: "#/studio" }, "Studio"), ".")));
      return;
    }
    for (const d of designs) {
      const st = d.stats || {};
      const by = madeBy(d);
      const others = (d.models || []).filter((m) => !(m.provider === d.provider && m.model === d.model));
      const card = h("a", { class: "lib-card", href: `#/studio/${d.id}` },
        h("div", { class: "lib-thumb" }, h("img", { src: `/api/designs/${d.id}/download?format=png&version=${d.current_version}`, alt: "", loading: "lazy" })),
        h("div", { class: "lib-info" },
          h("h3", {}, d.title),
          h("div", { class: "meta" }, `v${d.current_version}, ${ago(d.updated_at)}`),
          h("div", { class: "made-by" },
            h("span", { class: `pill made-by-pill ${by.kind}`, title: by.title }, h("span", { class: "made-by-label" }, by.label)),
            others.length ? h("span", { class: "pill", title: `Earlier versions also used:\n${others.map((m) => `${providerName(m.provider) || "External"} \u00b7 ${m.model}`).join("\n")}` }, `+${others.length}`) : null),
          d.tags?.length ? h("div", { class: "tag-list" }, ...d.tags.map((t) => h("button", { class: `tag tag-link${f.tags.includes(t) ? " on" : ""}`, title: `Show designs tagged ${t}`, onclick: (e) => {
            e.preventDefault();
            if (!f.tags.includes(t)) { f.tags.push(t); sync(); }
          } }, t))) : null,
          h("div", { class: "row" },
            st.size_mm ? h("span", { class: "pill tabular" }, `${st.size_mm.map((n) => Math.round(n)).join(" x ")} mm`) : null,
            d.hardware ? h("span", { class: `pill ${d.hardware.to_buy ? "warn" : ""}`, title: `${d.hardware.parts} hardware parts${d.hardware.to_buy ? `; ${d.hardware.to_buy} kind${d.hardware.to_buy === 1 ? "" : "s"} to buy` : ", all on hand"}` },
              `${d.hardware.parts} part${d.hardware.parts === 1 ? "" : "s"}${d.hardware.to_buy ? `, ${d.hardware.to_buy} to buy` : ""}`) : null,
            h("span", { class: "spacer" }),
            h("button", { class: "btn btn-ghost btn-sm btn-danger", title: "Delete design", onclick: async (e) => {
              e.preventDefault();
              if (!confirm(`Delete "${d.title}" and all its versions?`)) return;
              await api(`/api/designs/${d.id}`, { method: "DELETE" });
              toast("Design deleted");
              sync();
            } }, "Delete"))));
      grid.append(card);
    }
  }

  search.addEventListener("input", debounce(() => { f.q = search.value; sync(); }, 250));
  sourceSel.addEventListener("change", () => { f.source = sourceSel.value; sync(); });
  modelSel.addEventListener("change", () => { f.model = modelSel.value; sync(); });
  tagSel.addEventListener("change", () => { if (tagSel.value && !f.tags.includes(tagSel.value)) f.tags.push(tagSel.value); sync(); });
  const sizeChange = debounce(() => { f.minSize = minIn.value; f.maxSize = maxIn.value; sync(); }, 400);
  minIn.addEventListener("input", sizeChange);
  maxIn.addEventListener("input", sizeChange);
  sortSel.addEventListener("change", () => { f.sort = sortSel.value; sync(); });

  await loadFacets();
  renderChips();
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
  const planSec = h("div", { class: "section", hidden: true });
  const chartSec = h("div", { class: "section" });
  const modelSec = h("div", { class: "section" });
  const runsSec = h("div", { class: "section" });
  page.append(kpis, planSec, chartSec, modelSec, runsSec);
  root.append(page);
  let days = 30;

  async function load() {
    const sum = await api(`/api/usage/summary?days=${days}`);
    const t = sum.totals;
    kpis.innerHTML = "";
    const kpi = (k, v, sub) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), sub ? h("div", { class: "sub" }, sub) : null);
    kpis.append(...[
      kpi("Requests", fmtInt(t.runs), `avg ${fmtTokens(Math.round(t.avg_tokens))} tokens each`),
      kpi("Input tokens", fmtTokens(t.input_tokens), t.cache_read_tokens ? `${fmtTokens(t.cache_read_tokens)} from cache` : "sent to the model"),
      kpi("Output tokens", fmtTokens(t.output_tokens), "generated by the model"),
      kpi("Pay-per-use cost", fmtCost(t.cost_usd), "estimated at list prices"),
      t.plan_runs ? kpi("Claude plan usage", `~${fmtCost(t.plan_est_cost_usd)}`,
        `${fmtInt(t.plan_runs)} request${t.plan_runs === 1 ? "" : "s"} included in your plan, valued at API prices${t.plan_unpriced_runs ? ` (${fmtInt(t.plan_unpriced_runs)} without a price)` : ""}`) : null].filter(Boolean));
    loadPlanUsage();

    chartSec.innerHTML = "";
    chartSec.append(h("div", { class: "section-head" }, h("h2", {}, "Tokens per day")),
      h("div", { class: "section-body" }, sum.daily.length ? chart(sum.daily, days) : h("p", { class: "hint" }, "No requests in this period."),
        h("div", { class: "legend", style: "margin-top:8px" }, h("span", {}, h("i", { style: "background:var(--accent)" }), "Input"), h("span", {}, h("i", { style: "background:var(--part)" }), "Output"))));

    modelSec.innerHTML = "";
    const mt = h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", {}, "Provider"), h("th", { class: "r" }, "Requests"), h("th", { class: "r" }, "Input"), h("th", { class: "r" }, "Output"), h("th", { class: "r" }, "Cost"))),
      h("tbody", {}, sum.by_model.map((m) => h("tr", {}, h("td", {}, m.model), h("td", {}, providerLabel(m.provider)), h("td", { class: "r" }, fmtInt(m.runs)), h("td", { class: "r" }, fmtInt(m.input_tokens)), h("td", { class: "r" }, fmtInt(m.output_tokens)), h("td", { class: "r" }, costView(m.billing, m.cost_usd, true, m.est_cost_usd))))));
    modelSec.append(h("div", { class: "section-head" }, h("h2", {}, "By model")), h("div", { class: "section-body table-scroll" }, sum.by_model.length ? mt : h("p", { class: "hint" }, "No requests in this period.")));
    await loadRuns();
  }

  // Usage meters for each signed-in Claude plan, from Claude Code's own /usage report.
  async function loadPlanUsage(refresh = false) {
    const conns = (app.settings?.connections || []).filter((c) => c.method === "anthropic.claude_code" && c.status?.state === "connected");
    if (!conns.length) { planSec.hidden = true; return; }
    planSec.hidden = false;
    const body = h("div", { class: "section-body plan-usage" }, h("p", { class: "hint" }, "Asking Claude Code"));
    const refreshBtn = h("button", { class: "btn btn-sm btn-ghost", onclick: () => loadPlanUsage(true) }, "Refresh");
    planSec.innerHTML = "";
    planSec.append(h("div", { class: "section-head row" }, h("div", {}, h("h2", {}, "Claude plan usage"),
      h("p", {}, "How much of each plan's limits are used, as Claude reports it. Requests from Buildplate share these limits with Claude on the web and in Claude Code.")),
      h("span", { class: "spacer" }), refreshBtn), body);
    body.innerHTML = "";
    for (const c of conns) {
      const box = h("div", { class: "plan-box" }, h("h3", {}, c.label), h("p", { class: "hint" }, "Loading"));
      body.append(box);
      try {
        const u = await api(`/api/connections/${c.id}/plan-usage${refresh ? "?refresh=1" : ""}`);
        box.lastChild.remove();
        box.append(planMeters(u));
      } catch (err) { box.lastChild.textContent = err.message; box.lastChild.style.color = "var(--bad)"; }
    }
  }

  function planMeters(u) {
    const wrap = h("div", { class: "plan-meters" });
    const rows = u.report?.rate_limits?.limits;
    const label = (row) => {
      const scope = row.scope?.model?.display_name || row.scope?.surface?.display_name;
      const base = { session: "Current session", weekly_all: "Weekly, all models", weekly_scoped: "Weekly" }[row.kind] || row.kind.replace(/_/g, " ");
      return scope ? `${base} (${scope})` : base;
    };
    if (Array.isArray(rows) && rows.length) {
      for (const row of rows) {
        const pct = Math.max(0, Math.min(100, Number(row.percent) || 0));
        const resets = row.resets_at ? new Date(row.resets_at).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
        wrap.append(h("div", { class: `plan-meter ${row.severity || "normal"}` },
          h("div", { class: "row" }, h("span", {}, label(row)), h("span", { class: "spacer" }), h("span", { class: "tabular" }, `${Math.round(pct)}% used`)),
          h("div", { class: "meter", role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(pct), "aria-label": label(row) }, h("i", { style: `width:${pct}%` })),
          resets ? h("p", { class: "hint" }, `Resets ${resets}`) : null));
      }
      const extra = u.report.rate_limits.extra_usage;
      if (extra?.is_enabled && extra.used_credits != null) {
        const cur = (extra.currency || "USD").toUpperCase();
        const money = (minor) => new Intl.NumberFormat([], { style: "currency", currency: cur }).format(minor / 100);
        wrap.append(h("p", { class: "hint" }, `Usage credits this month: ${money(extra.used_credits)}${extra.monthly_limit != null ? ` of ${money(extra.monthly_limit)}` : ""}`));
      }
    } else if (u.text) {
      wrap.append(h("pre", { class: "mono plan-text" }, u.text));
    } else {
      wrap.append(h("p", { class: "hint" }, "Claude did not report plan limits for this account."));
    }
    const lim = u.plan_limit;
    if (lim?.status === "rejected") wrap.append(h("div", { class: "issue" }, "The last request hit this plan's limit."));
    else if (lim?.status === "allowed_warning") wrap.append(h("div", { class: "issue" }, "Close to this plan's limit."));
    wrap.append(h("p", { class: "hint" }, `Checked ${ago(u.fetched_at)}`));
    return wrap;
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
        h("td", { class: "r" }, costView(r.billing, r.cost_usd, !!r.cost_known, r.est_cost_usd)),
        h("td", {}, r.design_id ? h("a", { href: `#/studio/${r.design_id}`, onclick: (e) => e.stopPropagation() }, "Open") : ""));
      const toggle = async () => {
        if (tr.nextSibling?.classList?.contains("steps")) { tr.nextSibling.remove(); return; }
        const d = await api(`/api/usage/runs/${r.id}`);
        const inner = h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, "Step"), h("th", {}, "Tools called"), h("th", { class: "r" }, "Input"), h("th", { class: "r" }, "Output"), h("th", { class: "r" }, "Cached"), h("th", { class: "r" }, "Cost"), h("th", { class: "r" }, "Time"))),
          h("tbody", {}, d.steps.map((st) => h("tr", {}, h("td", {}, st.step), h("td", {}, st.tool_calls.join(", ") || "none"), h("td", { class: "r" }, fmtInt(st.input_tokens)), h("td", { class: "r" }, fmtInt(st.output_tokens)), h("td", { class: "r" }, fmtInt(st.cache_read_tokens)), h("td", { class: "r" }, costView(r.billing, st.cost_usd, true, st.est_cost_usd)), h("td", { class: "r" }, `${(st.duration_ms / 1000).toFixed(1)} s`)))));
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
async function Settings(root, [flag] = []) {
  // returning from a sign-in on another site
  const oauthMsg = { "oauth-ok": ["Signed in. The connection is ready.", ""], "oauth-failed": ["The sign-in did not finish. Try again from the connection.", "bad"], "oauth-cancelled": ["Sign-in cancelled.", ""] }[flag];
  if (oauthMsg) { history.replaceState(null, "", "#/settings"); setTimeout(() => toast(...oauthMsg), 300); }
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
    ...[["connections", "AI connections"], ["agent", "Design agent"], ["printer", "Printers"], ["render", "Rendering"], ["access", "Access and MCP"], ["pricing", "Pricing"], ["parts", "Parts and shopping"], ["about", "About"]].map(([id, l]) => h("a", { href: `#/settings`, onclick: (e) => { e.preventDefault(); $(`#sec-${id}`).scrollIntoView({ behavior: "smooth", block: "start" }); } }, l)));
  const body = h("div", {});
  page.append(h("div", { class: "settings-layout" }, toc, body));
  root.append(page);

  const section = (id, title, desc, ...content) => h("section", { class: "section", id: `sec-${id}` }, h("div", { class: "section-head" }, h("h2", {}, title), desc ? h("p", {}, desc) : null), h("div", { class: "section-body" }, ...content));

  // ---- AI connections (saved individually, since connecting needs a live test)
  const connWrap = h("div", {});
  const STATE = { connected: ["good", "Connected"], untested: ["", "Not tested"], error: ["bad", "Error"], needs_sign_in: ["warn", "Needs sign-in"], expired: ["warn", "Sign-in expired"], not_installed: ["warn", "Not installed"] };
  let methods = null;
  let connCtx = { passwordSession: false, passwordReason: "", claudeCode: { installed: false } };

  // Inputs for a method's fields. Secrets are never sent back, so a saved one shows
  // as a hint and is only replaced when something is typed.
  function methodForm(method, conn) {
    const inputs = {};
    const clear = new Set();
    const rows = method.fields.filter((f) => f.type !== "hidden").map((f) => {
      const id = `cf-${f.name}-${++uid}`;
      const saved = conn?.secrets?.[f.name];
      const value = f.secret ? "" : (conn?.fields?.[f.name] ?? f.default ?? "");
      const placeholder = saved?.set ? (f.type === "json" ? "Saved. Paste or load a new key to replace it." : `Saved ${saved.hint}. Enter a new one to replace it.`) : f.placeholder || "";
      let input;
      if (f.type === "select") {
        input = h("select", { id }, ...f.options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)));
      } else if (f.type === "json") {
        input = h("textarea", { id, rows: 5, class: "mono", spellcheck: "false", autocomplete: "off", placeholder: placeholder || "{ ... }" });
      } else {
        input = h("input", { id, autocomplete: "off", value, placeholder, type: f.type === "password" ? "password" : f.type === "url" ? "url" : "text" });
      }
      inputs[f.name] = input;
      const extra = [];
      if (f.type === "json") {
        const file = h("input", { type: "file", accept: ".json,application/json", hidden: true });
        file.addEventListener("change", async () => { if (file.files[0]) input.value = await file.files[0].text(); file.value = ""; });
        extra.push(file, h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => file.click() }, "Load key file"));
      }
      if (f.help && f.help.startsWith("http")) extra.push(h("a", { href: f.help, target: "_blank", rel: "noopener", class: "hint", style: "margin:0" }, "Get a key"));
      else if (f.help) extra.push(h("p", { class: "hint" }, f.help));
      if (saved?.set && !f.required) {
        const btn = h("button", { class: "btn btn-ghost btn-sm", type: "button" }, "Clear saved value");
        btn.addEventListener("click", () => { clear.add(f.name); input.value = ""; input.placeholder = "Will be cleared when you save"; });
        extra.push(btn);
      }
      return h("div", { class: f.type === "url" || f.type === "json" ? "full" : "" }, h("label", { for: id }, f.label), input, ...extra);
    });
    return {
      el: h("div", { class: "conn-fields" }, ...rows),
      read() {
        const out = {};
        for (const f of method.fields) {
          if (!inputs[f.name]) continue;
          const v = inputs[f.name].value.trim();
          if (!f.secret) out[f.name] = v;
          else if (v) out[f.name] = v;
          else if (clear.has(f.name)) out[f.name] = "";
        }
        return out;
      },
    };
  }

  async function afterConnectionChange() {
    await loadSettings();
    Object.assign(draft.agent, app.settings.agent);
    await renderConnections();
    renderAgent();
  }

  async function testConnection(c, statusEl) {
    statusEl.textContent = "Connecting"; statusEl.style.color = "";
    try {
      const r = await api(`/api/connections/${c.id}/test`, { method: "POST" });
      toast(`${c.label} connected: ${r.count} models`);
    } catch (err) { toast(err.message, "bad"); }
    await afterConnectionChange();
  }

  function connCard(c, editable, active) {
    const method = methods.find((m) => m.id === c.method);
    const [tone, stateLabel] = STATE[c.status?.state] || ["", c.status?.state || ""];
    const isClaude = method?.kind === "cli_session";
    const statusEl = h("span", { class: "hint", style: `margin:0${c.status?.state === "error" ? ";color:var(--bad)" : ""}` },
      c.status?.state === "error" || isClaude ? c.status?.detail || "" : c.models?.length ? `${c.models.length} model${c.models.length === 1 ? "" : "s"}` : c.status?.detail || "");
    const enabled = h("input", { type: "checkbox", checked: c.enabled, disabled: !editable, "aria-label": `Enable ${c.label}` });
    enabled.addEventListener("change", async () => {
      try { await api(`/api/connections/${c.id}`, { method: "PATCH", json: { enabled: enabled.checked } }); }
      catch (err) { toast(err.message, "bad"); }
      await afterConnectionChange();
    });
    const editArea = h("div", { hidden: true });
    const card = h("div", { class: `provider ${c.enabled ? "on" : ""}` },
      h("div", { class: "provider-head" },
        h("label", { class: "switch" }, enabled),
        h("div", { class: "conn-title" }, h("h3", {}, c.label), h("span", { class: "hint", style: "margin:0" }, c.method_label)),
        h("span", { class: `pill ${tone}` }, stateLabel),
        h("span", { class: "pill" }, BILLING_LABELS[c.billing] || c.billing),
        c.id === active ? h("span", { class: "pill accent" }, "Default") : null,
        h("span", { class: "spacer" }),
        editable ? h("button", { class: "btn btn-sm", onclick: () => testConnection(c, statusEl) }, "Test") : null,
        editable ? h("button", { class: "btn btn-sm btn-ghost", onclick: () => { editArea.hidden = !editArea.hidden; } }, "Edit") : null),
      h("div", { class: "conn-status" }, statusEl),
      isClaude && editable ? claudeActions(c, statusEl) : null,
      method?.oauth && editable ? h("div", { class: "row conn-actions" }, h("button", { class: `btn btn-sm ${c.status?.state === "connected" ? "" : "btn-primary"}`, onclick: () => startOAuth(c.id) },
        c.status?.state === "connected" ? "Sign in again" : "Sign in with OpenRouter")) : null,
      editArea);
    if (editable && method) {
      const labelIn = h("input", { type: "text", value: c.label, id: `cl-${c.id}`, maxlength: 60 });
      const form = methodForm(method, c);
      const save = h("button", { class: "btn btn-primary btn-sm" }, "Save and test");
      const remove = h("button", { class: "btn btn-ghost btn-sm btn-danger" }, "Remove connection");
      save.addEventListener("click", async () => {
        save.disabled = true;
        try {
          await api(`/api/connections/${c.id}`, { method: "PATCH", json: { label: labelIn.value, values: form.read() } });
          await testConnection(c, statusEl);
        } catch (err) { toast(err.message, "bad"); save.disabled = false; }
      });
      remove.addEventListener("click", async () => {
        if (!confirm(`Remove the connection "${c.label}" and its saved credentials?`)) return;
        try { await api(`/api/connections/${c.id}`, { method: "DELETE" }); toast("Connection removed"); }
        catch (err) { toast(err.message, "bad"); }
        await afterConnectionChange();
      });
      editArea.append(h("div", { class: "conn-fields" }, h("div", {}, h("label", { for: labelIn.id }, "Name"), labelIn)), form.el,
        h("div", { class: "row", style: "margin-top:10px" }, save, h("span", { class: "spacer" }), remove));
    }
    return card;
  }

  // Install, sign-in and sign-out controls for a Claude plan connection.
  function claudeActions(c, statusEl) {
    const row = h("div", { class: "row conn-actions" });
    if (!connCtx.passwordSession) {
      row.append(h("span", { class: "hint", style: "margin:0" }, connCtx.passwordReason));
      return row;
    }
    if (!connCtx.claudeCode.installed) {
      const btn = h("button", { class: "btn btn-sm btn-primary" }, "Install Claude Code");
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        try { await installClaudeCode(statusEl); await afterConnectionChange(); }
        catch (err) { statusEl.textContent = err.message; statusEl.style.color = "var(--bad)"; btn.disabled = false; }
      });
      row.append(btn, h("span", { class: "hint", style: "margin:0" }, `Downloads Claude Code ${connCtx.claudeCode.version} from Anthropic and checks it before first use.`));
      return row;
    }
    if (c.status?.state === "connected") {
      const out = h("button", { class: "btn btn-sm" }, "Sign out");
      out.addEventListener("click", async () => {
        if (!confirm(`Sign "${c.label}" out of Claude?`)) return;
        try { await api(`/api/connections/${c.id}/sign-out`, { method: "POST" }); toast("Signed out of Claude"); }
        catch (err) { toast(err.message, "bad"); }
        await afterConnectionChange();
      });
      row.append(out);
    } else {
      const sign = h("button", { class: "btn btn-sm btn-primary" }, "Sign in with Claude");
      sign.addEventListener("click", async () => {
        const st = await openClaudeTerminal(c.id, "login");
        if (st?.state === "connected") toast(`${c.label} is signed in`);
        await afterConnectionChange();
      });
      row.append(sign);
    }
    return row;
  }

  // Send the browser to the provider's sign-in; it comes back to Settings.
  async function startOAuth(cid) {
    try { location.href = (await api(`/api/connections/${cid}/oauth/start`, { method: "POST" })).url; }
    catch (err) { toast(err.message, "bad"); }
  }

  function addConnectionForm() {
    const wrap = h("div", { class: "conn-add" });
    const methodSel = h("select", { id: "conn-method", "aria-label": "Connection type" }, h("option", { value: "" }, "Choose a provider and method"));
    const byFamily = {};
    for (const m of methods) (byFamily[m.family_label] ||= []).push(m);
    for (const [fam, list] of Object.entries(byFamily)) methodSel.append(h("optgroup", { label: fam }, ...list.map((m) => h("option", { value: m.id }, m.label))));
    const detail = h("div", {});
    methodSel.addEventListener("change", () => {
      detail.innerHTML = "";
      const m = methods.find((x) => x.id === methodSel.value);
      if (!m) return;
      const labelIn = h("input", { type: "text", id: "conn-new-label", value: m.family_label, maxlength: 60 });
      const form = methodForm(m, null);
      const go = h("button", { class: "btn btn-primary", disabled: m.requires_password && !connCtx.passwordSession }, m.kind === "cli_session" ? "Add and sign in" : m.oauth ? "Sign in with OpenRouter" : "Connect");
      const status = h("span", { class: "hint", style: "margin:0" }, m.requires_password && !connCtx.passwordSession ? connCtx.passwordReason : "");
      go.addEventListener("click", async () => {
        go.disabled = true; status.textContent = "Saving"; status.style.color = "";
        let created;
        try {
          created = await api("/api/connections", { method: "POST", json: { method: m.id, label: labelIn.value, values: form.read() } });
        } catch (err) { status.textContent = err.message; status.style.color = "var(--bad)"; go.disabled = false; return; }
        if (m.oauth) { await startOAuth(created.id); return; }
        if (m.kind === "cli_session") {
          try {
            if (!connCtx.claudeCode.installed) await installClaudeCode(status);
            const st = await openClaudeTerminal(created.id, "login");
            if (st?.state === "connected") toast(`${created.label} is signed in`);
          } catch (err) { toast(`Added, but ${err.message}`, "bad"); }
          await afterConnectionChange();
          return;
        }
        status.textContent = "Testing";
        try {
          const r = await api(`/api/connections/${created.id}/test`, { method: "POST" });
          toast(`${created.label} connected: ${r.count} models`);
        } catch (err) { toast(`Saved, but the test failed: ${err.message}`, "bad"); }
        await afterConnectionChange();
      });
      detail.append(...[
        h("p", { class: "hint" }, m.description, " ", h("span", { class: "pill" }, m.billing_label)),
        m.compliance ? h("p", { class: "hint" }, m.compliance) : null,
        m.probe ? h("p", { class: "hint" }, "Connecting sends one tiny test request (a few tokens) to check the credentials and a model work.") : null,
        h("div", { class: "conn-fields" }, h("div", {}, h("label", { for: labelIn.id }, "Name"), labelIn)),
        form.el,
        h("div", { class: "row", style: "margin-top:10px" }, go, status)].filter(Boolean));
    });
    wrap.append(h("h3", {}, "Add a connection"), h("div", { class: "conn-fields" }, h("div", { class: "full" }, h("label", { for: "conn-method" }, "Provider"), methodSel)), detail);
    return wrap;
  }

  async function renderConnections() {
    methods ||= (await api("/api/auth-methods")).methods;
    const listing = await api("/api/connections");
    const { connections: conns, editable, reason, active } = listing;
    connCtx = { passwordSession: listing.password_session, passwordReason: listing.password_reason, claudeCode: listing.claude_code };
    connWrap.innerHTML = "";
    if (!editable) {
      connWrap.append(h("div", { class: "issue" }, h("span", {}, reason, " ",
        h("a", { href: "#/settings", onclick: (e) => { e.preventDefault(); $("#sec-access").scrollIntoView({ behavior: "smooth", block: "start" }); } }, "Go to Access and MCP"))));
    }
    if (!conns.length) connWrap.append(h("div", { class: "empty", style: "padding:24px" }, h("h3", {}, "No AI connections yet"), editable ? "Add one below to start designing." : "Set a password to add one."));
    for (const c of conns) connWrap.append(connCard(c, editable, active));
    if (editable) connWrap.append(addConnectionForm());
  }

  // ---- agent
  const agentWrap = h("div", {});
  function renderAgent() {
    agentWrap.innerHTML = "";
    const sel = modelSelect({ id: "agent-model" });
    sel.addEventListener("change", () => { const [c, m] = sel.value.split("::"); draft.agent.connection = c; draft.agent.model = m; markDirty(); });
    const custom = h("input", { type: "text", id: "agent-custom", placeholder: "or type a model ID" });
    custom.addEventListener("input", () => { if (custom.value.trim()) { const [c] = (sel.value || "::").split("::"); draft.agent.connection = c || draft.agent.connection; draft.agent.model = custom.value.trim(); markDirty(); } });
    agentWrap.append(
      h("div", { class: "grid-2" }, field("Default model", sel, "Used for new requests. You can override it per request in Studio."), field("Custom model ID", custom, "For a model that is not listed, on the connection selected to the left.")),
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
    // A real form: Enter submits, and password managers recognise the current and new
    // password (the hidden username field tells them which account this is).
    const pwForm = h("form", { class: "pw-form", autocomplete: "on" },
      h("input", { type: "text", name: "username", autocomplete: "username", value: "buildplate", hidden: true, readonly: true }),
      h("div", { class: "pw-row" },
        sec.password_enabled ? field("Current password", cur) : null,
        field(sec.password_enabled ? "New password" : "Password", nw),
        h("div", { class: "pw-action" }, h("button", { class: "btn", type: "submit" }, sec.password_enabled ? "Change password" : "Set password"))),
      h("p", { class: "hint" }, sec.password_enabled
        ? "At least 8 characters. To remove the password, leave New password empty and select Change password."
        : "At least 8 characters."));
    pwForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (sec.password_enabled && !nw.value && !confirm("Remove the password? Anyone who can reach this address will be able to use Buildplate.")) return;
      try {
        const r = await api("/api/settings/password", { method: "POST", json: { current: cur.value, new: nw.value } });
        toast(r.password_enabled ? "Password saved" : "Password removed");
        await loadSettings(); renderAccess();
        $("#logout").hidden = !r.password_enabled;
      } catch (err) { toast(err.message, "bad"); }
    });
    const mcpUrl = `${location.origin}/mcp`;
    const reveal = h("div", {});
    accessWrap.append(
      h("h3", { class: "h3" }, "Web interface password"),
      h("p", { class: "hint", style: "margin:0 0 10px" }, sec.password_enabled ? "A password is required to open Buildplate." : "Anyone who can reach this address can use Buildplate and your API keys. Set a password unless it sits behind an authenticating proxy."),
      pwForm,
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
    if (rows.length) priceWrap.append(h("table", { class: "data", style: "margin-bottom:16px" }, h("thead", {}, h("tr", {}, h("th", {}, "Model"), h("th", { class: "r" }, "Input per 1M"), h("th", { class: "r" }, "Output per 1M"), h("th", {}))), tbody));
    priceWrap.append(
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
    section("connections", "AI connections", "Add a connection for each way you reach a model: an API key, a local server, or several keys for one provider. Credentials are encrypted on disk and never sent back to the browser.", connWrap),
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
    section("parts", "Parts and shopping", "How the design agent handles hardware, and where buy links for parts point.",
      h("div", { class: "grid-2" },
        field("Hardware by default", bind("agent.hardware_mode", "select", { options: Object.entries(HARDWARE_MODES) }), "Ask me shows a choice when a request needs screws, inserts, magnets or other hardware. You can change it per request in Studio."),
        field("Bambu Lab Store", bind("parts.store_region", "select", { options: [["us", "United States"], ["ca", "Canada"], ["eu", "Europe"], ["uk", "United Kingdom"], ["au", "Australia"], ["asia", "Asia"]] }), "The regional store buy links search."),
        field("Amazon site", bind("parts.amazon_domain", "select", { options: AMAZON_SITES.map((d) => [d, d]) })),
        field("Amazon associate tag", bind("parts.amazon_tag", "text", { placeholder: "optional, e.g. yourtag-20", maxlength: 40 }), "Added to Amazon links if you have an Amazon Associates account. Leave empty for plain links."))),
    section("about", "About", null,
      h("div", { class: "about" },
        h("div", { class: "about-text" },
          h("p", {}, "Buildplate is an open-source project by Mike Roughton (",
            h("a", { href: "https://about.mroughton.com", target: "_blank", rel: "noopener noreferrer" }, "about.mroughton.com"),
            "), released under the MIT license. If you like this project and it saves you time at the printer, please consider supporting its development with a coffee."),
          h("div", { class: "row" },
            h("a", { class: "btn btn-sm", href: "https://github.com/rough-ton/buildplate", target: "_blank", rel: "noopener noreferrer" }, "Source on GitHub"),
            h("a", { class: "btn btn-sm btn-ghost", href: "https://github.com/rough-ton/buildplate/issues/new", target: "_blank", rel: "noopener noreferrer" }, "Report a problem"))),
        h("a", { class: "bmc-btn", href: "https://www.buymeacoffee.com/mikeroughton", target: "_blank", rel: "noopener noreferrer" },
          h("span", { class: "bmc-icon", html: '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="#coffee"/></svg>' }), h("span", {}, "Buy me a coffee"))),
      h("h3", { class: "about-sub" }, "System"),
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

  await renderConnections(); renderAgent(); renderAccess(); renderPricing(); renderPrinters();
  if (location.hash.endsWith("#printers") || sessionStorage.getItem("bp-jump") === "printer") {
    sessionStorage.removeItem("bp-jump");
    requestAnimationFrame(() => $("#sec-printer")?.scrollIntoView({ block: "start" }));
  }

  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const { providers, connections: _c, security, printer, ...rest } = draft;
      rest.security = { session_hours: draft.security.session_hours };
      await api("/api/settings", { method: "PATCH", json: rest });
      // Overrides are replaced as a whole so removals stick.
      await loadSettings();
      dirty = false;
      saveState.textContent = "Saved. Changes are live.";
      toast("Settings saved");
      Object.assign(draft, structuredClone(app.settings));
      editId = draft.printers.some((x) => x.id === editId) ? editId : draft.active_printer;
      renderAgent(); renderAccess(); renderPrinters();
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
  ["providers", "AI connections"],
  ["using-studio", "Using Studio"],
  ["parts", "Parts inventory"],
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
