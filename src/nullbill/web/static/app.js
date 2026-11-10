"use strict";

// NullBill's page. A sidebar of pages (a tab bar on a phone), the run in
// progress in the top bar, and the agent in a panel beside every page (its
// own tab on a phone). Every control answers beside itself: busy while the
// server works, then done or the reason it was refused. The agent proposes;
// the page decides by the owner's access mode and runs what is approved.

const VIEWS = ["overview", "services", "backups", "activity", "alerts", "setup", "access", "agent", "updates"];
const ALIASES = {home: "overview", agents: "agent", "": ""};
const STEPS = ["hardware", "choices", "plan", "build"];
const AGENT_TABS = ["chat", "settings", "log"];
const ACCESS_TABS = ["tailscale", "people", "lan", "trust", "ssh"];
const PLATFORMS = ["android", "windows", "apple"];
// Runs that open their page when they start; the others show in the top
// bar while the owner stays where they pressed.
const OPENS_RUN = new Set(["apply", "verify", "upgrade", "remove-guests"]);
const SETUP_RUNS = new Set(["apply", "remove-guests", "rebake-ci"]);
const SERVICE_ICONS = {
  proxmox: "proxmox", grafana: "chart", forgejo: "git", files: "folder", jellyfin: "film",
  workspace: "terminal", operator: "bot", "windows-dev": "window", "linux-runner": "cog", "model-gateway": "chip",
};
const JOB_NAMES = {
  probe: "Hardware read", apply: "Setup", verify: "Checks", upgrade: "NullBill update",
  "close-lan": "Closing the home network", "keep-lan-closed": "Keeping the home network closed",
  "open-lan": "Reopening the home network", "rebake-ci": "CI image rebuild", "remove-guests": "Setup with deletions",
};
// A run of one setup stage (the agent's, or `apply --stage`) is not "Setup":
// it built one thing and checked nothing else.
function jobName(v) { return v.only ? "Setup stage" : JOB_NAMES[v.action] || (v.progress && v.progress.title) || v.action; }
const STARTING = {
  probe: "Reading…", apply: "Starting setup…", verify: "Starting the checks…", upgrade: "Starting the update…",
  "close-lan": "Closing…", "keep-lan-closed": "Keeping it closed…", "open-lan": "Opening…", "remove-guests": "Starting setup…",
};
const MODE_NAMES = {manual: "Manual", auto: "Auto", yolo: "YOLO"};
const MODE_DESC = {
  manual: "Asks before every change.",
  auto: "Changes go ahead; deleting, rebooting and scripts wait.",
  yolo: "Nothing waits. Everything is logged.",
};
// Past this much quiet the page calls a run stuck and points at Stop.
const STUCK_S = 30 * 60;

const phone = matchMedia("(max-width: 759px)");
const narrow = matchMedia("(max-width: 1099px)");

const $ = (id) => document.getElementById(id);
const make = (tag, props, ...children) => {
  const node = Object.assign(document.createElement(tag), props || {});
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
};
const li = (text, cls) => make("li", {textContent: text, className: cls || ""});
function icon(name, cls) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `icon ${cls || ""}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
// A status: a dot and a word, never the colour alone.
function status(kind, text, sub) {
  return make("span", {className: `status ${kind || "muted"}`}, make("span", {className: `dot ${kind || ""}`, ariaHidden: "true"}),
    make("span", {className: "text", textContent: text}), sub ? make("span", {className: "meta", textContent: sub}) : null);
}

// Per-browser conveniences only; the page works without them.
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* private mode */ } },
};

let state = null;          // the last /api/state
let savedChoices = null;   // the choices form as last saved
let optionsView = null;    // what the choices come to: state.options, or the server's answer for the unsaved form
let previewTimer = null;
let previewSeq = 0;
let statusTimer = null;
let lastStatus = null;     // the last /api/status
let pathView = null;       // the last /api/path: how devices reach the server, read on the host just now
let demand = null;         // the last /api/on-demand: what runs on demand, live from the host
let job = null;            // the followed run: {id, view, node, offset, timer, watched, feedback}
let currentView = null;
let currentSub = "";
let currentStep = null;
let agentTab = null;
let installPrompt = null;  // the browser's install offer, once it makes one
let trusted = null;        // whether this browser trusts the server's certificate: checkTrust's finding, null before it or where it cannot tell
const expanded = new Set(); // services whose details are open

// Words.
function sentence(text) {
  const t = String(text || "").trim();
  if (!t) return t;
  const s = /^[a-z]+[\s,:;]/.test(t) ? t[0].toUpperCase() + t.slice(1) : t;
  return /[.!?]$/.test(s) ? s : s + ".";
}
function gib(mib) { return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${mib} MiB`; }
function andList(names) { return names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names.join(""); }
function ago(seconds) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function duration(s) {
  if (s === null || s === undefined) return "";
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${s % 60} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}
function clock(t) { return new Date(t * 1000).toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}); }
function day(t) { return new Date(t * 1000).toLocaleDateString([], {month: "short", day: "numeric"}); }
// When something was read: to the second today, with the day before that.
function readAt(t) {
  const d = new Date(t * 1000);
  return d.toDateString() === new Date().toDateString() ? d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : clock(t);
}
function count(n, word) { return `${n} ${word}${n === 1 ? "" : "s"}`; }

function announce(text, urgent) {
  const region = $(urgent ? "live-urgent" : "live");
  region.textContent = "";
  setTimeout(() => { region.textContent = text; }, 60);
}

// What the server refused, one message per cause (.messages); .message is
// all of them in a line, for the places that have only a line.
class ApiError extends Error {
  constructor(message, status, messages) {
    super(message);
    this.status = status;
    this.messages = messages && messages.length ? messages : [message];
  }
}

// The digest of the page's files this script came with; every answer names
// the server's. They differ after Update NullBill restarted the server under
// an open page, whose script cannot read the new server's answers: the page
// reloads itself, once per version, unless that would lose what the owner is
// in the middle of. Then it says so, and reloads when asked or when it can.
const SHELL = (document.querySelector('meta[name="nb-shell"]') || {}).content || "";
const RELOADED = "nb-reloaded";

function midWork() {
  const active = document.activeElement;
  const typing = active && active.matches("input:not([type=checkbox]):not([type=radio])") && active.value;
  const draft = [...document.querySelectorAll("textarea")].some((t) => t.value.trim());
  return Boolean(choicesDirty() || document.querySelector("dialog[open]") || typing || draft);
}

// The reload must come from the server: the service worker keeps the page's
// files for a slow line, and on one it answered a reload with the old ones.
async function reloadFresh() {
  try {
    for (const key of await caches.keys()) await caches.delete(key);
  } catch { /* no cache to empty */ }
  location.reload();
}

// True when the page is on its way out and the answer need not be read.
function outdated(shell) {
  let tried = shell;
  try { tried = sessionStorage.getItem(RELOADED); } catch { /* no storage: never reload unasked */ }
  if (tried !== shell && !midWork()) {
    try {
      sessionStorage.setItem(RELOADED, shell);
      reloadFresh();
      return true;
    } catch { /* say so instead */ }
  }
  show("outdated", true);
  return false;
}

// After such a reload: say once that this is the new version.
function greetUpdated() {
  try {
    if (!SHELL || sessionStorage.getItem(RELOADED) !== SHELL || sessionStorage.getItem("nb-greeted") === SHELL) return;
    sessionStorage.setItem("nb-greeted", SHELL);
  } catch { return; }
  toast("NullBill was updated, and this page reloaded with the new version.", currentView === "updates" ? null : {text: "What changed", href: "#updates"}, 20);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-NullBill": "1"},
    body: JSON.stringify(body),
  };
  let response;
  try {
    response = await fetch(path, {credentials: "same-origin", cache: "no-store", ...options});
  } catch {
    connection(false);
    throw new ApiError("The page cannot reach the NullBill server. While NullBill updates it is away for about a minute; then try again.", 0);
  }
  connection(true);
  const shell = response.headers.get("X-NullBill-Shell");
  if (SHELL && shell && shell !== SHELL && outdated(shell)) return new Promise(() => {});
  let data = {};
  try { data = await response.json(); } catch { /* not JSON */ }
  if (response.status === 401 && path !== "/api/login") {
    signedOut();
    throw new ApiError("You were signed out. Sign in again; nothing you saved is lost.", 401);
  }
  if (!response.ok) {
    // Several reasons come as a list too (.list as sent, .messages as
    // sentences, .headline what did not happen), for a place with room.
    const list = Array.isArray(data.errors) && data.errors.length > 1 ? data.errors : null;
    const error = new ApiError(sentence(data.error || `The server answered ${response.status} ${response.statusText}`), response.status, list && list.map(sentence));
    error.review = Boolean(data.review);
    error.list = list;
    error.headline = data.headline || "";
    throw error;
  }
  return data;
}

// Said only when it is lost: a connected page needs no word for it.
function connection(ok) {
  show("conn", !ok);
}

// A line at the bottom until it is closed, or for `seconds` when it only
// tells good news.
let toastTimer = null;
function toast(text, link, seconds) {
  clearTimeout(toastTimer);
  $("toast-text").textContent = text;
  $("toast").hidden = !text;
  show("toast-link", Boolean(text && link));
  if (link) Object.assign($("toast-link"), {textContent: link.text, href: link.href});
  if (text && seconds) toastTimer = setTimeout(() => toast(""), seconds * 1000);
}

// The line next to a control: busy, ok, error or info. Several things to
// say (an array) become a short list, one each, never one run-on line,
// under `head` (what did not happen) when the server sent one.
function say(id, kind, text, head) {
  const node = typeof id === "string" ? $(id) : id;
  if (!node) return;
  const lines = (Array.isArray(text) ? text : [text]).filter(Boolean);
  node.className = `${node.className.split(" ").filter((c) => !["ok", "error", "info", "busy", "several"].includes(c)).join(" ")} ${kind || ""}${lines.length > 1 ? " several" : ""}`.trim();
  node.replaceChildren();
  if (!lines.length) return;
  const mark = {busy: make("span", {className: "spin", ariaHidden: "true"}), ok: icon("check"), error: icon("alert")}[kind];
  const body = lines.length > 1
    ? make("span", {className: "say-body"}, make("span", {textContent: `${head ? head.replace(/[.:]+$/, "") : `${lines.length} things ${kind === "error" ? "are in the way" : "to know"}`}:`}), make("ul", {className: "say-list"}, ...lines.map((l) => li(l))))
    : make("span", {textContent: lines[0]});
  node.append(...[mark, body].filter(Boolean));
}

// One press: the button waits, the line beside it says what happens. work()
// returns the success text. The button keeps focus (aria-disabled).
async function press(button, feedback, busyText, work, field) {
  if (button.classList.contains("busy")) return;
  if (button.getAttribute("aria-disabled") === "true") {
    say(feedback, "info", button.dataset.why || "Wait until the running job finishes.");
    return;
  }
  button.classList.add("busy");
  button.setAttribute("aria-busy", "true");
  say(feedback, "busy", busyText);
  if (field) field.removeAttribute("aria-invalid");
  try {
    const text = await work();
    say(feedback, text ? "ok" : "", text);
  } catch (error) {
    // Several reasons: a list under what did not happen, unless a block of
    // its own took them (showProblem).
    if (error.listed) say(feedback, "", "");
    else say(feedback, "error", error.list ? error.messages : error.message, error.headline);
    if (field && error.status !== 0 && error.status !== 401) {
      field.setAttribute("aria-invalid", "true");
      field.focus();
    }
  } finally {
    button.classList.remove("busy");
    button.removeAttribute("aria-busy");
    gateJobButtons();
  }
}

// A refusal with several reasons, in a block of its own: what did not
// happen, then each reason on its line. True when it took the error, which
// then needs no line beside the button; one reason stays such a line.
function showProblem(id, error) {
  const node = $(id);
  const list = (error && error.list) || null;
  node.hidden = !list;
  node.replaceChildren();
  if (!list) return false;
  node.append(make("h3", {textContent: sentence(error.headline)}), make("ul", {}, ...list.map((line) => make("li", {textContent: sentence(line)}))));
  error.listed = true;
  setTimeout(() => node.scrollIntoView({block: "nearest"}), 0);
  return true;
}

function show(id, visible) { const node = typeof id === "string" ? $(id) : id; if (node) node.hidden = !visible; }
function setCount(node, text, kind, label) {
  if (!node) return;
  node.hidden = !text;
  node.textContent = text || "";
  node.className = `count ${kind || ""}`.trim();
  if (label) node.setAttribute("aria-label", label); else node.removeAttribute("aria-label");
}
function setNav(view, text, kind, label) {
  setCount(document.querySelector(`#sidebar a[data-view="${view}"] .count`), text, kind, label);
}

// A small, safe Markdown: paragraphs, lists, code, bold and links. Built
// from nodes, never from HTML, since the text comes from the model.
function inline(text) {
  const nodes = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|\[([^\]]+)\]\((https?:\/\/[^)\s]+|#[\w/-]*)\)|(\*[^*\s](?:[^*]*[^*\s])?\*)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[1]) nodes.push(make("code", {textContent: m[1].slice(1, -1)}));
    else if (m[2]) nodes.push(make("b", {textContent: m[2].slice(2, -2)}));
    else if (m[5]) nodes.push(make("em", {textContent: m[5].slice(1, -1)}));
    else {
      const a = make("a", {href: m[4], textContent: m[3]});
      if (!m[4].startsWith("#")) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
      nodes.push(a);
    }
    last = re.lastIndex;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

// A table the model wrote with pipes: a real one, which scrolls sideways
// on its own instead of wrapping into a wall of bars.
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}
function tableView(head, rows) {
  const table = make("table", {},
    make("thead", {}, make("tr", {}, ...head.map((c) => make("th", {scope: "col"}, ...inline(c))))),
    make("tbody", {}, ...rows.map((r) => make("tr", {}, ...head.map((_, n) => make("td", {}, ...inline(r[n] || "")))))));
  return make("div", {className: "md-table", tabIndex: 0}, table);
}

function markdown(text) {
  const out = [];
  const lines = String(text || "").split("\n");
  const BLOCK = /^```|^\s*[-*] |^\s*\d+[.)] |^#{1,6} |^\s*\|.*\|\s*$/;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i += 1;
      out.push(outputBox(make("pre", {}, make("code", {textContent: buf.join("\n")}))));
    } else if (/^\s*[-*] /.test(line) || /^\s*\d+[.)] /.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const pattern = ordered ? /^\s*\d+[.)] / : /^\s*[-*] /;
      const list = make(ordered ? "ol" : "ul");
      while (i < lines.length && pattern.test(lines[i])) list.append(make("li", {}, ...inline(lines[i++].replace(pattern, ""))));
      out.push(list);
    } else if (/^#{1,6} /.test(line)) {
      out.push(make("p", {}, make("b", {}, ...inline(line.replace(/^#+ /, "")))));
      i += 1;
    } else if (/^\s*\|.*\|\s*$/.test(line) && TABLE_RULE.test(lines[i + 1] || "")) {
      const head = tableCells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(tableCells(lines[i++]));
      out.push(tableView(head, rows));
    } else if (/^\s*([-*_]\s*){3,}$/.test(line)) {
      i += 1; // a rule between sections: the gap between paragraphs says as much
    } else if (!line.trim()) {
      i += 1;
    } else {
      const p = make("p");
      let first = true;
      while (i < lines.length && lines[i].trim() && !BLOCK.test(lines[i])) {
        if (!first) p.append(make("br"));
        p.append(...inline(lines[i++]));
        first = false;
      }
      out.push(p);
    }
  }
  return out;
}

// Dialogs.
function confirmDialog({title, text, points, yes, word, danger = true}) {
  const dialog = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-points").replaceChildren(...(points || []).map((t) => li(t)));
  show("confirm-points", Boolean(points && points.length));
  const yesButton = $("confirm-yes");
  yesButton.textContent = yes;
  yesButton.className = `btn ${danger ? "danger" : "primary"}`;
  const field = $("confirm-word");
  show("confirm-word-field", Boolean(word));
  field.value = "";
  field.removeAttribute("aria-invalid");
  $("confirm-word-label").textContent = word ? `Type ${word} to go ahead` : "";
  return new Promise((resolve) => {
    const done = (ok) => {
      dialog.removeEventListener("close", onClose);
      $("confirm-no").onclick = null;
      dialog.querySelector("form").onsubmit = null;
      if (dialog.open) dialog.close();
      resolve(ok);
    };
    const onClose = () => done(false);
    dialog.addEventListener("close", onClose);
    $("confirm-no").onclick = () => done(false);
    dialog.querySelector("form").onsubmit = (event) => {
      event.preventDefault();
      if (word && field.value.trim().toLowerCase() !== word.toLowerCase()) {
        field.setAttribute("aria-invalid", "true");
        field.focus();
        return;
      }
      done(true);
    };
    dialog.showModal();
    (word ? field : $("confirm-no")).focus();
  });
}

// Menus: a list under the button that opened it, arrow keys inside, Escape
// or a click elsewhere closes it and focus goes back to the button.
const menu = {button: null};
function openMenu(button, items, align) {
  const box = $("menu");
  if (menu.button === button && !box.hidden) { closeMenu(true); return; }
  closeMenu(false);
  box.replaceChildren();
  for (const item of items) {
    if (item.sep) { box.append(make("div", {className: "menu-sep", role: "separator"})); continue; }
    if (item.heading) { box.append(make("div", {className: "menu-label", textContent: item.heading})); continue; }
    const radio = "checked" in item;
    const node = make("button", {type: "button", tabIndex: -1},
      radio ? make("span", {className: "check"}, item.checked ? icon("check") : null) : item.icon ? icon(item.icon) : null,
      make("span", {}, make("span", {textContent: item.label}), item.desc ? make("span", {className: "menu-desc", textContent: item.desc}) : null));
    node.setAttribute("role", radio ? "menuitemradio" : "menuitem");
    if (radio) node.setAttribute("aria-checked", item.checked ? "true" : "false");
    node.addEventListener("click", () => { closeMenu(true); item.run(); });
    box.append(node);
  }
  box.setAttribute("aria-label", button.getAttribute("aria-label") || button.textContent.trim() || button.title);
  box.hidden = false;
  menu.button = button;
  button.setAttribute("aria-expanded", "true");
  const r = button.getBoundingClientRect();
  const w = box.offsetWidth;
  const left = align === "left" ? r.left : r.right - w;
  box.style.left = `${Math.max(8, Math.min(left, innerWidth - w - 8))}px`;
  const below = r.bottom + 4;
  box.style.top = below + box.offsetHeight > innerHeight - 8 ? `${Math.max(8, r.top - box.offsetHeight - 4)}px` : `${below}px`;
  const first = box.querySelector('[aria-checked="true"]') || box.querySelector("[role^=menuitem]");
  if (first) first.focus();
}
function closeMenu(focusBack) {
  const box = $("menu");
  if (box.hidden) return;
  box.hidden = true;
  if (menu.button) {
    menu.button.setAttribute("aria-expanded", "false");
    if (focusBack) menu.button.focus();
  }
  menu.button = null;
}

// Pages.
function built() { return Boolean(state && state.built); }
function defaultView() { return "overview"; }

function stepDone(step) {
  if (!state) return false;
  if (step === "hardware") return state.probed;
  if (step === "choices") return Boolean(state.choices.owner_login) && !choicesDirty();
  if (step === "plan") return Boolean(state.plan.text) && !state.plan.errors.length;
  return built() && !state.config_changed;
}
function defaultStep() { return STEPS.find((s) => !stepDone(s)) || "build"; }

function parseHash() {
  const [raw, ...rest] = location.hash.slice(1).split("/");
  const sub = rest.join("/");
  const name = raw in ALIASES ? ALIASES[raw] : raw;
  if (raw && !VIEWS.includes(name) && currentView) return [currentView, currentSub];
  // The agent's sign-in was once a setup step.
  if (name === "setup" && sub === "agent") return ["agent", "settings"];
  return [VIEWS.includes(name) ? name : defaultView(), raw === "agents" ? "settings" : sub || ""];
}

function route(focus) {
  if (!state) return;
  const [opened, word] = parseHash();
  if (opened === "alerts" && word.startsWith("heard/")) {
    history.replaceState(null, "", "#alerts");
    confirmHeard(word.slice("heard/".length));
  }
  const [view, sub] = parseHash();
  closeMenu(false);
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== view;
  for (const link of document.querySelectorAll("#sidebar .nav a, .tabbar a, #more a")) {
    if (link.dataset.view === view || link.getAttribute("href") === `#${view}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const inMore = ["setup", "access", "alerts", "updates", "backups"].includes(view);
  $("tab-more").classList.toggle("current", inMore);
  const changed = currentView !== view || currentSub !== sub;
  const viewChanged = currentView !== view;
  currentView = view;
  currentSub = sub;
  let title = $(`view-${view}`).dataset.title;
  let heading = $(`h-${view}`);
  if (view === "setup") {
    const step = STEPS.includes(sub) ? sub : defaultStep();
    const stepChanged = step !== currentStep;
    showStep(step);
    if (stepChanged || changed) heading = $(`h-step-${step}`);
    const stepLink = document.querySelector(`#stepper a[data-step="${step}"]`);
    if (stepLink) title = `${stepLink.textContent.replace(/,.*$/s, "").trim()} · Setup`;
  } else if (view === "agent") {
    showAgentTab(sub);
    // Arrow keys in its tab list move between tabs: focus stays on the tab.
    if (!viewChanged) heading = null;
    title = `${$(`tab-${sub || "settings"}`) ? $(`tab-${sub || "settings"}`).textContent : "Agent"} · Agent`;
  } else if (view === "access") {
    showAccessTab(sub);
    if (!viewChanged) heading = null;
    title = `${$(`tab-${sub || "tailscale"}`) ? $(`tab-${sub || "tailscale"}`).textContent : "Access"} · Access`;
  } else if (view === "activity") {
    const detail = Boolean(sub);
    show("activity-list", !detail);
    show("activity-detail", detail);
    if (detail) {
      openRun(sub, focus);
      heading = null;
      title = `${job && job.id === sub ? jobName(job.view) : "Run"} · Activity`;
    }
  }
  if (view === "services" && viewChanged) loadDemand().catch(() => {});
  if (view === "backups" && viewChanged) loadBackups().catch(() => {});
  if (view === "updates" && viewChanged) say("update-result", "", "");
  placeChat();
  placeJob();
  setSide(false);
  if (narrow.matches && !phone.matches && dockOpen() && changed) setDock(false);
  if (changed) $("main").scrollTo(0, 0);
  document.title = `${title} · NullBill`;
  if (focus && heading) {
    heading.tabIndex = -1;
    heading.focus({preventScroll: true});
  }
  paintStates();
  paintBanner();
}

function go(hash) {
  if (location.hash === `#${hash}`) route(true);
  else location.hash = hash;
}

function showStep(step) {
  currentStep = step;
  for (const s of STEPS) show(`step-${s}`, s === step);
  for (const a of document.querySelectorAll("#stepper a")) {
    if (a.dataset.step === step) a.setAttribute("aria-current", "step");
    else a.removeAttribute("aria-current");
  }
}

function selectTab(listId, tab) {
  for (const button of $(listId).querySelectorAll("[role=tab]")) {
    const on = button.dataset.tab === tab;
    button.setAttribute("aria-selected", on ? "true" : "false");
    button.tabIndex = on ? 0 : -1;
  }
}

function showAgentTab(tab) {
  const tabs = phone.matches ? AGENT_TABS : AGENT_TABS.filter((t) => t !== "chat");
  if (!phone.matches && tab === "chat") { openAgent(true); tab = "settings"; }
  agentTab = tabs.includes(tab) ? tab : tabs[0];
  show("tab-chat", phone.matches);
  selectTab("agent-tabs", agentTab);
  const slot = $("agent-chat-slot");
  slot.hidden = agentTab !== "chat";
  slot.classList.toggle("active", agentTab === "chat");
  $("view-agent").classList.toggle("chat-tab", agentTab === "chat");
  show("agent-settings", agentTab === "settings");
  show("agent-log", agentTab === "log");
  if (agentTab === "log") loadAgentLog();
  if (agentTab === "chat") { markSeen(); requestAnimationFrame(scrollChatToNews); }
}

// The newest waiting request, or else the end of the conversation.
function scrollChatToNews() {
  const card = latestRequest();
  if (card) card.scrollIntoView({block: "center"});
  else $("chat-log").scrollTop = $("chat-log").scrollHeight;
}

let sshDirty = false; // the owner is editing the SSH keys field

function renderSshKeys(st) {
  const keys = (st.choices && st.choices.ssh_keys) || [];
  const n = keys.length;
  $("ssh-state").replaceChildren(!n ? status("", "No keys yet")
    : st.config_changed ? status("warn", `${n} saved: run setup`) : status("good", `${n} key${n === 1 ? "" : "s"}`));
  if (!sshDirty) $("ssh-keys").value = keys.join("\n");
}

function showAccessTab(tab) {
  const fallback = state && state.lan.state === "closing" ? "lan" : "tailscale";
  const chosen = ACCESS_TABS.includes(tab) ? tab : fallback;
  selectTab("access-tabs", chosen);
  for (const t of ACCESS_TABS) show(`sec-${t}`, t === chosen);
  // Five tabs are wider than a small phone: the chosen one scrolls into sight.
  if (phone.matches) $(`tab-${chosen}`).scrollIntoView({block: "nearest", inline: "nearest"});
  if (chosen === "people") loadPeople(false).catch((error) => say("people-result", "error", error.message));
  if (chosen === "tailscale") loadPath().catch(() => {});
}

function showPlatform(name) {
  selectTab("platform-tabs", name);
  for (const p of PLATFORMS) show(`howto-${p}`, p === name);
}
function guessPlatform() {
  const ua = navigator.userAgent;
  return /Android/i.test(ua) ? "android" : /iPhone|iPad|Macintosh.*Mobile/i.test(ua) ? "apple" : /Windows/i.test(ua) ? "windows" : "android";
}

// Arrow keys move along a tab list, as in every other tab list.
function wireTabs(listId, onPick) {
  const list = $(listId);
  list.addEventListener("click", (event) => {
    const button = event.target.closest("[role=tab]");
    if (button) onPick(button.dataset.tab);
  });
  list.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...list.querySelectorAll("[role=tab]")].filter((t) => !t.hidden);
    const at = tabs.indexOf(document.activeElement);
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (at + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    tabs[next].focus();
    tabs[next].click();
  });
}

// The sidebar slides in below laptop width.
function setSide(open) {
  if (!narrow.matches || phone.matches) open = false;
  $("app").classList.toggle("side-open", open);
  $("open-side").setAttribute("aria-expanded", open ? "true" : "false");
  $("main").inert = open;
  if (open) {
    if (dockOpen()) setDock(false);
    const current = $("sidebar").querySelector('[aria-current="page"]') || $("sidebar").querySelector("a");
    current.focus();
  }
}
function sideOpen() { return $("app").classList.contains("side-open"); }

// The agent's place: a panel on a wide screen, over the page on a narrower
// one, the Agent tab on a phone.
function dockOpen() { return !$("app").classList.contains("dock-closed"); }
function setDock(open, remember) {
  $("app").classList.toggle("dock-closed", !open);
  // Over the page: keep focus inside the panel.
  const overlay = open && narrow.matches && !phone.matches;
  for (const id of ["sidebar", "main", "banner"]) { const node = $(id); if (node) node.inert = overlay; }
  $("toggle-dock").setAttribute("aria-expanded", open && !phone.matches ? "true" : "false");
  if (remember && !narrow.matches) store.set("nb-dock", open ? null : "closed");
  if (open) markSeen();
  paintChatBadge();
  if (state) requestAnimationFrame(() => paintBanner());
}
function placeChat() {
  const chatNode = $("chat");
  const target = phone.matches ? $("agent-chat-slot") : $("dock");
  if (chatNode.parentElement !== target) target.append(chatNode);
}
function chatVisible() {
  if (document.hidden) return false;
  if (phone.matches) return currentView === "agent" && agentTab === "chat";
  return dockOpen();
}
function openAgent(focusComposer) {
  if (phone.matches) {
    if (!(currentView === "agent" && agentTab === "chat")) go("agent/chat");
  } else {
    setDock(true, !narrow.matches);
  }
  if (focusComposer) {
    setTimeout(() => {
      const target = !$("chat-signin").hidden ? $("chat-credential") : $("composer-text");
      target.focus();
    }, 30);
  }
}

function signedOut() {
  clearInterval(statusTimer);
  stopFollowing();
  stopChat();
  job = null;
  state = null;
  show("app", false);
  show("gate", true);
  show("password", false);
  show("login", true);
  $("login-hint").textContent = "You were signed out, for example because NullBill was updated. Sign in again; nothing you saved is lost.";
  $("secret").focus();
}

// Setup: workloads, extras, graphics cards and disks.
function option(input, title, desc, why, cls) {
  return make("label", {className: `option ${cls || ""}`.trim()}, input,
    make("span", {className: "option-text"},
      make("span", {className: "option-title", textContent: title}),
      desc ? make("span", {className: "option-desc", textContent: desc}) : null,
      why ? make("span", {className: "option-why", textContent: why}) : null));
}

// What to run: a row per choice. The server says what each makes, takes
// and deletes (web/workloads.py), for the saved choices in /api/state and
// for the form as it stands through /api/choices/preview, so the meter and
// each row follow every tick before anything is saved.
function optionRow(item) {
  const id = `opt-${item.name}`;
  const input = make("input", {type: "checkbox", id, value: item.name});
  input.setAttribute("aria-describedby", `${id}-desc ${id}-state ${id}-note`);
  const memory = make("span", {className: "opt-num opt-memory"});
  const disk = make("span", {className: "opt-num opt-disk"});
  memory.dataset.label = "Memory";
  disk.dataset.label = "Disk";
  const row = make("div", {className: "opt"},
    make("label", {className: "opt-main"}, input, make("span", {className: "opt-text"},
      make("span", {className: "opt-title", textContent: item.title}),
      make("span", {className: "opt-desc", id: `${id}-desc`, textContent: item.what}))),
    memory,
    disk,
    make("span", {className: "opt-state", id: `${id}-state`}),
    make("p", {className: "opt-note", id: `${id}-note`, hidden: true}),
    item.game ? gameAsks(item) : item.torrents ? torrentAsks(item) : null,
    make("p", {className: "opt-change", role: "status", hidden: true}),
    make("details", {className: "opt-details"}, make("summary", {textContent: "What it makes, needs and deletes"}), make("dl", {className: "opt-facts"}),
      item.own ? ownTools(item) : null,
      item.game && item.game.more ? gameTools(item) : null));
  row.dataset.name = item.name;
  row.dataset.find = `${item.title} ${item.name} ${item.what}`.toLowerCase();
  return row;
}

// A service of the owner's own (web/workloads.py's `own`): its setup
// script to read, and Remove once it is off and its guest is gone.
function ownTools(item) {
  const said = make("p", {className: "feedback"});
  said.setAttribute("role", "status");
  const scripts = make("div", {className: "own-scripts"});
  const showScript = make("button", {type: "button", className: "btn small"}, "Show its setup script");
  showScript.dataset.role = "script";
  showScript.setAttribute("aria-expanded", "false");
  showScript.addEventListener("click", () => {
    if (showScript.getAttribute("aria-expanded") === "true") {
      scripts.replaceChildren();
      showScript.setAttribute("aria-expanded", "false");
      showScript.firstChild.textContent = "Show its setup script";
      return;
    }
    press(showScript, said, "Reading…", async () => {
      const names = (state.options.items.find((i) => i.name === item.name) || item).own.scripts;
      const found = await Promise.all(names.map((name) => api(`/api/own-service?name=${encodeURIComponent(name)}`)));
      scripts.replaceChildren(...found.flatMap((f) => [
        make("p", {className: "own-path mono", textContent: `${f.path}, beside config.yaml on the NullBill container`}),
        make("pre", {className: "own-script", tabIndex: 0, textContent: f.script || ""})]));
      showScript.setAttribute("aria-expanded", "true");
      showScript.firstChild.textContent = "Hide its setup script";
      return "";
    });
  });
  const remove = make("button", {type: "button", className: "btn small danger"}, "Remove", make("span", {className: "sr-only", textContent: ` ${item.title}`}));
  remove.dataset.role = "remove";
  remove.addEventListener("click", async () => {
    const ok = await confirmDialog({title: `Remove ${item.title}?`,
      text: "It leaves What to run. Its guest is gone already; its setup script is set aside in custom/.removed, not erased.", yes: "Remove", danger: true});
    if (!ok) return;
    press(remove, said, "Removing…", async () => {
      await api("/api/own-service/remove", {name: item.name});
      announce(`Removed ${item.title}`, true);
      await refresh();
      return "";
    });
  });
  return make("div", {className: "own-tools"}, make("div", {className: "row"}, showScript, remove, said), scripts);
}

// What the download client asks beside its tick (web/workloads.py's
// `torrents`): the way downloads go, which has no default and is the
// owner's own choice, the VPN's WireGuard file, and where downloads land.
// The file holds a key: it goes up with Save and never comes back.
function torrentAsks(item) {
  const t = item.torrents;
  const id = `opt-${item.name}`;
  const way = (value, title, hint) => {
    const input = make("input", {type: "radio", name: `${id}-route`, value});
    input.dataset.role = "route";
    return make("label", {className: "opt-ask check"}, input,
      make("span", {className: "way"}, make("span", {className: "way-title", textContent: title}), make("span", {className: "hint", textContent: hint})));
  };
  const routes = make("fieldset", {className: "ways"}, make("legend", {className: "label", textContent: "Which way downloads go. Nothing is chosen for you."}),
    way("vpn", "Through a VPN of my own", t.vpn), way("home", "From my home connection, without a VPN", t.home));
  const conf = make("textarea", {id: `${id}-conf`, rows: 6, spellcheck: false, autocomplete: "off", autocapitalize: "off",
    placeholder: "[Interface]\nPrivateKey = …\nAddress = …\nDNS = …\n\n[Peer]\nPublicKey = …\nAllowedIPs = 0.0.0.0/0\nEndpoint = …"});
  conf.dataset.role = "conf";
  conf.setAttribute("aria-describedby", `${id}-conf-hint`);
  const said = make("span", {className: "feedback"});
  said.setAttribute("role", "status");
  const forget = make("button", {type: "button", className: "btn small"}, "Forget the saved file");
  forget.dataset.role = "forget";
  forget.addEventListener("click", async () => {
    const ok = await confirmDialog({title: "Forget the saved WireGuard file?",
      text: "NullBill drops the file and its key. While downloads are set to go through a VPN, setup then waits for a new file.", yes: "Forget it", danger: true});
    if (!ok) return;
    press(forget, said, "Forgetting…", async () => {
      await api("/api/torrents/forget-vpn", {});
      await refresh();
      if (choicesDirty()) previewChoices();
      return "Forgotten.";
    });
  });
  const vpn = make("div", {className: "torrents-vpn", hidden: true},
    make("p", {className: "vpn-state"}),
    make("label", {className: "game-field wide"}, make("span", {className: "label", textContent: "Your VPN's WireGuard file"}), conf,
      make("span", {className: "hint", id: `${id}-conf-hint`,
        textContent: "Your VPN provider's site makes one (a WireGuard configuration, a .conf file): paste all of it. It holds a key, so it is saved with NullBill's secrets when you save, and never shown again."})),
    make("div", {className: "row"}, forget, said));
  const ask = make("input", {type: "checkbox", id: `${id}-port`});
  ask.dataset.role = "port";
  const port = make("div", {className: "torrents-vpn torrents-port", hidden: true},
    make("label", {className: "opt-ask check"}, ask, make("span", {className: "way"},
      make("span", {className: "way-title", textContent: `Ask my home router to open port ${t.port} for it`}),
      make("span", {className: "hint", textContent: `So that others can connect to you, which seeding and private trackers need. The server asks every 15 minutes (UPnP or NAT-PMP), as it does for Tailscale, and only the download client answers on that port. Without the tick, forward TCP and UDP ${t.port} to the server yourself, or leave it shut: downloads work either way. The checks say whether the home router opened it.`}))));
  const field = (label, control, hint) => {
    control.id = `${id}-${control.dataset.role}`;
    const note = make("span", {className: "hint", id: `${control.id}-hint`, textContent: hint});
    control.setAttribute("aria-describedby", note.id);
    return make("label", {className: "game-field"}, make("span", {className: "label", textContent: label}), control, note);
  };
  const share = make("select", {});
  share.dataset.role = "share";
  const folder = make("input", {type: "text", maxLength: 40, autocomplete: "off", spellcheck: false});
  folder.dataset.role = "folder";
  const room = make("input", {type: "number", min: "1", step: "1", inputMode: "numeric"});
  room.dataset.role = "room";
  const where = make("div", {className: "game-fields"},
    field("Kept disk", share, "Downloads go to a kept disk you may change files on. Jellyfin and the file share see them there."),
    field("Folder on it", folder, "Made if it is not there. The client sees this folder and nothing else of the disk."),
    field("Room to keep free, in GiB", room, "Downloads pause while the disk has less free, and go on once it has more."));
  return make("div", {className: "opt-asks", hidden: true},
    make("p", {className: "opt-ask", textContent: t.notice}), routes, vpn, port,
    make("p", {className: "opt-note why no-disk", hidden: true}), where);
}

// The saved choices, into the controls.
function paintTorrents(item) {
  const row = $("pick").querySelector(`.opt[data-name="${item.name}"]`);
  const t = item.torrents;
  for (const input of row.querySelectorAll("[data-role=route]")) input.checked = input.value === t.route;
  row.querySelector("[data-role=conf]").value = "";
  row.querySelector("[data-role=port]").checked = t.port_mapping;
  row.querySelector("[data-role=folder]").value = t.folder;
  row.querySelector("[data-role=room]").value = String(t.keep_free_gib);
  const share = row.querySelector("[data-role=share]");
  share.dataset.want = t.share || "";
  share.replaceChildren();
}

// What follows from the form as it stands (a preview too): the disks that
// can take downloads, and what is known of the VPN.
function paintTorrentsView(item, row) {
  const t = item.torrents;
  const share = row.querySelector("[data-role=share]");
  const want = share.options.length ? share.value : share.dataset.want;
  const names = t.shares.includes(want) || !want ? t.shares : [...t.shares, want];
  if ([...share.options].map((o) => o.value).join("\n") !== names.join("\n")) {
    share.replaceChildren(...names.map((n) => make("option", {value: n, textContent: t.shares.includes(n) ? n : `${n} (cannot be changed now)`})));
    if (names.includes(want)) share.value = want;
  }
  share.disabled = names.length === 0;
  const none = row.querySelector(".no-disk");
  none.hidden = t.shares.length > 0;
  none.textContent = t.read_only.length
    ? `No kept disk can be changed yet: ${andList(t.read_only)} ${t.read_only.length === 1 ? "is" : "are"} read-only. Under Other disks below, tick Let me change files on it.`
    : "No disk is kept yet. Under Other disks below, keep the disk downloads should go to and tick Let me change files on it.";
  const chosen = row.querySelector("[data-role=route]:checked");
  row.querySelector(".torrents-vpn").hidden = !chosen || chosen.value !== "vpn";
  row.querySelector(".torrents-port").hidden = !chosen || chosen.value !== "home";
  const pasted = row.querySelector("[data-role=conf]").value.trim() !== "";
  row.querySelector(".vpn-state").replaceChildren(status(t.endpoint ? "good" : "warn",
    t.endpoint ? `${pasted ? "The file below, once saved" : "A file is saved"}: the firewall lets the client reach its server ${t.endpoint} and nothing else outside.`
      : pasted ? "This file cannot be read yet; saving says what is wrong with it." : "No file saved yet. Downloads wait for it."));
  row.querySelector(".game-field.wide .label").textContent = t.endpoint && !pasted ? "Replace it with another WireGuard file" : "Your VPN's WireGuard file";
  row.querySelector("[data-role=forget]").hidden = !(state.options.items.find((i) => i.name === item.name) || item).torrents.endpoint;
}

// What a game server asks beside its tick (web/workloads.py's `game`): the
// owner's agreement to the game's EULA, who its admins are, and its
// settings.
function gameAsks(item) {
  const asks = make("div", {className: "opt-asks", hidden: true});
  if (item.game.eula) {
    const agree = make("input", {type: "checkbox", id: `opt-${item.name}-eula`});
    agree.dataset.role = "eula";
    agree.dataset.kind = item.game.kind;
    // Agreed once for the game: every server of it shows the same tick.
    agree.addEventListener("change", () => {
      for (const other of $("game-rows").querySelectorAll("input[data-role=eula]")) if (other.dataset.kind === agree.dataset.kind) other.checked = agree.checked;
    });
    asks.append(make("label", {className: "opt-ask check"}, agree, make("span", {}, `${item.game.eula.text} (`,
      make("a", {href: item.game.eula.url, target: "_blank", rel: "noopener noreferrer", textContent: "read it"}), "). Its server does not start without.")));
  }
  if (item.game.admins) {
    const admins = make("input", {type: "text", id: `opt-${item.name}-admins`, autocomplete: "off", spellcheck: false, placeholder: "none"});
    admins.dataset.role = "admins";
    asks.append(make("label", {className: "opt-ask field"}, make("span", {className: "label", textContent: "Admins: player names as shown in the game, separated by commas"}), admins));
  }
  asks.append(gameSettings(item));
  return asks;
}

// A game server's own settings: the ones most people change by name, any
// other of the server's in a box, and its memory. Saved with the choices.
function gameSettings(item) {
  const g = item.game;
  const field = (label, control, hint, cls) => {
    const id = `opt-${item.name}-${control.dataset.key || control.dataset.role}`;
    control.id = id;
    const text = make("span", {className: "label", textContent: label});
    const note = hint ? make("span", {className: "hint", id: `${id}-hint`, textContent: hint}) : null;
    if (note) control.setAttribute("aria-describedby", note.id);
    return make("label", {className: `game-field ${cls || ""}`.trim()}, ...(cls === "check" ? [control, text] : [text, control]), note);
  };
  const fields = make("div", {className: "game-fields"});
  if (g.more) {
    const title = make("input", {type: "text", maxLength: 40, autocomplete: "off"});
    title.dataset.role = "title";
    fields.append(field("Name", title, "What you and your friends see it called."));
  }
  const memory = make("input", {type: "number", min: String(g.floor_mib / 1024), max: String(g.max_mib / 1024), step: "0.5", inputMode: "decimal"});
  memory.dataset.role = "memory";
  fields.append(field("Memory, in GiB", memory, `At least ${gib(g.floor_mib)}. Give it more for a larger world or more players.`));
  for (const o of g.options) {
    let control;
    if (o.type === "choice") {
      control = make("select", {}, ...o.choices.map(([value, label]) => make("option", {value: String(value), textContent: label})));
    } else if (o.type === "switch") {
      control = make("input", {type: "checkbox"});
    } else if (o.type === "number") {
      control = make("input", {type: "number", min: String(o.min), max: String(o.max), step: "1", inputMode: "numeric"});
    } else {
      control = make("input", {type: "text", maxLength: 200, autocomplete: "off", spellcheck: false});
    }
    control.dataset.role = "setting";
    control.dataset.key = o.key;
    control.dataset.type = o.type;
    fields.append(field(o.label, control, o.new_world ? "Used when the world is made, not after." : "", o.type === "switch" ? "check" : ""));
  }
  const other = make("textarea", {rows: 3, spellcheck: false, autocomplete: "off", placeholder: "name = value"});
  other.dataset.role = "other";
  const more = field("Other settings, one on each line", other,
    `Any other option of the server's own, by its name in ${g.file}. NullBill keeps the port and who may connect to itself.`, "wide");
  return make("details", {className: "game-settings"}, make("summary", {textContent: "Settings of this server"}), fields, more);
}

// The value a setting's control holds, as the server takes it.
function settingValue(control) {
  if (control.type === "checkbox") return control.checked;
  if (control.dataset.type === "number") return control.value.trim() === "" ? control.dataset.start : control.value.trim();
  return control.value;
}

function paintGame(item) {
  const row = $("pick").querySelector(`.opt[data-name="${item.name}"]`);
  const g = item.game;
  if (g.eula) $(`opt-${item.name}-eula`).checked = g.eula.agreed;
  if (g.admins) $(`opt-${item.name}-admins`).value = g.admins.join(", ");
  const title = row.querySelector("[data-role=title]");
  if (title) title.value = item.title;
  row.querySelector("[data-role=memory]").value = String(g.memory_mib / 1024);
  row.querySelector("[data-role=other]").value = g.other;
  const options = new Map(g.options.map((o) => [o.key, o]));
  for (const control of row.querySelectorAll("[data-role=setting]")) {
    const o = options.get(control.dataset.key);
    if (!o) continue;
    control.dataset.start = String(o.default);
    if (control.type === "checkbox") control.checked = Boolean(o.value);
    else control.value = String(o.value);
  }
}

// A further server of a game: Remove, once it is off and its guest is gone.
function gameTools(item) {
  const said = make("p", {className: "feedback"});
  said.setAttribute("role", "status");
  const remove = make("button", {type: "button", className: "btn small danger"}, "Remove", make("span", {className: "sr-only", textContent: ` ${item.title}`}));
  remove.dataset.role = "remove";
  remove.addEventListener("click", async () => {
    const ok = await confirmDialog({title: `Remove ${item.title}?`,
      text: "It leaves What to run, with its settings. Its guest and world are gone already.", yes: "Remove", danger: true});
    if (!ok) return;
    press(remove, said, "Removing…", async () => {
      await api("/api/game-server/remove", {name: item.name});
      announce(`Removed ${item.title}`, true);
      await refresh();
      return "";
    });
  });
  return make("div", {className: "own-tools"}, make("div", {className: "row"}, remove, said));
}

// Add a game server: one more server of a game NullBill has, in a guest
// of its own. Saved at once and ticked; the next setup run makes it.
function openGameAdd(open) {
  show("game-add-form", open);
  $("game-add").setAttribute("aria-expanded", open ? "true" : "false");
  if (!open) return;
  const kinds = (state.options && state.options.games ? state.options.games.kinds : []);
  $("game-add-kind").replaceChildren(...kinds.map((k) => make("option", {value: k.name, textContent: k.title})));
  $("game-add-title").value = "";
  say("game-add-result", "", "");
  $("game-add-title").focus();
}

// The server names the game server each refusal is about first ("Creative
// world: ..."): open those servers' settings, so what to change is in view.
// The first such row, if any.
function openRefusedGames(messages) {
  let first = null;
  for (const row of $("pick").querySelectorAll(".opt")) {
    const settings = row.querySelector(".game-settings");
    const title = settings && row.querySelector(".opt-title").textContent.toLowerCase();
    if (!title || !messages.some((m) => m.toLowerCase().startsWith(`${title}:`) || m.toLowerCase().startsWith(`${title},`))) continue;
    row.hidden = false;
    settings.open = true;
    first = first || row;
  }
  return first;
}

function addGameServer() {
  const title = $("game-add-title");
  showProblem("game-add-problem", null);
  press($("game-add-go"), "game-add-result", "Adding…", async () => {
    if (!title.value.trim()) throw new ApiError("Give it a name, such as Creative world.", 400);
    let added;
    try { ({added} = await api("/api/game-server", {game: $("game-add-kind").value, title: title.value})); } catch (error) { showProblem("game-add-problem", error); throw error; }
    await refresh();
    openGameAdd(false);
    const row = $("pick").querySelector(`.opt[data-name="${added.name}"]`);
    if (row) {
      row.hidden = false;
      row.querySelector(".game-settings").open = true;
      row.querySelector(".opt-main input").focus();
      row.scrollIntoView({block: "nearest"});
    }
    announce(`Added ${added.title}`, true);
    say("game-add-said", "ok", `Added ${added.title}. It is on: change its settings in its row, and the next setup run makes it.`);
    return "";
  }, title);
}

function optionsHead() {
  return make("div", {className: "opts-head", ariaHidden: "true"}, make("span", {textContent: "Choice"}),
    make("span", {className: "opt-num", textContent: "Memory"}), make("span", {className: "opt-num", textContent: "Disk"}), make("span", {textContent: "State"}));
}

const OPTION_GROUPS = {workloads: "workload-rows", games: "game-rows", own: "own-rows", extras: "extra-rows"};
// A group with more rows than this folds away those that are off.
const FOLD_OVER = 8;

// Long lists: the filter narrows every group (and When it runs) by name;
// without one, a long group shows what is on, chosen or on the server and
// keeps the rest behind a button. A row the owner has ticked never hides.
function foldOptions() {
  const view = optionsView;
  if (!view) return;
  const q = $("pick-filter").value.trim().toLowerCase();
  show("pick-tools", view.items.length > FOLD_OVER || Boolean(q));
  const saved = new Map(view.items.map((i) => [i.name, i]));
  let matched = 0;
  for (const set of $("pick").querySelectorAll("fieldset[data-group]")) {
    const rows = [...set.querySelectorAll(".opt")];
    const more = set.querySelector(".opts-more");
    const count = set.querySelector(".count");
    const on = rows.filter((r) => r.querySelector(".opt-main input").checked).length;
    if (count) count.textContent = rows.length > 1 ? `${on} of ${rows.length} on` : "";
    const long = rows.length > FOLD_OVER;
    const fold = long && !q && set.dataset.open !== "true";
    let folded = 0;
    let found = 0;
    for (const row of rows) {
      const item = saved.get(row.dataset.name) || {};
      const match = !q || row.dataset.find.includes(q);
      const quiet = !item.chosen && !(item.on_server || []).length && !row.querySelector(".opt-main input").checked;
      const away = fold && quiet && !row.contains(document.activeElement);
      row.hidden = !match || away;
      if (match) found += 1;
      if (match && away) folded += 1;
    }
    matched += found;
    set.querySelector(".opts").hidden = found - folded === 0;
    if (more) {
      more.hidden = !long || Boolean(q);
      more.textContent = fold ? `Show ${folded} more that ${folded === 1 ? "is" : "are"} off` : "Show only what is on";
      more.setAttribute("aria-expanded", fold ? "false" : "true");
      if (fold && !folded) more.hidden = true;
    }
    // The owner's own group stays, empty too: it says how to add one.
    set.hidden = found === 0 && (Boolean(q) || set.dataset.group !== "own");
  }
  for (const row of $("when-rows").querySelectorAll(".when")) {
    row.hidden = Boolean(q) && !`${row.getAttribute("aria-label")} ${row.dataset.name}`.toLowerCase().includes(q);
  }
  const whens = [...$("when-rows").querySelectorAll(".when")];
  if (whens.length) $("when").hidden = whens.every((r) => r.hidden);
  $("pick-count").textContent = q ? `${matched} of ${view.items.length}` : "";
  show("pick-none", Boolean(q) && matched === 0);
}

// The rows are built once and then only repainted, so a refresh closes no
// details and moves no focus.
function renderOptions(st) {
  const items = st.options ? st.options.items : [];
  // Rows also carry their title and text: a service of the owner's own can change both.
  const key = (list) => list.slice().sort().join("\n");
  const have = key([...$("pick").querySelectorAll(".opt")].map((r) => `${r.dataset.name} ${r.dataset.find}`));
  if (have !== key(items.map((i) => `${i.name} ${`${i.title} ${i.name} ${i.what}`.toLowerCase()}`))) {
    for (const [group, id] of Object.entries(OPTION_GROUPS)) $(id).replaceChildren(optionsHead(), ...items.filter((i) => i.group === group).map(optionRow));
  }
  for (const item of items) {
    const input = $(`opt-${item.name}`);
    input.checked = item.chosen;
    // What cannot work here can still be turned off, never on.
    input.disabled = Boolean(item.blocked) && !item.chosen;
    if (item.game) paintGame(item);
    if (item.torrents) paintTorrents(item);
  }
  optionsView = st.options;
  paintWhen(st.options, true);
  paintOptions();
}

// When it runs: a row per workload that can be stopped (options.power). The
// controls are set when a row is made and when the saved choices are shown;
// a preview only repaints what follows from them, so nothing the owner is
// typing is overwritten.
const STOPS = {auto: "By itself when unused", ask: "Only by me, with a reminder", manual: "Only by me"};

function whenRow(w) {
  const id = `when-${w.name}`;
  const run = make("select", {id: `${id}-run`}, make("option", {value: "always", textContent: "Always"}), make("option", {value: "on-demand", textContent: "On demand"}));
  const stop = make("select", {id: `${id}-stop`}, ...Object.entries(STOPS).map(([value, textContent]) => make("option", {value, textContent})));
  const idle = make("input", {type: "number", id: `${id}-idle`, min: "5", max: "10080", step: "5", inputMode: "numeric"});
  run.dataset.role = "run";
  stop.dataset.role = "stop";
  idle.dataset.role = "idle";
  for (const control of [run, stop, idle]) control.setAttribute("aria-describedby", `${id}-note`);
  const row = make("div", {className: "when", role: "group", ariaLabel: w.title},
    make("div", {className: "when-main"}, make("span", {className: "opt-title", textContent: w.title}), make("span", {className: "opt-desc when-size"})),
    make("label", {className: "when-field"}, make("span", {className: "label", textContent: "Runs"}), run),
    make("label", {className: "when-field when-stop"}, make("span", {className: "label", textContent: "Stops"}), stop),
    make("label", {className: "when-field when-idle"}, make("span", {className: "label", textContent: "Unused for, min"}), idle),
    make("p", {className: "when-note", id: `${id}-note`}));
  row.dataset.name = w.name;
  setWhen(row, w);
  return row;
}

function setWhen(row, w) {
  row.querySelector("[data-role=run]").value = w.blocked ? "always" : w.run;
  row.querySelector("[data-role=stop]").value = w.stop === "auto" && !w.auto ? "ask" : w.stop;
  row.querySelector("[data-role=idle]").value = w.idle_minutes;
}

function span(minutes) {
  if (!Number.isFinite(minutes) || minutes <= 0) return "that long";
  if (minutes % 1440 === 0) return minutes === 1440 ? "a day" : `${minutes / 1440} days`;
  if (minutes % 60 === 0) return `${minutes / 60} h`;
  return minutes > 60 ? `${Math.floor(minutes / 60)} h ${minutes % 60} min` : `${minutes} min`;
}

function paintWhen(view, reset) {
  const rows = (view && view.power) || [];
  const box = $("when-rows");
  show("when", rows.length > 0);
  const have = new Map([...box.querySelectorAll(".when")].map((r) => [r.dataset.name, r]));
  for (const [name, row] of have) if (!rows.some((w) => w.name === name)) row.remove();
  for (const w of rows) {
    let row = have.get(w.name);
    if (!row) row = whenRow(w);
    else if (reset) setWhen(row, w);
    have.set(w.name, row);
  }
  // In the server's order; a row is moved only when the order changed, so focus stays.
  const order = rows.map((w) => w.name);
  if ([...box.children].map((r) => r.dataset.name).join() !== order.join()) box.replaceChildren(...order.map((n) => have.get(n)));
  for (const w of rows) {
    const row = have.get(w.name);
    const run = row.querySelector("[data-role=run]");
    const stop = row.querySelector("[data-role=stop]");
    const idle = row.querySelector("[data-role=idle]");
    run.disabled = Boolean(w.blocked);
    stop.querySelector("option[value=auto]").disabled = !w.auto;
    const onDemand = run.value === "on-demand" && !w.blocked;
    row.querySelector(".when-stop").hidden = !onDemand;
    row.querySelector(".when-idle").hidden = !onDemand || stop.value === "manual";
    row.querySelector(".when-size").textContent = `${andList(w.guests)} · ${gib(w.memory_mib)} while it runs`;
    const after = span(Number(idle.value));
    const text = w.blocked ? w.blocked
      : !onDemand ? `Always on: it holds its ${gib(w.memory_mib)} all the time.`
        : `Off until you start it on the Services page. ${{
          auto: `Stops by itself once nobody has used it for ${after}.`,
          ask: `Never stops by itself; you are told once nobody has used it for ${after}.`,
          manual: "Runs until you stop it.",
        }[stop.value]}`;
    const note = row.querySelector(".when-note");
    const why = onDemand && !w.auto ? w.auto_why : "";
    if (note.dataset.text !== text + why) {
      note.dataset.text = text + why;
      note.replaceChildren(text, why ? make("span", {className: "why", textContent: ` ${why}`}) : "");
    }
  }
}

function guestLine(g) {
  const size = `${g.cores} core${g.cores === 1 ? "" : "s"}, ${gib(g.memory_mib)} memory, ${g.disk_gib} GiB disk`;
  if (g.kind === "vm-pool") return `${g.name}: a VM per job, ${g.count} at a time, each ${size}; one more disk for their image`;
  return `${g.name}: a ${g.kind === "vm" ? "VM" : "container"}, ${size}`;
}

// What a tick comes to on the server. kind: on, add, delete, change, off, chosen.
function optionState(item, checked) {
  if (item.blocked && !checked) return {kind: "blocked", dot: "", text: "Not possible"};
  const here = item.on_server;
  if (!here) {
    // No guest of its own, or no setup run yet to compare with.
    const saved = Boolean((state.options.items.find((i) => i.name === item.name) || {}).chosen);
    if (!built()) return checked ? {kind: "chosen", dot: "", text: "Chosen"} : {kind: "off", dot: "", text: "Off"};
    if (checked === saved) return checked ? {kind: "on", dot: "good", text: "On"} : {kind: "off", dot: "", text: "Off"};
    return {kind: "change", dot: "warn", text: checked ? "Will be turned on" : "Will be turned off"};
  }
  if (checked && here.length === item.guests.length) return {kind: "on", dot: "good", text: "On the server"};
  if (checked) return {kind: "add", dot: "warn", text: "Will be added"};
  if (here.length) return {kind: "delete", dot: "bad", text: "Will be deleted"};
  return {kind: "off", dot: "", text: "Off"};
}

function paintOptions() {
  const view = optionsView;
  if (!view) return;
  paintWhen(view, false);
  const share = view.memory ? view.memory.share_pct : 100;
  for (const item of view.items) {
    const row = $("pick").querySelector(`.opt[data-name="${item.name}"]`);
    if (!row) continue;
    const checked = $(`opt-${item.name}`).checked;
    const st = optionState(item, checked);
    row.dataset.state = st.kind;
    row.querySelector(".opt-memory").replaceChildren(make("span", {className: "sr-only", textContent: "Memory "}), gib(item.memory_mib));
    row.querySelector(".opt-disk").replaceChildren(make("span", {className: "sr-only", textContent: "Disk "}), item.disk_gib ? `${item.disk_gib} GiB` : "None");
    row.querySelector(".opt-state").replaceChildren(status(st.dot, st.text));
    const asks = row.querySelector(".opt-asks");
    if (asks) asks.hidden = !checked;
    if (item.torrents) paintTorrentsView(item, row);
    const note = row.querySelector(".opt-note");
    const noteText = item.blocked || (checked ? item.note : "");
    note.hidden = !noteText;
    note.textContent = noteText || "";
    note.classList.toggle("why", Boolean(item.blocked));
    const change = row.querySelector(".opt-change");
    const text = {
      add: item.on,
      delete: `${item.off} Nothing is deleted until the next setup run, which asks you once more.`,
      change: checked ? item.on : item.off,
    }[st.kind];
    change.hidden = !text;
    // Said once, when the tick changes what happens: not again on each repaint.
    if (change.textContent !== (text || "")) change.textContent = text || "";
    const makes = item.guests.length
      ? [make("ul", {}, ...item.guests.map((g) => li(guestLine(g)))),
        make("p", {textContent: `The plan counts ${gib(item.memory_mib)} of memory for it: a VM holds all of its memory, a container counts at ${share}% of its limit because it takes memory only as it uses it.`})]
      : [make("p", {textContent: `No guest: it runs on Proxmox itself, in ${gib(item.memory_mib)} of the memory left to Proxmox.`})];
    const fact = (term, ...body) => [make("dt", {textContent: term}), make("dd", {}, ...body)];
    if (item.own) {
      row.querySelector("[data-role=script]").hidden = item.own.scripts.length === 0;
      row.querySelector("[data-role=remove]").hidden = !item.own.removable || checked;
    }
    if (item.game && item.game.more) row.querySelector("[data-role=remove]").closest(".own-tools").hidden = !item.game.removable || checked;
    row.querySelector(".opt-facts").replaceChildren(
      ...(item.own ? fact("It is", make("ul", {}, ...item.own.facts.map((f) => li(f)))) : []),
      ...fact("It makes", ...makes),
      ...fact("You need", item.needs.length ? make("ul", {}, ...item.needs.map((n) => li(n))) : make("p", {textContent: "Nothing more."})),
      ...fact("Turning it on", make("p", {textContent: item.on})),
      ...fact("Turning it off", make("p", {textContent: item.off})));
  }
  foldOptions();
  paintBudget(view);
}

function paintBudget(view) {
  const m = view.memory;
  show("budget", Boolean(m));
  show("budget-hint", !m);
  show("budget-base", Boolean(m));
  const hasErrors = view.errors.length > 0;
  planProblems("choices", view);
  show("choices-problems", hasErrors);
  if (!m) return;
  const over = m.free_mib < 0;
  $("budget-total").textContent = `${gib(m.host_mib)} in the server`;
  // What runs on demand shares what is left: drawn hatched, inside the free part.
  const demandMib = m.demand_mib || 0;
  // Not every refusal is about memory: Minecraft waits for its EULA, say.
  const fit = hasErrors ? status("bad", over ? `Does not fit: ${gib(-m.free_mib)} too much` : "Cannot be planned yet")
    : !demandMib ? status("good", `Fits, ${gib(m.free_mib)} free`)
      : m.together ? status("good", `Fits, ${gib(m.free_mib - demandMib)} free with everything on`)
        : status("warn", "Fits, but not all at once");
  if ($("budget-fit").textContent !== fit.textContent) $("budget-fit").replaceChildren(fit);
  const bar = $("budget-bar");
  bar.classList.toggle("over", over);
  const room = Math.max(0, m.host_mib - m.reserve_mib - m.base_mib);
  const shared = Math.max(0, Math.min(demandMib, m.free_mib));
  const widths = [["m-reserve", m.reserve_mib], ["m-base", m.base_mib], ["m-chosen", Math.min(m.chosen_mib, room)], ["m-demand", shared], ["m-free", Math.max(0, m.free_mib - shared)]];
  for (const [cls, v] of widths) {
    const part = bar.querySelector(`.${cls}`);
    part.style.width = `${(100 * v) / m.host_mib}%`;
    part.hidden = v <= 0;
  }
  const left = !demandMib ? `Free ${gib(m.free_mib)}`
    : m.together ? `Free with all of it on ${gib(m.free_mib - demandMib)}`
      : `Not all at once: it shares ${gib(m.free_mib)}`;
  $("budget-legend").replaceChildren(...[
    make("li", {className: "k-reserve", textContent: `Proxmox ${gib(m.reserve_mib)}`}),
    make("li", {className: "k-base", textContent: `Always on ${gib(m.base_mib)}`}),
    make("li", {className: over ? "k-over" : "k-chosen", textContent: `Your choices${demandMib ? ", always on" : ""} ${gib(m.chosen_mib)}`}),
    demandMib ? make("li", {className: "k-demand", textContent: `On demand ${gib(demandMib)}`}) : null,
    make("li", {className: "k-free", textContent: over ? `${gib(-m.free_mib)} too much` : left})].filter(Boolean));
  $("budget-base-text").textContent = `Always on: ${andList(m.base)}. They and Proxmox itself are not a choice. `
    + `A VM counts with all of its memory; a container counts at ${m.share_pct}% of its limit, because it takes memory only as it uses it. `
    + "Each row shows the least it takes; memory left free goes to more CI runners at a time. "
    + "What runs on demand is off until you start it, so it counts only against the memory the rest leaves: each has to fit there by itself, "
    + "and when not all of it fits at once, starting one asks before it stops another.";
}

// The form changed: ask the server what it comes to. The newest answer wins.
function previewChoices() {
  clearTimeout(previewTimer);
  const seq = ++previewSeq;
  if (!choicesDirty()) {
    optionsView = state.options;
    paintOptions();
    return;
  }
  paintOptions();
  previewTimer = setTimeout(async () => {
    try {
      const view = await api("/api/choices/preview", choicesBody());
      if (seq !== previewSeq || !choicesDirty()) return;
      optionsView = view;
      paintOptions();
      paintSavebar();
      // A row that came or went with this answer may have made the form the saved one again.
      if (!choicesDirty()) paintStates();
    } catch { /* the rows keep the last answer; saving says what is wrong */ }
  }, 150);
}

const CHOICES = {
  alone: "Leave it alone",
  keep: "Keep its files and share them to me",
  copy: "Copy its files onto the server, check the copy, then erase the disk",
  erase: "Erase everything on it",
};

function diskChoice(disk, value, keepable) {
  if (value === "keep" && !keepable && disk.choice !== "keep") return [CHOICES.keep, "", "Not possible: no NTFS, exFAT or FAT partition on it."];
  if (value === "copy") {
    const size = disk.used_gib === null ? "" : `About ${disk.used_gib} GiB, shared to you like now, and backed up.`;
    if (disk.choice === "copy") return [CHOICES.copy, size];
    if (!disk.copy_to) return [CHOICES.copy, "", "Not possible: the server's storage is not LVM; see docs/runbooks/legacy-disk.md."];
    if (disk.used_gib === null) return [CHOICES.copy, "", "Not possible: how much it holds is unknown; read the hardware again."];
    return [CHOICES.copy, size];
  }
  if (value === "erase" && disk.kept) return ["Erase everything on it without a copy", "I have its files elsewhere."];
  return [CHOICES[value], ""];
}

// Whether Windows left a kept disk fit to be changed (disk.write, from the
// last hardware read, or "editable" once setup has made it so): a status
// line, and for the states only Windows can mend, why the box cannot be ticked.
const FULL_SHUTDOWN = "shutdown /s /t 0";
function writeStatus(write) {
  switch (write.state) {
    case "editable": return ["good", "Its files can be changed now."];
    case "clean": return ["good", "Checked: Windows shut this disk down fully. Setup makes it editable; no file is touched for that."];
    case "unclean": return ["warn", "Windows did not shut this disk down fully. It stays read-only until you choose one of the two ways below."];
    case "hibernated": return ["warn", `Not possible yet: a partition holds a hibernated Windows. Start that Windows, shut it down with ${FULL_SHUTDOWN}, then read the hardware again.`];
    case "chkdsk": return ["warn", `Not possible yet: Windows marked this disk for a disk check. Connect it to a Windows PC, run chkdsk /f on it, shut that PC down with ${FULL_SHUTDOWN}, then read the hardware again.`];
    case "damaged": return ["bad", "Not possible: its NTFS is inconsistent or the disk is failing. Copy what you need off it first; chkdsk /f on a Windows PC may mend it."];
    case "other": return ["", `Not possible: ${write.why}, and only NTFS can be changed.`];
    default: return ["", "Not checked yet. Setup checks the disk before it changes anything: if Windows did not shut it down fully, setup stops there and the disk stays shared as it is, read-only."];
  }
}

function renderDisks(disks) {
  const list = $("disk-list");
  list.replaceChildren();
  show("disks", disks.length > 0);
  for (const disk of disks) {
    const box = make("fieldset", {className: "disk"});
    box.dataset.id = disk.id;
    const hours = disk.power_on_hours ? `, ${disk.power_on_hours} hours in use` : "";
    const healthKind = disk.health === "PASSED" ? "good" : disk.health === "FAILED" ? "bad" : "";
    box.append(make("legend", {}, make("div", {className: "disk-head"},
      make("span", {className: "disk-id", textContent: disk.id}),
      make("span", {className: "meta", textContent: `${disk.size_gib} GiB ${disk.rotational ? "hard disk" : "SSD"}${hours}`}),
      make("span", {className: "push"}, status(healthKind, `Health ${disk.health}`)))));
    if (disk.health === "FAILED") {
      box.append(make("div", {className: "problem"}, make("p", {textContent: "This disk reports that it is failing. Copy what you need off it soon."})));
    }
    const parts = make("ul", {className: "disk-parts"});
    for (const p of disk.partitions) {
      const label = p.label ? ` "${p.label}"` : "";
      parts.append(li(`${p.what}${label}, ${gib(p.size_mib)}${p.keepable ? "" : ", left alone"}`));
    }
    if (!disk.partitions.length) parts.append(li("No partitions."));
    box.append(parts);
    const keepable = disk.partitions.some((p) => p.keepable);
    const choices = make("div", {className: "options"});
    for (const value of Object.keys(CHOICES)) {
      if (value === "copy" && !disk.kept && disk.choice !== "copy") continue;
      const [text, desc, why] = diskChoice(disk, value, keepable);
      const input = make("input", {type: "radio", name: `disk-${disk.id}`, value, checked: disk.choice === value, disabled: Boolean(why)});
      input.dataset.role = "choice";
      choices.append(option(input, text, desc, why, ["erase", "copy"].includes(value) ? "danger-option" : ""));
    }
    box.append(choices);
    const write = disk.write;
    const [writeKind, writeText] = writeStatus(write);
    const blocked = ["hibernated", "chkdsk", "damaged", "other"].includes(write.state);
    const rwInput = make("input", {type: "checkbox", checked: disk.rw, disabled: blocked && !disk.rw});
    rwInput.dataset.role = "rw";
    const discardInput = make("input", {type: "checkbox", checked: disk.rw && write.discard, disabled: !write.can_discard});
    discardInput.dataset.role = "discard";
    // Only for a disk Windows did not shut down fully: the two ways out.
    const unclean = make("div", {className: "disk-unclean"},
      make("ol", {className: "ways"},
        make("li", {}, make("b", {textContent: "Lose nothing. "}), "Connect the disk to a Windows PC and wait until it shows in File Explorer. Shut that PC down with ",
          make("code", {textContent: FULL_SHUTDOWN}), " (a normal Shut down with Fast Startup leaves it as it is now). Put the disk back and press Read the hardware."),
        make("li", {}, make("b", {textContent: "That Windows is gone, or the disk cannot be moved. "}), "Tick the box below. Setup then empties the journal Windows left on the disk and makes it editable.")),
      make("div", {className: "options"},
        option(discardInput, "Discard what Windows left unfinished",
          "Every file you can open now stays as it is. Changes Windows had not finished writing are lost; after a normal Shut down there are usually none. Not for a Windows that is only asleep and will start again with this disk: it would write its old view over your changes.",
          write.can_discard ? "" : "Not possible: its journal could not be read. Read the hardware again.", "danger-option")),
      make("label", {className: "field narrow disk-discard-confirm"},
        make("span", {className: "label", textContent: `Type ${disk.confirm}, the end of its name, to discard what Windows left unfinished`}),
        make("input", {autocomplete: "off", spellcheck: false})));
    const rw = make("div", {className: "disk-sub disk-rw"},
      make("div", {className: "options"}, option(rwInput, "Let me change files on it (NTFS only)",
        "Without this the share is read-only. Windows must have shut this disk down fully for it; NullBill checks, and changes nothing until it is safe.")),
      make("p", {className: "disk-write"}, make("span", {className: `dot ${writeKind}`, ariaHidden: "true"}), make("span", {textContent: writeText})),
      unclean);
    const uses = make("div", {className: "options"});
    for (const [value, text, desc] of [["backups", "For the nightly backups", ""],
      ["storage", "As extra storage in Proxmox", "A copy made first moves onto it, checked again."]]) {
      uses.append(option(make("input", {type: "radio", name: `disk-for-${disk.id}`, value, checked: disk.for === value}), text, desc));
    }
    const usesBox = make("div", {className: "disk-sub disk-for"}, make("span", {className: "label", textContent: "Then use it"}), uses);
    const confirm = make("div", {className: "disk-sub"}, make("label", {className: "field narrow disk-confirm"},
      make("span", {className: "label", textContent: `Type ${disk.confirm}, the end of its name, to erase everything on it`}),
      make("input", {autocomplete: "off", spellcheck: false})));
    box.append(rw, usesBox, confirm);
    const sync = () => {
      const chosen = box.querySelector("input[data-role=choice]:checked");
      rw.hidden = !chosen || chosen.value !== "keep";
      unclean.hidden = write.state !== "unclean" || !rwInput.checked;
      unclean.querySelector(".disk-discard-confirm").hidden = !discardInput.checked || (disk.rw && write.discard);
      const erasing = Boolean(chosen) && ["erase", "copy"].includes(chosen.value);
      const usedFor = uses.querySelector("input:checked").value;
      usesBox.hidden = !erasing;
      confirm.hidden = !erasing || (disk.choice === chosen.value && disk.for === usedFor);
    };
    box.addEventListener("change", sync);
    sync();
    list.append(box);
  }
}

function renderGpus(gpus) {
  const list = $("gpu-list");
  list.replaceChildren();
  const unread = gpus.filter((g) => !g.probed_mib);
  show("gpus", unread.length > 0);
  for (const gpu of unread) {
    const input = make("input", {type: "number", min: "1", max: "256", step: "1", inputMode: "numeric",
      value: gpu.vram_mib ? Math.round(gpu.vram_mib / 1024) : ""});
    input.dataset.pci = gpu.pci;
    list.append(make("label", {className: "field narrow"}, make("span", {className: "label", textContent: `Memory of the ${gpu.name}, in GB`}), input));
  }
}

function choicesBody() {
  const gpus = {};
  for (const input of $("gpu-list").querySelectorAll("input")) gpus[input.dataset.pci] = input.value;
  const disks = {};
  for (const box of $("disk-list").querySelectorAll(".disk")) {
    const chosen = box.querySelector("input[data-role=choice]:checked");
    disks[box.dataset.id] = {
      choice: chosen ? chosen.value : "alone",
      rw: box.querySelector("input[data-role=rw]").checked,
      discard: box.querySelector("input[data-role=rw]").checked && box.querySelector("input[data-role=discard]").checked,
      for: box.querySelector(".disk-for input:checked").value,
      confirm: box.querySelector(".disk-confirm input").value,
      discard_confirm: box.querySelector(".disk-discard-confirm input").value,
    };
  }
  const games = {};
  for (const row of $("game-rows").querySelectorAll(".opt")) {
    const eula = row.querySelector("input[data-role=eula]");
    const admins = row.querySelector("input[data-role=admins]");
    const title = row.querySelector("[data-role=title]");
    const memory = row.querySelector("[data-role=memory]");
    const size = Number(memory.value);
    games[row.dataset.name] = {
      ...(eula ? {eula: eula.checked} : {}),
      ...(admins ? {admins: admins.value} : {}),
      ...(title ? {title: title.value} : {}),
      // Empty: the game's own. What is no number goes as typed, for the server to refuse.
      memory_mib: memory.value.trim() === "" ? null : Number.isFinite(size) ? Math.round(size * 1024) : memory.value,
      settings: Object.fromEntries([...row.querySelectorAll("[data-role=setting]")].map((c) => [c.dataset.key, settingValue(c)])),
      other: row.querySelector("[data-role=other]").value,
    };
  }
  const client = $("pick").querySelector(".opt .torrents-vpn");
  let torrents;
  if (client) {
    const row = client.closest(".opt");
    const way = row.querySelector("[data-role=route]:checked");
    const share = row.querySelector("[data-role=share]");
    const room = row.querySelector("[data-role=room]").value.trim();
    const conf = row.querySelector("[data-role=conf]").value;
    torrents = {
      route: way ? way.value : "",
      share: share.options.length ? share.value : share.dataset.want,
      folder: row.querySelector("[data-role=folder]").value,
      keep_free_gib: /^\d+$/.test(room) ? Number(room) : room,
      port_mapping: row.querySelector("[data-role=port]").checked,
      // Only with the VPN chosen, and only a file that was pasted.
      ...(way && way.value === "vpn" && conf.trim() ? {conf} : {}),
    };
  }
  return {
    workloads: [...$("pick").querySelectorAll(".opt-main input:checked")].filter((i) => !i.closest("#extra-rows")).map((i) => i.value),
    games,
    ...(torrents ? {torrents} : {}),
    backups: Boolean($("opt-backups") && $("opt-backups").checked),
    console: Boolean($("opt-console") && $("opt-console").checked),
    owner_login: $("owner-login").value.trim(),
    disks,
    gpus,
    power: Object.fromEntries([...$("when-rows").querySelectorAll(".when")].map((row) => {
      const idle = row.querySelector("[data-role=idle]").value.trim();
      return [row.dataset.name, {
        run: row.querySelector("[data-role=run]").value,
        stop: row.querySelector("[data-role=stop]").value,
        idle_minutes: /^\d+$/.test(idle) ? Number(idle) : idle,
      }];
    })),
  };
}

// The form as compared with what was saved; typing an erase confirmation
// alone changes nothing.
function choicesKey() {
  const body = choicesBody();
  for (const d of Object.values(body.disks)) { delete d.confirm; delete d.discard_confirm; }
  return JSON.stringify(body);
}
function choicesDirty() { return savedChoices !== null && choicesKey() !== savedChoices; }

function renderChoices(st) {
  renderDisks(st.disks);
  renderGpus(st.gpus);
  $("owner-login").value = st.choices.owner_login;
  savedChoices = null;
  renderOptions(st);
  savedChoices = choicesKey();
}

// The bar under the form: what is unsaved, and what saving it comes to.
function paintSavebar() {
  const dirty = choicesDirty();
  const view = optionsView;
  let text = "Saving makes a new plan. Nothing on the server changes until you run setup.";
  if (dirty) {
    const parts = ["Unsaved changes."];
    if (view && view !== state.options) {
      if (view.errors.length) parts.push(view.memory && view.memory.free_mib < 0 ? "They do not fit yet." : "They cannot be planned yet.");
      // By the rows' names; a guest of no row (the kept disks' share) by its own.
      const titled = (kind) => view.items.filter((i) => optionState(i, $(`opt-${i.name}`).checked).kind === kind).map((i) => i.title);
      const own = new Set(view.items.flatMap((i) => i.guests.map((g) => g.name)));
      const adds = [...titled("add"), ...(view.adds || []).filter((n) => !own.has(n))];
      const deletes = [...titled("delete"), ...(view.removes || []).filter((n) => !own.has(n))];
      if (adds.length) parts.push(`Adds ${andList(adds)}.`);
      if (deletes.length) parts.push(`Deletes ${andList(deletes)} at the next setup run.`);
    }
    text = parts.join(" ");
  }
  $("savebar").classList.toggle("dirty", dirty);
  $("savebar-text").textContent = text;
  show("choices-discard", dirty);
}

function onChoicesChanged() {
  const dirty = choicesDirty();
  if (!dirty && $("choices-result").classList.contains("info")) say("choices-result", "", "");
  previewChoices();
  paintSavebar();
  $("plan-stale").hidden = !dirty || !state || !(state.plan.text || state.plan.errors.length);
  paintStates();
}

function renderHardware(hw) {
  const list = $("hardware");
  list.hidden = !hw;
  $("probe-button").querySelector("span").textContent = hw ? "Read it again" : "Read the hardware";
  $("probe-button").className = `btn ${hw ? "" : "primary"}`;
  if (!hw) return;
  const items = [];
  const prop = (label, value) => items.push(make("div", {}, make("dt", {textContent: label}), make("dd", {textContent: value})));
  if (hw.cpu || hw.threads) prop("Processor", `${hw.cpu || "CPU"}, ${hw.threads} threads`);
  if (hw.memory_mib) prop("Memory", gib(hw.memory_mib));
  prop("Graphics", hw.gpus.length ? hw.gpus.join(", ") : "No separate card");
  prop("Disks", `${hw.disks} disk${hw.disks === 1 ? "" : "s"}`);
  if (hw.pve_version) prop("Proxmox VE", hw.pve_version);
  list.replaceChildren(...items);
}

const KINDS = {"lxc": "Container", "vm": "VM", "vm-pool": "VM pool"};

// Why the choices cannot be planned: each problem with what to do about
// it beside it, and under them only the advice that belongs to no one
// problem.
function planProblems(prefix, view) {
  const problems = view.problems || view.errors.map((e) => ({text: e, advice: null}));
  $(`${prefix}-errors`).replaceChildren(...failItems(problems.map((x) => ({text: sentence(x.text), advice: x.advice}))));
  const rest = (view.advice || []).filter((a) => !problems.some((x) => x.advice === a));
  $(`${prefix}-advice`).replaceChildren(...rest.map((a) => li(a)));
  show(`${prefix}-advice-head`, rest.length > 0);
  show(`${prefix}-advice`, rest.length > 0);
}

function renderPlan(plan) {
  const hasErrors = plan.errors.length > 0;
  planProblems("plan", plan);
  show("plan-problems", hasErrors);
  show("plan-ok", Boolean(plan.text));
  $("plan").textContent = plan.text || "";
  show("plan-details", Boolean(plan.text));
  show("plan-hint", !plan.text && !hasErrors);
  show("plan-next", Boolean(plan.text) && !hasErrors);
  if (plan.summary) {
    const m = plan.summary.memory;
    const guests = plan.summary.guests;
    $("plan-ok-text").textContent = m.host_mib
      ? (m.limits_mib && m.limits_mib !== m.guests_mib
        ? `${guests.length} guests. Containers count at part of their limit, since they take memory only as they use it; all limits come to ${gib(m.limits_mib)}.`
        : `${guests.length} guests.`)
      : `${guests.length} guests.`;
    const bar = $("memory");
    $("memory-block").hidden = !m.host_mib;
    if (m.host_mib) {
      $("memory-total").textContent = `${gib(m.host_mib)} in all`;
      for (const [cls, v] of [["m-reserve", m.reserve_mib], ["m-guests", m.guests_mib], ["m-free", m.unallocated_mib]]) {
        bar.querySelector(`.${cls}`).style.width = `${Math.max(0, (100 * v) / m.host_mib)}%`;
      }
      $("memory-legend").replaceChildren(
        make("li", {className: "k-reserve", textContent: `Proxmox ${gib(m.reserve_mib)}`}),
        make("li", {className: "k-guests", textContent: `Guests ${gib(m.guests_mib)}`}),
        make("li", {className: "k-free", textContent: `Free ${gib(m.unallocated_mib)}`}));
    }
    $("plan-guests").replaceChildren(...guests.map((g) => make("tr", {},
      make("td", {className: "name", textContent: g.name}),
      make("td", {textContent: (KINDS[g.kind] || g.kind) + (g.count ? ` of ${g.count}` : "")}),
      make("td", {className: "hide-phone muted", textContent: g.zone || ""}),
      make("td", {className: "num", textContent: g.cores ?? ""}),
      make("td", {className: "num", textContent: g.memory_mib ? gib(g.memory_mib) + (g.count ? " each" : "") : ""}))));
  }
  $("tailnet-policy").textContent = plan.tailnet_policy || "";
  show("tailnet-details", Boolean(plan.tailnet_policy));
}

function renderAlerts(alerts) {
  $("ntfy-topic").textContent = alerts && alerts.ntfy_url ? alerts.ntfy_url.replace("https://", "") : "";
  $("healthchecks-url").value = alerts ? alerts.healthchecks_url : "";
}

// An alert's summary says what happened, then after a semicolon what to do:
// a line each.
function alertText(a) {
  const [what, ...todo] = String(a.summary || a.name).split("; ");
  return {what: sentence(what).replace(/\.$/, ""), todo: todo.map(sentence).join(" ")};
}

// What is firing, from monitoring.
function renderFiring() {
  const s = lastStatus;
  const ok = s && s.monitoring === "ok";
  const alerts = ok ? s.alerts : [];
  show("firing-table", alerts.length > 0);
  show("firing-empty", alerts.length === 0);
  $("firing-empty").textContent = !s ? "Asking monitoring…"
    : s.monitoring === "not planned" ? "Monitoring is not in your plan, so nothing can fire."
    : !ok ? (built() ? "Monitoring does not answer. If setup is running or just finished, wait a minute." : "Alerts show here once setup has built monitoring.")
    : "Nothing is firing.";
  $("firing-meta").textContent = ok ? `${s.targets} systems watched · read ${readAt(s.at)}` : "";
  const signed = Boolean(state && state.chat && state.chat.signed_in);
  $("firing-rows").replaceChildren(...alerts.map((a) => {
    const ask = signed ? make("button", {type: "button", className: "btn small", textContent: "Ask the agent"}) : null;
    if (ask) ask.addEventListener("click", () => askAgent(`The alert ${a.name} fires${a.summary ? `: ${a.summary.replace(/[.\s]+$/, "")}` : ""}. What does it mean, and what should I do?`));
    return make("tr", {},
      make("td", {className: "hide-phone"}, status(a.severity === "page" ? "bad" : "warn", a.severity === "page" ? "Urgent" : "When you can")),
      make("td", {}, make("div", {className: "name", textContent: alertText(a).what}),
        alertText(a).todo ? make("div", {className: "sub alert-todo", textContent: alertText(a).todo}) : null,
        make("div", {className: "sub"}, make("span", {className: "only-phone-inline", textContent: a.severity === "page" ? "Urgent · " : "When you can · "}), make("code", {textContent: a.name}))),
      make("td", {className: "actions"}, ask));
  }));
}

// The agent's settings.
function renderAgentSignin(st) {
  const kind = st.agent_credential;
  $("agent-label").textContent = kind ? "Replace it with a new API key or subscription token" : "API key or subscription token";
  $("signin-state").replaceChildren(kind ? status("good", `Signed in with ${kind === "API key" ? "an API key" : "a subscription token"}`) : status("warn", "Not signed in"));
}

function renderMode(settings) {
  for (const button of document.querySelectorAll("#modes .choice")) button.setAttribute("aria-pressed", button.dataset.mode === settings.mode ? "true" : "false");
  $("auto-fix").checked = settings.auto_fix;
  for (const button of $("chat-modes").children) {
    button.setAttribute("aria-pressed", button.dataset.mode === settings.mode ? "true" : "false");
    button.title = MODE_DESC[button.dataset.mode];
  }
  $("chat-modes").dataset.mode = settings.mode;
  for (const select of document.querySelectorAll("select[data-setting]")) select.value = settings[select.dataset.setting] || "default";
}

function renderAgentPowers(on) {
  $("agent-powers").setAttribute("aria-checked", on ? "true" : "false");
  $("powers-state").replaceChildren(on ? status("bad", "On: the operator agent has root on Proxmox") : status("", "Off"));
}

function renderAgentGithub(login) {
  $("github-state").replaceChildren(login ? status("good", `Saved, as ${login} on GitHub`) : status("", "Not set"));
  show("agent-github-remove", Boolean(login));
}

function renderRemoteAgents(names, ready) {
  const button = $("remote-agent-form").querySelector("button");
  button.setAttribute("aria-disabled", ready ? "false" : "true");
  button.dataset.why = ready ? "" : "This works once setup has built the VPN guest.";
  const list = $("remote-agents");
  const have = new Map([...list.children].map((node) => [node.dataset.name, node]));
  for (const [name, node] of have) if (!names.includes(name)) node.remove();
  for (const name of names) {
    if (have.has(name)) continue;
    const result = make("p", {className: "feedback"});
    result.setAttribute("role", "status");
    const revoke = make("button", {type: "button", className: "btn small ghost", textContent: "Revoke"});
    revoke.setAttribute("aria-label", `Revoke ${name}`);
    revoke.addEventListener("click", () => press(revoke, result, `Revoking ${name}…`, async () => {
      const left = (await api("/api/remote-agents/revoke", {name})).remote_agents;
      show("remote-agent-token-box", false);
      state.remote_agents = left;
      setTimeout(() => renderRemoteAgents(left, ready), 1200);
      return `${name} revoked.`;
    }));
    const item = make("li", {}, icon("key"), make("span", {className: "item-name", textContent: name}), result, revoke);
    item.dataset.name = name;
    list.append(item);
  }
}

let logEntries = [];
async function loadAgentLog() {
  try {
    logEntries = (await api("/api/agent-log")).entries.slice().reverse();
    paintAgentLog();
  } catch (error) {
    $("log-rows").replaceChildren(make("tr", {}, make("td", {colSpan: 5, textContent: error.message})));
  }
}
function paintAgentLog() {
  const WORD = {run: "Ran", refused: "Blocked", approve: "Approved", decline: "Declined", "ask-owner": "Asked you", ask: "Asked you", notes: "Notes"};
  const q = $("log-filter").value.trim().toLowerCase();
  const what = (e) => `${e.where && e.where !== "host" ? `${e.where}: ` : ""}${e.command || e.what || e.question || e.kind || ""}`;
  const rows = logEntries.filter((e) => !q || `${WORD[e.action] || e.action} ${what(e)} ${e.tier || ""}`.toLowerCase().includes(q)).map((e) => make("tr", {},
    make("td", {className: "muted", textContent: clock(e.t)}),
    make("td", {textContent: WORD[e.action] || e.action}),
    make("td", {}, e.command ? make("code", {textContent: what(e)}) : make("span", {textContent: what(e)})),
    make("td", {className: "hide-phone", textContent: e.by === "owner" ? "You" : "Agent"}),
    make("td", {className: "hide-phone"}, e.tier ? make("span", {className: `tier ${e.tier}`, textContent: e.tier}) : null)));
  const empty = q ? "Nothing matches the filter." : "Nothing yet. Each action the agent takes shows here before it runs.";
  $("log-rows").replaceChildren(...(rows.length ? rows : [make("tr", {}, make("td", {colSpan: 5, className: "muted", textContent: empty}))]));
}

// Access.
const LAN = {
  open: ["warn", "Open", "Proxmox's web UI and SSH answer on your home network."],
  closing: ["warn", "Closing", "Press Keep it closed on this page opened over Tailscale, or it opens again by itself."],
  closed: ["good", "Closed", "Proxmox answers only over Tailscale."],
};

// People (Access, People): who else reaches the server and what each may
// open. Asked when the tab opens; a change answers with the new list.
let peopleView = null;
let personEdited = null;   // the name in the form, "" for a new person, null while it is closed
let personInvited = null;  // whose invitation steps show

function peopleShown() { return currentView === "access" && !$("sec-people").hidden; }

async function loadPeople(fresh) {
  peopleView = await api(`/api/people${fresh ? "?fresh=1" : ""}`);
  renderPeople();
}

function shareTitle(name) {
  const found = peopleView.shareable.find((s) => s.name === name);
  return found ? found.title : name;
}

function deviceLines(row) {
  if (row.devices === null) return [make("span", {className: "muted", textContent: "Not known yet"})];
  if (!row.devices.length) return [row.owner ? status("", "No device") : status("warn", "Not joined yet")];
  const lines = row.devices.slice(0, 3).map((d) => status(d.connected ? "good" : "", d.name || d.os || "Device",
    d.connected ? "connected" : d.seen ? `seen ${ago(d.seen)}` : ""));
  if (row.devices.length > 3) lines.push(make("span", {className: "sub", textContent: `and ${row.devices.length - 3} more`}));
  return lines;
}

function mayOpen(row) {
  if (row.owner) return [make("span", {textContent: "Everything"})];
  const planned = new Map(peopleView.shareable.map((s) => [s.name, s.planned]));
  const names = [...row.by_role.filter((n) => !row.services.includes(n)), ...row.services];
  if (!names.length) return [make("span", {className: "muted", textContent: "Nothing yet"})];
  const waiting = row.services.filter((n) => planned.get(n) === false);
  return [make("span", {textContent: names.map(shareTitle).join(", ")}),
    waiting.length ? make("span", {className: "sub", textContent: `${andList(waiting.map(shareTitle))} ${waiting.length > 1 ? "are" : "is"} not set up on the server yet`}) : null];
}

// On a phone a row stands alone, so each cell says what it is.
function labelled(cell, label) { cell.dataset.label = label; return cell; }

function renderPeople() {
  const view = peopleView;
  if (!view) return;
  const ts = view.tailscale;
  const others = view.people.length - 1;
  $("people-state").replaceChildren(
    ts.waits ? status("", "Waits for setup")
      : ts.error ? status("warn", "Tailscale did not answer")
        : ts.in_place === false ? status("bad", "Changed in Tailscale")
          : status("good", "In force", others ? `${others} ${others > 1 ? "people" : "person"} besides you` : "only you"));
  show("people-waits", Boolean(ts.waits));
  $("people-waits").querySelector("span").textContent = ts.waits || "";
  show("people-drift", !ts.waits && ts.in_place === false);
  show("people-error", Boolean(ts.error));
  $("people-error").querySelector("span").textContent = ts.error ? `${ts.error.split("; ").map(sentence).join(" ")} Who is listed here still holds. Their devices show again once Tailscale answers.` : "";
  $("people-count").textContent = `${view.limit.used} of ${view.limit.max} people on Tailscale's free plan, you included`;
  const full = view.limit.used >= view.limit.max;
  $("person-add").setAttribute("aria-disabled", full ? "true" : "false");
  $("person-add").dataset.why = full ? `Tailscale's free plan holds ${view.limit.max} people, you included. Remove someone first.` : "";

  $("people-strangers").replaceChildren(...view.strangers.map((login) => {
    const add = make("button", {type: "button", className: "btn small", textContent: "Add them"});
    add.addEventListener("click", () => openPerson("", login));
    return make("div", {className: "note warn"}, icon("alert"),
      make("span", {}, make("b", {textContent: login}), " is on your Tailscale network and not listed here, so they can open nothing. Add them, or remove them on ",
        make("a", {href: view.users_page, target: "_blank", rel: "noopener noreferrer", textContent: "Tailscale's Users page"}), "."), add);
  }));

  $("people-rows").replaceChildren(...view.people.map((row) => {
    const actions = make("td", {className: "actions"});
    if (!row.owner) {
      const invite = make("button", {type: "button", className: "btn small ghost", textContent: "Invite"});
      invite.setAttribute("aria-label", `How ${row.name} joins`);
      invite.addEventListener("click", () => showInvite(row.name, true));
      const edit = make("button", {type: "button", className: "btn small ghost", textContent: "Edit"});
      edit.setAttribute("aria-label", `Edit ${row.name}`);
      edit.addEventListener("click", () => openPerson(row.name));
      const remove = make("button", {type: "button", className: "btn small ghost", textContent: "Remove"});
      remove.setAttribute("aria-label", `Remove ${row.name}`);
      remove.addEventListener("click", () => removePerson(row, remove));
      actions.append(invite, edit, remove);
    }
    const tr = make("tr", {},
      make("td", {}, make("span", {className: "name", textContent: row.name}), row.owner ? make("span", {className: "meta", textContent: " you"}) : null,
        make("span", {className: "sub login", textContent: row.login})),
      labelled(make("td", {}, ...mayOpen(row)), "May open"),
      labelled(make("td", {className: "devices"}, ...deviceLines(row)), "Devices"),
      actions);
    if (row.name === personEdited || row.name === personInvited) tr.className = "current";
    return tr;
  }));
  if (personInvited) showInvite(personInvited, false);
}

function nameFrom(login) {
  return (login || "").split("@")[0].toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^[^a-z]+|-+$/g, "").slice(0, 31);
}

// Under the ticks, in one line: what this person opens once saved.
function personSummary() {
  const boxes = [...$("person-services").querySelectorAll("input:checked")];
  const who = $("person-name").value.trim() || "They";
  $("person-summary").replaceChildren(...(boxes.length
    ? [`${who} will open `, make("b", {textContent: andList(boxes.map((box) => shareTitle(box.value)))}), ", and nothing else on the server."]
    : [`Nothing is ticked: ${who === "They" ? "they" : who} can join your Tailscale network and open nothing.`]));
}

// A long tick list narrowed by name. What is ticked never hides.
function filterPerson() {
  const q = $("person-filter").value.trim().toLowerCase();
  for (const option of $("person-services").querySelectorAll(".option")) {
    option.hidden = Boolean(q) && !option.dataset.find.includes(q) && !option.querySelector("input").checked;
  }
}

function openPerson(name, login) {
  const add = $("person-add");
  if (!name && add.getAttribute("aria-disabled") === "true") { say("people-result", "info", add.dataset.why); return; }
  const row = name ? peopleView.people.find((p) => p.name === name) : null;
  personEdited = row ? row.name : "";
  personInvited = null;
  show("person-invite", false);
  say("people-result", "", "");
  say("person-result", "", "");
  showProblem("person-problem", null);
  showProblem("people-problem", null);
  $("person-form-title").textContent = row ? `What ${row.name} may open` : "Add a person";
  $("person-save").textContent = row ? "Save" : "Add";
  const nameField = $("person-name");
  nameField.value = row ? row.name : nameFrom(login);
  nameField.readOnly = Boolean(row);
  // Until the owner types a name, it follows the login.
  nameField.dataset.follows = row ? "" : "login";
  $("person-name-hint").textContent = row ? "Stays as it is. For another name, remove them and add them again." : "For you only. Lowercase letters, digits and dashes.";
  $("person-login").value = row ? row.login : login || "";
  for (const field of [nameField, $("person-login")]) field.removeAttribute("aria-invalid");
  $("person-services").replaceChildren(...peopleView.shareable.map((s) => {
    const byRole = Boolean(row && row.by_role.includes(s.name));
    const box = make("input", {type: "checkbox", value: s.name, checked: byRole || Boolean(row && row.services.includes(s.name)), disabled: byRole});
    const option = make("label", {className: "option"}, box, make("span", {className: "option-text"},
      make("span", {className: "option-title", textContent: s.title}),
      s.what ? make("span", {className: "option-desc", textContent: s.what}) : null,
      // Shown once ticked (style.css): what the tick opens, and what they can do behind it.
      make("span", {className: "option-access", textContent: `Opens ${s.opens}. ${s.access}`}),
      byRole ? make("span", {className: "option-desc", textContent: "Their role gives them this."}) : null,
      !s.planned && !byRole ? make("span", {className: "option-why", textContent: "Not set up on the server yet. It opens for them once you turn it on under Setup, What to run."}) : null));
    option.dataset.find = `${s.title} ${s.name} ${s.what}`.toLowerCase();
    return option;
  }));
  $("person-filter").value = "";
  show("person-find", peopleView.shareable.length > FOLD_OVER);
  if (!peopleView.shareable.length) {
    $("person-services").replaceChildren(make("p", {className: "empty-line", textContent: "Nothing that can be given to a person is turned on under Setup, What to run."}));
  }
  personSummary();
  show("person-form", true);
  renderPeople();
  // What is known is filled in; the cursor goes to what is next.
  ((row || login) && $("person-services").querySelector("input:not(:disabled)") || $("person-login")).focus();
  $("person-form").scrollIntoView({block: "nearest"});
}

function closePerson() {
  personEdited = null;
  showProblem("person-problem", null);
  show("person-form", false);
  renderPeople();
}

function savePerson(button) {
  const name = $("person-name").value.trim().toLowerCase();
  const fields = {name: $("person-name"), login: $("person-login")};
  for (const field of Object.values(fields)) field.removeAttribute("aria-invalid");
  const live = !peopleView.tailscale.waits;
  showProblem("person-problem", null);
  press(button, "person-result", live ? "Sending to Tailscale, which tests it first…" : "Saving…", async () => {
    let out;
    try {
      out = await api("/api/people", {
        name, login: fields.login.value.trim(),
        services: [...$("person-services").querySelectorAll("input:checked:not(:disabled)")].map((box) => box.value),
      });
    } catch (error) {
      const field = /^Name the person|is you/.test(error.message) ? fields.name : /login/i.test(error.message) ? fields.login : null;
      if (field && error.status === 400 && !error.list) { field.setAttribute("aria-invalid", "true"); field.focus(); }
      else if (!showProblem("person-problem", error)) setTimeout(() => $("person-result").scrollIntoView({block: "nearest"}), 0);
      throw error;
    }
    peopleView = out;
    personEdited = null;
    show("person-form", false);
    say("people-result", "ok", out.message);
    announce(out.message);
    renderPeople();
    if (out.added) showInvite(name, true);
    else $("person-add").focus();
    return "";
  });
}

function showInvite(name, focus) {
  const row = peopleView.people.find((p) => p.name === name && !p.owner);
  personInvited = row ? name : null;
  show("person-invite", Boolean(row));
  if (!row) return;
  if (focus) { personEdited = null; show("person-form", false); }
  $("person-invite-title").textContent = `How ${name} joins`;
  $("person-invite-login").textContent = row.login;
  $("person-invite-link").href = peopleView.users_page;
  $("person-invite-text").textContent = row.instructions;
  show("person-invite-root", Boolean(row.root));
  if (focus) {
    say("person-invite-result", "", "");
    renderPeople();
    $("person-invite").focus();
    $("person-invite").scrollIntoView({block: "nearest"});
  }
}

async function removePerson(row, button) {
  const sure = await confirmDialog({
    title: `Remove ${row.name}?`,
    text: `From the moment you confirm, ${row.login} opens nothing on your server. This page then takes their account and devices off your Tailscale network too, or says what is left for you to do there.`,
    yes: "Remove",
  });
  if (!sure) { button.focus(); return; }
  press(button, "people-result", `Removing ${row.name}…`, async () => {
    const out = await peopleAsk("/api/people/remove", {name: row.name});
    peopleView = out;
    if (personEdited === row.name) { personEdited = null; show("person-form", false); }
    if (personInvited === row.name) { personInvited = null; show("person-invite", false); }
    renderPeople();
    $("person-add").focus();
    announce(out.message);
    return out.message;
  });
}

// A change from the toolbar or a row. Several reasons for a refusal go into
// the block under the toolbar.
async function peopleAsk(path, body) {
  showProblem("people-problem", null);
  try { return await api(path, body); } catch (error) { showProblem("people-problem", error); throw error; }
}

function wirePeople() {
  $("person-add").addEventListener("click", () => openPerson(""));
  $("person-services").addEventListener("change", () => { personSummary(); filterPerson(); });
  $("person-filter").addEventListener("input", filterPerson);
  $("person-name").addEventListener("input", () => { $("person-name").dataset.follows = ""; personSummary(); });
  $("person-login").addEventListener("input", () => {
    const name = $("person-name");
    if (name.dataset.follows === "login") { name.value = nameFrom($("person-login").value); personSummary(); }
  });
  $("person-cancel").addEventListener("click", () => { closePerson(); $("person-add").focus(); });
  $("person-form").addEventListener("submit", (event) => { event.preventDefault(); savePerson(submitter(event)); });
  $("person-invite-close").addEventListener("click", () => { personInvited = null; show("person-invite", false); renderPeople(); $("person-add").focus(); });
  $("person-invite-copy").addEventListener("click", () => press($("person-invite-copy"), "person-invite-result", "Copying…", async () => {
    try { await copyText($("person-invite-text").textContent); } catch {
      selectAll($("person-invite-text"));
      throw new ApiError("Copying is blocked here. The steps are selected: copy them with Ctrl+C or your phone's Copy.", 400);
    }
    return "Copied.";
  }));
  $("people-sync").addEventListener("click", () => press($("people-sync"), "people-result", "Sending the policy to Tailscale…", async () => {
    peopleView = await peopleAsk("/api/people/sync", {});
    renderPeople();
    return peopleView.message;
  }));
  $("people-refresh").addEventListener("click", () => press($("people-refresh"), "people-result", "Asking Tailscale…", async () => {
    await loadPeople(true);
    return "";
  }));
}

// Whether a phone on mobile data reaches the server directly or through
// Tailscale's relays, as the last setup run or checks found it (direct.py).
// How devices reach the server (nullbill/direct.py), by what the reading
// comes to: a path seen in use, the port alone, or neither.
const PATHS = {
  measured: ["good", "Direct"],
  open: ["good", "Port open"],
  relayed: ["warn", "Relayed right now"],
  unmeasured: ["", "Not measured"],
  waiting: ["", "Not on your tailnet yet"],
  unread: ["", "Not known"],
};
// A record kept by a NullBill from before paths were measured.
const OLD_PATHS = {direct: "open", waiting: "waiting", unread: "unread"};
const DEVICE_PATHS = {home: "direct, on the home network", away: "direct, away from home", direct: "direct", relayed: "only through a Tailscale relay"};

function pathShown() { return currentView === "access" && !$("sec-tailscale").hidden; }
function pathNow() { return pathView || (state && state.direct) || null; }

// Read on the host while the tab is open: the path a phone takes changes
// with the network it is on, and a setup run from days ago cannot know it.
// The server pings the devices that are on the tailnet and idle, so a
// reading can take some seconds; the last one shows until then.
async function loadPath() {
  if (!state || !built()) return;
  pathView = (await api("/api/path")).direct;
  paintStates();
}

function renderDirect(found) {
  const verdict = found && (found.verdict || OLD_PATHS[found.kind] || "unmeasured");
  const path = verdict && PATHS[verdict];
  show("direct-row", Boolean(path));
  if (!path) { show("direct-devices-row", false); show("direct-help", false); return; }
  const devices = found.devices || [];
  const names = (list) => andList(list.map((d) => d.name));
  const away = devices.filter((d) => d.path === "direct" && d.where === "away");
  const relayed = devices.filter((d) => d.path === "relayed");
  const sub = {
    measured: `Measured with ${names(away)}, from outside the home network`,
    open: `The home router sends UDP ${found.port} here${found.method ? ` (${found.method})` : ""}`,
    relayed: `Measured with ${names(relayed)}`,
    unmeasured: "No device away from home answered",
  }[verdict] || "";
  const [kind, word] = path;
  $("direct-state").replaceChildren(status(kind, word));
  // What it rests on, when it was read, and the last time a device away
  // from home was direct.
  const read = sub ? [sub] : [];
  if (found.at) read.push(`read ${readAt(found.at)}`);
  if (found.at && found.live === false) read.push("the server could not be asked just now");
  if (verdict !== "measured" && found.seen && found.seen.at) read.push(`last direct from away: ${andList(found.seen.names || [])}, ${clock(found.seen.at)}`);
  $("direct-read").textContent = read.join(" · ");
  show("direct-devices-row", devices.length > 0);
  $("direct-devices").replaceChildren(...devices.map((d) => li(`${d.name}: ${DEVICE_PATHS[d.path === "direct" ? d.where || "direct" : "relayed"]}`)));
  // What the server read about the port and what to do, a line each; with
  // a direct path seen in use there is nothing to do.
  const settled = verdict === "measured" || verdict === "open";
  const notes = settled ? [] : String(found.detail || "").split("; ").filter(Boolean).map(sentence);
  if (verdict === "unmeasured" && found.kind !== "waiting") notes.unshift("The server asks every device on your tailnet while this tab is open, and a phone that is asleep may not answer. Open this page on the phone, on mobile data, and its path shows here within seconds.");
  show("direct-help", notes.length > 0);
  $("direct-help").replaceChildren(...notes.map((t) => li(t)));
}

// The port and where a forward made by hand goes, wherever the page names them.
function renderPortMapping(asked, found) {
  $("port-mapping").checked = asked;
  if (!found) return;
  for (const id of ["direct-port", "portmap-port"]) $(id).textContent = found.port;
  for (const id of ["direct-where", "portmap-where"]) $(id).textContent = found.address ? ` to ${found.address}` : "";
}

// The time left to confirm a close, counted from when the server said it.
function lanLeft() {
  const lan = state.lan;
  if (lan.state !== "closing" || lan.left_s === null || lan.left_s === undefined) return null;
  return Math.max(0, lan.left_s - Math.round(Date.now() / 1000 - state.at_local));
}
function lanLeftText() {
  const left = lanLeft();
  if (left === null) return `within ${state.lan.minutes} minutes`;
  return left > 90 ? `within ${Math.ceil(left / 60)} minutes` : left > 0 ? "within a minute" : "now";
}

function renderLan(lan) {
  const [kind, word, text] = LAN[lan.state];
  const left = lanLeft();
  const over = left === 0;
  const extra = lan.state === "closing" && !lan.via_vpn && !over ? " This page is not open over Tailscale right now." : "";
  $("lan-state").replaceChildren(status(kind, over ? "Ran out" : word, lan.state === "closing" && left ? `${Math.ceil(left / 60)} min left` : ""));
  $("lan-status").textContent = over ? "The time to press Keep it closed is over, so the server is opening Proxmox to your home network again. This page says so once the server confirms it."
    : lan.state === "open" && lan.lapsed ? `${text} The last close was not confirmed in time, so the server opened it again at ${clock(lan.lapsed)}.`
    : text + extra;
  $("lan-minutes").textContent = lan.minutes;
  show("lan-close", lan.state === "open");
  show("lan-keep", lan.state === "closing");
  show("lan-open", lan.state !== "open");
}

function renderTrust(trust) {
  const ready = Boolean(trust && trust.ready);
  show("trust-download", ready);
  show("trust-wait", false);
  show("trust-props", ready && Boolean(trust.fingerprint));
  $("trust-fingerprint").textContent = ready && trust.fingerprint ? trust.fingerprint : "";
  $("trust-state").replaceChildren(!ready ? status("", "Made by the first setup run")
    : trusted === true ? status("good", "This browser trusts this server")
    : trusted === false ? status("warn", "This browser does not trust it yet")
    : status("", "Ready to install", "this browser cannot tell whether it trusts it"));
}

// The setup guide: what is needed, then what is recommended. It sits on
// Overview until the first setup run finishes, then behind the top bar's
// ring until everything is done.
function runningAction() { return job && job.view && job.view.state === "running" ? job.view.action : null; }

function guideItems(st) {
  const apply = st.last_jobs.apply;
  const running = runningAction();
  const hw = st.hardware;
  const signed = st.chat && st.chat.signed_in;
  const items = [
    ["agent/settings", "Sign in the agent", signed ? ["done", "It helps from here on."] : ["todo", "It reads the hardware for you and fixes what breaks."]],
    ["setup/hardware", "Read the hardware",
      running === "probe" ? ["running", "Reading…"]
        : st.probed ? ["done", hw ? `${hw.cpu || "CPU"}${hw.memory_mib ? `, ${gib(hw.memory_mib)}` : ""}` : "Done."] : ["todo", "Processor, memory, disks and graphics. Changes nothing."]],
    ["setup/choices", "Choose what to run",
      choicesDirty() ? ["attention", "Changed but not saved."] : st.choices.owner_login ? ["done", "Saved."] : ["todo", "Workloads, disks and your Tailscale login."]],
    ["setup/plan", "Check that it fits",
      st.plan.errors.length ? ["attention", "The plan does not fit yet."] : st.plan.text ? ["done", "The plan fits."] : ["todo", "Once the choices are saved."]],
  ];
  const removals = st.removals || [];
  if (removals.length && st.removals_stopped) items.push(["setup/build", "Keep or delete guests", ["attention", `Setup stopped before deleting ${andList(removals)}.`]]);
  const due = setupDue();
  items.push(["setup/build", "Review and run setup",
    SETUP_RUNS.has(running) ? ["running", "Running now."]
      : !apply ? ["todo", "Shows what it builds, then builds it in about 20 minutes."]
      : built() && apply.state === "done" ? (due.length ? ["attention", due[0].text] : ["done", `Finished ${ago(apply.started)}.`])
      : apply.state === "running" ? ["running", "Running now."] : ["attention", "Stopped. Activity says why."]]);
  const needed = items.length;
  // Done only for what the page can see: a test alert opened on a device,
  // this page reached over Tailscale, this browser trusting the certificate.
  const heard = st.alerts && st.alerts.heard;
  items.push(["alerts", "Get alerts on your phone",
    heard ? ["done", `A test alert reached a device ${ago(heard)}.`] : ["todo", "Subscribe to your ntfy topic, send a test alert and tap it."]]);
  const joined = st.lan.via_vpn || st.lan.state === "closed";
  const onTailnet = st.direct && st.direct.kind && !["waiting", "unread"].includes(st.direct.kind);
  items.push(["access/tailscale", "Reach it over Tailscale",
    joined ? ["done", "This page answers over Tailscale."]
      : st.tailscale_key ? ["attention", "Auth key saved: run setup."]
      : onTailnet ? ["todo", "The server is on your tailnet: open this page over Tailscale."]
      : st.tailscale_client ? ["todo", "Add an auth key next."] : ["todo", "Add an OAuth client."]]);
  items.push(["access/lan", "Close Proxmox to the home network",
    st.lan.state === "closed" ? ["done", "Only over Tailscale."]
      : st.lan.state === "closing" ? ["attention", lanLeft() === 0 ? "The time to confirm ran out." : `Confirm ${lanLeftText()}.`] : ["todo", "After Tailscale works."]]);
  items.push(["access/trust", "Trust this server on your devices",
    !(st.trust && st.trust.ready) ? ["todo", "After setup makes the certificate."]
      : trusted === true ? ["done", "This browser trusts it. Install it on your other devices too."]
      : trusted === false ? ["todo", "This browser does not trust it yet."] : ["todo", "Install the root certificate."]]);
  return items.map(([href, title, [kind, sub]], i) => ({href, title, kind, sub, later: i >= needed}));
}

function guideList(items) {
  const current = items.find((i) => i.kind !== "done");
  return items.map((item, i) => {
    const mark = make("span", {className: "step-mark", ariaHidden: "true"},
      item.kind === "done" ? icon("check") : item.kind === "attention" ? "!" : item.kind === "running" ? "" : String(i + 1));
    const word = {done: "done", attention: "needs you", running: "running", todo: "to do"}[item.kind];
    const go = item === current ? make("span", {className: "go"}, item.kind === "attention" ? "Fix" : "Start", icon("chevron-right"))
      : item.later && item.kind !== "done" ? make("span", {className: "later", textContent: "Recommended"}) : null;
    return make("li", {className: `${item.kind}${item === current ? " current" : ""}`},
      make("a", {href: `#${item.href}`}, mark,
        make("span", {className: "step-text"}, make("span", {className: "step-title", textContent: item.title}),
          make("span", {className: "sr-only", textContent: `, ${word}. `}),
          make("span", {className: "step-sub", textContent: item.sub})),
        go));
  });
}

function renderGuide(items) {
  const done = items.filter((i) => i.kind === "done").length;
  const text = `${done} of ${items.length} done`;
  const inline = !built();
  show("guide-inline", inline);
  if (inline) {
    $("guide-list").replaceChildren(...guideList(items));
    $("guide-progress").textContent = text;
    show("guide-agent", Boolean(state.chat && state.chat.signed_in));
  }
  const left = items.length - done;
  const ring = $("guide-btn");
  const required = items.filter((i) => !i.later && i.kind !== "done").length;
  ring.hidden = left === 0 || (inline && currentView === "overview") || (phone.matches && required === 0);
  $("guide-frac").textContent = `${done}/${items.length}`;
  ring.querySelector(".ring-fill").style.strokeDashoffset = `${50.27 * (1 - done / items.length)}`;
  ring.title = `Setup guide: ${text}`;
  $("guide-count").textContent = `, ${text}`;
  if ($("guide").open) {
    $("guide-pop-list").replaceChildren(...guideList(items));
    $("guide-pop-progress").textContent = text;
  }
}

// Each count, the guide, the overview and the banner, from state and the run.
function paintStates() {
  if (!state) return;
  const st = state;
  const running = runningAction();
  const items = guideItems(st);
  renderGuide(items);
  for (const a of document.querySelectorAll("#stepper a")) {
    const s = a.dataset.step;
    const attention = (s === "plan" && st.plan.errors.length > 0) || (s === "choices" && choicesDirty()) || (s === "build" && built() && setupDue().length > 0);
    a.classList.toggle("done", stepDone(s) && !attention);
    a.classList.toggle("attention", attention);
    let note = a.querySelector(".step-state");
    if (!note) { note = make("span", {className: "sr-only step-state"}); a.append(note); }
    note.textContent = attention ? ", needs you" : stepDone(s) ? ", done" : "";
  }
  $("hardware-state").replaceChildren(running === "probe" ? status("run", "Reading")
    : st.probed ? status("good", "Read", st.hardware && st.hardware.read_at ? clock(st.hardware.read_at) : "") : status("", "Not read yet"));
  $("choices-state").replaceChildren(choicesDirty() ? status("warn", "Not saved") : st.choices.owner_login ? status("good", "Saved") : status("", "Not saved yet"));
  $("plan-state").replaceChildren(st.plan.errors.length ? status("bad", "Does not fit") : st.plan.text ? status("good", "Fits") : status("", "No plan yet"));
  const setupLeft = STEPS.slice(0, 3).filter((s) => !stepDone(s)).length;
  setNav("setup", setupLeft ? `${setupLeft}` : "", "", setupLeft ? `${setupLeft} steps left` : "");
  setNav("access", st.lan.state === "closing" ? "!" : "", "warn", "needs you");
  const failed = !running && st.last_jobs.apply && st.last_jobs.apply.state === "failed" && !st.last_jobs.apply.stopped;
  setNav("activity", failed ? "!" : "", "bad", failed ? "the last setup run failed" : "");
  setCount($("tab-activity-badge"), failed ? "!" : "", "bad", failed ? "the last setup run failed" : "");
  const firing = lastStatus && lastStatus.monitoring === "ok" ? lastStatus.alerts.length : 0;
  const paging = firing && lastStatus.alerts.some((a) => a.severity === "page");
  setNav("alerts", firing ? String(firing) : "", paging ? "bad" : "warn", firing ? `${firing} firing` : "");
  const moreCount = (firing ? 1 : 0) + (st.lan.state === "closing" ? 1 : 0);
  setCount($("tab-more-badge"), moreCount ? String(moreCount) : "", "warn", moreCount ? "something under More needs you" : "");
  // Connected: Tailscale on the server said so when the path was read, or
  // this very page came over it.
  const ts = items.find((i) => i.href === "access/tailscale");
  const p = pathNow();
  const onTailnet = Boolean(p && p.kind && !["waiting", "unread"].includes(p.kind));
  $("tailscale-state").replaceChildren(ts.kind === "done" || onTailnet ? status("good", "Connected") : st.tailscale_key ? status("warn", "Auth key saved: run setup")
    : st.tailscale_client ? status("", "Client saved; add an auth key") : status("", "Not set up"));
  $("ts-dot-client").classList.toggle("done", st.tailscale_client);
  $("ts-dot-key").classList.toggle("done", ts.kind === "done" || onTailnet);
  show("tailscale-client-saved", st.tailscale_client);
  renderDirect(pathNow());
  renderPortMapping(st.port_mapping, pathNow());
  renderLan(st.lan);
  renderSshKeys(st);
  show("vpn-saved", st.tailscale_key);
  // Run setup is the page's main action only when something waits to be
  // built. It opens the review; nothing starts from the top bar.
  const due = setupDue();
  const waits = due.length > 0 && (built() || st.review.ready);
  show("pending-dot", waits && built());
  const urge = !running && waits;
  for (const id of ["run-setup", "run-menu"]) $(id).classList.toggle("primary", urge);
  $("run-setup").title = waits ? `Review and run setup: ${due[0].text}` : "Review and run setup";
  renderUpdates();
  renderReview();
  renderReady();
  // What a finished run says changes with the time that has passed since.
  if (job && job.view && job.view.state !== "running") { drawOutcome(job.node, job.view); paintPill(job.view); }
  renderOverview(items);
  renderRuns();
  renderFiring();
  paintBanner();
  paintChips();
}

// Before Run setup: what is in place, and what is missing.
function renderReady() {
  const items = [];
  const ok = (good, text, href) => items.push(make("li", {className: good ? "ok" : "warn"}, icon(good ? "check" : "alert"),
    href ? make("a", {href, textContent: text}) : make("span", {textContent: text})));
  ok(state.probed, state.probed ? "The hardware is read." : "Read the hardware first.", state.probed ? "" : "#setup/hardware");
  if (choicesDirty()) ok(false, "Your choices are not saved.", "#setup/choices");
  if (state.plan.errors.length) ok(false, "The plan does not fit yet.", "#setup/plan");
  else if (state.plan.text) ok(true, "The plan fits.");
  else ok(false, "Save your choices to make a plan.", "#setup/choices");
  if (!(state.chat && state.chat.signed_in)) ok(false, "No agent yet: setup runs, but nobody looks into a failure for you.", "#agent/settings");
  if (!state.tailscale_client && !(state.lan.via_vpn || state.lan.state === "closed")) ok(false, "No Tailscale client: setup runs, but the server stays off Tailscale.", "#access/tailscale");
  const removals = state.removals || [];
  $("ready-list").replaceChildren(...items);
  show("removals", removals.length > 0);
  $("removals-title").textContent = state.removals_stopped ? `Setup stopped before deleting ${andList(removals)}` : `This run deletes ${andList(removals)}`;
  $("removals-notes").replaceChildren(...removalPoints().map((t) => li(t)));
  $("removals-disks").textContent = removals.length > 1 ? "their disks" : "its disk";
  for (const id of ["removals-it", "removals-it2"]) $(id).textContent = removals.length > 1 ? "them" : "it";
  // The one button that starts setup says what pressing it does.
  const button = $("start-setup");
  const ready = setupReady();
  button.className = `btn ${removals.length ? "danger" : setupDue().length || !built() ? "primary" : ""}`.trim();
  $("start-setup-text").textContent = removals.length ? `Delete ${count(removals.length, "guest")} and start setup` : built() && !setupDue().length ? "Run setup again" : "Start setup";
  $("start-note").textContent = !ready ? "" : built() ? "Unchanged parts are skipped." : "The first run takes about 20 minutes, more with local AI models.";
}

// Why a setup run is due, from the server (App.review): nothing when the
// server follows the saved choices and this NullBill.
function setupDue() { return (state && state.review && state.review.reasons) || []; }
function setupReady() { return Boolean(state && state.review && state.review.ready && state.probed && !state.plan.errors.length); }

// A commit as a line: what it says, when, and, with `tag`, whether setup
// has to follow. `waiting` holds the ids a setup run has not applied yet
// (null: not known); without it every change to what setup builds needs one.
function changeItem(c, tag, waiting) {
  const needs = waiting === undefined || (waiting !== null && waiting.has(c.id));
  const text = needs ? "Needs a setup run" : waiting === null ? "Changes what setup builds" : "Applied by a setup run";
  return make("li", {}, make("span", {className: "change-subject", textContent: c.subject}),
    make("span", {className: "meta"}, make("code", {textContent: c.id.slice(0, 7)}), ` ${day(c.time)}`),
    tag && c.setup ? make("span", {className: `tag${needs ? "" : " done"}`, textContent: text}) : null);
}

// Setup, Review: why a run is due and what it does to the server, before
// the owner starts it.
// What a run does to a machine, as the table says it (nullbill/preview.py).
const WHAT_WORDS = {create: ["good", "Create"], delete: ["bad", "Delete"], change: ["warn", "Change"], again: ["info", "Set up again"]};
// Behind a machine's row: why the run touches it, what that can restart
// and how long its setup took the last time, each on a line of its own.
function machineLines(m, shared) {
  const lines = m.why.map((text) => sentence(text));
  // Only what the row's own few words do not say already.
  if (m.restart_note && m.restart_note !== `${m.effect}.` && !m.restart_note.startsWith("Nothing in it restarts")) lines.push(m.restart_note);
  if (m.last) {
    const when = m.last.at ? ` the last time it ran, ${ago(m.last.at)}` : " the last time it ran";
    const answered = m.last.answered_s ? ` It answered again ${duration(Math.round(m.last.answered_s))} later.` : "";
    lines.push(`Its setup took ${duration(Math.round(m.last.took_s))}${when}.${answered}`);
  }
  if (m.on_demand && m.what !== "delete") lines.push("It runs on demand: if it is off, the run starts it and stops it again when it is done.");
  // Guests of one choice lose the same things: said once, then referred to.
  const text = lines.join("\n");
  if (m.what === "delete" && shared.has(text)) return [`The same as for ${shared.get(text)}, above.`];
  if (m.what === "delete") shared.set(text, m.name);
  return lines;
}
// A machine is one line: its name, what the run does and what happens to it
// in a few words. Why, and the rest, open under it. open: the names whose
// rows the owner opened, kept while the page draws the table again.
function machineRows(m, shared, open, prefix, guest) {
  const [kind, word] = m.what === "again" ? [m.interrupts ? "warn" : "info", "Set up again"] : WHAT_WORDS[m.what];
  const lines = machineLines(m, shared);
  if (guest && m.what === "create") {
    const size = [guest.cores ? count(guest.cores, "core") : "", guest.memory_mib ? `${gib(guest.memory_mib)} of memory` : ""].filter(Boolean);
    lines.push(sentence(`${guest.kind === "lxc" ? "A container" : "A virtual machine"} in the ${guest.zone} zone${size.length ? ` with ${andList(size)}` : ""}`));
  }
  const took = m.last && m.last.took_s !== null && m.last.took_s !== undefined ? duration(Math.round(m.last.took_s)) : "";
  const id = `${prefix}-${m.name.replace(/[^A-Za-z0-9_-]/g, "-")}`;
  const toggle = make("button", {type: "button", className: "icon-btn expand", title: "Why"}, icon("chevron-right"),
    make("span", {className: "sr-only", textContent: `${m.name}: why`}));
  toggle.setAttribute("aria-expanded", open.has(m.name) ? "true" : "false");
  toggle.setAttribute("aria-controls", id);
  toggle.disabled = lines.length === 0;
  const quiet = m.what === "again" && !m.interrupts;
  const row = make("tr", {className: `review-${m.what}${lines.length ? " link-row" : ""}${quiet ? " review-quiet" : ""}`},
    make("td", {}, make("div", {className: "review-name"}, toggle, make("span", {className: "name", textContent: m.name})),
      m.effect ? make("span", {className: "sub effect-narrow", textContent: m.effect}) : null),
    make("td", {}, status(kind, word)),
    make("td", {className: "hide-phone review-effect", textContent: m.effect || ""}),
    make("td", {className: "num muted hide-narrow", textContent: took}));
  const detail = make("tr", {className: "detail", id, hidden: !open.has(m.name)},
    make("td", {colSpan: 4}, make("ul", {className: "review-lines"}, ...lines.map((text) => make("li", {textContent: text})))));
  const flip = () => {
    const on = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", on ? "true" : "false");
    detail.hidden = !on;
    if (on) open.add(m.name); else open.delete(m.name);
  };
  if (lines.length) {
    toggle.addEventListener("click", (event) => { event.stopPropagation(); flip(); });
    row.addEventListener("click", (event) => { if (!event.target.closest("a, button")) flip(); });
  }
  return [row, detail];
}
// What interrupts first, then what is only set up again.
function byImpact(machines) {
  const rank = (m) => ["delete", "create", "change"].indexOf(m.what) + 1 || (m.interrupts ? 4 : 5);
  return machines.filter((m) => ["delete", "create", "change", "again"].includes(m.what)).sort((x, y) => rank(x) - rank(y));
}
const reviewOpen = new Set();
const updateOpen = new Set();
// The review's guests as rows, for a server whose machines could not be
// foreseen: what OpenTofu creates, deletes and changes, and nothing more.
function guestsAsMachines(guests) {
  return guests.map((g) => ({name: g.name, host: false, what: g.change === "keep" ? "unknown" : g.change, why: g.change === "change" ? [andList(g.detail)] : g.detail,
    effect: g.change === "create" ? "New guest" : g.change === "keep" ? "" : (g.detail[0] || "").split(". ")[0].replace(/\.$/, ""),
    interrupts: g.change === "delete", restarts: [], restart_note: "", last: null, on_demand: false}));
}
function renderReview() {
  const r = state.review;
  const due = setupDue();
  const running = SETUP_RUNS.has(runningAction());
  $("review-state").replaceChildren(running ? status("run", "Running") : !setupReady() ? status("", "Not ready")
    : due.length ? status("warn", built() ? "Changes wait" : "Ready to build") : status("good", "Nothing waits"));
  $("review-reasons").replaceChildren(...(due.length
    ? due.map((x) => make("li", {className: "warn"}, icon(x.kind === "first" ? "info" : "alert"), make("span", {textContent: x.text})))
    : [make("li", {className: "ok"}, icon("check"), make("span", {textContent: "Nothing waits: the server follows your saved choices and this version of NullBill. A run now checks every part again and repairs what drifted."}))]));
  const commits = r.commits || [];
  show("review-commits", commits.length > 0);
  $("review-commits-summary").textContent = `The ${commits.length === 1 ? "change" : `${commits.length} changes`} in NullBill this run applies`;
  $("review-commit-list").replaceChildren(...commits.map((c) => changeItem(c, false)));

  const guests = r.guests || [];
  show("review-what", r.ready);
  if (!r.ready) return;
  const seen = r.machines;
  const machines = seen ? seen.machines : guestsAsMachines(guests);
  const by = (what) => machines.filter((m) => m.what === what);
  const [created, deleted, changed, again, alone, unknown] = ["create", "delete", "change", "again", "alone", "unknown"].map(by);
  const erased = (r.disks || []).filter((d) => d.erases);
  const restart = again.filter((m) => m.interrupts);
  const parts = [];
  if (deleted.length) parts.push(`${deleted.length} deleted`);
  if (created.length) parts.push(`${created.length} created`);
  if (changed.length) parts.push(`${changed.length} changed`);
  if (erased.length) parts.push(`${count(erased.length, "disk")} erased`);
  if (restart.length) parts.push(`${restart.length} may restart`);
  if (again.length > restart.length) parts.push(`${again.length - restart.length} set up again, nothing restarts`);
  if (alone.length) parts.push(`${alone.length} left alone`);
  $("review-summary").replaceChildren(...parts.map((text) => make("li", {textContent: text})));
  show("review-unknown", r.known === false);
  // Before the first run that wrote down what each machine was set up with.
  show("review-unseen", built() && (!seen || !seen.known));
  show("review-full", Boolean(seen && seen.full));
  const shared = new Map();
  const guestOf = new Map(guests.map((g) => [g.name, g]));
  const rows = byImpact(machines).flatMap((m) => machineRows(m, shared, reviewOpen, "review-why", guestOf.get(m.name)));
  for (const d of r.disks || []) {
    rows.push(make("tr", {className: d.erases ? "review-delete" : ""},
      make("td", {}, make("div", {className: "review-name"}, make("span", {className: "name", textContent: `Disk ${d.id}`})), make("span", {className: "sub effect-narrow", textContent: d.text})),
      make("td", {}, d.erases ? status("bad", "Erase") : status("", "Disk")),
      make("td", {className: "hide-phone review-effect", textContent: d.text}),
      make("td", {className: "num muted hide-narrow", textContent: d.size_gib ? `${d.size_gib} GiB` : ""})));
  }
  show("review-table", rows.length > 0);
  $("review-rows").replaceChildren(...rows);
  // The machines the run does nothing to, by name: the answer to "what
  // happens to this one" is here as often as in the table.
  const names = (list) => list.map((m) => make("li", {textContent: m.name}));
  show("review-alone", alone.length > 0);
  $("review-alone-title").textContent = `Left alone (${alone.length})`;
  $("review-alone-names").replaceChildren(...names(alone));
  show("review-notknown", unknown.length > 0 && built());
  $("review-notknown-title").textContent = `Not known (${unknown.length})`;
  $("review-notknown-names").replaceChildren(...names(unknown));
  show("review-foot", Boolean(seen && seen.known));
  const sections = r.sections || [];
  show("review-sections", sections.length > 0);
  $("review-sections").textContent = sections.length ? sentence(`The plan also differs in ${andList(sections)}`) : "";
  show("review-diff", Boolean(r.difference));
  const slot = $("review-diff-slot");
  if (r.difference && slot.dataset.shown !== r.id) {
    slot.replaceChildren(diffView(r.difference));
    slot.dataset.shown = r.id;
  }
}

// Updates: NullBill's own version, whether the server was built with it,
// and the systems' security updates.
function renderUpdates() {
  const v = state.version;
  const waiting = v ? v.server.changes : [];
  const n = v ? v.available.length : 0;
  const behind = Boolean(v && v.server.follows === false);
  show("updates-none", !v);
  show("updates-body", Boolean(v));
  show("version-line", Boolean(v));
  show("update-btn", n > 0);
  $("update-count").textContent = n ? String(n) : "";
  $("update-btn").title = n ? `${count(n, "change")} to NullBill wait to be installed` : "";
  setNav("updates", n ? String(n) : behind ? "!" : "", n ? "run" : "warn", n ? `${count(n, "change")} available` : behind ? "the server waits for a setup run" : "");
  setCount($("more-updates-badge"), n ? String(n) : behind ? "!" : "", n ? "run" : "warn", n ? `${count(n, "change")} available` : behind ? "the server waits for a setup run" : "");
  if (!v) return;
  const short = v.current.id.slice(0, 7);
  $("version-line").replaceChildren(make("span", {textContent: `NullBill ${short}`}),
    ...(n ? [make("span", {className: "dot info", ariaHidden: "true"}), make("span", {className: "sr-only", textContent: `, ${count(n, "change")} available`})] : []));
  $("version-current").replaceChildren(make("code", {textContent: short}), make("span", {className: "meta", textContent: ` ${day(v.current.time)} · ${v.current.subject}`}));
  const checked = v.checked ? `checked ${ago(v.checked)}` : "not checked yet";
  $("version-newest").replaceChildren(v.check_error ? status("warn", "Could not check", sentence(v.check_error))
    : n ? status("info", `${count(n, "change")} available`, checked)
    : v.checked ? status("good", "Up to date", checked) : status("", "Not checked yet"));
  const now = $("update-now");
  now.hidden = n === 0;
  $("update-check").classList.toggle("primary", false);
  show("update-available", n > 0);
  $("update-available-title").textContent = n ? `What the update brings (${needSetup(v.available)})` : "";
  $("update-available-list").replaceChildren(...v.available.map((c) => changeItem(c, true)));
  renderUpdateEffect(n > 0 ? v.effect : null);
  // The server.
  const b = v.server.built;
  $("server-built").replaceChildren(...(b ? [make("code", {textContent: b.id.slice(0, 7)}), make("span", {className: "meta", textContent: ` ${day(b.time)} · ${b.subject}`})]
    : [make("span", {className: "meta", textContent: built() ? "Not recorded; the next setup run records it." : "Nothing is built yet."})]));
  $("server-follows").replaceChildren(!built() ? status("", "Not built yet")
    : behind ? status("warn", waiting.length ? `${count(waiting.length, "change")} wait for a setup run` : "Waits for a setup run")
    : v.server.follows ? status("good", "Built with this version") : status("", "Not known"));
  show("server-run", behind && !SETUP_RUNS.has(runningAction()));
  show("server-waiting", waiting.length > 0);
  $("server-waiting-list").replaceChildren(...waiting.map((c) => changeItem(c, false)));
  // What the last update brought.
  show("update-last", Boolean(v.last));
  if (v.last) {
    const all = v.last.changes;
    const k = all.filter((c) => c.setup).length;
    const builds = k === 0 ? "none of them to what setup builds" : k === all.length ? "all to what setup builds" : `${k} of them to what setup builds`;
    $("update-last-text").textContent = `${ago(v.last.at)[0].toUpperCase()}${ago(v.last.at).slice(1)}, from ${v.last.from.slice(0, 7)} to ${short}: ${count(all.length, "change")}, ${all.length === 1 ? (k ? "to what setup builds" : "not to what setup builds") : builds}.`;
    const pending = behind && waiting.length ? new Set(waiting.map((c) => c.id)) : v.server.follows ? new Set() : null;
    $("update-last-list").replaceChildren(...all.map((c) => changeItem(c, built(), pending)));
  }
  renderSystemUpdates();
}
// What the waiting update does to each machine: read from the files it
// changes (nullbill/preview.py), before anything of it is installed.
function renderUpdateEffect(e) {
  show("update-effect", Boolean(e));
  if (!e) return;
  const again = e.machines.filter((m) => m.what === "again");
  const alone = e.machines.filter((m) => m.what !== "again");
  const restart = again.filter((m) => m.interrupts).length;
  $("update-effect-text").textContent = !again.length ? "Updating restarts only this page. By the files it changes, the setup run after it sets no machine up again."
    : `Updating restarts only this page. The setup run after it sets ${count(again.length, "machine")} up again${restart ? `; ${restart === again.length ? (restart === 1 ? "it" : "all") : restart} may restart` : ", and nothing restarts"}.`;
  show("update-effect-table", again.length > 0);
  $("update-effect-rows").replaceChildren(...byImpact(again).flatMap((m) => machineRows(m, new Map(), updateOpen, "update-why")));
  show("update-effect-alone", alone.length > 0);
  $("update-effect-alone-title").textContent = `Left alone (${alone.length})`;
  $("update-effect-alone-names").replaceChildren(...alone.map((m) => make("li", {textContent: m.name})));
  show("update-effect-code", Boolean(e.code));
}
function needSetup(changes) {
  const k = changes.filter((c) => c.setup).length;
  return k === 0 ? "none needs a setup run" : k === changes.length ? (k === 1 ? "it needs a setup run" : "all need a setup run") : `${k} of them need a setup run`;
}
function renderSystemUpdates() {
  const s = lastStatus;
  if (!s || s.monitoring !== "ok") {
    $("system-updates").replaceChildren(status("", built() ? "Monitoring does not answer" : "Shown once the server is built"));
    show("system-restart-row", false);
    return;
  }
  $("system-updates").replaceChildren(s.security_waiting.length ? status("warn", `Waiting on ${s.security_waiting.length}`, s.security_waiting.join(", ")) : status("good", "Up to date", "installed nightly"));
  show("system-restart-row", s.reboot_waiting.length > 0);
  $("system-restart").replaceChildren(status("warn", "Needed", s.reboot_waiting.join(", ")));
}

// What deleting the waiting guests loses, one line per choice they belong to.
function removalPoints() {
  const groups = new Map();
  for (const name of state.removals || []) {
    const note = (state.removal_notes || {})[name] || "";
    groups.set(note, [...(groups.get(note) || []), name]);
  }
  return [...groups].map(([note, names]) => `${andList(names)}: ${note}`);
}

// Setup cannot go on without deleting what the choices no longer plan, so
// every way to start it asks here first. True when the owner said delete.
async function confirmRemovals(button) {
  const names = andList(state.removals || []);
  const many = state.removals.length > 1;
  const ok = await confirmDialog({
    title: `Delete ${names}?`,
    text: `Everything on ${many ? "their disks" : "its disk"} is erased, for good. Then setup runs. To keep ${many ? "them" : "it"}, cancel, turn ${many ? "them" : "it"} on again under Setup, What to run, and save.`,
    points: removalPoints(), yes: "Delete and run setup", word: "delete",
  });
  button.focus();
  return ok;
}

// Overview: what needs the owner, health, services and recent runs.
const DUE_TITLES = {
  choices: "Your saved choices are not built yet",
  update: "The last update has not reached the server yet",
  key: "A Tailscale auth key waits for a setup run",
};
function attentionItems() {
  const out = [];
  const signed = Boolean(state.chat && state.chat.signed_in);
  const add = (kind, title, sub, actions) => out.push({kind, title, sub, actions});
  const link = (text, href) => make("a", {className: "btn small", href, textContent: text});
  const ask = (text, prompt) => {
    const b = make("button", {type: "button", className: "btn small", textContent: text});
    b.addEventListener("click", () => askAgent(prompt));
    return b;
  };
  const pending = chat.pending.length;
  if (pending) {
    const card = latestRequest();
    const b = make("button", {type: "button", className: "btn small primary", textContent: "Review"});
    b.addEventListener("click", () => showRequest());
    add("warn", pending > 1 ? `The agent waits for your OK on ${pending} requests` : "The agent waits for your OK",
      card ? card.querySelector(".card-title").textContent : "", [b]);
  }
  const apply = state.last_jobs.apply;
  if (apply && apply.state === "failed" && !apply.stopped && !runningAction()) {
    const view = job && job.id === apply.id ? job.view : null;
    const e = view && view.progress.error;
    add("bad", e && e.stage_text ? `Setup failed at "${e.stage_text}"` : "The last setup run failed", e ? e.message : "",
      [link("Open the run", `#activity/${apply.id}`), signed ? ask("Ask the agent", `The last setup run failed${e && e.stage_text ? ` at "${e.stage_text}"` : ""}. Find out why and propose the fix.`) : null].filter(Boolean));
  }
  if ((state.removals || []).length && state.removals_stopped) add("bad", `Setup stopped before deleting ${andList(state.removals)}`, "Keep them or confirm the deletion.", [link("Decide", "#setup/build")]);
  const s = lastStatus;
  if (s && s.monitoring === "ok") {
    for (const a of s.alerts) add(a.severity === "page" ? "bad" : "warn", alertText(a).what, alertText(a).todo || a.name, [link("Alerts", "#alerts")]);
    if (s.down.length) add("bad", `${s.down.length} system${s.down.length > 1 ? "s do" : " does"} not answer`, s.down.join(", "),
      signed ? [ask("Ask the agent", "Look at the systems that do not answer, find the cause and propose the fix.")] : []);
    if (s.reboot_waiting.length) add("warn", "A restart would finish installed updates", s.reboot_waiting.join(", "), []);
  } else if (s && built() && s.monitoring !== "not planned" && !SETUP_RUNS.has(runningAction())) {
    add("warn", "Monitoring does not answer", "If setup just finished, wait a minute.", signed ? [ask("Ask the agent", "Monitoring does not answer. Find out why.")] : []);
  }
  // A failed run and guests waiting to be deleted have rows of their own.
  const due = setupDue().filter((x) => DUE_TITLES[x.kind]);
  if (built() && due.length && !SETUP_RUNS.has(runningAction())) {
    add("warn", due.length > 1 ? "The server waits for a setup run" : DUE_TITLES[due[0].kind], due.map((x) => x.text).join(" "),
      [make("a", {className: "btn small primary", href: "#setup/build", textContent: "Review and run"})]);
  }
  const fresh = (state.version && state.version.available) || [];
  if (fresh.length && runningAction() !== "upgrade") {
    add("info", "A NullBill update is available", `${count(fresh.length, "change")}, ${needSetup(fresh)}.`, [link("See the update", "#updates")]);
  }
  if (built() && state.backups.ready && !state.backups.on && !state.backups.built) {
    add("warn", "Nothing on this server is backed up", "Backups can start on the server's own disk; no second disk is needed.", [link("Turn on backups", "#backups")]);
  }
  if (state.agent_powers) add("warn", "The operator agent has root on Proxmox", "The checks warn while this is on.", [link("Agent settings", "#agent/settings")]);
  // What only the owner stops, left on and unused for as long as its rule says.
  for (const a of (s && s.monitoring === "ok" && s.asks) || []) {
    const fb = make("p", {className: "feedback"});
    const act = (text, body) => {
      const b = make("button", {type: "button", className: "btn small", textContent: text});
      b.addEventListener("click", () => demandAct(b, fb, {name: a.name, ...body}));
      return b;
    };
    const planned = ((state.on_demand || {}).groups || []).find((g) => g.name === a.name);
    add("warn", `${a.title} is on and has not been used for ${span(Math.floor(a.idle_s / 60))}`,
      planned ? `Stopping it frees ${gib(planned.memory_mib)}; nothing is deleted.` : "It runs on demand.",
      [fb, act("Keep on", {action: "keep", on: true}), act("Stop", {action: "stop"})]);
  }
  return out;
}

function renderOverview(items) {
  const st = state;
  const hw = st.hardware;
  const install = st.install || {};
  $("host-line").textContent = [install.node, install.host].filter(Boolean).join(" · ");
  $("overview-sub").textContent = hw ? [`${hw.cpu || "CPU"}, ${hw.threads} threads`, hw.memory_mib ? gib(hw.memory_mib) : "", hw.pve_version ? `Proxmox VE ${hw.pve_version}` : ""].filter(Boolean).join(" · ")
    : install.host ? `Proxmox at ${install.host}. Read the hardware to begin.` : "";
  // Needs attention.
  const att = attentionItems();
  show("attention", att.length > 0);
  $("attention-list").replaceChildren(...att.map((a) => make("li", {},
    make("span", {className: `dot ${a.kind}`, ariaHidden: "true"}),
    make("span", {className: "row-text"}, make("span", {className: "row-title", textContent: a.title}),
      make("span", {className: "sr-only", textContent: a.kind === "bad" ? " (problem)" : a.kind === "info" ? " (note)" : " (warning)"}),
      a.sub ? make("span", {className: "row-sub", textContent: ` ${a.sub}`}) : null),
    make("span", {className: "row"}, ...a.actions))));
  // Health, once something is built.
  const s = lastStatus;
  show("health", built());
  if (built()) {
    const stats = [];
    const cell = (label, value, kind, sub, href) => {
      const inner = make("span", {className: "status-val"}, kind !== null ? make("span", {className: `dot ${kind}`, ariaHidden: "true"}) : null, value);
      stats.push(make("div", {}, make("dt", {textContent: label}),
        make("dd", {}, href ? make("a", {href}, inner) : inner), sub ? make("dd", {className: "sub", textContent: sub, title: sub}) : null));
    };
    if (s && s.monitoring === "ok") {
      const off = s.off || [];
      cell("Systems", `${s.targets - s.down.length} of ${s.targets} up`, s.down.length ? "bad" : "good",
        s.down.length ? `Down: ${s.down.join(", ")}` : off.length ? `Off on demand: ${off.join(", ")}` : "", off.length && !s.down.length ? "#services" : null);
      cell("Alerts", s.alerts.length ? `${s.alerts.length} firing` : "None firing", s.alerts.length ? (s.alerts.some((a) => a.severity === "page") ? "bad" : "warn") : "good", "", "#alerts");
      cell("Security updates", s.security_waiting.length ? `${s.security_waiting.length} waiting` : "Up to date", s.security_waiting.length ? "warn" : "good",
        s.security_waiting.length ? s.security_waiting.join(", ") : "Installed nightly");
    } else {
      cell("Monitoring", !s ? "Asking…" : s.monitoring === "not planned" ? "Not planned" : "Not answering", !s ? "" : s.monitoring === "not planned" ? "" : "warn", "");
    }
    const [bKind, bWord, bSub] = backupsWord(st.backups);
    cell("Backups", bWord, bKind, bSub, "#backups");
    const lan = st.lan.state;
    cell("Home network", lan === "closed" ? "Closed" : lan === "closing" ? "Closing" : "Open", lan === "closed" ? "good" : "warn",
      lan === "closed" ? "Proxmox only over Tailscale" : "Proxmox answers on your LAN", "#access/lan");
    // The checks of the last run that got as far as them: a setup run ends
    // with them. What they found then, with when; nothing checks in between.
    const c = st.checks;
    const checking = runningAction();
    const n = c ? c.counts : null;
    const found = !n ? "" : [n.FAIL ? `${n.FAIL} failed` : "", n.DEGRADED ? `${n.DEGRADED} degraded` : "", n.SKIP ? `${n.SKIP} skipped` : ""].filter(Boolean).join(", ");
    cell("Checks", checking === "verify" ? "Running" : !c ? "Not run" : n.FAIL ? "Failed" : n.DEGRADED ? "Passed, with warnings" : "Passed",
      checking === "verify" ? "run" : !c ? "" : n.FAIL ? "bad" : n.DEGRADED ? "warn" : "good",
      c ? [found || `${n.PASS} passed`, `${c.action === "verify" || c.only ? "run" : "in the setup run"} ${ago(c.at)}`].join(" · ") : "", c ? `#activity/${c.id}` : null);
    $("stats").replaceChildren(...stats);
    $("health-meta").textContent = s && s.at ? `${s.monitoring === "ok" ? "From Prometheus, read" : "Asked"} ${readAt(s.at)}` : "";
  }
  // Services and recent runs.
  const services = (st.services && st.services.items) || [];
  show("ov-services", services.length > 0 && built());
  $("ov-services-rows").replaceChildren(...services.slice(0, 6).map((sv) => {
    const [kind, word] = serviceState(sv.watch, sv.watch || sv.name);
    const off = Boolean((demandOf(sv.watch || sv.name) || {}).off);
    const address = sv.url || (sv.addresses || [])[0] || "";
    return make("tr", {},
      make("td", {className: "name"}, make("a", {href: "#services", textContent: sv.title})),
      make("td", {}, status(kind, word)),
      make("td", {className: "hide-phone mono", textContent: address.replace(/^[A-Z][^:]{0,40}: /, "")}),
      make("td", {className: "actions"}, off ? make("a", {className: "btn small", href: "#services", textContent: "Start"}) : openLink(sv)));
  }));
  const runs = st.runs || [];
  show("ov-runs", runs.length > 0);
  $("ov-runs-rows").replaceChildren(...runs.slice(0, 5).map(runRow));
}

// One line across the top for what cannot wait.
function paintBanner() {
  if (!state) return;
  let text = "";
  let href = "";
  let label = "Open";
  if (state.lan.state === "closing" && lanLeft() !== 0) {
    text = state.lan.via_vpn ? `Press Keep it closed ${lanLeftText()}, or the home network opens again by itself.`
      : `Open this page over Tailscale and press Keep it closed ${lanLeftText()}.`;
    href = "#access/lan";
  } else if (chat.pending.length && !chatVisible() && currentView !== "overview") {
    text = chat.pending.length > 1 ? `The agent waits for your OK on ${chat.pending.length} requests.` : "The agent waits for your OK.";
    href = "#agent/chat";
    label = "Review";
  }
  show("banner", Boolean(text));
  $("banner-text").textContent = text;
  $("banner-link").href = href || "#overview";
  $("banner-link").textContent = label;
  $("banner-link").dataset.request = href === "#agent/chat" ? "1" : "";
}

// Services.
const secrets = {};
async function secret(name) {
  if (!(name in secrets)) secrets[name] = (await api("/api/password-reveal", {name})).password;
  return secrets[name];
}
async function copyText(text) { await navigator.clipboard.writeText(text); }

// Where the clipboard is refused (an insecure origin, an old browser), the
// text is selected instead so Ctrl+C or the phone's Copy takes it.
function selectAll(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

// A block of output that scrolls on its own, with a button that copies all
// of it: the whole output from the server when the block shows only its end.
function outputBox(pre, full) {
  pre.tabIndex = 0;
  const label = "Copy all of it";
  const button = make("button", {type: "button", className: "icon-btn out-copy", title: label, ariaLabel: label}, icon("copy"));
  button.addEventListener("click", async () => {
    let text = pre.textContent;
    try {
      if (full) text = (await api(`/api/chat/output?id=${encodeURIComponent(full)}`)).text;
      await copyText(text);
      button.classList.add("done");
      button.replaceChildren(icon("check"));
      announce("Copied.");
      setTimeout(() => { button.classList.remove("done"); button.replaceChildren(icon("copy")); }, 1500);
    } catch (error) {
      if (error instanceof ApiError && full) { toast(error.message); return; }
      selectAll(pre);
      toast("Copying is blocked here. The text is selected: copy it with Ctrl+C or your phone's Copy.");
    }
  });
  const box = make("div", {className: "out"});
  if (pre.parentNode) pre.replaceWith(box);
  box.append(pre, button);
  return box;
}

// The server cuts long output to its end and says so on the first line.
function shownOutput(text, full) {
  const pre = make("pre", {});
  const nodes = [];
  if (full) {
    const [first, ...rest] = String(text).split("\n");
    const cut = /^\[(\d+) characters cut\]$/.exec(first);
    pre.textContent = cut ? rest.join("\n") : text;
    if (cut) nodes.push(make("p", {className: "out-note", textContent: `Shows the end. Copy takes all of it, ${Number(cut[1]).toLocaleString()} characters more.`}));
  } else {
    pre.textContent = text;
  }
  return [...nodes, outputBox(pre, full)];
}

async function copyConversation(button) {
  try {
    const {text} = await api("/api/chat/transcript");
    try {
      await copyText(text);
      toast("Copied the conversation, every output whole.");
    } catch {
      const area = make("textarea", {value: text, readOnly: true, className: "copy-area"});
      addToLog(area);
      area.select();
      toast("Copying is blocked here. The conversation is selected at the end of the chat: copy it by hand.");
    }
  } catch (error) {
    toast(error.message);
  }
  if (button) button.focus();
}

function copyButton(label, get, feedback) {
  const button = make("button", {type: "button", className: "icon-btn", title: label, ariaLabel: label}, icon("copy"));
  button.addEventListener("click", () => press(button, feedback, "Copying…", async () => {
    try {
      await copyText(await get());
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError("Copying is blocked here; select the text and copy it by hand.", 400);
    }
    announce("Copied.");
    return "Copied.";
  }));
  return button;
}

// On demand. A service's guest may be off on purpose: the live answer from
// the host says so at once, Prometheus within a minute or two.
function demandOf(watch) {
  if (!watch || !state) return null;
  const live = demand && demand.live ? demand.groups : [];
  for (const g of live) {
    const guest = g.guests.find((m) => m.name === watch);
    if (!guest || g.state === "pending") continue;
    // A pool has no guest of its own to be off: it follows its workload.
    const off = guest.state ? guest.state !== "running" : g.state === "off";
    return {name: g.name, title: g.title, off, acting: g.acting || "", live: true};
  }
  const planned = ((state.on_demand || {}).groups || []).find((g) => g.guests.includes(watch));
  if (!planned || !lastStatus || lastStatus.monitoring !== "ok") return null;
  return {name: planned.name, title: planned.title, off: (lastStatus.off || []).includes(watch), acting: "", live: false};
}

// watch: the name monitoring knows the service's guest by; guest: the guest
// itself, which the host may know more about than monitoring does yet.
function serviceState(watch, guest = watch) {
  const ok = lastStatus && lastStatus.monitoring === "ok";
  if (!built()) return ["", "Not built yet"];
  const d = demandOf(guest);
  if (d && d.acting) return ["run", d.acting === "starting" ? "Starting" : "Stopping"];
  if (d && d.off) return ["", "Off, on demand"];
  // Just started, or a guest monitoring cannot ask: the host says it runs.
  const heard = Boolean(watch) && ok && (lastStatus.answering.includes(watch) || lastStatus.down.includes(watch));
  if (d && d.live && !heard) return ["good", "On"];
  if (!watch) return ["", "No status"];
  if (!ok) return ["", lastStatus ? "No status" : "Asking…"];
  if (lastStatus.answering.includes(watch)) return ["good", "Running"];
  if (lastStatus.down.includes(watch)) return ["bad", "Not answering"];
  return ["", "No status"];
}

function openLink(s) {
  if (!s.url || !built()) return null;
  return make("a", {className: "btn small", href: s.url, target: "_blank", rel: "noopener noreferrer"},
    "Open", make("span", {className: "sr-only", textContent: ` ${s.title} in a new tab`}), icon("external"));
}

function serviceDetail(s, feedback) {
  const rows = [];
  const kv = (label, value, ...buttons) => rows.push(make("dt", {textContent: label}), make("dd", {}, value, ...buttons));
  if (s.url) kv("Address", make("code", {textContent: s.url}), copyButton(`Copy ${s.title} address`, () => s.url, feedback));
  for (const a of s.addresses || []) {
    const m = /^([A-Z][^:]{0,40}): (.+)$/.exec(a);
    const [label, value] = m ? [m[1], m[2]] : ["Address", a];
    kv(label, make("code", {textContent: value}), copyButton(`Copy ${s.title}: ${label.toLowerCase()}`, () => value, feedback));
  }
  if (s.user) kv("User", make("code", {textContent: s.user}), copyButton(`Copy ${s.title} user`, () => s.user, feedback));
  if (s.password) {
    const word = s.password === "model-gateway" ? "key" : "password";
    const value = make("code", {className: "masked", textContent: "••••••••••"});
    value.id = `secret-${s.name}`;
    const reveal = make("button", {type: "button", className: "icon-btn", title: `Show ${word}`, ariaLabel: `Show ${s.title} ${word}`}, icon("eye"));
    reveal.setAttribute("aria-pressed", "false");
    reveal.setAttribute("aria-controls", value.id);
    reveal.addEventListener("click", () => press(reveal, feedback, "Fetching…", async () => {
      const shown = reveal.getAttribute("aria-pressed") === "true";
      value.textContent = shown ? "••••••••••" : await secret(s.password);
      value.className = shown ? "masked" : "";
      reveal.setAttribute("aria-pressed", shown ? "false" : "true");
      reveal.replaceChildren(icon(shown ? "eye" : "eye-off"));
      reveal.title = shown ? `Show ${word}` : `Hide ${word}`;
      announce(shown ? "Hidden." : "Shown.");
      return "";
    }));
    kv(word === "key" ? "API key" : "Password", value, reveal, copyButton(`Copy ${s.title} ${word}`, () => secret(s.password), feedback));
  }
  return [
    s.what ? make("p", {className: "svc-note", textContent: s.what, style: "margin-top:0;margin-bottom:10px"}) : null,
    rows.length ? make("dl", {className: "kv"}, ...rows) : null,
    s.password_note ? make("p", {className: "svc-note", textContent: s.password_note}) : null,
    s.game ? gameRoom(s) : null,
    serviceSteps(s, feedback),
    feedback,
  ];
}

// A game server as it runs: who plays, its worlds, its console. Asked of
// the server when its row is opened, and again after every change.
const gameViews = new Map();   // name -> the last /api/game view
function bytes(n) {
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

function gameRoom(s) {
  const room = make("div", {className: "game-room"});
  room.dataset.game = s.name;
  const view = gameViews.get(s.name);
  if (view) paintGameRoom(room, view);
  else room.append(make("p", {className: "svc-note", textContent: built() ? "Asking the server…" : "Its worlds show here once setup has made the server."}));
  // The server's guest is asked only for a row that is open.
  if (built() && expanded.has(s.name)) loadGameRoom(s.name);
  return room;
}

async function loadGameRoom(name, fresh) {
  let view;
  try {
    view = (await api(`/api/game?name=${encodeURIComponent(name)}${fresh ? "&fresh=1" : ""}`)).view;
  } catch (error) {
    view = {name, live: false, why: error.message, new_world: []};
  }
  setGameRoom(view);
}

function setGameRoom(view) {
  gameViews.set(view.name, view);
  const room = document.querySelector(`.game-room[data-game="${view.name}"]`);
  // An open form keeps what was typed: only the lists are painted anew.
  if (room) paintGameRoom(room, view);
}

function gameAct(button, feedback, name, body, busy, done) {
  return press(button, feedback, busy, async () => {
    let answer = await api("/api/game", {name, ...body});
    if (!answer.ok && answer.players) {
      const n = answer.players;
      const ok = await confirmDialog({title: `Disconnect ${n} ${n === 1 ? "player" : "players"}?`,
        text: `${n === 1 ? "A player is" : `${n} players are`} in ${answer.title}. The server stops for this and they are disconnected. Their progress is saved first.`,
        yes: "Disconnect and go on"});
      if (!ok) return "";
      answer = await api("/api/game", {name, ...body, kick: true});
      if (!answer.ok) throw new ApiError("Someone joined meanwhile. Try again.", 409);
    }
    setGameRoom(answer.view);
    return typeof done === "function" ? done(answer) : done;
  });
}

function paintGameRoom(room, v) {
  const word = v.word || "world";
  const Word = word[0].toUpperCase() + word.slice(1);
  if (!v.live) {
    room.replaceChildren(make("p", {className: "svc-note", textContent: v.why || "The server does not answer."}));
    return;
  }
  // Built once per row, then repainted, so forms and focus survive.
  if (!room.querySelector(".game-head")) {
    const said = make("p", {className: "feedback game-said"});
    said.setAttribute("role", "status");
    const restart = make("button", {type: "button", className: "btn small"}, icon("refresh"), "Restart", make("span", {className: "sr-only", textContent: ` ${v.title}`}));
    restart.addEventListener("click", () => gameAct(restart, said, v.name, {action: "restart"}, "Restarting…", "Restarted."));
    const fresh = make("button", {type: "button", className: "icon-btn", title: "Ask the server again", ariaLabel: `Ask ${v.title} again`}, icon("refresh"));
    fresh.addEventListener("click", () => press(fresh, said, "Asking…", async () => { await loadGameRoom(v.name, true); return ""; }));
    const add = make("button", {type: "button", className: "btn small"}, icon("plus"), `New ${word}`);
    const upload = make("button", {type: "button", className: "btn small"}, icon("upload"), `Upload a ${word}`);
    const forms = make("div", {className: "game-forms"});
    add.addEventListener("click", () => forms.replaceChildren(newWorldForm(room, forms)));
    upload.addEventListener("click", () => forms.replaceChildren(uploadWorldForm(room, forms)));
    const line = make("input", {type: "text", maxLength: 200, autocomplete: "off", spellcheck: false, className: "console-line", ariaLabel: `A line for ${v.title}'s console`});
    const send = make("button", {type: "submit", className: "btn small"}, "Send");
    const consoleSaid = make("p", {className: "feedback"});
    consoleSaid.setAttribute("role", "status");
    const form = make("form", {className: "console-form"}, line, send);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      gameAct(send, consoleSaid, v.name, {action: "console", line: line.value}, "Sending…", () => {
        line.value = "";
        // The server answers in its log a moment later.
        setTimeout(() => loadGameRoom(v.name, true), 1500);
        return "Sent. Its answer shows in the log.";
      });
    });
    room.replaceChildren(
      make("div", {className: "game-head"}, make("span", {className: "game-state"}), make("span", {className: "row"}, fresh, restart)),
      said,
      make("h3", {textContent: `${Word}s`}),
      make("ul", {className: "world-list"}),
      make("div", {className: "row game-buttons"}, add, upload, make("span", {className: "hint game-room-left"})),
      forms,
      make("h3", {textContent: "Console"}),
      make("pre", {className: "game-log", tabIndex: 0, ariaLabel: `${v.title}'s log`}),
      form, consoleSaid);
  }
  const who = v.names && v.names.length ? `: ${v.names.join(", ")}${v.names.length < v.players ? " and others" : ""}` : "";
  const state = v.acting ? status("run", sentence(v.acting))
    : v.running ? status("good", "Running", `${v.players} ${v.players === 1 ? "player" : "players"}${who}`)
    : status(v.active ? "warn" : "bad", v.active ? "Starting" : "Stopped", v.active ? "It does not answer yet." : "Restart starts it.");
  room.querySelector(".game-state").replaceChildren(state);
  const said = room.querySelector(".game-said");
  const list = room.querySelector(".world-list");
  list.replaceChildren(...(v.worlds || []).map((w) => worldRow(v, w, said)));
  const full = (v.worlds || []).length >= v.most;
  room.querySelector(".game-room-left").textContent = full ? `The server keeps at most ${v.most}. Delete one to add another.` : `${bytes(v.free_bytes)} free on its disk`;
  for (const b of room.querySelectorAll(".game-buttons .btn")) {
    if (full) { b.setAttribute("aria-disabled", "true"); b.dataset.why = `Delete a ${word} first.`; }
    else { b.removeAttribute("aria-disabled"); delete b.dataset.why; }
  }
  const log = room.querySelector(".game-log");
  const text = (v.log || []).join("\n") || "Nothing in its log yet.";
  if (log.textContent !== text) {
    log.textContent = text;
    log.scrollTop = log.scrollHeight;
  }
  room.querySelector(".console-line").placeholder = v.say;
}

function worldRow(v, w, said) {
  const facts = [bytes(w.bytes), w.origin + (w.made ? ` ${clock(w.made)}` : ""), ...w.options].filter(Boolean).join(" · ");
  const name = make("div", {className: "world-name"}, make("span", {className: "name", textContent: w.title}),
    w.active ? make("span", {className: "badge good", textContent: "Runs now"}) : null,
    make("span", {className: "sub", textContent: facts}));
  const button = (text, cls, onClick, glyph) => {
    const b = make("button", {type: "button", className: `btn small ${cls || ""}`.trim()}, glyph ? icon(glyph) : null, text, make("span", {className: "sr-only", textContent: ` ${w.title}`}));
    b.addEventListener("click", () => onClick(b));
    return b;
  };
  const actions = make("span", {className: "row world-actions"});
  if (!w.active) {
    actions.append(button("Run this one", "", (b) => gameAct(b, said, v.name, {action: "use", id: w.id}, `Starting ${w.title}…`, `${w.title} runs now.`)));
  }
  const get = make("a", {className: "btn small", href: `/api/game/world?name=${encodeURIComponent(v.name)}&id=${w.id}`, download: ""}, icon("download"), "Download", make("span", {className: "sr-only", textContent: ` ${w.title}`}));
  actions.append(get, button("Rename", "ghost", () => {
    const input = make("input", {type: "text", maxLength: 40, value: w.title, autocomplete: "off", ariaLabel: `New name for ${w.title}`});
    const save = make("button", {type: "submit", className: "btn small primary"}, "Save");
    const cancel = make("button", {type: "button", className: "btn small ghost"}, "Cancel");
    const form = make("form", {className: "row world-rename"}, input, save, cancel);
    cancel.addEventListener("click", () => setGameRoom(gameViews.get(v.name)));
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      gameAct(save, said, v.name, {action: "rename", id: w.id, title: input.value}, "Renaming…", "Renamed.");
    });
    name.replaceChildren(form);
    input.focus();
    input.select();
  }));
  if (!w.active) {
    actions.append(button("Delete", "ghost danger", async (b) => {
      const ok = await confirmDialog({title: `Delete ${w.title}?`, text: `The ${v.word} and everything built in it is gone for good. Download it first if you may want it back.`, yes: "Delete it"});
      if (ok) gameAct(b, said, v.name, {action: "delete", id: w.id}, "Deleting…", `Deleted ${w.title}.`);
    }));
  }
  return make("li", {className: w.active ? "active" : ""}, name, actions);
}

function worldOptionFields(v) {
  return v.new_world.map((o) => {
    let control;
    if (o.type === "choice") control = make("select", {}, ...o.choices.map(([value, label]) => make("option", {value: String(value), textContent: label})));
    else if (o.type === "switch") control = make("input", {type: "checkbox"});
    else if (o.type === "number") control = make("input", {type: "number", min: String(o.min), max: String(o.max), step: "1", inputMode: "numeric"});
    else control = make("input", {type: "text", maxLength: 100, autocomplete: "off", spellcheck: false});
    control.dataset.key = o.key;
    if (control.type === "checkbox") control.checked = Boolean(o.value); else control.value = String(o.value);
    const text = make("span", {className: "label", textContent: o.label});
    return make("label", {className: `game-field ${o.type === "switch" ? "check" : ""}`.trim()}, ...(o.type === "switch" ? [control, text] : [text, control]));
  });
}

function newWorldForm(room, forms) {
  const v = gameViews.get(room.dataset.game);
  const running = (v.worlds || []).find((w) => w.active);
  const title = make("input", {type: "text", maxLength: 40, autocomplete: "off", required: true});
  const go = make("button", {type: "submit", className: "btn small primary"}, `Make it and run it`);
  const cancel = make("button", {type: "button", className: "btn small ghost"}, "Cancel");
  const said = make("p", {className: "feedback"});
  said.setAttribute("role", "status");
  const fields = make("div", {className: "game-fields"}, make("label", {className: "game-field"}, make("span", {className: "label", textContent: "Name"}), title), ...worldOptionFields(v));
  const form = make("form", {className: "game-form"},
    make("p", {className: "help", textContent: `The server stops for a moment and starts with the new ${v.word}.${running ? ` ${running.title} is kept in the list, not deleted.` : ""}`}),
    fields, make("div", {className: "row"}, go, cancel), said);
  cancel.addEventListener("click", () => forms.replaceChildren());
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const options = {};
    for (const control of fields.querySelectorAll("[data-key]")) options[control.dataset.key] = control.type === "checkbox" ? control.checked : control.value;
    gameAct(go, said, v.name, {action: "new", title: title.value, options}, `Making it. A large ${v.word} can take a few minutes…`, () => {
      forms.replaceChildren();
      say(room.querySelector(".game-said"), "ok", `${title.value.trim()} runs now.`);
      return "";
    });
  });
  setTimeout(() => title.focus());
  return form;
}

// A world file goes up as the request's body, with its progress shown.
function uploadWorldForm(room, forms) {
  const v = gameViews.get(room.dataset.game);
  const title = make("input", {type: "text", maxLength: 40, autocomplete: "off", required: true});
  const file = make("input", {type: "file", required: true, accept: v.kind === "terraria" ? ".wld" : ".zip"});
  const go = make("button", {type: "submit", className: "btn small primary"}, "Upload");
  const cancel = make("button", {type: "button", className: "btn small ghost"}, "Cancel");
  const said = make("p", {className: "feedback"});
  said.setAttribute("role", "status");
  const form = make("form", {className: "game-form"},
    make("p", {className: "help", textContent: `Choose ${v.upload}. It joins the list; the ${v.word} that runs stays until you press Run this one.`}),
    make("div", {className: "game-fields"},
      make("label", {className: "game-field"}, make("span", {className: "label", textContent: "File"}), file),
      make("label", {className: "game-field"}, make("span", {className: "label", textContent: "Name"}), title)),
    make("div", {className: "row"}, go, cancel), said);
  file.addEventListener("change", () => {
    if (file.files[0] && !title.value) title.value = file.files[0].name.replace(/\.(zip|wld)$/i, "").slice(0, 40);
  });
  let request = null;
  cancel.addEventListener("click", () => { if (request) request.abort(); forms.replaceChildren(); });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const chosen = file.files[0];
    press(go, said, "Uploading…", () => new Promise((resolve, reject) => {
      if (!chosen) return reject(new ApiError("Choose a file first.", 400));
      if (chosen.size > v.max_upload) return reject(new ApiError(`The file is ${bytes(chosen.size)}; the page takes at most ${bytes(v.max_upload)}.`, 400));
      request = new XMLHttpRequest();
      request.open("POST", `/api/game/world?name=${encodeURIComponent(v.name)}&title=${encodeURIComponent(title.value)}`);
      request.setRequestHeader("X-NullBill", "1");
      request.setRequestHeader("Content-Type", "application/octet-stream");
      request.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) say(said, "busy", e.loaded < e.total ? `Uploading… ${Math.floor((100 * e.loaded) / e.total)}% of ${bytes(e.total)}` : "The server is checking the file…");
      });
      request.addEventListener("load", () => {
        let data = {};
        try { data = JSON.parse(request.responseText); } catch { /* not JSON */ }
        if (request.status !== 200) return reject(new ApiError(sentence(data.error || `The server answered ${request.status}`), request.status));
        setGameRoom(data.view);
        forms.replaceChildren();
        say(room.querySelector(".game-said"), "ok", `${title.value.trim()} is in the list. Press Run this one to play it.`);
        resolve("");
      });
      request.addEventListener("error", () => reject(new ApiError("The upload broke off. Try again.", 0)));
      request.addEventListener("abort", () => reject(new ApiError("Upload cancelled.", 0)));
      request.send(chosen);
    }));
  });
  return form;
}

// What to do to set a service up, in order; a step the page can tell is
// done says so, and one done on another page links there.
function serviceSteps(s, feedback) {
  const steps = s.steps || [];
  if (!steps.length) return null;
  const items = steps.map((st, n) => {
    const mark = st.done === true ? make("span", {className: "step-mark done", title: "Done"}, icon("check"), make("span", {className: "sr-only", textContent: "Done: "}))
      : make("span", {className: "step-mark", textContent: String(n + 1), ariaHidden: "true"});
    const body = make("div", {className: "step-body"}, make("span", {textContent: st.text}));
    if (st.href) body.append(" ", make("a", {className: "link", href: st.href, textContent: st.link || "Open"}));
    if (st.code) {
      const pre = make("pre", {className: "step-code"}, make("code", {textContent: st.code}));
      body.append(make("div", {className: "step-code-row"}, pre, copyButton(`Copy step ${n + 1} of ${s.title}`, () => st.code, feedback)));
    }
    return make("li", {className: st.done === true ? "done" : ""}, mark, body);
  });
  return make("div", {className: "svc-steps"}, make("h3", {textContent: "Set it up"}), make("ol", {className: "steps-list"}, ...items));
}

function renderServices(st) {
  const services = st.services || {items: [], background: []};
  const items = services.items;
  show("services-empty", items.length === 0);
  show("services-table", items.length > 0);
  show("services-unbuilt", items.length > 0 && !built());
  const rows = [];
  for (const s of items) {
    const detailId = `svc-detail-${s.name}`;
    const open = expanded.has(s.name);
    const toggle = make("button", {type: "button", className: "icon-btn expand", title: "Details"}, icon("chevron-right"),
      make("span", {className: "sr-only", textContent: `${s.title} details`}));
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-controls", detailId);
    const first = s.url || (s.addresses || [])[0] || "";
    const said = make("p", {className: "feedback svc-feedback"});
    said.setAttribute("role", "status");
    const row = make("tr", {className: "link-row"},
      make("td", {}, make("div", {className: "svc"}, toggle, make("span", {className: "name", textContent: s.title})), said),
      make("td", {className: "state-cell"}),
      make("td", {className: "hide-phone mono", textContent: first.replace(/^[A-Z][^:]{0,40}: /, "")}),
      make("td", {className: "hide-narrow mono nowrap", textContent: (s.user || "").split(",")[0]}),
      make("td", {className: "actions"}, openLink(s)));
    row.dataset.watch = s.watch || "";
    row.dataset.guest = s.watch || s.name;
    row.dataset.find = `${s.title} ${s.name}`.toLowerCase();
    const feedback = make("p", {className: "feedback"});
    feedback.setAttribute("role", "status");
    const detail = make("tr", {className: "detail", id: detailId, hidden: !open}, make("td", {colSpan: 5}, ...serviceDetail(s, feedback)));
    const flip = () => {
      const on = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", on ? "true" : "false");
      detail.hidden = !on;
      if (on) expanded.add(s.name); else expanded.delete(s.name);
      if (on && s.game && built()) loadGameRoom(s.name);
    };
    toggle.addEventListener("click", (event) => { event.stopPropagation(); flip(); });
    row.addEventListener("click", (event) => { if (!event.target.closest("a, button")) flip(); });
    rows.push(row, detail);
  }
  $("service-rows").replaceChildren(...rows);
  show("services-find", items.length > FOLD_OVER || Boolean($("services-filter").value));
  filterServices();
  $("services-background").textContent = services.background.length ? `Also running, with nothing to open: ${services.background.join(", ")}.` : "";
  paintServices();
}

// A long list of services, and of what runs on demand, narrowed by name.
function filterServices() {
  const q = $("services-filter").value.trim().toLowerCase();
  let found = 0;
  for (const row of $("service-rows").querySelectorAll("tr.link-row")) {
    const detail = row.nextElementSibling;
    row.hidden = Boolean(q) && !row.dataset.find.includes(q);
    detail.hidden = row.hidden || row.querySelector(".expand").getAttribute("aria-expanded") !== "true";
    if (!row.hidden) found += 1;
  }
  for (const row of $("demand-rows").children) {
    row.hidden = Boolean(q) && !`${row.querySelector(".name").textContent} ${row.dataset.name}`.toLowerCase().includes(q);
  }
  show("services-none", Boolean(q) && found === 0);
}

function paintServices() {
  for (const row of document.querySelectorAll("#service-rows tr.link-row")) {
    const [kind, word] = serviceState(row.dataset.watch, row.dataset.guest);
    const cell = row.querySelector(".state-cell");
    if (cell.textContent !== word) cell.replaceChildren(status(kind, word));
    // A service that is off on demand gets Start where Open would be.
    const d = built() ? demandOf(row.dataset.guest) : null;
    const off = Boolean(d && d.off);
    const actions = row.querySelector("td.actions");
    const open = actions.querySelector("a.btn");
    let start = actions.querySelector("button.start");
    if (off && !start) {
      start = make("button", {type: "button", className: "btn small start"}, "Start", make("span", {className: "sr-only", textContent: ` ${d.title}`}));
      start.addEventListener("click", (event) => {
        event.stopPropagation();
        demandAct(start, row.querySelector(".svc-feedback"), {action: "start", name: start.dataset.name});
      });
      actions.append(start);
    }
    if (start) {
      start.hidden = !off;
      if (d) start.dataset.name = d.name;
    }
    if (open) open.hidden = off;
  }
}

function demandState(g) {
  const idle = g.idle_s === undefined ? "" : g.idle_s < 90 ? "used just now" : `unused for ${span(Math.floor(g.idle_s / 60))}`;
  const changes = {always: "Always on after the next setup run.", rules: "Its rule changes with the next setup run.", "on-demand": ""}[g.next] || "";
  const sub = (...parts) => parts.filter(Boolean).join(" ");
  if (g.acting) return ["run", g.acting === "starting" ? "Starting" : "Stopping", "This can take a minute or two."];
  if (g.state === "pending") return ["", "Not yet", "Runs on demand after the next setup run."];
  if (g.state === "unknown") return ["", "No status", ""];
  if (g.state === "missing") return ["", "Not made yet", "Run setup to make it."];
  if (g.state === "off") return ["", "Off", changes];
  if (g.setup) return ["run", "On for setup", "A setup run started it and stops it again when it is done."];
  if (g.state === "partly") {
    const down = g.guests.filter((m) => m.state && m.state !== "running").map((m) => m.name);
    return ["warn", "Partly on", sub(`${andList(down)} ${down.length === 1 ? "is" : "are"} off; Start brings ${down.length === 1 ? "it" : "them"} up.`, changes)];
  }
  if (g.busy && g.busy.length) return ["good", "In use", sub(`${sentence(g.busy.join("; "))}`, changes)];
  if (g.keep) return ["good", "Kept on", sub(`${sentence(idle)} It stays on until you stop it.`, changes)];
  if (g.asks) return ["warn", "On, unused", sub(`${sentence(idle)} Stop it to free ${gib(g.memory_mib)}, or keep it on.`, changes)];
  if (g.stops_in_s !== undefined) {
    const left = Math.ceil(g.stops_in_s / 60);
    return ["good", "On", sub(`${sentence(idle)} Stops by itself ${left <= 1 ? "within a minute" : `in ${span(left)}`} if nobody uses it.`, changes)];
  }
  const rule = g.stop === "ask" ? `You are told once it has gone ${span(g.idle_minutes)} unused.` : g.stop === "manual" ? "It runs until you stop it." : "";
  return ["good", "On", sub(sentence(idle), rule, changes)];
}

function demandRow(g) {
  const said = make("p", {className: "feedback"});
  said.setAttribute("role", "status");
  const button = (cls, text, body) => {
    const b = make("button", {type: "button", className: `btn small ${cls}`}, text, make("span", {className: "sr-only", textContent: ` ${g.title}`}));
    b.addEventListener("click", () => demandAct(b, said, body(b)));
    return b;
  };
  const keep = button("keep", "Keep on", (b) => ({action: "keep", name: g.name, on: b.getAttribute("aria-pressed") !== "true"}));
  keep.title = "Not stopped for being unused, and you are not asked, until you stop it";
  const row = make("tr", {},
    make("td", {}, make("span", {className: "name", textContent: g.title}), make("span", {className: "sub demand-guests"}), said),
    make("td", {className: "demand-state"}),
    make("td", {className: "num hide-phone demand-memory"}),
    make("td", {className: "actions"}, make("span", {className: "row"}, keep,
      button("start", "Start", () => ({action: "start", name: g.name})), button("stop", "Stop", () => ({action: "stop", name: g.name})))));
  row.dataset.name = g.name;
  return row;
}

// Rows are made once per workload and then repainted, so a button keeps
// its focus and its line of feedback through every refresh.
function paintDemand() {
  const view = demand;
  const groups = view ? view.groups : [];
  show("demand", groups.length > 0);
  if (!groups.length) return;
  const body = $("demand-rows");
  const have = new Map([...body.children].map((r) => [r.dataset.name, r]));
  if ([...have.keys()].join() !== groups.map((g) => g.name).join()) body.replaceChildren(...groups.map((g) => have.get(g.name) || demandRow(g)));
  for (const g of groups) {
    const row = body.querySelector(`tr[data-name="${g.name}"]`);
    const [kind, word, sub] = demandState(g);
    const cell = row.querySelector(".demand-state");
    if (cell.dataset.text !== word + sub) {
      cell.dataset.text = word + sub;
      cell.replaceChildren(status(kind, word), sub ? make("span", {className: "sub", textContent: sub}) : "");
    }
    row.querySelector(".demand-guests").textContent = andList(g.guests.map((m) => m.name));
    row.querySelector(".demand-memory").textContent = gib(g.memory_mib);
    const on = g.state === "on" || g.state === "partly";
    const known = view.live && !["pending", "unknown", "missing"].includes(g.state);
    row.querySelector(".start").hidden = !known || g.state === "on";
    row.querySelector(".stop").hidden = !known || !on;
    const keep = row.querySelector(".keep");
    keep.hidden = !known || !on || g.stop === "manual" || Boolean(g.setup);
    keep.setAttribute("aria-pressed", g.keep ? "true" : "false");
    for (const b of row.querySelectorAll("button")) {
      if (g.acting || g.setup) { b.setAttribute("aria-disabled", "true"); b.dataset.why = g.acting ? "Wait until it has finished." : "A setup run is using it."; }
      else if (b.dataset.why) { b.removeAttribute("aria-disabled"); delete b.dataset.why; }
    }
  }
  show("demand-why", Boolean(view.why));
  $("demand-why-text").textContent = view.why || "";
  const used = view.used_mib;
  show("demand-memory", view.live && used !== null);
  if (view.live && used !== null) {
    const room = Math.max(view.headroom_mib, used, 1);
    const over = used > view.headroom_mib;
    $("demand-bar").classList.toggle("over", over);
    $("demand-bar").querySelector(".m-chosen").style.width = `${(100 * used) / room}%`;
    $("demand-bar").querySelector(".m-free").style.width = `${(100 * Math.max(0, view.headroom_mib - used)) / room}%`;
    const text = over ? `${gib(used)} in use, ${gib(used - view.headroom_mib)} more than the plan counts` : `${gib(used)} of ${gib(view.headroom_mib)} in use`;
    if ($("demand-fit").textContent !== text) $("demand-fit").replaceChildren(over ? status("warn", text) : text);
  }
  const on = groups.filter((g) => g.state === "on" || g.state === "partly").length;
  $("demand-meta").textContent = view.live ? `${on} of ${groups.length} on${view.setup ? " · a setup run is holding what it started" : ""}` : "";
  show("demand-log", view.events.length > 0);
  $("demand-events").replaceChildren(...view.events.map((e) => make("li", {}, make("time", {textContent: clock(e.at)}),
    `${e.title}: ${e.what}${e.why ? ` (${e.why})` : ""}`)));
}

function setDemand(view) {
  demand = view || null;
  paintDemand();
  paintServices();
  paintStates();
}

async function loadDemand() {
  if (!state || !(state.on_demand || state.config_changed)) return setDemand(null);
  setDemand((await api("/api/on-demand")).view);
}

// Backups (web/backupview.py): where they go, what the server measured of
// them and when, and the key to keep off the server.
let backupsLive = null;    // the last /api/backups reading from the server
let backupsDrawn = "";     // the places as last drawn: a refresh must not undo a choice being made
const KEEP_WORDS = {"keep-daily": "daily", "keep-weekly": "weekly", "keep-monthly": "monthly"};
function keepsText(keeps) { return Object.entries(keeps).map(([k, n]) => `${n} ${KEEP_WORDS[k] || k}`).join(", "); }
function bytesGib(n) { const g = n / 2 ** 30; return g >= 100 ? `${Math.round(g)}` : g.toFixed(1); }

// Backups in a word, for Overview: what was chosen and built, not a reading.
function backupsWord(b) {
  if (!b || !b.ready) return ["", "Not set up", ""];
  if (!b.on) return b.built ? ["warn", "Being turned off", "The next setup run deletes them"] : ["warn", "Off", "Nothing is backed up"];
  if (!b.built) return ["warn", b.where ? "Chosen, not built" : "No room", b.where ? "The next setup run makes them" : "Nothing is backed up"];
  return b.built.kind === "disk" ? ["good", "On a disk of their own", `Nightly at ${b.schedule}`] : ["warn", "On the server's disk", "Lost if this disk fails"];
}

async function loadBackups() {
  if (!state || !state.backups.ready) return;
  const got = await api("/api/backups");
  state.backups = got.backups;
  backupsLive = got.live;
  renderBackups();
}

function backupPlace(p, b) {
  const size = `${p.size_gib} GiB ${p.rotational ? "hard disk" : "SSD"}`;
  const failing = p.health === "FAILED" ? " It reports that it is failing." : "";
  switch (p.kind) {
    case "blank": return [p.id, `${size}, blank. A disk of their own: the backups outlive the server's disk.${failing}`, "", ""];
    case "erase": return [p.id, `${size}, holding ${andList(p.holds)}. Erased for the backups: everything on it is lost.${failing}`, "", "danger-option"];
    case "kept": return [p.id, `${size}, with files you keep.`, "Not offered: NullBill puts no backups on a disk it shares. Setup, What to run, Other disks can copy its files off and erase it.", ""];
    case "storage": return [p.id, `${size}, extra storage in Proxmox.`, "Not offered while it is storage.", ""];
    default: return [p.id, `${size}. The backups' own disk.`, "", ""];
  }
}

function backupsPicked() {
  const input = $("backups-places").querySelector("input:checked");
  return input ? input.value : "";
}

function paintBackupsForm() {
  const b = state.backups;
  const picked = backupsPicked();
  const place = b.places.find((p) => p.id === picked);
  show("backups-size-field", picked === "system");
  show("backups-confirm-field", Boolean(place && place.kind === "erase"));
  if (place) $("backups-confirm-label").textContent = `Type ${place.confirm}, the end of its name, to erase everything on it`;
  $("backups-save").textContent = b.on && (b.where || b.built) ? "Save" : "Turn on backups";
}

function renderBackups() {
  if (!state) return;
  const b = state.backups;
  show("backups-unready", !b.ready);
  show("backups-body", b.ready);
  if (!b.ready) { $("backups-sub").textContent = ""; return; }
  const has = b.built;
  const want = b.where;
  const live = has ? backupsLive : null;
  const problems = state.plan.errors.length > 0;
  $("backups-sub").textContent = !b.on ? (has ? "Turned off in your choices." : "Off. Nothing on this server is backed up.")
    : has ? `Every service, each night at ${b.schedule}, encrypted on the server. Kept: ${keepsText(b.keeps)}.`
      : "Every service, each night, encrypted on the server.";
  const pending = !b.pending ? ""
    : !b.on ? "Backups are turned off in your choices. The next setup run deletes the backup server and every backup."
      : !want ? ""
        : !has ? "Saved. The next setup run makes the backup server and the first backups."
          : want.kind === "disk" && has.kind === "system" ? `${want.disk} is in the server for the backups. The next setup run moves them onto it with their history; the backup server keeps running.`
            : want.size_gib !== has.size_gib ? `Saved. The next setup run gives the backups ${want.size_gib} GiB.` : "Saved. The next setup run makes the change.";
  const stuck = b.on && !want ? (problems ? "The plan has problems, so nothing here can be built. Settle them under Setup, Plan."
    : b.status === "DEGRADED" ? "Backups are on, and nothing is backed up: no disk for them, and too little room left on the server's disk. Plug in a disk and press Look for a new disk." : "") : "";
  show("backups-pending", Boolean(pending || stuck));
  $("backups-pending-text").textContent = pending || stuck;
  $("backups-pending").classList.toggle("warn", Boolean(stuck));
  show($("backups-pending").querySelector("a"), Boolean(pending));

  // Now: what the server measured, with when.
  show("backups-now", Boolean(has));
  if (has) {
    const stats = [];
    const cell = (label, kind, value, sub) => stats.push(make("div", {}, make("dt", {textContent: label}),
      make("dd", {}, make("span", {className: "status-val"}, make("span", {className: `dot ${kind}`, ariaHidden: "true"}), value)),
      sub ? make("dd", {className: "sub", textContent: sub}) : null));
    const why = !live ? "" : live.state === "unreachable" ? "Monitoring does not answer, so there is no reading of the backups. If setup just finished, wait a minute."
      : live.state === "waiting" ? "No reading yet. The server reads its backups at the end of a setup run and every hour."
        : live.state === "unlisted" ? "The server cannot list its backups: the backup server does not answer it. The alert BackupStatusUnknown follows if this lasts." : "";
    show("backups-unread", Boolean(why));
    $("backups-unread-text").textContent = why;
    $("backups-unread").classList.toggle("warn", Boolean(live && live.state === "unlisted"));
    cell("Where", has.kind === "disk" ? "good" : "warn", has.kind === "disk" ? "A disk of their own" : "The server's disk",
      has.kind === "disk" ? has.disk : "Lost if this disk fails");
    if (live && live.state === "ok") {
      const n = live.guests.length;
      const newest = Math.max(0, ...live.guests.map((g) => g.last || 0));
      if (live.late.length) cell("Newest backups", "bad", `${live.late.length} of ${n} late`, `None from the last 26 hours: ${live.late.join(", ")}`);
      else cell("Newest backups", "good", `${n} of ${n} in time`, newest ? `The newest from ${clock(newest)}` : "");
      const r = live.restore;
      if (!r) cell("Restore test", "", "Not run yet", "One backup is opened after each night's backups");
      else cell("Restore test", r.ok ? "good" : "bad", r.ok ? "Passed" : "Failed",
        `${r.ok ? "A file was read back from" : "No file could be read back from"} ${r.guest}'s backup of ${clock(r.backup)}; tested ${ago(r.at)}`);
      const room = live.store;
      if (room) {
        const full = room.used / room.size;
        cell("Room", full > 0.9 ? "warn" : "good", `${bytesGib(room.used)} of ${bytesGib(room.size)} GiB`,
          full > 0.9 ? (has.kind === "system" ? "Nearly full: give them more below" : "Nearly full") : "Old backups are pruned daily");
      }
    }
    $("backups-stats").replaceChildren(...stats);
    $("backups-meta").textContent = live && live.read ? `Measured on the server ${readAt(live.read)}` : "";
  }

  // Where they go.
  const holder = b.places.find((p) => p.kind === "made" || p.kind === "chosen");
  const system = b.system;
  show("backups-weak", b.on && Boolean(want || has) && (want || has).kind === "system");
  show("backups-own", Boolean(holder));
  show("backups-form", !holder);
  show("backups-off-note", b.on);
  show(document.querySelector(".backups-look"), !holder);
  if (holder) {
    $("backups-own").textContent = `${holder.id}, ${holder.size_gib} GiB: a disk of their own, so they outlive the disk the server runs from.`;
  }
  const drawn = JSON.stringify([b.places, system, want, b.on]);
  if (!holder && drawn !== backupsDrawn) {
    backupsDrawn = drawn;
    const list = $("backups-places");
    const firstBlank = b.places.find((p) => p.kind === "blank");
    const chosen = want && want.kind === "system" ? "system" : firstBlank ? firstBlank.id : system.allowed ? "system" : "";
    const radio = (value, disabled) => make("input", {type: "radio", name: "backups-place", value, checked: value === chosen, disabled});
    const room = system.free_gib === null || system.free_gib === undefined ? "" : ` ${system.free_gib} GiB are free on it.`;
    list.replaceChildren(
      option(radio("system", !system.allowed), "On the disk the server runs from",
        `Nothing to plug in.${room} Brings back a broken service, a bad update or a mistake; lost if this disk fails.`,
        system.allowed ? "" : `Not offered: ${system.why}`),
      ...b.places.map((p) => {
        const [title, desc, why, cls] = backupPlace(p, b);
        return option(radio(p.id, Boolean(why)), title, desc, why, cls);
      }));
    const size = $("backups-size");
    size.min = system.min_gib;
    size.max = 4096;
    size.value = system.size_gib || system.default_gib;
    $("backups-size-hint").textContent = "They take only what they use, up to this. It can grow later, not shrink.";
    $("backups-confirm").value = "";
    $("backups-refused").hidden = true;
  }
  if (!holder) paintBackupsForm();

  // What is backed up, and what is not.
  const rows = live && live.state === "ok" ? live.guests : b.covered.map((name) => ({name}));
  show("backups-what", rows.length > 0);
  $("backups-schedule").textContent = `Each night at ${b.schedule} · kept: ${keepsText(b.keeps)}`;
  $("backups-rows").replaceChildren(...rows.map((g) => {
    const late = live && live.state === "ok" && live.late.includes(g.name);
    const when = g.last ? status(late ? "bad" : "good", clock(g.last), ago(g.last))
      : g.last === null ? status("bad", "None yet") : status("", has ? "No reading yet" : b.on ? "After the next setup run" : "Once backups are on");
    return make("tr", {}, make("td", {className: "name", textContent: g.name}), make("td", {}, when));
  }));
  $("backups-not").replaceChildren(
    ...b.left_out.map((name) => make("li", {}, make("b", {textContent: `Not ${name}. `}), "Setup builds it again from what it pins, and nothing in it is yours.")),
    make("li", {}, make("b", {textContent: "Not your kept disks. "}), "They hold your own files, and a media library is far larger than everything else here together."));

  // The key.
  const key = b.key;
  show("backups-key", key.have);
  if (key.have) {
    $("backups-key-why").textContent = "Backups are encrypted on the server with this key before they are stored. If the server's disk is lost, backups on another disk open only with it, "
      + "and the server's own copies of the key are lost too. Keep the file on another device, or print it."
      + ((has || want || {}).kind === "system" ? " It starts to matter once the backups have a disk of their own." : "");
    $("backups-key-state").replaceChildren(key.saved_at ? status("good", "Downloaded from this page", clock(key.saved_at)) : status("warn", "Not taken off the server yet"));
    show("backups-key-print", Boolean(key.fingerprint));
    $("backups-key-fingerprint").textContent = key.fingerprint ? `Fingerprint ${key.fingerprint}` : "";
  }
}

function saveBackups(event) {
  event.preventDefault();
  const picked = backupsPicked();
  const body = {place: picked};
  if (picked === "system") body.size_gib = Number($("backups-size").value);
  else body.confirm = $("backups-confirm").value.trim();
  const field = picked === "system" ? $("backups-size") : $("backups-confirm-field").hidden ? null : $("backups-confirm");
  $("backups-refused").hidden = true;
  press($("backups-save"), "backups-result", "Saving…", async () => {
    if (!picked) throw new ApiError("Choose where the backups go.", 400);
    let got;
    try {
      got = await api("/api/backups", body);
    } catch (error) {
      showProblem("backups-refused", error);
      throw error;
    }
    state.plan = got.plan;
    state.backups = got.backups;
    backupsDrawn = "";
    renderBackups();
    refreshSoon();
    return got.backups.pending ? "Saved. A setup run makes it so." : "Saved. Nothing changes on the server.";
  }, field);
}

// Start, Stop or Keep on. A start that does not fit, and a stop of
// something in use, change nothing on the server and come back as a
// question; only the owner's yes sends them again.
async function demandAct(button, feedback, body) {
  const busy = {start: "Starting…", stop: "Stopping…", keep: "Saving…"}[body.action];
  await press(button, feedback, busy, async () => {
    const group = demand && demand.groups.find((g) => g.name === body.name);
    const acting = (on) => {
      if (!group || body.action === "keep") return;
      if (on) group.acting = body.action === "start" ? "starting" : "stopping"; else delete group.acting;
      paintDemand();
      paintServices();
    };
    acting(true);
    let answer;
    try {
      answer = await api("/api/on-demand", body);
      if (!answer.ok && answer.no_room) {
        const r = answer.no_room;
        if (!r.enough) {
          throw new ApiError(`${r.title} needs ${gib(r.need_mib)} and does not fit, even with everything else on demand stopped. Run setup if you changed what runs since the last run; otherwise let more run on demand under Setup, What to run.`, 409);
        }
        acting(false);
        const used = r.stop.some((o) => o.busy.length);
        const ok = await confirmDialog({
          title: `Stop ${andList(r.stop.map((o) => o.title))} to start ${r.title}?`,
          text: `${r.title} needs ${gib(r.need_mib)} and ${gib(Math.max(0, r.free_mib))} is free. Stopping shuts down cleanly; nothing is deleted.`,
          points: r.stop.map((o) => `${o.title}, ${gib(o.memory_mib)}: ${o.busy.length ? `in use (${o.busy.join("; ")})` : o.idle_s < 90 ? "used just now" : `unused for ${span(Math.floor(o.idle_s / 60))}`}`),
          yes: `Stop ${r.stop.length > 1 ? "them" : "it"} and start ${r.title}`,
          danger: used,
        });
        if (!ok) { setDemand(answer.view); return ""; }
        acting(true);
        answer = await api("/api/on-demand", {...body, stop: r.stop.map((o) => o.name)});
        if (!answer.ok) { setDemand(answer.view); throw new ApiError("What is running changed meanwhile. Press Start again.", 409); }
      }
      if (!answer.ok && answer.busy) {
        acting(false);
        const ok = await confirmDialog({title: `Stop ${answer.title} while it is in use?`, text: "It shuts down cleanly, but whoever is using it is cut off:", points: answer.busy.map(sentence), yes: "Stop it"});
        if (!ok) { setDemand(answer.view); return ""; }
        acting(true);
        answer = await api("/api/on-demand", {...body, force: true});
      }
    } finally {
      acting(false);
    }
    setDemand(answer.view);
    // Prometheus hears of it within a minute or two; the page already knows.
    if (lastStatus && lastStatus.asks) lastStatus.asks = lastStatus.asks.filter((a) => a.name !== body.name);
    paintStates();
    if (body.action === "keep") return body.on ? "Kept on until you stop it." : "It may stop by itself again.";
    if (body.action === "stop") return answer.already ? `${answer.title} was off already.` : `Stopped ${answer.title}.`;
    const parts = [answer.already ? `${answer.title} is on.` : `Started ${answer.title}.`];
    if ((answer.stopped || []).length) parts.push(`Stopped ${andList(answer.stopped)} to make room.`);
    for (const w of answer.warnings || []) parts.push(sentence(w));
    return parts.join(" ");
  });
}

// The page's regular question, every 30 s and whenever it comes back into
// sight: health from Prometheus, and the stamp that says whether anything
// the rest of the page shows has changed on the server since it was asked.
async function refreshStatus() {
  lastStatus = await api("/api/status");
  paintServices();
  paintStates();
  if (currentView === "services" && !$("confirm").open) loadDemand().catch(() => {});
  if (pathShown()) loadPath().catch(() => {});
  if (currentView === "backups") loadBackups().catch(() => {});
  if (state && lastStatus.stamp && lastStatus.stamp !== state.stamp) refreshSoon();
}

async function refresh() {
  const st = await api("/api/state");
  show("loading", false);
  if (!st.auth || !st.has_password) {
    clearInterval(statusTimer);
    stopChat();
    show("app", false);
    show("gate", true);
    show("login", !st.auth);
    show("password", st.auth && !st.has_password);
    if (!st.auth) {
      $("login-hint").textContent = st.has_password ? "Enter your password." : "Enter the setup code the installer printed in the Proxmox shell.";
      $("secret").focus();
    } else {
      $("new-password").focus();
    }
    return;
  }
  const first = !state;
  const disksChanged = !state || JSON.stringify(state.disks.map((d) => d.id)) !== JSON.stringify(st.disks.map((d) => d.id));
  const choicesChanged = !state || JSON.stringify(state.choices) !== JSON.stringify(st.choices);
  const wasBuilt = built();
  st.at_local = Date.now() / 1000;
  state = st;
  show("gate", false);
  show("app", true);
  // Never undo what the owner is changing: unsaved choices stay.
  if (!choicesDirty() || disksChanged || (choicesChanged && !choicesDirty())) renderChoices(st);
  renderHardware(st.hardware);
  renderServices(st);
  if (wasBuilt !== built()) renderServices(st);
  renderPlan(st.plan);
  renderAlerts(st.alerts);
  renderAgentSignin(st);
  renderMode(st.chat);
  renderAgentPowers(st.agent_powers);
  renderAgentGithub(st.agent_github);
  renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
  renderLan(st.lan);
  if (peopleShown() && !$("confirm").open) loadPeople(false).catch(() => {});
  renderTrust(st.trust);
  renderBackups();
  onChoicesChanged();
  paintChatShell();
  if (first) {
    route(false);
    clearInterval(statusTimer);
    refreshStatus().catch(() => {});
    statusTimer = setInterval(() => refreshStatus().catch(() => {}), 30000);
    startChat();
    greetUpdated();
  }
  // Follow the latest run unless the owner opened another one.
  if (st.job && (!job || (job.id !== st.job.id && !(currentView === "activity" && currentSub === job.id)))) follow(st.job, st.job.state === "running");
  if (!st.job) paintPill(null);
  gateJobButtons();
}

// A refresh soon, once: the agent and runs change state in bursts.
let refreshSoonTimer = null;
function refreshSoon() {
  clearTimeout(refreshSoonTimer);
  refreshSoonTimer = setTimeout(() => refresh().catch(() => {}), 400);
}

// Runs.
const RUNNING_WHY = "Wait until the running job finishes; the top bar follows it.";
function stopFollowing() { if (job) clearTimeout(job.timer); }

function gateJobButtons() {
  const running = Boolean(runningAction());
  for (const button of document.querySelectorAll("button[data-action], .job-retry, .job-again, #agent-powers, #start-setup, #update-now")) {
    if (button.classList.contains("busy")) continue;
    const why = running ? RUNNING_WHY
      : button.id === "start-setup" && !setupReady() ? "Setup needs the hardware read and a plan that fits; the list above says what is missing." : "";
    button.setAttribute("aria-disabled", why ? "true" : "false");
    button.dataset.why = why;
  }
}

// A run as the tables show it, live for the followed one.
function runView(r) {
  if (job && job.id === r.id && job.view) {
    const v = job.view;
    const steps = (v.progress && v.progress.steps) || [];
    const done = steps.filter((s) => s.status === "done").length;
    return {...r, state: v.state, stopped: v.stopped, seconds: v.seconds, where: v.progress && v.progress.error && v.progress.error.stage_text ? `At "${v.progress.error.stage_text}"` : r.where, stage: v.state === "running" && steps.length ? `Stage ${Math.min(steps.length, done + 1)} of ${steps.length}` : ""};
  }
  return r;
}
function runRow(raw) {
  const r = runView(raw);
  const result = r.state === "running" ? status("run", r.stage || "Running") : r.state === "done" ? status("good", "Finished") : r.stopped ? status("", "Stopped by you") : status("bad", "Failed");
  const row = make("tr", {className: `link-row${job && job.id === r.id && currentSub === r.id ? " current" : ""}`},
    make("td", {className: "name"}, make("a", {href: `#activity/${r.id}`, textContent: jobName(r)}), r.only ? make("span", {className: "sub", textContent: r.only.text}) : null),
    make("td", {}, result, r.state === "failed" && !r.stopped && r.where ? make("span", {className: "sub run-where", textContent: r.where}) : null),
    make("td", {className: "muted"}, make("span", {textContent: clock(r.started), title: ago(r.started)})),
    make("td", {className: "num muted hide-phone", textContent: r.state === "running" ? duration(r.seconds) : duration(r.seconds)}),
    make("td", {className: "muted hide-phone", textContent: r.by === "agent" ? "Agent" : r.by === "owner" ? "You" : r.by}));
  row.addEventListener("click", (event) => { if (!event.target.closest("a, button")) go(`activity/${r.id}`); });
  return row;
}
function renderRuns() {
  const runs = state.runs || [];
  show("runs-empty", runs.length === 0);
  show("runs-table", runs.length > 0);
  $("runs-rows").replaceChildren(...runs.map(runRow));
}

// The run page: the one the address names.
function focusRun() {
  if (!job || !job.node.isConnected) return;
  document.title = `${jobName(job.view)} · Activity · NullBill`;
  job.node.querySelector(".job-title").focus({preventScroll: true});
}
async function openRun(id, focus) {
  $("crumb-run").textContent = job && job.id === id ? jobName(job.view) : "Run";
  if (job && job.id === id) {
    if (focus) requestAnimationFrame(focusRun);
    return;
  }
  try {
    const view = await api(`/api/jobs/${id}`);
    follow(view, view.state === "running");
    placeJob();
    if (focus) focusRun();
  } catch (error) {
    $("job-slot").replaceChildren(make("div", {className: "empty"}, make("h2", {textContent: "This run is not kept any more"}),
      make("p", {textContent: error.message}), make("a", {className: "btn", href: "#activity", textContent: "All activity"})));
  }
}

// The followed run's card goes where the owner looks at it: its own page,
// or under Build while a setup run goes.
function placeJob() {
  if (!job) return;
  const onRun = currentView === "activity" && currentSub === job.id;
  const onBuild = currentView === "setup" && currentStep === "build" && SETUP_RUNS.has(job.view.action) && !job.view.only;
  const target = onRun ? $("job-slot") : onBuild ? $("build-job") : null;
  if (target && job.node.parentElement !== target) {
    for (const slot of [$("job-slot"), $("build-job")]) if (slot !== target && slot.contains(job.node)) slot.replaceChildren();
    target.replaceChildren(job.node);
  }
  if (onRun) $("crumb-run").textContent = jobName(job.view);
  show("build-start", !(onBuild && job.view.state === "running"));
}

function follow(first, watched, feedback) {
  stopFollowing();
  if (job && job.feedback && job.feedback !== feedback && !job.feedback.classList.contains("error")) say(job.feedback, "", "");
  const node = $("job-template").content.firstElementChild.cloneNode(true);
  // A clone keeps no listeners, so the copy buttons go on here.
  for (const pre of node.querySelectorAll(".job-log pre")) outputBox(pre);
  // The first view came with the log up to its offset; the next asks from there.
  job = {id: first.id, view: first, node, offset: first.offset || 0, timer: null, watched, misses: 0, feedback, stage: null};
  const again = ["verify", "upgrade"].includes(first.action) ? first.action : "apply";
  for (const button of node.querySelectorAll(".job-retry, .job-again")) {
    button.addEventListener("click", () => {
      const feedback = node.querySelector(button.classList.contains("job-again") ? ".job-stop-result" : ".job-copy-result");
      // Setup starts from its review, except a failed run of exactly what
      // the owner read there, which starts again here.
      if (again !== "apply" || (button.classList.contains("job-retry") && sameReview(job.view))) startJob(again, button, feedback);
      else if (runningAction()) say(feedback, "info", RUNNING_WHY);
      else go("setup/build");
    });
  }
  node.querySelector(".job-copy").addEventListener("click", () => copyDetails(node));
  node.querySelector(".job-agent").addEventListener("click", () => {
    const view = job.view;
    const e = view.progress.error || {};
    const where = e.stage_text ? ` at "${e.stage_text}"` : "";
    askAgent(`${view.progress.title} failed${where}: ${(e.message || "see its log").replace(/[.\s]+$/, "")}. Find out why and propose the fix (run ${view.id}).`);
  });
  const stopButton = node.querySelector(".job-stop");
  stopButton.addEventListener("click", async () => {
    if (stopButton.classList.contains("busy")) return;
    const ok = await confirmDialog({title: "Stop this run?", text: "What it finished stays. You can start it again afterwards.", yes: "Stop it"});
    stopButton.focus();
    if (!ok) return;
    press(stopButton, node.querySelector(".job-stop-result"), "Stopping…", async () => {
      await api(`/api/jobs/${first.id}/stop`, {});
      return "Stopping; the run ends in a moment.";
    });
  });
  placeJob();
  drawJob(job, first);
  tick(job);
}

// A setup run started from the review the page shows now: nothing it
// would do has changed since the owner read it.
function sameReview(view) {
  return Boolean(view.review && state && state.review && view.review === state.review.id && !(state.removals || []).length);
}

async function tick(current) {
  let view;
  try {
    view = await api(`/api/jobs/${current.id}?offset=${current.offset}`);
    current.misses = 0;
  } catch (error) {
    if (job !== current || error.status === 401) return;
    // The page's server restarts during an update; keep trying.
    current.misses += 1;
    current.node.querySelector(".job-now").textContent = `Lost contact with the server; trying again… (${error.message})`;
    $("run-sub").textContent = "Lost contact; trying again…";
    current.timer = setTimeout(() => tick(current), Math.min(10000, 1000 * current.misses));
    return;
  }
  if (job !== current) return;
  const wasRunning = current.view.state === "running";
  current.offset = view.offset;
  current.view = view;
  drawJob(current, view);
  gateJobButtons();
  paintStates();
  placeJob();
  if (view.state === "running") {
    current.timer = setTimeout(() => tick(current), 1000);
  } else if (current.watched) {
    current.watched = false;
    const title = view.progress.title;
    if (view.state === "failed") announce(view.stopped ? `${title}: stopped.` : `${title} failed. ${view.progress.error ? view.progress.error.message : ""}`, true);
    else announce(`${title}: finished.`);
    if (current.feedback && wasRunning) {
      say(current.feedback, view.state === "done" ? "ok" : "error", view.state === "done" ? "Finished." : view.stopped ? "Stopped." : "Failed. The run says why.");
    }
    refresh().catch((error) => toast(error.message));
    if (view.state === "failed" && !view.stopped && current.node.isConnected) current.node.querySelector(".job-error-title").focus();
  }
}

function paintPill(view) {
  const pill = $("run");
  const fill = $("run-fill");
  if (!view) {
    pill.dataset.state = "idle";
    $("run-title").textContent = "No runs yet";
    $("run-sub").textContent = "";
    pill.href = "#activity";
    fill.style.width = "0";
    return;
  }
  const p = view.progress;
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  pill.dataset.state = view.stopped ? "stopped" : view.state;
  pill.href = `#activity/${view.id}`;
  const name = jobName(view);
  // Which stage, for a run of one.
  const which = view.only ? [view.only.text] : [];
  if (view.state === "running") {
    $("run-title").textContent = `${name}${steps.length ? ` ${Math.min(steps.length, done + 1)}/${steps.length}` : ""}`;
    $("run-sub").textContent = [now ? now.text : "Starting", duration(view.seconds)].filter(Boolean).join(" · ");
    fill.style.width = steps.length ? `${Math.max(2, Math.round((100 * done) / steps.length))}%` : "30%";
  } else if (view.state === "done") {
    const degraded = p.verify ? p.verify.counts.DEGRADED : 0;
    $("run-title").textContent = `${name} finished`;
    $("run-sub").textContent = [...which, `${ago(view.started + view.seconds)}, took ${duration(view.seconds)}${degraded ? `, ${degraded} degraded` : ""}`].join(" · ");
    fill.style.width = "0";
  } else {
    $("run-title").textContent = view.stopped ? `${name} stopped` : `${name} failed`;
    $("run-sub").textContent = p.error && p.error.stage_text ? `at ${p.error.stage_text}` : [...which, ago(view.started + view.seconds)].join(" · ");
    fill.style.width = "0";
  }
}

// What failed, one item per cause: what it is about, what it said in a
// line, what to do about it when that is known, and what it printed only
// when asked for.
function failItems(items) {
  return items.map((item) => {
    const node = make("li", {className: "fail"});
    if (item.title) node.append(make("b", {className: "fail-title", textContent: item.title}));
    if (item.text) node.append(make("p", {className: "fail-text", textContent: item.text}));
    if (item.advice) node.append(make("p", {className: "fail-fix"}, make("b", {textContent: "What to do: "}), item.advice));
    if (item.detail) {
      const pre = make("pre", {textContent: item.detail});
      node.append(make("details", {className: "fail-more"}, make("summary", {textContent: "What it printed"}), outputBox(pre)));
    }
    return node;
  });
}

function drawJob(current, view) {
  const node = current.node;
  const p = view.progress;
  node.dataset.state = view.state;
  paintPill(view);
  node.querySelector(".job-title").textContent = p.title;
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  const [kind, word] = view.stopped ? ["", "Stopped by you"] : {running: ["run", "Running"], done: ["good", "Finished"], failed: ["bad", "Failed"]}[view.state];
  node.querySelector(".job-state").replaceChildren(status(kind, word));
  const meta = [];
  if (steps.length) meta.push(`Stage ${Math.min(steps.length, done + (now ? 1 : 0))} of ${steps.length}`);
  if (view.state === "running") meta.push(`running for ${duration(view.seconds)}`);
  else meta.push(`took ${duration(view.seconds)}`);
  meta.push(`started ${clock(view.started)}`);
  if (view.by === "agent") meta.push("by the agent");
  node.querySelector(".job-meta").textContent = meta.join(" · ");
  if (view.state === "running" && now && current.stage !== now.name) {
    if (current.stage !== null) announce(`Stage ${done + 1} of ${steps.length}: ${now.text}.`);
    current.stage = now.name;
  }
  const bar = node.querySelector(".bar");
  bar.hidden = view.state === "done" && !steps.length;
  const fraction = steps.length ? done / steps.length : view.state === "running" ? null : 1;
  bar.classList.toggle("indeterminate", fraction === null);
  bar.querySelector(".fill").style.width = fraction === null ? "" : `${Math.round(100 * fraction)}%`;
  if (fraction === null) {
    bar.removeAttribute("aria-valuenow");
    bar.setAttribute("aria-valuetext", "Running");
  } else {
    bar.setAttribute("aria-valuenow", String(Math.round(100 * fraction)));
    bar.setAttribute("aria-valuetext", steps.length ? `Stage ${Math.min(steps.length, done + (now ? 1 : 0))} of ${steps.length}` : "Finished");
  }
  const nowLine = node.querySelector(".job-now");
  if (view.state === "running") {
    const parts = [];
    if (now) parts.push(now.text);
    if (p.activity) parts.push(p.activity);
    let text = parts.length ? parts.join(": ") : "Starting…";
    if (p.quiet_s !== null && p.quiet_s >= STUCK_S) text += ` (no output for ${duration(p.quiet_s)}; this looks stuck: stop it and start it again)`;
    else if (p.quiet_s !== null && p.quiet_s >= 20) text += ` (quiet for ${duration(p.quiet_s)}; downloads and image builds can be quiet for minutes)`;
    nowLine.textContent = text;
  } else {
    nowLine.textContent = "";
  }
  node.querySelector(".job-stop").hidden = view.state !== "running";
  const againButton = node.querySelector(".job-again");
  const canAgain = view.state === "done" && ["apply", "verify", "rebake-ci", "upgrade", "remove-guests"].includes(view.action);
  // On its review a setup run starts from the button above it.
  const reviewed = SETUP_RUNS.has(view.action) && node.parentElement === $("build-job");
  againButton.hidden = !canAgain || reviewed;
  againButton.textContent = view.action === "verify" ? "Run the checks again" : view.action === "upgrade" ? "Check and update again" : "Review and run again";
  node.querySelector(".job-stages").hidden = !steps.length;
  const WORD = {done: "done", failed: "failed", running: "running", waiting: "waiting"};
  node.querySelector(".stages").replaceChildren(...steps.map((s) => make("li", {className: s.status},
    make("span", {className: "s-icon", ariaHidden: "true"}, s.status === "done" ? icon("check") : s.status === "failed" ? icon("x") : null),
    make("span", {className: "s-text"}, s.text, make("span", {className: "sr-only", textContent: `, ${WORD[s.status]}`})),
    make("span", {className: "s-time", textContent: s.status === "waiting" ? "" : duration(s.seconds)}))));
  const failed = view.state === "failed" && p.error;
  node.querySelector(".job-error").hidden = !failed;
  if (failed) {
    const e = p.error;
    node.querySelector(".job-error-title").textContent = view.stopped ? (e.stage_text ? `Stopped by you at "${e.stage_text}"` : "Stopped by you") : e.stage_text ? `Failed at "${e.stage_text}"` : "What went wrong";
    node.querySelector(".job-message").textContent = e.message;
    // Drawn again each second while the page follows a run: keep what is
    // there, with whatever the owner opened, unless it changed.
    const items = node.querySelector(".job-items");
    const key = JSON.stringify(e.items);
    if (items.dataset.key !== key) {
      items.dataset.key = key;
      items.replaceChildren(...failItems(e.items));
    }
    items.hidden = !e.items.length;
    node.querySelector(".job-advice").textContent = e.advice;
    const retry = node.querySelector(".job-retry");
    retry.hidden = !["apply", "verify", "rebake-ci", "upgrade", "remove-guests"].includes(view.action) || (reviewed && !sameReview(view));
    retry.textContent = view.action === "verify" ? "Run the checks again" : view.action === "upgrade" ? "Update again" : sameReview(view) ? "Run setup again" : "Review and run again";
    node.querySelector(".job-agent").hidden = !(state && state.chat && state.chat.signed_in);
  }
  if (view.state !== "running") node.querySelector(".job-stop-result").textContent = "";
  drawOutcome(node, view);
  const v = p.verify;
  const box = node.querySelector(".job-verify");
  box.hidden = !v;
  if (v) {
    const c = v.counts;
    const part = (n, word) => [make("b", {textContent: String(n)}), ` ${word}`];
    box.querySelector(".counts").replaceChildren(...part(c.PASS, "passed"), " · ", ...part(c.FAIL, "failed"), " · ", ...part(c.DEGRADED, "degraded"), " · ", ...part(c.SKIP, "skipped"));
    const list = box.querySelector(".problems");
    const key = JSON.stringify(v.problems);
    if (list.dataset.key !== key) {
      list.dataset.key = key;
      // Failed before degraded; what a check found on lines of its own.
      const rows = [...v.problems].sort((a, b) => (a.status === "FAIL" ? 0 : 1) - (b.status === "FAIL" ? 0 : 1));
      list.replaceChildren(...rows.map((x) => make("li", {className: x.status.toLowerCase()},
        status(x.status === "FAIL" ? "bad" : "warn", x.status === "FAIL" ? "Failed" : "Degraded"),
        make("div", {className: "check"}, make("span", {className: "check-name", textContent: x.name || x.text}),
          ...String(x.detail || "").split("; ").filter(Boolean).map((d) => make("span", {className: "check-detail", textContent: d}))))));
    }
    list.hidden = !v.problems.length;
    // A check that did not run is no pass: which, and why not, on request.
    const skipped = v.skipped || [];
    const more = box.querySelector(".skipped");
    more.hidden = !skipped.length;
    if (more.dataset.key !== JSON.stringify(skipped)) {
      more.dataset.key = JSON.stringify(skipped);
      more.querySelector("summary").textContent = `${count(skipped.length, "check")} did not run`;
      more.querySelector("ul").replaceChildren(...skipped.map((x) => make("li", {},
        make("div", {className: "check"}, make("span", {className: "check-name", textContent: x.name}),
          ...String(x.detail || "").split("; ").filter(Boolean).map((d) => make("span", {className: "check-detail", textContent: d}))))));
    }
  }
  const pre = node.querySelector(".job-log pre");
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent += view.text;
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

// What a finished run comes to, and what to do next. Drawn again when the
// state changes: what an update needs next is known once the page restarted.
function drawOutcome(node, view) {
  const p = view.progress;
  const ok = node.querySelector(".job-ok");
  ok.hidden = view.state !== "done";
  const next = node.querySelector(".job-ok-next");
  next.replaceChildren();
  if (view.state !== "done") return;
  const text = {
    probe: ["The hardware is read.", "Choose what to run next."],
    apply: ["Setup finished.", "Everything in the plan is in place; the checks ran at the end."],
    verify: ["The checks finished.", "No check failed."],
    upgrade: ["NullBill is up to date.", ""],
    "close-lan": ["The home network is closing.", "Open this page over Tailscale and press Keep it closed under Access."],
    "keep-lan-closed": ["The home network stays closed.", "Proxmox now answers only over Tailscale."],
    "open-lan": ["The home network is open again.", "Proxmox answers on your home network."],
    "rebake-ci": ["The CI images are rebuilt.", ""],
    "remove-guests": ["Setup finished.", "The guests you chose to delete are gone; everything else in the plan is in place."],
  }[view.action] || ["Finished.", ""];
  // One stage is not the server: say which ran, and that nothing else was touched or checked.
  if (view.only) { text[0] = "This stage finished."; text[1] = `Only "${view.only.text}" ran. The rest of the server was left as it was${view.only.name === "verify" ? "" : ", and the checks did not run"}.`; }
  // An orange outcome says in words what it is orange for.
  const warned = p.verify && p.verify.counts.DEGRADED ? p.verify.problems.filter((x) => x.status === "DEGRADED").map((x) => x.text) : [];
  const degraded = `No check failed; ${warned.length === 1 ? "one is degraded" : `${warned.length} are degraded`}, listed below.`;
  if (warned.length && view.action === "verify") text[1] = degraded;
  else if (warned.length && view.only) text[1] = `${text[1]} ${degraded}`;
  else if (warned.length && SETUP_RUNS.has(view.action)) text[1] = `${text[1].split("; ")[0].replace(/\.$/, "")}. ${degraded}`;
  let warn = warned.length > 0;
  if (view.action === "probe") next.append(make("a", {className: "btn small", href: "#setup/choices", textContent: "Choose what to run"}));
  if (view.action === "apply" && !view.only) next.append(make("a", {className: "btn small", href: "#services", textContent: "Open your services"}));
  if (view.action === "upgrade") {
    // Did this run move NullBill, and does the server have to follow?
    const v = state && state.version;
    const moved = v && v.last && v.last.at >= view.started - 1 ? v.last : null;
    if (moved) {
      text[0] = `NullBill is updated to ${v.current.id.slice(0, 7)}.`;
      text[1] = sentence(`${count(moved.changes.length, "change")}, ${needSetup(moved.changes)}`);
    } else if (v) {
      text[1] = "There was nothing newer to install.";
    }
    if (v && built() && v.server.follows === false) {
      warn = true;
      text[1] += " The server follows once you run setup; until then it runs as before.";
      next.append(make("a", {className: "btn primary small", href: "#setup/build", textContent: "Review and run setup"}));
    } else if (moved && built()) {
      text[1] += " The server needs nothing more.";
    }
    next.append(make("a", {className: "btn small", href: "#updates", textContent: "Updates"}));
  }
  ok.classList.toggle("warn", warn);
  ok.querySelector("use").setAttribute("href", warn ? "#i-alert" : "#i-check");
  node.querySelector(".job-ok-title").textContent = text[0];
  node.querySelector(".job-ok-text").textContent = text[1];
}

async function copyDetails(node) {
  const view = job.view;
  const e = view.progress.error;
  const log = node.querySelector(".job-log pre").textContent.trimEnd().split("\n").slice(-60).join("\n");
  const text = [
    `${view.progress.title}: ${view.state} (nullbill ${view.args.join(" ")})`,
    e ? `${e.stage ? `Stage ${e.stage}: ` : ""}${e.message}` : "",
    ...(e ? e.items : []).map((i) => [[i.title, i.text].filter(Boolean).join(": "), i.detail].filter(Boolean).join("\n")),
    "Last lines of the log:",
    log,
  ].filter(Boolean).join("\n\n");
  const result = node.querySelector(".job-copy-result");
  try {
    await copyText(text);
    say(result, "ok", "Copied. It holds addresses on your network and guest names, no passwords.");
  } catch {
    const area = make("textarea", {value: text, readOnly: true, className: "copy-area"});
    node.querySelector(".job-error .row").after(area);
    area.select();
    say(result, "info", "Select all of the text below and copy it.");
  }
}

async function startJob(action, button, feedback) {
  const setup = action === "apply" || action === "remove-guests";
  if (setup) {
    if (button.getAttribute("aria-disabled") === "true") { say(feedback, "info", button.dataset.why || RUNNING_WHY); return; }
    action = (state.removals || []).length ? "remove-guests" : "apply";
    if (action === "remove-guests" && !(await confirmRemovals(button))) {
      say(feedback, "info", "Nothing was started.");
      return;
    }
  }
  if (action === "upgrade") {
    const fresh = (state.version && state.version.available) || [];
    const ok = await confirmDialog({
      title: "Update NullBill now?",
      text: `${fresh.length ? `It installs ${count(fresh.length, "change")}. ` : ""}This page restarts and is away for about a minute, then reloads by itself. Nothing that runs on the server is touched${fresh.some((c) => c.setup) ? "; what changes there waits for the setup run you start afterwards" : ""}.`,
      yes: "Update", danger: false,
    });
    button.focus();
    if (!ok) return;
  }
  let changed = false;
  await press(button, feedback, STARTING[action] || "Starting…", async () => {
    // Setup names the review the owner read, and deleting names the guests
    // they were shown; the server refuses when either has changed since.
    const body = setup ? {action, review: state.review.id, ...(action === "remove-guests" ? {guests: state.removals} : {})} : {action};
    let view;
    try {
      view = await api("/api/jobs", body);
    } catch (error) {
      if (!error.review) throw error;
      changed = true;
      return "";
    }
    follow(view, true, feedback);
    refresh().catch(() => {});
    // A run started where it is reviewed shows there: setup on its review,
    // an update on Updates, which is also where the page comes back to.
    const here = (currentView === "setup" && currentStep === "build" && SETUP_RUNS.has(action)) || (currentView === "updates" && action === "upgrade");
    if (OPENS_RUN.has(action) && !here) go(`activity/${view.id}`);
    else placeJob();
    if (action === "upgrade" && here) return "Updating. This page reloads by itself when the new version runs.";
    return OPENS_RUN.has(action) ? "Started." : "Started. The top bar follows it.";
  });
  if (changed) {
    await refresh().catch(() => {});
    go("setup/build");
    say("build-result", "info", "Nothing was started: what setup would do changed while this page was open. It is shown again above. Read it, then start setup.");
  }
}

function feedbackFor(button) {
  if (button.closest(".topbar")) return $("actions-result");
  const row = button.closest(".row");
  return row ? row.querySelector(".feedback") : $("actions-result");
}

function submitter(event) { return event.submitter || event.target.querySelector("button[type=submit]"); }

// The agent's sign-in, from either of its forms.
function signInAgent(event, fieldId, resultId) {
  event.preventDefault();
  const field = $(fieldId);
  press(submitter(event), resultId, "Checking and saving…", async () => {
    if (!field.value.trim()) throw new ApiError("Paste the API key or token into the field first.", 400);
    const result = await api("/api/agent", {credential: field.value});
    field.value = "";
    state.agent_credential = result.agent_credential;
    if (result.chat) state.chat = result.chat;
    renderAgentSignin(state);
    paintChatShell();
    paintStates();
    pollChat();
    if (fieldId !== "chat-credential") openAgent(false);
    return result.chat && !result.chat.installed
      ? "Saved, but the agent program is not installed here yet: press Update NullBill on the Updates page."
      : "Signed in. The agent says hello in its panel.";
  }, field);
}

// The chat with the agent.
const chat = {
  seq: 0, timer: null, running: false, busy: false, pending: [], asks: new Set(), nodes: new Map(),
  typing: null, seen: 0, latest: 0, mode: "manual", failures: 0,
  // The agent's checklist that is still current, and pictures waiting to be sent.
  plan: null, pictures: [], task: null,
};

function stopChat() {
  chat.running = false;
  clearTimeout(chat.timer);
}

function startChat() {
  if (chat.running) return;
  chat.running = true;
  pollChat();
}

function scheduleChat() {
  clearTimeout(chat.timer);
  if (!chat.running) return;
  const quick = chat.busy || chat.pending.length;
  const delay = chat.failures ? Math.min(15000, 2000 * chat.failures) : quick ? 1200 : chatVisible() ? 4000 : 12000;
  chat.timer = setTimeout(pollChat, delay);
}

async function pollChat() {
  if (!state) return;
  try {
    const view = await api(`/api/chat?after=${chat.seq}`);
    chat.failures = 0;
    applyChat(view);
  } catch (error) {
    chat.failures += 1;
    if (error.status === 401) return;
  }
  scheduleChat();
}

function clearChatLog() {
  $("chat-log").replaceChildren();
  chat.nodes.clear();
  chat.asks.clear();
  chat.typing = null;
  chat.plan = null;
}

function applyChat(view) {
  // The server's count went back (its files were reset): start over.
  if (view.seq < chat.seq) {
    chat.seq = 0;
    clearChatLog();
    pollChat();
    return;
  }
  const log = $("chat-log");
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  let news = false;
  let refreshNeeded = false;
  for (const event of view.events) {
    if (event.seq <= chat.seq) continue;
    chat.seq = event.seq;
    if (event.reset) clearChatLog();
    renderEvent(event);
    if (["assistant", "proposal", "ask", "refused"].includes(event.kind)) { news = true; chat.latest = event.seq; }
    if (event.kind === "job" || (event.kind === "proposal_update" && ["done", "failed"].includes(event.state)) || (event.kind === "turn" && event.state === "end")) refreshNeeded = true;
  }
  const wasBusy = chat.busy;
  const hadPending = chat.pending.length;
  chat.busy = view.busy;
  chat.queued = view.queued || 0;
  chat.pending = view.pending || [];
  // A request that is no longer pending on the server has expired.
  for (const [id, node] of chat.nodes) {
    if (node.classList.contains("request") && node.dataset.state === "pending" && !chat.pending.includes(id)) settleRequest(node, "expired");
  }
  const SETTINGS = ["mode", "auto_fix", "model", "effort"];
  if (view.mode && state && state.chat && SETTINGS.some((k) => view[k] !== state.chat[k])) {
    for (const k of SETTINGS) state.chat[k] = view[k];
    renderMode(state.chat);
  }
  chat.task = view.task || null;
  paintTask();
  if (state && state.chat) { state.chat.busy = view.busy; state.chat.pending = chat.pending.length; }
  paintTyping();
  paintChatShell();
  if (news && atEnd || view.events.some((e) => e.kind === "user")) log.scrollTop = log.scrollHeight;
  if (news && chatVisible()) markSeen();
  if (news && !chatVisible()) {
    const last = view.events.filter((e) => ["assistant", "proposal", "ask"].includes(e.kind)).at(-1);
    if (last && last.kind !== "assistant") announce("The agent needs you.", true);
  }
  paintChatBadge();
  if (state && hadPending !== chat.pending.length) paintStates();
  else if (state) paintBanner();
  if (refreshNeeded || (wasBusy && !view.busy)) refreshSoon();
}

function markSeen() { chat.seen = chat.latest; paintChatBadge(); }

function paintChatBadge() {
  const waiting = chat.pending.length + [...chat.asks].length;
  const unread = chat.latest > chat.seen && !chatVisible();
  const text = waiting ? String(waiting) : unread ? "1" : "";
  const label = waiting ? `${waiting} waiting for you` : unread ? "new message" : "";
  setCount($("tab-agent-badge"), text, waiting ? "warn" : "", label);
  setCount($("agent-count"), text, waiting ? "warn" : "", label);
  if ("setAppBadge" in navigator) (waiting ? navigator.setAppBadge(waiting) : navigator.clearAppBadge()).catch(() => {});
  setNav("agent", waiting ? String(waiting) : "", "warn", label);
}

// An empty conversation: what the agent is for, and where to start.
function chatEmpty() {
  return make("div", {className: "chat-empty"},
    make("span", {className: "chat-empty-mark", ariaHidden: "true"}, icon("agent")),
    make("b", {textContent: "Ask about your server"}),
    make("span", {textContent: "The agent reads logs and runs checks by itself, and asks before any change your access mode keeps for you."}),
    make("div", {className: "starters", role: "group", ariaLabel: "Suggestions"}));
}

function paintChatShell() {
  if (!state) return;
  const c = state.chat || {};
  const signed = Boolean(c.signed_in);
  show("chat-signin", !signed);
  show("chat-log", signed);
  $("composer").hidden = !signed;
  show("chat-new", signed);
  show("chat-copy", signed);
  const missing = signed && c.installed === false;
  show("chat-notice", missing);
  if (missing) $("chat-notice").textContent = "The agent's program is not on the server yet. Press Update NullBill in the menu beside Run setup, then ask again.";
  const log = $("chat-log");
  const empty = log.querySelector(".chat-empty");
  const hasMessages = [...log.children].some((n) => !n.classList.contains("chat-empty") && !n.classList.contains("typing"));
  if (signed && !hasMessages && !empty) log.prepend(chatEmpty());
  if (hasMessages && empty) empty.remove();
  $("chat").classList.toggle("blank", signed && !hasMessages);
  paintChips();
  $("chat").classList.toggle("working", chat.busy);
  $("chat").classList.toggle("asking", chat.pending.length > 0);
  show("chat-stop", chat.busy);
}

function paintTyping() {
  const log = $("chat-log");
  if (chat.busy) {
    if (!chat.typing) {
      chat.typing = make("div", {className: "typing"}, make("i"), make("i"), make("i"), make("span"));
      chat.typing.setAttribute("aria-hidden", "true");
    }
    chat.typing.lastChild.textContent = chat.queued ? `Working, then ${chat.queued} more message${chat.queued === 1 ? "" : "s"} of yours` : "Working";
    log.append(chat.typing);
  } else if (chat.typing) {
    chat.typing.remove();
    chat.typing = null;
  }
}

function addToLog(node) {
  const log = $("chat-log");
  const empty = log.querySelector(".chat-empty");
  if (empty) empty.remove();
  if (chat.typing && chat.typing.parentElement === log) log.insertBefore(node, chat.typing);
  else log.append(node);
  return node;
}

const TIER_TEXT = {read: "Reads only", change: "Makes a change", risky: "Risky", blocked: "Blocked"};

function turnMeta(e) {
  const parts = [...(e.model ? [e.model] : []), ...(e.effort ? [`${e.effort} thinking`] : []), `${e.seconds} s`];
  if (e.steps) parts.push(`${e.steps} step${e.steps === 1 ? "" : "s"}`);
  if (e.cost && state && state.agent_credential === "API key") parts.push(`$${Number(e.cost).toFixed(2)}`);
  return parts.join(" · ");
}

// A unified diff, each line coloured by what it does.
function diffView(text) {
  const pre = make("pre", {className: "diff", tabIndex: 0});
  for (const line of String(text).split("\n")) {
    const kind = line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "";
    pre.append(make("span", {className: kind, textContent: `${line}\n`}));
  }
  return pre;
}

let cardCount = 0;

function renderEvent(e) {
  switch (e.kind) {
    case "user": {
      // What the owner says next is about something new: a later plan is a new card.
      chat.plan = null;
      const pictures = (e.images || []).map((image, n) => {
        const src = `/api/chat/image?id=${encodeURIComponent(image.id)}`;
        const label = `Picture ${n + 1}, opens in a new tab`;
        return make("a", {href: src, target: "_blank", rel: "noopener", className: "msg-picture", title: label, ariaLabel: label},
          make("img", {src, alt: `Picture ${n + 1} you attached`, loading: "lazy"}));
      });
      addToLog(make("div", {className: "msg user"},
        pictures.length ? make("div", {className: "msg-pictures"}, ...pictures) : null,
        e.text ? make("span", {textContent: e.text}) : null));
      break;
    }
    case "assistant":
      addToLog(make("div", {className: "msg assistant"}, ...markdown(e.text)));
      break;
    case "system":
      // A new conversation shows as the empty one it is.
      if (!e.reset) addToLog(make("div", {className: "msg system", textContent: e.text}));
      break;
    case "error":
      addToLog(make("div", {className: "msg error", textContent: e.text}));
      break;
    case "turn":
      if (e.state !== "end") break;
      if (e.error) {
        const node = addToLog(make("div", {className: "msg error"}, make("p", {textContent: e.error})));
        // What the agent's program printed, for whoever wants to read it.
        if (e.detail) node.append(make("details", {className: "fail-more"}, make("summary", {textContent: "Details"}), outputBox(make("pre", {textContent: e.detail}))));
        if (/carry on/i.test(e.error)) {
          const chip = make("button", {type: "button", className: "chip", textContent: "Carry on"});
          chip.addEventListener("click", () => askAgent("Carry on."));
          node.append(make("div", {className: "chips"}, chip));
        }
      }
      if (e.seconds) addToLog(make("div", {className: "turn-meta", textContent: turnMeta(e)}));
      break;
    case "ran": {
      // What ran without a card: under the access mode, or under a task approved once.
      const node = addToLog(make("div", {className: "ran"},
        make("span", {className: `tier ${e.tier}`, textContent: e.task ? "Ran for the task" : `Ran in ${MODE_NAMES[e.mode] || e.mode}`}),
        make("span", {textContent: e.title}),
        e.command ? make("code", {textContent: e.command}) : null));
      if (e.lines && e.lines.length) node.append(make("ul", {className: "says"}, ...e.lines.map((l) => li(l))));
      if (e.diff) node.append(make("details", {className: "ran-diff"}, make("summary", {textContent: "What changed"}), diffView(e.diff)));
      break;
    }
    case "plan":
      if (chat.plan && chat.plan.isConnected) paintPlan(chat.plan, e.steps || []);
      else chat.plan = addToLog(paintPlan(make("div", {className: "card plan"}), e.steps || []));
      break;
    case "task":
      if (e.state === "start") {
        addToLog(make("div", {className: "msg system"}, make("b", {textContent: "Task approved: "}), e.title, ". ", taskScope(e)));
      } else {
        addToLog(make("div", {className: "msg system"}, make("b", {textContent: "Task ended: "}), e.title, "."));
        if (e.by === "agent" && e.text) addToLog(make("div", {className: "msg assistant"}, ...markdown(e.text)));
      }
      break;
    case "tool": {
      const node = make("details", {className: "tool"},
        make("summary", {}, make("span", {className: "t-mark", ariaHidden: "true"}), make("span", {className: "t-label", textContent: e.label, title: e.label})));
      node.dataset.state = "running";
      if (e.id) chat.nodes.set(`tool-${e.id}`, node);
      addToLog(node);
      break;
    }
    case "tool_done": {
      const node = chat.nodes.get(`tool-${e.id}`);
      if (!node) break;
      node.dataset.state = e.ok ? "ok" : "fail";
      node.querySelector(".t-mark").replaceChildren(icon(e.ok ? "check" : "x"));
      if (e.output) node.append(...shownOutput(e.output, e.full));
      else node.querySelector("summary").addEventListener("click", (ev) => ev.preventDefault());
      break;
    }
    case "proposal":
      addToLog(requestCard(e));
      break;
    case "proposal_update": {
      const node = chat.nodes.get(e.id);
      if (node) settleRequest(node, e.state, e.output, e.full, e.says);
      break;
    }
    case "ask":
      addToLog(askCard(e));
      break;
    case "answer": {
      const node = chat.nodes.get(`ask-${e.id}`);
      if (node) settleAsk(node, e.text);
      chat.asks.delete(e.id);
      break;
    }
    case "refused":
      addToLog(make("div", {className: "card refused"},
        make("div", {className: "card-head"}, icon("shield"), make("b", {className: "card-title", textContent: "Blocked by the page"})),
        make("div", {className: "card-body"},
          make("span", {textContent: e.title}),
          e.command ? make("div", {className: "cmd", textContent: e.command}) : null,
          make("span", {className: "why", textContent: `Refused in every mode: ${e.why}.`}))));
      break;
    case "job":
      addToLog(make("div", {className: "msg system"}, `Started: ${e.title || JOB_NAMES[e.action] || e.action}. `, make("a", {href: e.id ? `#activity/${e.id}` : "#activity", textContent: "Follow it"})));
      break;
    default:
      break;
  }
}

// The agent's checklist for a longer job. One card, kept current in place.
const STEP_TEXT = {done: "Done", doing: "In progress", todo: "Not started"};
function paintPlan(card, steps) {
  const done = steps.filter((s) => s.state === "done").length;
  const doing = steps.find((s) => s.state === "doing");
  card.replaceChildren(
    make("div", {className: "card-head"}, icon("checks"), make("b", {className: "card-title", textContent: "Plan"}),
      make("span", {className: "plan-count", textContent: `${done} of ${steps.length} done`})),
    make("ol", {className: "plan-list"}, ...steps.map((s) => {
      const mark = make("span", {className: "plan-mark", ariaHidden: "true"}, s.state === "done" ? icon("check") : null);
      const item = make("li", {}, mark, make("span", {textContent: s.text}), make("span", {className: "sr-only", textContent: `: ${STEP_TEXT[s.state] || s.state}`}));
      item.dataset.state = s.state;
      return item;
    })));
  card.dataset.doing = doing ? doing.text : "";
  return card;
}

function taskScope(task) {
  const guests = (task.guests || []).join(", ");
  return `The agent changes things inside ${guests}${task.jobs ? " and runs setup" : ""} without asking each time. Risky steps still ask.`;
}

// The task that is on, above the conversation, with the way to end it.
function paintTask() {
  const task = chat.task;
  show("chat-task", Boolean(task));
  if (!task) return;
  $("chat-task-title").textContent = `Task: ${task.title}`;
  $("chat-task-scope").textContent = taskScope(task);
}

function requestCard(e) {
  const card = make("div", {className: "card request"});
  card.dataset.tier = e.tier;
  card.dataset.state = "pending";
  card.dataset.id = e.id;
  const where = e.where && e.where !== "host" ? e.where : "Proxmox";
  const result = make("p", {className: "feedback"});
  result.setAttribute("role", "status");
  const titleId = `req-title-${++cardCount}`;
  card.setAttribute("role", "group");
  card.setAttribute("aria-labelledby", titleId);
  const body = make("div", {className: "card-body"});
  card.append(make("div", {className: "card-head"}, icon(e.tier === "risky" ? "alert" : "shield"),
    make("b", {id: titleId, className: "card-title", textContent: e.act === "command" && !e.lines ? `Run on ${where}${e.background ? " in the background" : ""}` : e.title}),
    make("span", {className: `tier ${e.tier}`, textContent: TIER_TEXT[e.tier] || e.tier})), body);
  if (e.act === "command") body.append(make("div", {className: "cmd", textContent: e.command}));
  if (e.act === "steps") {
    body.append(make("ol", {className: "steps-list"}, ...(e.steps || []).map((step) => make("li", {},
      make("span", {className: "step-where", textContent: step.where === "host" ? "Proxmox" : step.where}),
      make("span", {className: `tier ${step.tier}`, textContent: TIER_TEXT[step.tier] || step.tier}),
      make("div", {className: "cmd", textContent: step.command})))));
  }
  if (e.act === "write" && e.diff) body.append(diffView(e.diff));
  if (e.act === "task" && e.plan && e.plan.length) body.append(make("ol", {className: "says plan-steps"}, ...e.plan.map((l) => li(l))));
  if (e.lines && e.lines.length) body.append(make("ul", {className: "says"}, ...e.lines.map((l) => li(l))));
  // A service of the owner's own: what it is first, then its whole setup script.
  if (e.act === "service" && e.diff) body.append(diffView(e.diff));
  // One approval for the whole job: what the page does next, said before the button.
  if (e.then && e.then.length) {
    body.append(make("div", {className: "then"}, make("b", {textContent: "If you approve, in one go:"}),
      make("ul", {className: "says"}, ...e.then.map((l) => li(l)))));
  }
  if (e.why) body.append(make("p", {className: "says", textContent: e.why}));
  body.append(make("p", {className: "why", textContent: `Asked because of: ${e.reason}.`}));
  const approve = make("button", {type: "button", className: `btn ${e.tier === "risky" ? "danger" : "primary"} small`, textContent: "Approve"});
  approve.setAttribute("aria-keyshortcuts", "Alt+Y");
  const decline = make("button", {type: "button", className: "btn small", textContent: "Decline"});
  decline.setAttribute("aria-keyshortcuts", "Alt+N");
  decline.dataset.role = "decline";
  approve.dataset.role = "approve";
  const actions = make("div", {className: "actions"});
  let word = null;
  if (e.tier === "risky" && e.word) {
    word = make("input", {autocomplete: "off", spellcheck: false, placeholder: e.word, className: "word"});
    word.setAttribute("aria-label", `Type ${e.word} to approve`);
    actions.append(make("label", {className: "field narrow"}, make("span", {className: "label", textContent: `Type ${e.word} to approve`}), word));
    word.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); approve.click(); } });
  }
  actions.append(approve, decline, result);
  body.append(actions);
  const decide = (yes) => press(yes ? approve : decline, result, yes ? "Approving…" : "Declining…", async () => {
    await api("/api/chat/decide", {id: e.id, approve: yes, typed: word ? word.value : ""});
    announce(`${yes ? "Approved" : "Declined"}: ${e.title}`, true);
    card.tabIndex = -1;
    card.focus();
    pollChat();
    return "";
  }, yes ? word : null);
  approve.addEventListener("click", () => decide(true));
  decline.addEventListener("click", () => decide(false));
  chat.nodes.set(e.id, card);
  if (e.state && e.state !== "pending") settleRequest(card, e.state);
  return card;
}

const SETTLED = {
  running: ["run", "Approved, running"], done: ["good", "Done"], failed: ["bad", "Failed"],
  declined: ["", "Declined"], expired: ["", "Expired: not answered in 30 minutes"],
};

function settleRequest(card, stateName, output, full, says) {
  card.dataset.state = stateName;
  if (stateName === "pending") return;
  card.classList.add("settled");
  const actions = card.querySelector(".actions");
  if (actions) actions.remove();
  const body = card.querySelector(".card-body");
  let line = card.querySelector(".settled-line");
  if (!line) {
    line = make("p", {className: "settled-line"});
    body.append(line);
  }
  const [kind, text] = SETTLED[stateName] || ["", stateName];
  line.replaceChildren(status(kind, text));
  if (says) {
    // The page's own word on what it did: a sentence, not a terminal's output.
    for (const old of card.querySelectorAll(".result")) old.remove();
    body.append(make("div", {className: "result said"}, ...markdown(says)));
  } else if (output) {
    for (const old of card.querySelectorAll(".result")) old.remove();
    const shown = shownOutput(output, full);
    for (const node of shown) node.classList.add("result");
    body.append(...shown);
  }
}

function askCard(e) {
  const card = make("div", {className: "card ask"});
  card.dataset.id = e.id;
  const result = make("p", {className: "feedback"});
  result.setAttribute("role", "status");
  const input = make("input", {autocomplete: "off", placeholder: "Your answer"});
  input.setAttribute("aria-label", "Your answer");
  const send = make("button", {type: "submit", className: "icon-btn send", title: "Send answer"}, icon("send"), make("span", {className: "sr-only", textContent: "Send answer"}));
  const form = make("form", {className: "answer-form"}, input, send);
  const answer = (text) => press(send, result, "Sending…", async () => {
    if (!text.trim()) throw new ApiError("Write or pick an answer first.", 400);
    await api("/api/chat/answer", {id: e.id, text});
    pollChat();
    return "";
  }, input);
  form.addEventListener("submit", (ev) => { ev.preventDefault(); answer(input.value); });
  const options = make("div", {className: "chips"}, ...(e.options || []).map((o) => {
    const chip = make("button", {type: "button", className: "chip", textContent: o});
    chip.addEventListener("click", () => answer(o));
    return chip;
  }));
  card.append(make("div", {className: "card-head"}, icon("agent"), make("b", {className: "card-title", textContent: "The agent asks"})),
    make("div", {className: "card-body"},
      make("div", {className: "msg assistant"}, ...markdown(e.question)),
      (e.options || []).length ? options : null,
      make("div", {className: "answer-box"}, form, result)));
  chat.nodes.set(`ask-${e.id}`, card);
  chat.asks.add(e.id);
  return card;
}

function settleAsk(card, text) {
  card.classList.add("settled");
  const box = card.querySelector(".answer-box");
  if (box) box.remove();
  const chips = card.querySelector(".card-body > .chips");
  if (chips) chips.remove();
  card.querySelector(".card-body").append(make("p", {className: "says"}, make("b", {textContent: "You: "}), text));
}

// Pictures for the next message. Each is drawn smaller than MAX_SIDE and
// sent as PNG while that stays small, else as JPEG: a phone's screenshot is
// several megabytes, and the model reads nothing finer than this anyway.
const MAX_SIDE = 1568;
const MAX_PICTURES = 4;
const MAX_PICTURE_BYTES = 3 * 1024 * 1024;
const PNG_UP_TO = 1024 * 1024;

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

function blobBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function readPicture(file) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new ApiError(`${file.name || "That file"} is not a picture this browser can read. Send a PNG or JPEG.`, 400);
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = make("canvas", {width: Math.max(1, Math.round(bitmap.width * scale)), height: Math.max(1, Math.round(bitmap.height * scale))});
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  let blob = file.type === "image/jpeg" ? null : await canvasBlob(canvas, "image/png");
  if (!blob || blob.size > PNG_UP_TO) blob = await canvasBlob(canvas, "image/jpeg", 0.85);
  if (!blob || blob.size > MAX_PICTURE_BYTES) throw new ApiError("That picture is too large even made smaller. Crop it to the part that matters.", 400);
  const thumb = make("canvas", {width: 112, height: 112, className: "thumb"});
  const side = Math.min(bitmap.width, bitmap.height);
  thumb.getContext("2d").drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side, 0, 0, 112, 112);
  if (bitmap.close) bitmap.close();
  return {type: blob.type, data: await blobBase64(blob), thumb, name: file.name || "screenshot"};
}

async function addPictures(files) {
  const list = [...files].filter((f) => f.type.startsWith("image/"));
  if (!list.length) return;
  if (!(state && state.chat && state.chat.signed_in)) return;
  say("composer-result", "", "");
  for (const file of list) {
    if (chat.pictures.length >= MAX_PICTURES) {
      say("composer-result", "info", `A message takes up to ${MAX_PICTURES} pictures. Send these, then attach the rest.`);
      break;
    }
    try {
      chat.pictures.push(await readPicture(file));
    } catch (error) {
      say("composer-result", "error", error.message);
    }
  }
  paintPictures();
  $("composer-text").focus();
}

function paintPictures() {
  const box = $("composer-pictures");
  show(box, chat.pictures.length > 0);
  box.replaceChildren(...chat.pictures.map((picture, n) => {
    const label = `Remove picture ${n + 1}`;
    const remove = make("button", {type: "button", className: "icon-btn thumb-x", title: label, ariaLabel: label}, icon("x"));
    remove.addEventListener("click", () => {
      chat.pictures.splice(n, 1);
      paintPictures();
      announce(`Picture ${n + 1} removed.`);
      $("composer-text").focus();
    });
    picture.thumb.setAttribute("role", "img");
    picture.thumb.setAttribute("aria-label", `Picture ${n + 1}: ${picture.name}`);
    return make("li", {}, picture.thumb, remove);
  }), make("li", {className: "composer-pictures-note", textContent: "Sent to Claude as they are. Leave out a picture that shows a password."}));
}

async function sendChat(text, feedback, pictures) {
  text = text.trim();
  const images = (pictures || []).map((p) => ({type: p.type, data: p.data}));
  if (!text && !images.length) return false;
  if (!(state.chat && state.chat.signed_in)) {
    openAgent(true);
    say("chat-signin-result", "info", "Sign the agent in first; then send your message again.");
    return false;
  }
  try {
    const view = await api("/api/chat", {text, after: chat.seq, ...(images.length ? {images} : {})});
    applyChat(view);
    scheduleChat();
    say(feedback, "", "");
    return true;
  } catch (error) {
    say(feedback, "error", error.message);
    return false;
  }
}

// From anywhere on the page: open the agent and send.
async function askAgent(text) {
  openAgent(false);
  const sent = await sendChat(text, "composer-result");
  if (!sent && state.chat && state.chat.signed_in) $("composer-text").focus();
}

// Suggestions that fit where setup stands.
function suggestions() {
  if (!state) return [];
  const out = [];
  const apply = state.last_jobs.apply;
  if (job && job.view && job.view.state === "failed") out.push([`Why did ${JOB_NAMES[job.view.action] || "the run"} stop?`, `The run ${job.view.id} stopped. Find out why and propose the fix.`]);
  if (!state.probed) out.push(["Read the hardware for me", "Read the hardware and tell me what this machine can run."]);
  if (state.plan.errors.length) out.push(["Make my plan fit", "The plan does not fit. Tell me the smallest change that makes it fit."]);
  if (state.probed && !state.choices.owner_login) out.push(["What should I run?", "Look at my hardware and suggest what this server should run, in a few lines."]);
  if (state.plan.text && !apply) out.push(["Explain the plan", "Explain in a few lines what the plan builds and how long the first setup run takes."]);
  if (built()) out.push(["Is everything healthy?", "Check the server's health and tell me anything that needs attention."]);
  if (built() && state.lan.state === "open") out.push(["Close the home network", "Walk me through reaching NullBill over Tailscale and closing Proxmox to the home network."]);
  if (built()) out.push(["Anything to update?", "Are there updates waiting for Proxmox, the guests or NullBill itself?"]);
  out.push(["What next?", "What should I do next?"]);
  return out.slice(0, 4);
}

// An empty conversation lists them in full, where there is room for each; a
// conversation under way keeps a few above the box, wrapped, never cut off.
function paintChips() {
  const signed = Boolean(state && state.chat && state.chat.signed_in);
  const starters = $("chat-log").querySelector(".starters");
  const items = signed ? suggestions() : [];
  const box = starters || $("chat-chips");
  show("chat-chips", signed && !starters && items.length > 0);
  if (starters) $("chat-chips").dataset.key = "";
  const shown = starters ? items : items.slice(0, 2);
  const key = JSON.stringify(shown);
  if (box.dataset.key === key) return;
  box.dataset.key = key;
  box.replaceChildren(...shown.map(([label, text]) => {
    const chip = starters
      ? make("button", {type: "button", className: "starter"}, make("span", {textContent: label}), icon("arrow"))
      : make("button", {type: "button", className: "chip", textContent: label});
    chip.addEventListener("click", () => askAgent(text));
    return chip;
  }));
}

async function saveSettings(body) {
  const result = await api("/api/chat/settings", body);
  for (const k of ["mode", "auto_fix", "model", "effort"]) state.chat[k] = result[k];
  renderMode(state.chat);
  pollChat();
  return result;
}

// The access mode, from the agent page or the panel's menu. YOLO asks for
// the word first.
async function setMode(mode, feedback, focusAfter) {
  if (mode === state.chat.mode) return;
  if (mode === "yolo") {
    const ok = await confirmDialog({title: "Let the agent act without asking?", text: "In YOLO it runs every command, script and setup run it decides on, without waiting for you, including reboots and deleting. What is always blocked stays blocked, and every action is logged.", yes: "Turn on YOLO", word: "yolo"});
    if (!ok) { if (focusAfter) focusAfter.focus(); return; }
  }
  try {
    say(feedback, "busy", "Saving…");
    await saveSettings({mode});
    say(feedback, "ok", `${MODE_NAMES[mode]}: ${MODE_DESC[mode].replace(/\.$/, "").replace(/^./, (c) => c.toLowerCase())}, from the agent's next step.`);
    announce(`Access mode: ${MODE_NAMES[mode]}.`);
  } catch (error) {
    renderMode(state.chat);
    say(feedback, "error", error.message);
    toast(error.message);
  }
  if (focusAfter) focusAfter.focus();
}

// Menus.
function runMenu() {
  const button = $("run-menu");
  const start = (action) => () => {
    if (runningAction()) say("actions-result", "info", RUNNING_WHY);
    else startJob(action, button, $("actions-result"));
  };
  const v = state.version;
  const fresh = v ? v.available.length : 0;
  openMenu(button, [
    {label: "Run the checks", desc: "Every allowed and denied path, then a report. Changes nothing.", icon: "checks", run: start("verify")},
    {label: "Read the hardware", desc: "Changes nothing.", icon: "chip", run: start("probe")},
    {sep: true},
    {label: "Updates", desc: fresh ? `${count(fresh, "change")} to NullBill available.` : v ? `NullBill ${v.current.id.slice(0, 7)}.` : "", icon: "update", run: () => go("updates")},
  ]);
}
function themeName() { return document.documentElement.dataset.theme || "system"; }
function accountMenu() {
  const items = [
    {heading: "Theme"},
    ...[["system", "Match the system"], ["light", "Light"], ["dark", "Dark"]].map(([v, label]) => ({label, checked: themeName() === v, run: () => setTheme(v)})),
    {sep: true},
    {label: "Keyboard shortcuts", icon: "keyboard", run: () => $("keys").showModal()},
  ];
  if (installPrompt) items.push({label: "Install the app", icon: "download", run: install});
  items.push({sep: true}, {label: "Sign out", icon: "logout", run: logout});
  openMenu($("account"), items, "left");
}
// One press on the panel's button: asked first whenever there is something
// to lose, since an earlier conversation cannot be opened again.
async function newConversation() {
  if (!(state.chat && state.chat.signed_in) || $("chat").classList.contains("blank")) { openAgent(true); return; }
  openAgent(false);
  const text = chat.busy || chat.pending.length
    ? "The agent stops what it is doing, and waiting requests expire. This conversation leaves the page."
    : "This conversation leaves the page and the agent starts without it. What it did stays under Agent, Log.";
  const ok = await confirmDialog({title: "Start a new conversation?", text, yes: "Start over", danger: false});
  if (!ok) { $("chat-new").focus(); return; }
  try {
    await api("/api/chat/new", {});
    pollChat();
    $("composer-text").focus();
  } catch (error) { say("composer-result", "error", error.message); }
}

// Command palette.
function commands() {
  const out = [
    ...VIEWS.map((v) => ({label: $(`view-${v}`).dataset.title, kind: "Page", icon: {overview: "overview", agent: "agent", activity: "activity", alerts: "alerts", setup: "setup", access: "access", services: "services", backups: "history"}[v], run: () => go(v)})),
    ...STEPS.map((s) => ({label: `Setup: ${$("stepper").querySelector(`[data-step="${s}"]`).textContent.replace(/, (done|needs you)$/, "")}`, kind: "Step", icon: "setup", run: () => go(`setup/${s}`)})),
    {label: "Run setup: review, then start", kind: "Run", icon: "play", run: () => go("setup/build")},
    {label: "Run the checks", kind: "Run", icon: "checks", run: () => startJob("verify", $("run-menu"), $("actions-result"))},
    {label: "Update NullBill", kind: "Run", icon: "update", run: () => go("updates")},
    {label: "Check for updates", kind: "Run", icon: "update", run: () => { go("updates"); $("update-check").click(); }},
    {label: "Read the hardware", kind: "Run", icon: "chip", run: () => { go("setup/hardware"); $("probe-button").click(); }},
    {label: "Open the agent", kind: "Agent", icon: "agent", run: () => openAgent(true)},
    {label: "New conversation with the agent", kind: "Agent", icon: "new-chat", run: newConversation},
    {label: "Copy the conversation", kind: "Agent", icon: "copy", run: () => copyConversation()},
    {label: "Stop the agent", kind: "Agent", icon: "stop", run: () => api("/api/chat/stop", {}).then(pollChat).catch((e) => toast(e.message))},
    {label: "Access mode", kind: "Agent", icon: "shield", run: () => { go("agent/settings"); setTimeout(() => document.querySelector('#modes [aria-pressed="true"]').focus(), 30); }},
    {label: "What the agent did", kind: "Agent", icon: "history", run: () => go("agent/log")},
    {label: "Setup guide", kind: "Help", icon: "check", run: openGuide},
    {label: "Tailscale", kind: "Access", icon: "access", run: () => go("access/tailscale")},
    {label: "People and what each may open", kind: "Access", icon: "access", run: () => go("access/people")},
    {label: "Home network", kind: "Access", icon: "lock", run: () => go("access/lan")},
    {label: "Trust this server, install the app", kind: "Access", icon: "download", run: () => go("access/trust")},
    {label: "Theme: match the system", kind: "Look", icon: "moon", run: () => setTheme("system")},
    {label: "Theme: light", kind: "Look", icon: "sun", run: () => setTheme("light")},
    {label: "Theme: dark", kind: "Look", icon: "moon", run: () => setTheme("dark")},
    {label: "Keyboard shortcuts", kind: "Help", icon: "keyboard", run: () => $("keys").showModal()},
    {label: "Sign out", kind: "Account", icon: "logout", run: logout},
  ];
  for (const s of (state && state.services && state.services.items) || []) {
    if (s.url && built()) out.push({label: `Open ${s.title}`, kind: "Service", icon: SERVICE_ICONS[s.name] || "services", run: () => window.open(s.url, "_blank", "noopener")});
  }
  return out;
}

const palette = {items: [], index: 0};

function paintPalette() {
  const q = $("palette-q").value.trim().toLowerCase();
  const words = q.split(/\s+/).filter(Boolean);
  let items = commands().filter((c) => words.every((w) => `${c.label} ${c.kind}`.toLowerCase().includes(w)));
  if (q) {
    const ask = {label: `Ask the agent: “${$("palette-q").value.trim()}”`, kind: "Agent", icon: "agent", run: () => askAgent($("palette-q").value.trim())};
    items = items.length ? [...items.slice(0, 8), ask] : [ask];
  }
  palette.items = items.slice(0, 12);
  palette.index = Math.min(palette.index, Math.max(0, palette.items.length - 1));
  $("palette-list").replaceChildren(...palette.items.map((c, i) => {
    const item = make("li", {id: `pal-${i}`}, icon(c.icon), make("span", {textContent: c.label}), make("span", {className: "p-kind", textContent: c.kind}));
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", i === palette.index ? "true" : "false");
    item.addEventListener("click", () => runPalette(i));
    item.addEventListener("mousemove", () => { if (palette.index !== i) { palette.index = i; paintPaletteSelection(); } });
    return item;
  }));
  paintPaletteSelection();
}

function paintPaletteSelection() {
  [...$("palette-list").children].forEach((node, i) => node.setAttribute("aria-selected", i === palette.index ? "true" : "false"));
  const node = $(`pal-${palette.index}`);
  if (node) {
    $("palette-q").setAttribute("aria-activedescendant", node.id);
    node.scrollIntoView({block: "nearest"});
  } else {
    $("palette-q").removeAttribute("aria-activedescendant");
  }
}

function openPalette() {
  if ($("palette").open || !state) return;
  closeMenu(false);
  $("palette-q").value = "";
  palette.index = 0;
  paintPalette();
  $("palette").showModal();
  $("palette-q").focus();
}

function runPalette(i) {
  const c = palette.items[i];
  $("palette").close();
  if (c) c.run();
}

function openGuide() {
  if (!state) return;
  $("guide").showModal();
  renderGuide(guideItems(state));
  const current = $("guide-pop-list").querySelector(".current a") || $("guide-pop-list").querySelector("a");
  if (current) current.focus();
}

// Theme: the system's, or the owner's light or dark.
const lightSystem = matchMedia("(prefers-color-scheme: light)");
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  document.querySelector('meta[name="theme-color"]').content = bg || "#ffffff";
}
function setTheme(theme) {
  store.set("nb-theme", theme === "system" ? null : theme);
  applyTheme(theme);
  announce(`Theme: ${theme === "system" ? "matches the system" : theme}.`);
}

async function logout() {
  try { await api("/api/logout", {}); } catch (error) { toast(error.message); }
  state = null;
  currentView = null;
  stopFollowing();
  stopChat();
  job = null;
  chat.seq = 0;
  clearChatLog();
  await refresh().catch((error) => toast(error.message));
}

// A test alert's link, opened on the device it reached.
async function confirmHeard(word) {
  say("test-result", "busy", "Telling the server the test alert arrived…");
  try {
    const result = await api("/api/alerts/heard", {word});
    state.alerts = result.alerts;
    paintStates();
    say("test-result", "ok", "The test alert reached this device: alerts work here.");
  } catch (error) {
    say("test-result", "error", error.message);
  }
}

// The installable app.
function paintInstall() {
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const can = Boolean(installPrompt) && !standalone;
  show("install-app-2", can);
  const sheetButton = document.querySelector('#more [data-run="install"]');
  if (sheetButton) sheetButton.parentElement.hidden = !can;
  if (standalone) say("pwa-state", "ok", "You are using the installed app.");
}
async function install() {
  if (!installPrompt) {
    go("access/trust");
    say("pwa-state", "info", "Your browser offers Install once it trusts this server: install the root certificate first, then reload.");
    return;
  }
  installPrompt.prompt();
  const choice = await installPrompt.userChoice.catch(() => null);
  installPrompt = null;
  paintInstall();
  if (choice && choice.outcome === "accepted") say("pwa-state", "ok", "Installed. Open NullBill from your home screen.");
}
// Whether this browser trusts the server's certificate, which no script can
// read. A page's own requests do not tell: a browser that was told to
// continue past the warning answers them like any other. But it will not
// fetch a service worker's script over such a connection, so registering
// the worker, and asking it to update in case it was registered while the
// certificate was still trusted, tells. Checked in Chromium both ways; a
// browser without workers (some private windows) leaves it unknown.
async function checkTrust() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  try {
    const registration = await navigator.serviceWorker.register("/sw.js");
    await registration.update();
    trusted = true;
  } catch {
    trusted = false;
    say("pwa-state", "info", "This browser does not trust the server's certificate yet, so it cannot install the app. "
      + "Installed it already? Close the browser and open this page again.");
  }
  if (state) {
    renderTrust(state.trust);
    paintStates();
  }
}

// Keyboard.
let gPending = 0;
const G_KEYS = {o: "overview", h: "overview", s: "services", b: "backups", y: "activity", l: "alerts", u: "setup", x: "access", a: "agent", p: "updates"};

function typing(target) {
  return target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
}

// Bring the agent's waiting request into view and put focus on it.
function showRequest(card) {
  card = card || latestRequest();
  if (!card) { openAgent(true); return; }
  openAgent(false);
  requestAnimationFrame(() => {
    card.scrollIntoView({block: "center"});
    const word = card.querySelector("input.word");
    (word || card.querySelector('[data-role="approve"]')).focus();
    announce(`The agent asks: ${card.querySelector(".card-title").textContent}. Alt+Y approves, Alt+N declines.`, true);
  });
}

function latestRequest() {
  const ids = chat.pending;
  for (let i = ids.length - 1; i >= 0; i--) {
    const node = chat.nodes.get(ids[i]);
    if (node && node.dataset.state === "pending") return node;
  }
  return null;
}

function onMenuKey(event) {
  const box = $("menu");
  const items = [...box.querySelectorAll("[role^=menuitem]")];
  const at = items.indexOf(document.activeElement);
  if (event.key === "Escape") { event.preventDefault(); closeMenu(true); return true; }
  if (event.key === "Tab") { closeMenu(true); return false; }
  if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
    items[next].focus();
    return true;
  }
  return false;
}

function onKey(event) {
  if (!state || $("app").hidden) return;
  if (!$("menu").hidden && onMenuKey(event)) return;
  const mod = event.ctrlKey || event.metaKey;
  if (mod && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); return; }
  if (document.querySelector("dialog[open]")) return;
  if (event.altKey && !mod) {
    const k = event.code;
    if (k === "KeyA") { event.preventDefault(); if (phone.matches) openAgent(true); else { setDock(!dockOpen(), true); if (dockOpen()) $("composer-text").focus(); else $("toggle-dock").focus(); } return; }
    if (k === "KeyY" || k === "KeyN") {
      const inside = document.activeElement && document.activeElement.closest(".card.request");
      const card = inside && inside.dataset.state === "pending" ? inside : latestRequest();
      if (!card) return;
      event.preventDefault();
      // Away from the card, the keys only bring it into view: nothing is
      // approved that the owner has not seen.
      if (!inside) {
        showRequest(card);
        return;
      }
      const word = card.querySelector("input.word");
      if (k === "KeyY" && word && !word.value) { word.focus(); return; }
      card.querySelector(k === "KeyY" ? '[data-role="approve"]' : '[data-role="decline"]').click();
      return;
    }
  }
  if (event.key === "Escape" && sideOpen()) { setSide(false); $("open-side").focus(); return; }
  if (event.key === "Escape" && narrow.matches && !phone.matches && dockOpen()) {
    setDock(false);
    $("toggle-dock").focus();
    return;
  }
  if (mod || event.altKey || typing(event.target) || store.get("nb-single-keys") === "off") return;
  if (gPending && Date.now() - gPending < 1500 && G_KEYS[event.key]) {
    event.preventDefault();
    gPending = 0;
    go(G_KEYS[event.key]);
    return;
  }
  gPending = 0;
  if (event.key === "g") { gPending = Date.now(); return; }
  if (event.key === "/") { event.preventDefault(); openAgent(true); return; }
  if (event.key === "?") { event.preventDefault(); $("keys").showModal(); return; }
  if ((event.key === "[" || event.key === "]") && currentView === "setup") {
    const at = STEPS.indexOf(currentStep) + (event.key === "]" ? 1 : -1);
    if (at >= 0 && at < STEPS.length) { event.preventDefault(); go(`setup/${STEPS[at]}`); }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  applyTheme(store.get("nb-theme"));
  lightSystem.addEventListener("change", () => { if (!document.documentElement.dataset.theme) applyTheme(null); });
  $("single-keys").checked = store.get("nb-single-keys") !== "off";
  $("single-keys").addEventListener("change", () => store.set("nb-single-keys", $("single-keys").checked ? null : "off"));
  document.querySelector(".skip").addEventListener("click", (event) => { event.preventDefault(); $("main").focus(); });
  $("side-scrim").addEventListener("click", () => {
    if (sideOpen()) { setSide(false); $("open-side").focus(); } else { setDock(false); $("toggle-dock").focus(); }
  });
  $("banner-link").addEventListener("click", (event) => {
    if (!event.currentTarget.dataset.request) return;
    event.preventDefault();
    showRequest();
  });
  setDock(!narrow.matches && store.get("nb-dock") !== "closed");
  placeChat();
  $("toast-close").addEventListener("click", () => toast(""));
  $("outdated-reload").addEventListener("click", reloadFresh);
  $("toast-link").addEventListener("click", () => toast(""));
  // Logs and the plan: each scrolls on its own and copies whole.
  for (const pre of document.querySelectorAll("#plan, #tailnet-policy")) outputBox(pre);
  window.addEventListener("hashchange", () => route(true));
  document.addEventListener("keydown", onKey);
  document.addEventListener("pointerdown", (event) => {
    if (!$("menu").hidden && !$("menu").contains(event.target) && !(menu.button && menu.button.contains(event.target))) closeMenu(false);
  });
  window.addEventListener("resize", () => closeMenu(false));
  for (const media of [phone, narrow]) {
    media.addEventListener("change", () => {
      setDock(!narrow.matches && store.get("nb-dock") !== "closed");
      setSide(false);
      placeChat();
      if (state) route(false);
    });
  }
  // Back in sight after minutes or hours in the background, where timers
  // sleep: everything is asked again at once.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state) { refreshSoon(); refreshStatus().catch(() => {}); pollChat(); }
  });
  document.addEventListener("input", (event) => { if (event.target.removeAttribute) event.target.removeAttribute("aria-invalid"); });
  // Buttons that hand a question to the agent.
  document.addEventListener("click", (event) => {
    const ask = event.target.closest("[data-ask]");
    if (ask) askAgent(ask.dataset.ask);
    const link = event.target.closest('a[href^="#"]');
    if (link && $("guide").open && $("guide").contains(link)) $("guide").close();
    if (link && narrow.matches && !phone.matches && dockOpen() && !$("dock").contains(link)) setDock(false);
  });

  // Sign-in.
  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("secret");
    press(submitter(event), "login-result", "Signing in…", async () => {
      if (!field.value.trim()) throw new ApiError("Enter the setup code or your password.", 400);
      await api("/api/login", {secret: field.value});
      field.value = "";
      say("login-result", "", "");
      await refresh();
      route(true);
      return "";
    }, field);
  });
  $("new-password").addEventListener("input", () => {
    const n = $("new-password").value.length;
    $("password-count").textContent = n === 0 ? "" : n < 12 ? `${n} characters; ${12 - n} more to go.` : `${n} characters. Good.`;
  });
  $("password-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("new-password");
    press(submitter(event), "password-result", "Saving the password…", async () => {
      if (field.value.length < 12) throw new ApiError(`Use at least 12 characters; this one has ${field.value.length}.`, 400);
      await api("/api/password", {password: field.value});
      field.value = "";
      await refresh();
      return "Password saved.";
    }, field);
  });

  // The shell.
  $("toggle-dock").addEventListener("click", () => {
    if (phone.matches) { openAgent(true); return; }
    setDock(!dockOpen(), true);
    if (dockOpen()) setTimeout(() => $("composer-text").focus(), 30);
  });
  $("chat-close").addEventListener("click", () => { setDock(false, true); $("toggle-dock").focus(); });
  $("open-side").addEventListener("click", () => setSide(!sideOpen()));
  $("open-palette").addEventListener("click", openPalette);
  $("account").addEventListener("click", accountMenu);
  $("run-menu").addEventListener("click", runMenu);
  $("guide-btn").addEventListener("click", openGuide);
  $("chat-new").addEventListener("click", newConversation);
  $("chat-copy").addEventListener("click", () => copyConversation($("chat-copy")));
  $("chat-modes").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-mode]");
    if (button) setMode(button.dataset.mode, "composer-result", button);
  });
  $("install-app-2").addEventListener("click", install);
  const paintThemePick = () => {
    for (const b of document.querySelectorAll("[data-theme-pick]")) b.setAttribute("aria-checked", b.dataset.themePick === themeName() ? "true" : "false");
  };
  $("tab-more").addEventListener("click", () => { paintThemePick(); $("more").showModal(); });
  $("theme-pick").addEventListener("click", (event) => {
    const b = event.target.closest("[data-theme-pick]");
    if (!b) return;
    setTheme(b.dataset.themePick);
    paintThemePick();
  });
  $("theme-pick").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const all = [...document.querySelectorAll("[data-theme-pick]")];
    const next = all[(all.indexOf(document.activeElement) + (event.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
    event.preventDefault();
    next.focus();
    next.click();
  });
  $("more").addEventListener("click", (event) => {
    if (event.target.closest("#theme-pick")) return;
    const target = event.target.closest("a, [data-run]");
    if (event.target === $("more")) { $("more").close(); return; }
    if (!target) return;
    $("more").close();
    const run = target.dataset.run;
    if (run === "verify") startJob(run, $("run-menu"), $("actions-result"));
    if (run === "install") install();
    if (run === "logout") logout();
  });
  for (const dialog of [$("guide"), $("palette")]) {
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  }
  wireTabs("agent-tabs", (tab) => go(`agent/${tab}`));
  wireTabs("access-tabs", (tab) => go(`access/${tab}`));
  wirePeople();
  wireTabs("platform-tabs", showPlatform);
  showPlatform(guessPlatform());
  for (const code of document.querySelectorAll(".page-origin")) code.textContent = location.origin;

  // Palette.
  let paletteSay = 0;
  $("palette-q").addEventListener("input", () => {
    palette.index = 0;
    paintPalette();
    clearTimeout(paletteSay);
    paletteSay = setTimeout(() => {
      const n = $("palette-list").querySelectorAll("[role=option]").length;
      announce(n ? `${n} result${n === 1 ? "" : "s"}` : "No results");
    }, 400);
  });
  $("palette-q").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const n = palette.items.length;
      if (!n) return;
      palette.index = (palette.index + (event.key === "ArrowDown" ? 1 : -1) + n) % n;
      paintPaletteSelection();
    } else if (event.key === "Enter") {
      event.preventDefault();
      runPalette(palette.index);
    }
  });

  // The agent.
  $("agent-form").addEventListener("submit", (event) => signInAgent(event, "agent-credential", "agent-result"));
  $("chat-signin-form").addEventListener("submit", (event) => signInAgent(event, "chat-credential", "chat-signin-result"));
  const composer = $("composer-text");
  const grow = () => { composer.style.height = "auto"; composer.style.height = `${Math.min(composer.scrollHeight, 180)}px`; };
  composer.addEventListener("input", grow);
  composer.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("composer").requestSubmit();
    } else if (event.key === "Escape") {
      $("chat-log").focus();
    }
  });
  composer.addEventListener("focus", () => { if (phone.matches) { $("app").classList.add("composing"); requestAnimationFrame(scrollChatToNews); } });
  composer.addEventListener("blur", () => $("app").classList.remove("composing"));
  $("composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = composer.value;
    const pictures = chat.pictures;
    if (!text.trim() && !pictures.length) return;
    composer.value = "";
    chat.pictures = [];
    paintPictures();
    grow();
    if (pictures.length) say("composer-result", "busy", `Sending ${count(pictures.length, "picture")}…`);
    if (!(await sendChat(text, "composer-result", pictures))) {
      composer.value = text;
      chat.pictures = pictures;
      paintPictures();
      grow();
    }
  });
  // A screenshot: picked, pasted, or dropped anywhere on the conversation.
  $("composer-attach").addEventListener("click", () => $("composer-file").click());
  $("composer-file").addEventListener("change", async () => {
    await addPictures($("composer-file").files);
    $("composer-file").value = "";
  });
  composer.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData ? event.clipboardData.files : [])].filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;
    event.preventDefault();
    addPictures(files);
  });
  const dropZone = $("chat");
  const carriesFiles = (event) => event.dataTransfer && [...event.dataTransfer.types].includes("Files");
  dropZone.addEventListener("dragover", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    dropZone.classList.add("dropping");
  });
  dropZone.addEventListener("dragleave", (event) => { if (!dropZone.contains(event.relatedTarget)) dropZone.classList.remove("dropping"); });
  dropZone.addEventListener("drop", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    dropZone.classList.remove("dropping");
    addPictures(event.dataTransfer.files);
  });
  // The model and how long it thinks: beside the message box and on the Agent page.
  for (const select of document.querySelectorAll("select[data-setting]")) {
    select.addEventListener("change", async () => {
      const feedback = select.closest(".composer") ? "composer-result" : "model-result";
      const key = select.dataset.setting;
      try {
        say(feedback, "busy", "Saving…");
        await saveSettings({[key]: select.value});
        const picked = select.selectedOptions[0].textContent;
        say(feedback, "ok", `${key === "model" ? "Model" : "Thinking"}: ${picked}, from your next message.`);
      } catch (error) {
        renderMode(state.chat);
        say(feedback, "error", error.message);
      }
    });
  }
  $("chat-task-end").addEventListener("click", () => press($("chat-task-end"), "composer-result", "Ending the task…", async () => {
    await api("/api/chat/task", {});
    await pollChat();
    return "The task is over. From now on the agent asks as your access mode says.";
  }));
  $("chat-stop").addEventListener("click", () => press($("chat-stop"), "composer-result", "Stopping…", async () => {
    await api("/api/chat/stop", {});
    pollChat();
    return "";
  }));
  $("modes").addEventListener("click", (event) => {
    const button = event.target.closest(".choice");
    if (button) setMode(button.dataset.mode, "mode-result", button);
  });
  $("port-mapping").addEventListener("change", async () => {
    const asked = $("port-mapping").checked;
    say("port-mapping-result", "busy", "Saving…");
    try {
      state.port_mapping = (await api("/api/port-mapping", {enabled: asked})).port_mapping;
      say("port-mapping-result", "ok", asked ? "Saved. The next setup run asks the home router." : "Saved. The next setup run gives the port back and stops asking.");
      refresh();
    } catch (error) {
      $("port-mapping").checked = state.port_mapping;
      say("port-mapping-result", "error", error.message);
    }
  });
  $("auto-fix").addEventListener("change", async () => {
    try {
      await saveSettings({auto_fix: $("auto-fix").checked});
      say("mode-result", "ok", $("auto-fix").checked ? "It looks into each failed run by itself." : "It waits until you ask.");
    } catch (error) {
      $("auto-fix").checked = state.chat.auto_fix;
      say("mode-result", "error", error.message);
    }
  });
  $("log-refresh").addEventListener("click", loadAgentLog);
  $("log-filter").addEventListener("input", paintAgentLog);
  $("pick-filter").addEventListener("input", foldOptions);
  $("services-filter").addEventListener("input", filterServices);
  for (const more of $("pick").querySelectorAll(".opts-more")) {
    more.addEventListener("click", () => {
      const set = more.closest("fieldset");
      set.dataset.open = set.dataset.open === "true" ? "false" : "true";
      foldOptions();
    });
  }
  $("game-add").addEventListener("click", () => {
    say("game-add-said", "", "");
    const full = state.options && state.options.games ? state.options.games.full : null;
    if (full) return say("game-add-said", "info", full);
    if (choicesDirty()) return say("game-add-said", "info", "Save and plan first: you have changes here that are not saved yet.");
    return openGameAdd($("game-add-form").hidden);
  });
  $("game-add-go").addEventListener("click", addGameServer);
  $("game-add-cancel").addEventListener("click", () => openGameAdd(false));
  // Enter in the name adds the server; it must not save the whole form.
  $("game-add-title").addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addGameServer();
  });
  $("own-ask").addEventListener("click", () => {
    openAgent(true);
    const composer = $("composer-text");
    if (!composer.value.trim()) {
      composer.value = "Add a service of my own: ";
      composer.dispatchEvent(new Event("input", {bubbles: true}));
    }
  });

  const powers = (on) => press($("agent-powers"), "agent-powers-result", on ? "Giving the operator root on Proxmox…" : "Taking its key off Proxmox…", async () => {
    try {
      state.agent_powers = (await api("/api/agent-powers", {enabled: on})).agent_powers;
    } finally {
      if (!on) state.agent_powers = false;
      renderAgentPowers(state.agent_powers);
      paintStates();
    }
    return on ? "On. The operator agent can now manage the server." : "Off. Its key is off Proxmox.";
  });
  $("agent-powers").addEventListener("click", async () => {
    const sw = $("agent-powers");
    if (sw.getAttribute("aria-disabled") === "true") return say("agent-powers-result", "info", sw.dataset.why);
    if (state.agent_powers) return powers(false);
    const ok = await confirmDialog({title: "Give the operator agent root on Proxmox?", text: "It may then run any command on the server, setup and updates. Each run it starts is announced on your phone, and the checks warn while this is on.", yes: "Give root", word: "root"});
    sw.focus();
    if (ok) powers(true);
  });

  $("agent-github-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("agent-github-token");
    press(submitter(event), "agent-github-result", "Checking with GitHub…", async () => {
      if (!field.value.trim()) throw new ApiError("Paste the token into the field first.", 400);
      const result = await api("/api/agent-github", {token: field.value});
      field.value = "";
      state.agent_github = result.agent_github;
      renderAgentGithub(result.agent_github);
      return `Saved: GitHub knows it as ${result.agent_github}. Run setup so the agent gets it.`;
    }, field);
  });
  $("agent-github-remove").addEventListener("click", () => press($("agent-github-remove"), "agent-github-result", "Removing…", async () => {
    state.agent_github = (await api("/api/agent-github", {token: ""})).agent_github;
    renderAgentGithub(state.agent_github);
    $("agent-github-token").focus();
    return "Removed. Run setup so the agent's copy goes too; revoke it on GitHub as well.";
  }));
  $("remote-agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("remote-agent-name");
    press(submitter(event), "remote-agent-result", "Making a token…", async () => {
      const made = await api("/api/remote-agents", {name: field.value.trim()});
      field.value = "";
      $("remote-agent-token").textContent = [`NB_PAGE_URL=${made.url}`, `NB_PAGE_SPKI=${made.spki}`, `NB_PAGE_TOKEN=${made.token}`].join("\n");
      show("remote-agent-token-box", true);
      const st = await api("/api/state");
      renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
      return `Made a token for ${made.name}. Copy it now; it is shown only once.`;
    }, field);
  });
  $("remote-agent-copy").addEventListener("click", () => press($("remote-agent-copy"), "remote-agent-result", "Copying…", async () => {
    try { await copyText($("remote-agent-token").textContent); } catch {
      throw new ApiError("Copying is blocked here; select the text and copy it by hand.", 400);
    }
    return "Copied.";
  }));

  // Setup.
  const choices = $("choices-form");
  choices.addEventListener("change", onChoicesChanged);
  choices.addEventListener("input", onChoicesChanged);
  $("choices-discard").addEventListener("click", () => {
    renderChoices(state);
    onChoicesChanged();
    say("choices-result", "info", "Back to what is saved.");
  });
  choices.addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("owner-login");
    showProblem("choices-refused", null);
    field.removeAttribute("aria-invalid");
    // Only a refusal about the login marks its field; one about a game
    // server opens that server's settings instead.
    const login = (error) => { field.setAttribute("aria-invalid", "true"); field.focus(); return error; };
    press(submitter(event), "choices-result", "Saving and making the plan…", async () => {
      if (!field.value.trim()) throw login(new ApiError("Enter the email address you sign in to Tailscale with.", 400));
      let result;
      try { result = await api("/api/choices", choicesBody()); } catch (error) {
        if (/Tailscale login/.test(error.message)) login(error);
        const shown = openRefusedGames(error.list || [error.message]);
        // Several refusals are a list above the bar, a line each.
        if (!showProblem("choices-refused", error) && shown) shown.scrollIntoView({block: "nearest"});
        throw error;
      }
      savedChoices = null;
      await refresh();
      go("setup/plan");
      return result.plan.errors.length ? "Saved, but the plan does not fit yet." : "Saved. The plan fits.";
    });
  });

  // Access.
  $("tailscale-client-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const id = $("tailscale-client-id");
    const secretField = $("tailscale-client-secret");
    const missing = !id.value.trim() ? id : !secretField.value.trim() ? secretField : null;
    press(submitter(event), "tailscale-client-result", "Saving…", async () => {
      if (missing) throw new ApiError("Paste both the client ID and the secret.", 400);
      const result = await api("/api/tailscale-client", {client_id: id.value, secret: secretField.value});
      id.value = "";
      secretField.value = "";
      state.tailscale_client = result.tailscale_client;
      paintStates();
      return "Saved. The next setup run puts the policy in place with it.";
    }, missing || id);
  });
  $("ssh-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("ssh-keys");
    press(submitter(event), "ssh-result", "Saving…", async () => {
      const result = await api("/api/ssh-keys", {keys: field.value});
      field.value = result.ssh_keys.join("\n");
      sshDirty = false;
      await refresh();
      const n = result.ssh_keys.length;
      return n ? `Saved ${n} key${n === 1 ? "" : "s"}. Run setup to put them in place.` : "Saved: no keys. Run setup to take them off.";
    }, field);
  });
  $("ssh-keys").addEventListener("input", () => { sshDirty = true; });
  $("vpn-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("tailscale-key");
    press(submitter(event), "vpn-result", "Saving…", async () => {
      if (!field.value.trim()) throw new ApiError("Paste the auth key into the field first.", 400);
      const result = await api("/api/vpn", {key: field.value});
      field.value = "";
      state.tailscale_key = result.tailscale_key;
      paintStates();
      return "Saved. Run setup so the server joins.";
    }, field);
  });

  // Alerts.
  $("alerts-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("healthchecks-url");
    press(submitter(event), "alerts-result", "Saving…", async () => {
      const result = await api("/api/alerts", {healthchecks_url: field.value});
      renderAlerts(result.alerts);
      state.alerts = result.alerts;
      paintStates();
      return result.alerts.healthchecks_url ? "Saved. Send a test alert to check it." : "Saved without a Healthchecks.io URL.";
    }, field);
  });
  $("test-alerts").addEventListener("click", () => press($("test-alerts"), "test-result", "Sending a test alert…", async () => {
    const result = await api("/api/alerts/test", {origin: location.origin});
    return `Sent to ${result.sent.join(" and ")}. Tap it on your phone: that tells this page it arrived.`;
  }));
  $("copy-topic").addEventListener("click", () => press($("copy-topic"), "test-result", "Copying…", async () => {
    try { await copyText($("ntfy-topic").textContent); } catch {
      throw new ApiError("Copying is blocked here; select the topic and copy it by hand.", 400);
    }
    return "Topic copied.";
  }));

  $("backups-form").addEventListener("submit", saveBackups);
  $("backups-places").addEventListener("change", paintBackupsForm);
  // The download is a plain link; the page then says when the key was taken.
  $("backups-key-get").addEventListener("click", () => setTimeout(() => loadBackups().catch(() => {}), 1500));
  $("start-setup").addEventListener("click", () => startJob("apply", $("start-setup"), $("build-result")));
  $("update-now").addEventListener("click", () => startJob("upgrade", $("update-now"), $("update-result")));
  $("update-check").addEventListener("click", () => press($("update-check"), "update-result", "Asking the repository…", async () => {
    state.version = (await api("/api/updates/check", {})).version;
    paintStates();
    const v = state.version;
    if (!v) return "";
    if (v.check_error) throw new ApiError(sentence(v.check_error), 400);
    return v.available.length ? `${count(v.available.length, "change")} available.` : "NullBill is up to date.";
  }));
  for (const button of document.querySelectorAll("button[data-action]")) {
    button.addEventListener("click", () => startJob(button.dataset.action, button, feedbackFor(button)));
  }

  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    installPrompt = event;
    paintInstall();
  });
  window.addEventListener("appinstalled", () => { installPrompt = null; paintInstall(); });
  paintInstall();
  checkTrust();

  refresh().catch((error) => {
    show("loading", false);
    toast(error.message);
  });
});
