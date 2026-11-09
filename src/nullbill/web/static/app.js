"use strict";

// NullBill's page. Pages in a rail (a tab bar on a phone), the latest run in
// the bar at the top, and the agent docked beside every page (its own tab on
// a phone). Every control answers beside itself: busy while the server
// works, then done or the reason it was refused. The agent proposes; the
// page decides by the owner's access mode and runs what is approved.

const LABELS = {
  "agents": ["AI agents", "The operator and a workspace container for coding agents."],
  "linux-ci": ["Linux CI runners", "Forgejo Actions jobs in throwaway Linux VMs."],
  "inference": ["Local AI models", "On the graphics card. Needs more memory."],
  "media": ["Media server", "Jellyfin plays the films, series and music on the disks you keep."],
  "windows-ci": ["Windows CI runners", "Needs a Windows ISO and more memory."],
  "windows-dev": ["Windows desktop VM", "Upload the Windows 11 ISO in Proxmox first; you install Windows in it."],
};

const VIEWS = ["home", "agent", "services", "setup", "access", "alerts", "activity"];
const ALIASES = {overview: "home", agents: "agent", "": ""};
const STEPS = ["agent", "hardware", "choices", "plan", "build"];
const AGENT_TABS = ["chat", "settings", "log"];
const ACCESS_TABS = ["tailscale", "lan", "trust"];
// Runs that open Activity when they start; the others show in the bar at
// the top while the owner stays where they pressed.
const OPENS_ACTIVITY = new Set(["apply", "verify", "upgrade", "remove-guests"]);
const SERVICE_ICONS = {
  proxmox: "proxmox", grafana: "chart", forgejo: "git", files: "folder", jellyfin: "film",
  workspace: "terminal", operator: "bot", "windows-dev": "window", "linux-runner": "cog", "model-gateway": "chip",
};
const JOB_NAMES = {
  probe: "Reading the hardware", apply: "Setup", verify: "Checks", upgrade: "Update of NullBill",
  "close-lan": "Closing the home network", "keep-lan-closed": "Keeping the home network closed",
  "open-lan": "Reopening the home network", "rebake-ci": "Rebuild of the CI images", "remove-guests": "Setup",
};
const STARTING = {
  probe: "Reading…", apply: "Starting setup…", verify: "Starting the checks…", upgrade: "Starting the update…",
  "close-lan": "Closing…", "keep-lan-closed": "Keeping it closed…", "open-lan": "Opening…", "remove-guests": "Starting setup…",
};
const MODE_NAMES = {manual: "Manual", auto: "Auto", yolo: "YOLO"};
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

// Per-browser conveniences only; the page works without them.
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* private mode */ } },
};

let state = null;          // the last /api/state
let savedChoices = null;   // the choices form as last saved
let statusTimer = null;
let lastStatus = null;     // the last /api/status
let job = null;            // the followed run: {id, view, node, offset, timer, watched, feedback}
let currentView = null;
let currentStep = null;
let agentTab = null;
let accessTab = null;
let installPrompt = null;  // the browser's install offer, once it makes one

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

function announce(text, urgent) {
  const region = $(urgent ? "live-urgent" : "live");
  region.textContent = "";
  setTimeout(() => { region.textContent = text; }, 60);
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
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
    throw new ApiError("The page cannot reach the NullBill server. If you just updated NullBill, wait a few seconds and try again.", 0);
  }
  connection(true);
  let data = {};
  try { data = await response.json(); } catch { /* not JSON */ }
  if (response.status === 401 && path !== "/api/login") {
    signedOut();
    throw new ApiError("You were signed out. Sign in again; nothing you saved is lost.", 401);
  }
  if (!response.ok) throw new ApiError(sentence(data.error || `The server answered ${response.status} ${response.statusText}`), response.status);
  return data;
}

function connection(ok) {
  $("conn").classList.toggle("lost", !ok);
  $("conn-text").textContent = ok ? "Connected" : "Not connected";
}

function toast(text) {
  $("toast-text").textContent = text;
  $("toast").hidden = !text;
}

// The line next to a control: busy, ok, error or info.
function say(id, kind, text) {
  const node = typeof id === "string" ? $(id) : id;
  if (!node) return;
  node.className = `${node.className.split(" ").filter((c) => !["ok", "error", "info", "busy"].includes(c)).join(" ")} ${kind || ""}`.trim();
  node.replaceChildren();
  if (!text) return;
  const mark = {busy: make("span", {className: "spin", ariaHidden: "true"}), ok: icon("check"), error: icon("alert")}[kind];
  node.append(...[mark, make("span", {textContent: text})].filter(Boolean));
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
    say(feedback, "error", error.message);
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

function show(id, visible) { const node = typeof id === "string" ? $(id) : id; if (node) node.hidden = !visible; }
function setBadge(id, text, kind) {
  const node = typeof id === "string" ? $(id) : id;
  node.textContent = text || "";
  node.className = `badge ${kind || ""}`.trim();
}
function setNav(view, text, kind) {
  const badge = document.querySelector(`#nav a[data-view="${view}"] .nav-badge`);
  if (!badge) return;
  badge.hidden = !text;
  badge.textContent = text || "";
  badge.className = `nav-badge ${kind || ""}`.trim();
}

// A small, safe Markdown: paragraphs, lists, code, bold and links. Built
// from nodes, never from HTML, since the text comes from the model.
function inline(text) {
  const nodes = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|\[([^\]]+)\]\((https?:\/\/[^)\s]+|#[\w/-]*)\)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    if (m[1]) nodes.push(make("code", {textContent: m[1].slice(1, -1)}));
    else if (m[2]) nodes.push(make("b", {textContent: m[2].slice(2, -2)}));
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

function markdown(text) {
  const out = [];
  const lines = String(text || "").split("\n");
  const BLOCK = /^```|^\s*[-*] |^\s*\d+[.)] |^#{1,6} /;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i += 1;
      out.push(make("pre", {tabIndex: 0}, make("code", {textContent: buf.join("\n")})));
    } else if (/^\s*[-*] /.test(line) || /^\s*\d+[.)] /.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const pattern = ordered ? /^\s*\d+[.)] / : /^\s*[-*] /;
      const list = make(ordered ? "ol" : "ul");
      while (i < lines.length && pattern.test(lines[i])) list.append(make("li", {}, ...inline(lines[i++].replace(pattern, ""))));
      out.push(list);
    } else if (/^#{1,6} /.test(line)) {
      out.push(make("p", {}, make("b", {}, ...inline(line.replace(/^#+ /, "")))));
      i += 1;
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
function confirmDialog({title, text, yes, word, danger = true}) {
  const dialog = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
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

// Pages.
function built() { return Boolean(state && state.last_jobs.apply && state.last_jobs.apply.state === "done"); }
function defaultView() { return built() ? "home" : "setup"; }

function stepDone(step) {
  if (!state) return false;
  if (step === "agent") return Boolean(state.chat && state.chat.signed_in);
  if (step === "hardware") return state.probed;
  if (step === "choices") return Boolean(state.choices.owner_login) && !choicesDirty();
  if (step === "plan") return Boolean(state.plan.text) && !state.plan.errors.length;
  return built() && !state.config_changed;
}
function defaultStep() {
  const skipAgent = store.get("nb-skip-agent") === "1";
  return STEPS.find((s) => !stepDone(s) && !(s === "agent" && skipAgent)) || "build";
}

function parseHash() {
  const [raw, sub] = location.hash.slice(1).split("/");
  const name = raw in ALIASES ? ALIASES[raw] : raw;
  if (raw && !VIEWS.includes(name) && currentView) return [currentView, "", false];
  return [VIEWS.includes(name) ? name : defaultView(), sub || "", raw === "agents"];
}

function route(focus) {
  if (!state) return;
  const [view, sub, legacyAgents] = parseHash();
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== view;
  for (const link of document.querySelectorAll("#nav a, .tabbar a, #more a")) {
    if (link.dataset.view === view || link.getAttribute("href") === `#${view}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const inMore = ["access", "alerts", "activity"].includes(view);
  $("tab-more").classList.toggle("current", inMore);
  if (inMore) $("tab-more").setAttribute("aria-current", "page"); else $("tab-more").removeAttribute("aria-current");
  const changed = currentView !== view;
  currentView = view;
  document.title = `${$(`view-${view}`).dataset.title} · NullBill`;
  let heading = $(`h-${view}`);
  if (view === "setup") {
    const step = STEPS.includes(sub) ? sub : defaultStep();
    const stepChanged = step !== currentStep;
    showStep(step);
    if (stepChanged || changed) heading = $(`h-step-${step}`);
  } else if (view === "agent") {
    showAgentTab(legacyAgents ? "settings" : sub);
    if (sub && AGENT_TABS.includes(sub) === false && $(`sec-${sub}`)) heading = $(`sec-${sub}`).querySelector("h2");
  } else if (view === "access") {
    showAccessTab(sub);
  }
  placeChat();
  if (narrow.matches && !phone.matches && dockOpen() && !(view === "agent" && sub === "chat")) setDock(false);
  if (changed) $("main").scrollTo(0, 0);
  if (focus && heading) {
    heading.tabIndex = -1;
    heading.focus({preventScroll: true});
  }
  paintStates();
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

function showAccessTab(tab) {
  const fallback = state && state.lan.state === "closing" ? "lan" : "tailscale";
  accessTab = ACCESS_TABS.includes(tab) ? tab : fallback;
  selectTab("access-tabs", accessTab);
  for (const t of ACCESS_TABS) show(`sec-${t}`, t === accessTab);
}

// Arrow keys move along a tab list, as in every other tab list.
function wireTabs(listId, hashFor) {
  const list = $(listId);
  list.addEventListener("click", (event) => {
    const button = event.target.closest("[role=tab]");
    if (button) go(hashFor(button.dataset.tab));
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

// The agent's place: docked on a wide screen, an overlay on a narrow one,
// the Agent tab on a phone.
function dockOpen() { return !$("app").classList.contains("dock-closed"); }
function setDock(open, remember) {
  if (state) requestAnimationFrame(() => paintBanner());
  $("app").classList.toggle("dock-closed", !open);
  // On a tablet the dock covers the page: keep focus inside it.
  const overlay = open && narrow.matches && !phone.matches;
  for (const id of ["nav", "main", "banner"]) { const node = $(id); if (node) node.inert = overlay; }
  $("toggle-dock").setAttribute("aria-expanded", open ? "true" : "false");
  if (remember && !narrow.matches) store.set("nb-dock", open ? null : "closed");
  if (open) markSeen();
  paintChatBadge();
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
function tile(input, title, desc, why, cls) {
  return make("label", {className: `tile ${cls || ""}`.trim()}, input, make("span", {className: "tile-box", ariaHidden: "true"}),
    make("span", {className: "tile-text"},
      make("span", {className: "tile-title", textContent: title}),
      desc ? make("span", {className: "tile-desc", textContent: desc}) : null,
      why ? make("span", {className: "tile-why", textContent: why}) : null));
}

function renderWorkloads(st) {
  $("workload-tiles").replaceChildren(...st.workloads.map((name) => {
    const [title, help] = LABELS[name] || [name, ""];
    return tile(make("input", {type: "checkbox", value: name, checked: st.choices.workloads.includes(name)}), title, help);
  }));
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

function renderDisks(disks) {
  const list = $("disk-list");
  list.replaceChildren();
  show("disks", disks.length > 0);
  for (const disk of disks) {
    const box = make("fieldset", {className: "disk"});
    box.dataset.id = disk.id;
    const hours = disk.power_on_hours ? `, ${disk.power_on_hours} hours in use` : "";
    const health = make("span", {className: `badge ${disk.health === "PASSED" ? "good" : disk.health === "FAILED" ? "bad" : ""}`, textContent: `Health ${disk.health}`});
    box.append(make("legend", {}, make("span", {className: "disk-id", textContent: disk.id}),
      make("span", {className: "muted", textContent: `${disk.size_gib} GiB ${disk.rotational ? "hard disk" : "SSD"}${hours}`}), health));
    if (disk.health === "FAILED") {
      box.append(make("div", {className: "callout danger compact"}, icon("alert"), make("span", {textContent: "This disk reports that it is failing. Copy what you need off it soon."})));
    }
    const parts = make("ul", {className: "disk-parts"});
    for (const p of disk.partitions) {
      const label = p.label ? ` "${p.label}"` : "";
      parts.append(li(`${p.what}${label}, ${gib(p.size_mib)}${p.keepable ? "" : ", left alone"}`));
    }
    if (!disk.partitions.length) parts.append(li("No partitions."));
    box.append(parts);
    const keepable = disk.partitions.some((p) => p.keepable);
    const choices = make("div", {className: "tile-list"});
    for (const value of Object.keys(CHOICES)) {
      if (value === "copy" && !disk.kept && disk.choice !== "copy") continue;
      const [text, desc, why] = diskChoice(disk, value, keepable);
      const input = make("input", {type: "radio", name: `disk-${disk.id}`, value, checked: disk.choice === value, disabled: Boolean(why)});
      input.dataset.role = "choice";
      choices.append(tile(input, text, desc, why, ["erase", "copy"].includes(value) ? "danger-tile" : ""));
    }
    box.append(choices);
    const rwInput = make("input", {type: "checkbox", checked: disk.rw});
    const rw = make("div", {className: "disk-sub disk-rw"},
      tile(rwInput, "Let me change files on it (NTFS only)", "Shut Windows down fully on that disk first, with shutdown /s /t 0, or it stays read-only."));
    const uses = make("div", {className: "disk-sub disk-for"}, make("span", {className: "field-label", textContent: "Then use it"}));
    for (const [value, text, desc] of [["backups", "For the nightly backups", ""],
      ["storage", "As extra storage in Proxmox", "A copy made first moves onto it, checked again."]]) {
      uses.append(tile(make("input", {type: "radio", name: `disk-for-${disk.id}`, value, checked: disk.for === value}), text, desc));
    }
    const confirm = make("label", {className: "field disk-confirm"},
      make("span", {className: "field-label", textContent: `Type ${disk.confirm}, the end of its name, to erase everything on it`}),
      make("input", {autocomplete: "off", spellcheck: false}));
    box.append(rw, uses, confirm);
    const sync = () => {
      const chosen = box.querySelector("input[data-role=choice]:checked");
      rw.hidden = !chosen || chosen.value !== "keep";
      const erasing = Boolean(chosen) && ["erase", "copy"].includes(chosen.value);
      const usedFor = uses.querySelector("input:checked").value;
      uses.hidden = !erasing;
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
    list.append(make("label", {className: "field narrow-field"}, make("span", {className: "field-label", textContent: `Memory of the ${gpu.name}, in GB`}), input));
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
      rw: box.querySelector(".disk-rw input").checked,
      for: box.querySelector(".disk-for input:checked").value,
      confirm: box.querySelector(".disk-confirm input").value,
    };
  }
  return {
    workloads: [...$("workload-tiles").querySelectorAll("input:checked")].map((i) => i.value),
    backups: $("backups").checked,
    console: $("console").checked,
    owner_login: $("owner-login").value.trim(),
    disks,
    gpus,
  };
}

// The form as compared with what was saved; typing an erase confirmation
// alone changes nothing.
function choicesKey() {
  const body = choicesBody();
  for (const d of Object.values(body.disks)) delete d.confirm;
  return JSON.stringify(body);
}
function choicesDirty() { return savedChoices !== null && choicesKey() !== savedChoices; }

function renderChoices(st) {
  renderWorkloads(st);
  renderDisks(st.disks);
  renderGpus(st.gpus);
  $("backups").checked = st.choices.backups;
  $("console").checked = st.choices.console;
  $("owner-login").value = st.choices.owner_login;
  savedChoices = choicesKey();
}

function onChoicesChanged() {
  const dirty = choicesDirty();
  if (dirty) say("choices-result", "info", "Not saved yet.");
  else if ($("choices-result").classList.contains("info")) say("choices-result", "", "");
  $("plan-stale").hidden = !dirty || !state || !(state.plan.text || state.plan.errors.length);
  paintStates();
}

const SPEC = (iconName, label, value) => make("li", {}, icon(iconName), make("span", {}, make("small", {textContent: label}), value));

function renderHardware(hw) {
  const list = $("hardware");
  list.hidden = !hw;
  $("probe-button").querySelector("span").textContent = hw ? "Read it again" : "Read the hardware";
  $("probe-button").className = `btn ${hw ? "secondary" : "primary"}`;
  if (!hw) return;
  const items = [];
  if (hw.cpu || hw.threads) items.push(SPEC("chip", "Processor", `${hw.cpu || "CPU"}, ${hw.threads} threads`));
  if (hw.memory_mib) items.push(SPEC("memory", "Memory", gib(hw.memory_mib)));
  items.push(SPEC("gpu", "Graphics", hw.gpus.length ? hw.gpus.join(", ") : "No separate card"));
  items.push(SPEC("disk", "Disks", `${hw.disks} disk${hw.disks === 1 ? "" : "s"}`));
  if (hw.pve_version) items.push(SPEC("proxmox", "Proxmox VE", hw.pve_version));
  list.replaceChildren(...items);
}

const KINDS = {"lxc": "Container", "vm": "VM", "vm-pool": "VM pool"};

function renderPlan(plan) {
  const hasErrors = plan.errors.length > 0;
  $("plan-errors").replaceChildren(...plan.errors.map((e) => li(sentence(e))));
  $("plan-advice").replaceChildren(...(plan.advice || []).map((a) => li(a)));
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
      ? `${guests.length} guests count for ${gib(m.guests_mib)}; Proxmox keeps ${gib(m.reserve_mib)} and ${gib(m.unallocated_mib)} stays free.` +
        (m.limits_mib && m.limits_mib !== m.guests_mib
          ? ` Containers count at part of their limit, since they take memory only as they use it; all limits come to ${gib(m.limits_mib)}.`
          : "")
      : `${guests.length} guests.`;
    const bar = $("memory");
    bar.parentElement.hidden = !m.host_mib;
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
      make("td", {textContent: g.name}),
      make("td", {textContent: (KINDS[g.kind] || g.kind) + (g.count ? ` of ${g.count}` : "")}),
      make("td", {textContent: g.zone || ""}),
      make("td", {className: "num-col", textContent: g.cores ?? ""}),
      make("td", {className: "num-col", textContent: g.memory_mib ? gib(g.memory_mib) + (g.count ? " each" : "") : ""}))));
  }
  $("tailnet-policy").textContent = plan.tailnet_policy || "";
  show("tailnet-details", Boolean(plan.tailnet_policy));
}

function renderAlerts(alerts) {
  $("ntfy-topic").textContent = alerts && alerts.ntfy_url ? alerts.ntfy_url.replace("https://", "") : "";
  $("healthchecks-url").value = alerts ? alerts.healthchecks_url : "";
}

// The agent's settings.
function renderAgentSignin(st) {
  const kind = st.agent_credential;
  $("agent-label").textContent = kind ? "Replace it with a new API key or subscription token" : "API key or subscription token";
  setBadge("signin-state", ...(kind ? [`${kind === "API key" ? "API key" : "Subscription"}`, "good"] : ["Not signed in", "warn"]));
  setBadge("step-agent-state", ...(kind ? ["Done", "good"] : ["First", "agent"]));
  show("setup-agent-form", !kind);
  show("step-agent-done", Boolean(kind));
}

function renderMode(settings) {
  for (const button of document.querySelectorAll("#modes .mode")) button.setAttribute("aria-pressed", button.dataset.mode === settings.mode ? "true" : "false");
  $("auto-fix").checked = settings.auto_fix;
  setBadge("mode-state", MODE_NAMES[settings.mode], settings.mode === "yolo" ? "warn" : "agent");
  const pill = $("chat-mode");
  pill.textContent = MODE_NAMES[settings.mode];
  pill.dataset.mode = settings.mode;
  pill.title = `Access mode: ${MODE_NAMES[settings.mode]}. Change it under Agent, Settings.`;
  pill.setAttribute("aria-label", `Access mode: ${MODE_NAMES[settings.mode]}, change`);
  setBadge("home-mode", MODE_NAMES[settings.mode], settings.mode === "yolo" ? "warn" : "agent");
}

function renderAgentPowers(on) {
  $("agent-powers").setAttribute("aria-checked", on ? "true" : "false");
  setBadge("powers-state", ...(on ? ["Root on", "bad"] : ["Off", ""]));
}

function renderAgentGithub(login) {
  setBadge("github-state", ...(login ? [login, "good"] : ["Optional", ""]));
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
    const revoke = make("button", {type: "button", className: "btn ghost small", textContent: "Revoke"});
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

async function loadAgentLog() {
  try {
    const {entries} = await api("/api/agent-log");
    const WORD = {run: "Ran", refused: "Blocked", approve: "You approved", decline: "You declined", "ask-owner": "Asked you", ask: "Asked you"};
    const rows = entries.slice().reverse().map((e) => make("tr", {},
      make("td", {textContent: clock(e.t)}),
      make("td", {}, make("span", {textContent: `${WORD[e.action] || e.action}: `}),
        e.command ? make("code", {textContent: `${e.where && e.where !== "host" ? `${e.where}: ` : ""}${e.command}`}) : make("span", {textContent: e.what || e.question || e.kind || ""})),
      make("td", {textContent: e.by === "owner" ? "You" : "Agent"}),
      make("td", {}, e.tier ? make("span", {className: `badge ${{read: "", change: "agent", risky: "warn", blocked: "bad"}[e.tier] || ""}`, textContent: e.tier}) : null)));
    $("log-rows").replaceChildren(...(rows.length ? rows : [make("tr", {}, make("td", {colSpan: 4, className: "muted", textContent: "Nothing yet. Each action the agent takes shows here before it runs."}))]));
  } catch (error) {
    $("log-rows").replaceChildren(make("tr", {}, make("td", {colSpan: 4, textContent: error.message})));
  }
}

// Access.
const LAN = {
  open: ["Open", "", "Proxmox answers on your home network."],
  closing: ["Confirm", "warn", "Closing: press Keep it closed on this page opened over Tailscale, or it opens again by itself."],
  closed: ["Closed", "good", "Closed: Proxmox answers only over Tailscale."],
};

function renderLan(lan) {
  const [badge, kind, text] = LAN[lan.state];
  const extra = lan.state === "closing" && !lan.via_vpn ? " This page is not open over Tailscale right now." : "";
  $("lan-status").replaceChildren(icon(lan.state === "open" ? "unlock" : "lock"), make("span", {textContent: text + extra}));
  $("lan-status").className = `callout compact ${kind === "good" ? "success" : kind === "warn" ? "warn" : ""}`;
  $("lan-minutes").textContent = lan.minutes;
  setBadge("lan-state", badge, kind);
  show("lan-close", lan.state === "open");
  show("lan-keep", lan.state === "closing");
  show("lan-open", lan.state !== "open");
}

function renderTrust(trust) {
  const ready = Boolean(trust && trust.ready);
  show("trust-download", ready);
  show("trust-wait", !ready);
  $("trust-fingerprint").textContent = ready && trust.fingerprint ? `SHA-256 fingerprint: ${trust.fingerprint}` : "";
  setBadge("trust-state", ...(ready ? ["Ready", "good"] : ["After setup", ""]));
}

// The checklist on Home: what is needed, then what is recommended.
function runningAction() { return job && job.view && job.view.state === "running" ? job.view.action : null; }

function checklist(st) {
  const apply = st.last_jobs.apply;
  const running = runningAction();
  const hw = st.hardware;
  const signed = st.chat && st.chat.signed_in;
  const items = [
    ["setup/agent", "Sign in your agent", signed ? ["done", "It helps from here on."] : ["todo", "It reads the hardware and fixes what breaks."]],
    ["setup/hardware", "Read the hardware",
      running === "probe" ? ["running", "Reading…"]
        : st.probed ? ["done", hw ? `${hw.cpu || "CPU"}${hw.memory_mib ? `, ${gib(hw.memory_mib)}` : ""}` : "Done"] : ["todo", "CPU, memory, disks and graphics."]],
    ["setup/choices", "Choose what to run",
      choicesDirty() ? ["attention", "Changed but not saved."] : st.choices.owner_login ? ["done", "Saved."] : ["todo", "Workloads, disks, your Tailscale login."]],
    ["setup/plan", "Check that it fits",
      st.plan.errors.length ? ["attention", "The plan does not fit yet."] : st.plan.text ? ["done", "The plan fits."] : ["todo", "Once the choices are saved."]],
  ];
  const removals = st.removals || [];
  if (removals.length) items.push(["setup/build", "Keep or delete guests", ["attention", `Setup stopped before deleting ${andList(removals)}.`]]);
  items.push(["setup/build", "Run setup",
    ["apply", "rebake-ci", "remove-guests"].includes(running) ? ["running", "Running now."]
      : !apply ? ["todo", "Builds everything in the plan."]
      : apply.state === "done" ? (st.config_changed ? ["attention", "Choices changed since; run it again."] : ["done", `Finished ${ago(apply.started)}.`])
      : apply.state === "running" ? ["running", "Running now."] : ["attention", "Stopped. Activity says why."]]);
  const needed = items.length;
  items.push(["alerts", "Alerts on your phone",
    st.alerts && st.alerts.healthchecks_url ? ["done", "ntfy and Healthchecks.io set."] : ["todo", "Subscribe to your ntfy topic."]]);
  const joined = st.lan.via_vpn || st.lan.state === "closed";
  items.push(["access/tailscale", "Reach it over Tailscale",
    joined ? ["done", "This page answers over Tailscale."]
      : st.tailscale_key ? ["attention", "Auth key saved: run setup."]
      : st.tailscale_client ? ["todo", "Add an auth key next."] : ["todo", "Add an OAuth client."]]);
  items.push(["access/lan", "Close Proxmox to the home network",
    st.lan.state === "closed" ? ["done", "Only over Tailscale."]
      : st.lan.state === "closing" ? ["attention", `Confirm within ${st.lan.minutes} minutes.`] : ["todo", "After Tailscale works."]]);
  items.push(["access/trust", "Trust it, install the app",
    st.trust && st.trust.ready ? ["todo", "Install the root certificate."] : ["todo", "After setup makes the certificate."]]);
  return items.map(([href, title, [kind, sub]], i) => ({href, title, kind, sub, recommended: i >= needed}));
}

function renderChecklist(items) {
  const current = items.find((i) => i.kind !== "done");
  $("checklist").replaceChildren(...items.map((item) => make("li", {className: `${item.kind}${item === current ? " current" : ""}`},
    make("a", {href: `#${item.href}`},
      make("span", {className: "check-mark", ariaHidden: "true"}, item.kind === "done" ? icon("check") : item.kind === "attention" ? icon("alert") : null),
      make("span", {className: "check-title", textContent: item.title}),
      make("span", {className: "sr-only", textContent: `: ${{done: "done", attention: "needs you", running: "running", todo: "to do"}[item.kind]}. `}),
      make("span", {className: "check-sub", textContent: item.sub}),
      make("span", {className: "check-tag"}, item.recommended && item.kind !== "done" ? make("span", {className: "badge", textContent: "Later"}) : null)))));
  const done = items.filter((i) => i.kind === "done").length;
  $("progress-meter").style.width = `${Math.round((100 * done) / items.length)}%`;
  setBadge("progress-count", `${done} of ${items.length}`, done === items.length ? "good" : "");
}

// Each badge, the nav, the checklist and the banner, from state and the run.
function paintStates() {
  if (!state) return;
  const st = state;
  const running = runningAction();
  const items = checklist(st);
  renderChecklist(items);
  for (const a of document.querySelectorAll("#stepper a")) {
    const s = a.dataset.step;
    a.classList.toggle("done", stepDone(s));
    let note = a.querySelector(".step-state");
    if (!note) { note = make("span", {className: "sr-only step-state"}); a.append(note); }
    a.classList.toggle("attention", (s === "plan" && st.plan.errors.length > 0) || (s === "choices" && choicesDirty()) || (s === "build" && (st.removals || []).length > 0));
    note.textContent = a.classList.contains("attention") ? ", needs you" : a.classList.contains("done") ? ", done" : "";
  }
  setBadge("hardware-state", ...(running === "probe" ? ["Reading", "run"] : st.probed ? ["Done", "good"] : ["To do", ""]));
  setBadge("choices-state", ...(choicesDirty() ? ["Not saved", "warn"] : st.choices.owner_login ? ["Saved", "good"] : ["To do", ""]));
  setBadge("plan-state", ...(st.plan.errors.length ? ["Does not fit", "bad"] : st.plan.text ? ["Fits", "good"] : ["Waiting", ""]));
  const setupLeft = STEPS.slice(1, 4).filter((s) => !stepDone(s)).length;
  setNav("setup", setupLeft ? `${setupLeft}` : "", "warn");
  setNav("access", st.lan.state === "closing" ? "!" : "", "warn");
  setNav("activity", running ? "•" : st.last_jobs.apply && st.last_jobs.apply.state === "failed" ? "!" : "", running ? "run" : "bad");
  const alerts = lastStatus && lastStatus.monitoring === "ok" ? lastStatus.alerts.length : 0;
  setNav("home", alerts ? String(alerts) : "", "bad");
  setBadge("hc-state", ...(st.alerts && st.alerts.healthchecks_url ? ["Set", "good"] : ["Optional", ""]));
  const ts = items.find((i) => i.href === "access/tailscale");
  setBadge("tailscale-state", ...(ts.kind === "done" ? ["Connected", "good"] : st.tailscale_key ? ["Run setup", "warn"] : st.tailscale_client ? ["Client saved", ""] : ["Optional", ""]));
  $("ts-dot-client").classList.toggle("done", st.tailscale_client);
  $("ts-dot-key").classList.toggle("done", ts.kind === "done");
  show("tailscale-client-saved", st.tailscale_client);
  show("vpn-saved", st.tailscale_key);
  show("pending-dot", Boolean(st.config_changed || st.tailscale_key));
  renderReady();
  paintHero();
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
  if (!(state.chat && state.chat.signed_in)) ok(false, "No agent yet: setup runs, but nobody looks into a failure for you.", "#setup/agent");
  if (!state.tailscale_client) ok(false, "No Tailscale client: setup runs, but the server stays off Tailscale.", "#access/tailscale");
  if (state.config_changed) ok(false, "Choices changed since the last setup run; run it so the server follows.");
  $("ready-list").replaceChildren(...items);
  const removals = state.removals || [];
  show("removals", removals.length > 0);
  $("removals-names").textContent = andList(removals);
  $("removals-disks").textContent = removals.length > 1 ? "their disks" : "its disk";
}

// Home: health from monitoring once built, setup progress before.
function stat(label, value, sub, dot, href) {
  const body = [make("span", {className: "overline", textContent: label}),
    make("span", {className: "stat-value"}, dot ? make("span", {className: `dot ${dot}`, ariaHidden: "true"}) : null, value),
    sub ? make("span", {className: "stat-sub", textContent: sub}) : null];
  return make("li", {className: "stat"}, ...(href ? [make("a", {href, className: "stat-link"}, ...body)] : body));
}

// Each state of the hero has one main thing to do.
function heroActions() {
  const out = [];
  const signed = Boolean(state.chat && state.chat.signed_in);
  const running = runningAction();
  const failed = job && job.view && job.view.state === "failed" && job.view.action === "apply";
  const button = (label, kind, run, href) => {
    const node = href ? make("a", {className: `btn ${kind}`, href, textContent: label}) : make("button", {type: "button", className: `btn ${kind}`, textContent: label});
    if (run) node.addEventListener("click", run);
    return node;
  };
  if (running) out.push(button("Follow the run", "secondary", null, "#activity"));
  else if (failed && signed) out.push(button("Ask the agent to fix it", "primary", () => askAgent(`The setup run ${job.view.id} stopped. Find out why and propose the fix.`)), button("Open the run", "ghost", null, "#activity"));
  else if (failed) out.push(button("Open the run", "primary", null, "#activity"), button("Sign in the agent", "ghost", null, "#setup/agent"));
  else if (!built() && !signed) out.push(button("Sign in the agent", "primary", null, "#setup/agent"), button(`Go to ${defaultStep() === "agent" ? "hardware" : defaultStep()}`, "ghost", null, `#setup/${defaultStep() === "agent" ? "hardware" : defaultStep()}`));
  else if (!built()) out.push(button("Let the agent set it up", "primary", () => askAgent("Set this server up with me: read the hardware, suggest what it should run in a few lines, make the plan fit, then start setup. Ask me where the choice is mine, and fix what breaks.")), button("Continue setup", "ghost", null, `#setup/${defaultStep()}`));
  else if (lastStatus && (lastStatus.alerts.length || lastStatus.down.length) && signed) out.push(button("Ask the agent what is wrong", "primary", () => askAgent("Look at the firing alerts and the systems that do not answer, find the cause and propose the fix.")));
  return out;
}

function paintHero() {
  const s = lastStatus;
  $("hero-actions").replaceChildren(...heroActions());
  const failedRun = job && job.view && job.view.state === "failed" && job.view.action === "apply" && !built();
  const setIcon = (kind, name) => { $("hero-icon").dataset.state = kind; $("hero-icon").replaceChildren(icon(name)); };
  const alerts = $("hero-alerts");
  alerts.replaceChildren();
  const running = runningAction();
  if (!built()) {
    const left = STEPS.filter((x) => !stepDone(x)).length;
    $("hero-over").textContent = "Setup";
    const stage = failedRun && job.view.progress && job.view.progress.error ? job.view.progress.error.stage_text || job.view.progress.error.stage : "";
    $("hero-title").textContent = running ? `${JOB_NAMES[running] || "A run"} is running` : failedRun ? "Setup stopped" : left ? `${left} step${left > 1 ? "s" : ""} to a running server` : "Ready to build";
    const next = defaultStep();
    $("hero-sub").textContent = running ? "The bar at the top follows it." : failedRun ? [stage, (job.view.progress && job.view.progress.error && job.view.progress.error.message) || ""].filter(Boolean).join(": ") : {
      agent: "Start by signing in your agent: it guides you and fixes what breaks.",
      hardware: "Next: read the hardware. It changes nothing.",
      choices: "Next: choose what the server runs.",
      plan: "Next: make the plan fit this machine.",
      build: "Press Run setup. The first run takes a while.",
    }[next];
    setIcon(running ? "run" : failedRun ? "bad" : "idle", running ? "activity" : failedRun ? "alert" : "setup");
  } else if (!s) {
    $("hero-over").textContent = "Server";
    $("hero-title").textContent = "Checking…";
    $("hero-sub").textContent = "";
    setIcon("idle", "circle");
  } else if (s.monitoring === "not planned") {
    $("hero-over").textContent = "Server";
    $("hero-title").textContent = "Built";
    $("hero-sub").textContent = "Health shows here with monitoring, which your choices leave out.";
    setIcon("good", "check");
  } else if (s.monitoring !== "ok") {
    $("hero-over").textContent = "Server";
    $("hero-title").textContent = "Monitoring does not answer";
    $("hero-sub").textContent = "If setup is running or just finished, wait a minute. Or ask the agent.";
    setIcon("warn", "alert");
  } else {
    const n = s.alerts.length;
    $("hero-over").textContent = "Server";
    if (n) {
      $("hero-title").textContent = `${n} alert${n > 1 ? "s" : ""} firing`;
      setIcon(s.alerts.some((a) => a.severity === "page") ? "bad" : "warn", "alert");
    } else if (s.down.length) {
      $("hero-title").textContent = `${s.down.length} system${s.down.length > 1 ? "s" : ""} not answering`;
      setIcon("warn", "alert");
    } else {
      $("hero-title").textContent = "All clear";
      setIcon("good", "check");
    }
    $("hero-sub").textContent = n || s.down.length ? "Your phone gets these as alerts too." : "Every system answers and no alert is firing.";
    alerts.replaceChildren(...s.alerts.slice(0, 4).map((a) => make("li", {className: a.severity === "page" ? "page" : ""}, icon("alert"), make("span", {textContent: a.summary || a.name}))));
  }
  const stats = [];
  if (s && s.monitoring === "ok") {
    stats.push(stat("Answering", `${s.targets - s.down.length} of ${s.targets}`, s.down.length ? `Down: ${s.down.join(", ")}` : "All systems", s.down.length ? "bad" : "good"));
    stats.push(stat("Security updates", s.security_waiting.length ? `${s.security_waiting.length} waiting` : "Up to date",
      s.security_waiting.length ? s.security_waiting.join(", ") : "Installed nightly", s.security_waiting.length ? "warn" : "good"));
    if (s.reboot_waiting.length) stats.push(stat("Reboot needed", `${s.reboot_waiting.length}`, s.reboot_waiting.join(", "), "warn"));
  }
  const apply = state.last_jobs.apply;
  stats.push(stat("Last setup", !apply ? "Not run" : apply.state === "done" ? "Finished" : apply.state === "running" ? "Running" : "Stopped",
    apply ? ago(apply.started) : "", !apply ? "" : apply.state === "done" ? "good" : apply.state === "running" ? "" : "bad", "#activity"));
  const lan = state.lan.state;
  stats.push(stat("Home network", lan === "closed" ? "Closed" : lan === "closing" ? "Closing" : "Open",
    lan === "closed" ? "Proxmox only over Tailscale" : "Proxmox answers on your LAN", lan === "closed" ? "good" : "warn", "#access/lan"));
  if (state.agent_powers) stats.push(stat("Operator agent", "Root on", "Proxmox", "bad", "#agent/settings"));
  $("stats").replaceChildren(...stats);
}

// One line across the top for what needs the owner now.
function paintBanner() {
  const pending = chat.pending.length;
  let text = "";
  let href = "";
  let label = "Open";
  if (state.lan.state === "closing") {
    text = state.lan.via_vpn ? `Press Keep it closed within ${state.lan.minutes} minutes, or the home network opens again by itself.`
      : `Open this page over Tailscale and press Keep it closed within ${state.lan.minutes} minutes.`;
    href = "#access/lan";
  } else if (pending && !chatVisible()) {
    text = pending > 1 ? `The agent waits for your OK on ${pending} requests.` : "The agent waits for your OK.";
    href = "#agent/chat";
    label = "Show";
  } else if ((state.removals || []).length) {
    text = `Setup stopped before deleting ${andList(state.removals)}.`;
    href = "#setup/build";
  }
  show("banner", Boolean(text));
  $("banner-text").textContent = text;
  $("banner-link").href = href || "#home";
  $("banner-link").textContent = label;
  $("banner-link").dataset.request = href === "#agent/chat" ? "1" : "";
  // Asking from Home only when the agent is signed in and not already beside it.
  const signed = Boolean(state.chat && state.chat.signed_in);
  show("home-ask-panel", signed && (phone.matches || !dockOpen()));
}

// Services.
const secrets = {};
async function secret(name) {
  if (!(name in secrets)) secrets[name] = (await api("/api/password-reveal", {name})).password;
  return secrets[name];
}
async function copyText(text) { await navigator.clipboard.writeText(text); }

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

function kvRow(label, value, ...buttons) {
  return make("div", {className: "kv-row"}, make("dt", {textContent: label}), make("dd", {}, value, ...buttons));
}

function serviceState(watch) {
  const ok = lastStatus && lastStatus.monitoring === "ok";
  if (!watch || !ok) return ["", ""];
  if (lastStatus.answering.includes(watch)) return ["good", "Running"];
  if (lastStatus.down.includes(watch)) return ["bad", "Not answering"];
  return ["", ""];
}

function renderServices(st) {
  const services = st.services || {items: [], background: []};
  show("services-empty", services.items.length === 0);
  show("services-unbuilt", services.items.length > 0 && !built());
  $("service-list").replaceChildren(...services.items.map((s) => {
    const feedback = make("p", {className: "feedback"});
    feedback.setAttribute("role", "status");
    const rows = [];
    if (s.url) rows.push(kvRow("Address", make("code", {textContent: s.url}), copyButton(`Copy ${s.title} address`, () => s.url, feedback)));
    for (const a of s.addresses || []) {
      const m = /^([A-Z][^:]{0,40}): (.+)$/.exec(a);
      const [label, value] = m ? [m[1], m[2]] : ["Address", a];
      rows.push(kvRow(label, make("code", {textContent: value}), copyButton(`Copy ${s.title}: ${label.toLowerCase()}`, () => value, feedback)));
    }
    if (s.user) rows.push(kvRow("User", make("code", {textContent: s.user}), copyButton(`Copy ${s.title} user`, () => s.user, feedback)));
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
      rows.push(kvRow(word === "key" ? "API key" : "Password", value, reveal, copyButton(`Copy ${s.title} ${word}`, () => secret(s.password), feedback)));
    }
    const open = s.url ? make("a", {className: "btn primary small", href: s.url, target: "_blank", rel: "noopener noreferrer"}, "Open", make("span", {className: "sr-only", textContent: ` ${s.title}`}), icon("external")) : null;
    const item = make("li", {className: "service"},
      make("div", {className: "service-head"},
        make("span", {className: "service-icon", ariaHidden: "true"}, icon(SERVICE_ICONS[s.name] || "services")),
        make("div", {className: "service-title"},
          make("h2", {textContent: s.title}),
          make("span", {className: "service-state"}, make("span", {className: "dot", ariaHidden: "true"}), make("span", {className: "state-text"}))),
        open),
      make("p", {className: "service-what", textContent: s.what}),
      rows.length ? make("dl", {className: "kv"}, ...rows) : null,
      s.password_note ? make("p", {className: "service-note", textContent: s.password_note}) : null,
      s.how ? make("p", {className: "service-note", textContent: s.how}) : null,
      feedback);
    item.dataset.watch = s.watch || "";
    return item;
  }));
  $("services-background").textContent = services.background.length ? `Also running, with nothing to open: ${services.background.join(", ")}.` : "";
  show("glance-empty", services.items.length === 0);
  $("glance").replaceChildren(...services.items.map((s) => {
    const item = make("li", {},
      make("span", {className: "dot", ariaHidden: "true"}),
      make("span", {className: "glance-name"}, make("b", {textContent: s.title}), make("span", {className: "glance-state state-text"})),
      s.url ? make("a", {className: "btn ghost small", href: s.url, target: "_blank", rel: "noopener noreferrer"}, "Open", make("span", {className: "sr-only", textContent: ` ${s.title}`}), icon("external")) : null);
    item.dataset.watch = s.watch || "";
    return item;
  }));
  paintServices();
}

function paintServices() {
  for (const node of document.querySelectorAll("#service-list .service, #glance li")) {
    const watch = node.dataset.watch;
    const [dot, label] = serviceState(watch);
    node.querySelector(".dot").className = `dot ${dot}`;
    node.querySelector(".state-text").textContent = label || (built() ? (watch ? "No status yet" : "") : "Not built yet");
  }
}

async function refreshStatus() {
  lastStatus = await api("/api/status");
  paintServices();
  paintStates();
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
  state = st;
  show("gate", false);
  show("app", true);
  // Never undo what the owner is changing: unsaved choices stay.
  if (!choicesDirty() || disksChanged || (choicesChanged && !choicesDirty())) renderChoices(st);
  renderHardware(st.hardware);
  renderServices(st);
  renderPlan(st.plan);
  renderAlerts(st.alerts);
  renderAgentSignin(st);
  renderMode(st.chat);
  renderAgentPowers(st.agent_powers);
  renderAgentGithub(st.agent_github);
  renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
  renderLan(st.lan);
  renderTrust(st.trust);
  renderHistory(st.last_jobs);
  onChoicesChanged();
  paintChatShell();
  if (first) {
    route(false);
    clearInterval(statusTimer);
    refreshStatus().catch(() => {});
    statusTimer = setInterval(() => refreshStatus().catch(() => {}), 30000);
    startChat();
  }
  if (st.job && (!job || job.id !== st.job.id)) follow(st.job, st.job.state === "running");
  if (!st.job) paintPill(null);
  show("job-none", !st.job);
  gateJobButtons();
}

// A refresh soon, once: the agent and runs change state in bursts.
let refreshSoonTimer = null;
function refreshSoon() {
  clearTimeout(refreshSoonTimer);
  refreshSoonTimer = setTimeout(() => refresh().catch(() => {}), 400);
}

// Runs.
function stopFollowing() { if (job) clearTimeout(job.timer); }

function gateJobButtons() {
  const running = Boolean(runningAction());
  for (const button of document.querySelectorAll("button[data-action], .job-retry, #agent-powers")) {
    if (button.classList.contains("busy")) continue;
    button.setAttribute("aria-disabled", running ? "true" : "false");
    button.dataset.why = running ? "Wait until the running job finishes; the bar at the top shows it." : "";
  }
}

function renderHistory(lastJobs) {
  const entries = Object.entries(lastJobs).sort((a, b) => b[1].started - a[1].started);
  show("history-card", entries.length > 0);
  $("history").replaceChildren(...entries.map(([action, j]) => {
    const kind = j.state === "done" ? ["Finished", "good"] : j.state === "running" ? ["Running", "run"] : ["Stopped", "bad"];
    const button = make("button", {type: "button", className: "btn ghost small", textContent: "Show"});
    button.setAttribute("aria-label", `Show ${JOB_NAMES[action] || action}`);
    button.addEventListener("click", async () => {
      try {
        const view = await api(`/api/jobs/${j.id}`);
        follow(view, view.state === "running");
        job.node.querySelector(".job-title").focus();
      } catch (error) { toast(error.message); }
    });
    return make("li", {}, make("span", {className: "item-name", textContent: JOB_NAMES[action] || action}),
      make("span", {className: "muted small", textContent: clock(j.started)}),
      make("span", {className: `badge ${kind[1]}`, textContent: kind[0]}), button);
  }));
}

function follow(first, watched, feedback) {
  stopFollowing();
  if (job && job.feedback && job.feedback !== feedback && !job.feedback.classList.contains("error")) say(job.feedback, "", "");
  const node = $("job-template").content.firstElementChild.cloneNode(true);
  $("job-slot").replaceChildren(node);
  show("job-none", false);
  job = {id: first.id, view: first, node, offset: 0, timer: null, watched, misses: 0, feedback, stage: null};
  const again = ["verify", "upgrade"].includes(first.action) ? first.action : "apply";
  const retry = node.querySelector(".job-retry");
  retry.addEventListener("click", () => startJob(again, retry, node.querySelector(".job-copy-result")));
  node.querySelector(".job-copy").addEventListener("click", () => copyDetails(node));
  node.querySelector(".job-agent").addEventListener("click", () => {
    const view = job.view;
    const e = view.progress.error || {};
    const where = e.stage_text ? ` at "${e.stage_text}"` : "";
    askAgent(`The run ${view.id} (${view.progress.title}) stopped${where}: ${e.message || "see its log"}. Find out why and propose the fix.`);
  });
  const stopButton = node.querySelector(".job-stop");
  stopButton.addEventListener("click", async () => {
    if (stopButton.classList.contains("busy")) return;
    const ok = await confirmDialog({title: "Stop this run?", text: "What it finished stays. You can start it again afterwards.", yes: "Stop it"});
    stopButton.focus();
    if (!ok) return;
    press(stopButton, node.querySelector(".job-stop-result"), "Stopping…", async () => {
      await api(`/api/jobs/${first.id}/stop`, {});
      return "Stopped. What to do next shows here in a moment.";
    });
  });
  tick(job);
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
    $("jobpill-sub").textContent = "Lost contact; trying again…";
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
  if (view.state === "running") {
    current.timer = setTimeout(() => tick(current), 1000);
  } else if (current.watched) {
    current.watched = false;
    const title = view.progress.title;
    if (view.state === "failed") announce(`${title} stopped. ${view.progress.error ? view.progress.error.message : ""}`, true);
    else announce(`${title}: finished.`);
    if (current.feedback && wasRunning) {
      say(current.feedback, view.state === "done" ? "ok" : "error", view.state === "done" ? "Finished." : "Stopped. Activity says why.");
    }
    refresh().catch((error) => toast(error.message));
    if (view.state === "failed" && currentView === "activity") current.node.querySelector(".job-error-title").focus();
  }
}

function paintPill(view) {
  const pill = $("jobpill");
  const fill = pill.querySelector(".fill");
  if (!view) {
    pill.dataset.state = "idle";
    $("jobpill-title").textContent = "Nothing has run yet";
    $("jobpill-sub").textContent = "";
    fill.style.width = "0";
    return;
  }
  const p = view.progress;
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  pill.dataset.state = view.state;
  const name = JOB_NAMES[view.action] || p.title;
  if (view.state === "running") {
    $("jobpill-title").textContent = `${name}${steps.length ? ` · ${Math.min(steps.length, done + 1)}/${steps.length}` : ""}`;
    $("jobpill-sub").textContent = [now ? now.text : "", duration(view.seconds)].filter(Boolean).join(" · ");
    fill.style.width = steps.length ? `${Math.round((100 * done) / steps.length)}%` : "30%";
  } else if (view.state === "done") {
    $("jobpill-title").textContent = `${name} finished`;
    $("jobpill-sub").textContent = `${ago(view.started + view.seconds)} · ${duration(view.seconds)}`;
    fill.style.width = "100%";
  } else {
    $("jobpill-title").textContent = `${name} stopped`;
    $("jobpill-sub").textContent = p.error && p.error.stage_text ? `at ${p.error.stage_text}` : ago(view.started + view.seconds);
  }
}

function drawJob(current, view) {
  const node = current.node;
  const p = view.progress;
  node.dataset.state = view.state;
  node.querySelector(".job-icon").replaceChildren(...{done: [icon("check")], failed: [icon("x")]}[view.state] || []);
  paintPill(view);
  node.querySelector(".job-title").textContent = p.title;
  const badge = node.querySelector(".job-badge");
  const [btext, bkind] = {running: ["Running", "run"], done: ["Finished", "good"], failed: ["Stopped", "bad"]}[view.state];
  badge.textContent = btext;
  badge.className = `badge job-badge ${bkind}`;
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  const sub = [];
  if (steps.length) sub.push(`Stage ${Math.min(steps.length, done + (now ? 1 : 0))} of ${steps.length}`);
  if (view.state === "running") sub.push(`running for ${duration(view.seconds)}`);
  else sub.push(`took ${duration(view.seconds)}`, `started ${clock(view.started)}`);
  if (view.by === "agent") sub.push("started by the agent");
  node.querySelector(".job-sub").textContent = sub.join(" · ");
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
  node.querySelector(".job-stopper").hidden = view.state !== "running";
  const fold = node.querySelector(".job-stages");
  fold.hidden = !steps.length;
  if (view.state === "done" && !fold.dataset.folded) {
    fold.open = false;
    fold.dataset.folded = "1";
  }
  fold.querySelector("summary").textContent = view.state === "done" ? `All ${steps.length} stages` : "Stages";
  const WORD = {done: "done", failed: "failed", running: "running", waiting: "waiting"};
  node.querySelector(".stages").replaceChildren(...steps.map((s) => make("li", {className: s.status},
    make("span", {className: "s-icon", ariaHidden: "true"}, s.status === "done" ? icon("check") : s.status === "failed" ? icon("x") : null),
    make("span", {className: "s-text"}, s.text, make("span", {className: "sr-only", textContent: `, ${WORD[s.status]}`})),
    make("span", {className: "s-time", textContent: s.status === "waiting" ? "" : duration(s.seconds)}))));
  const failed = view.state === "failed" && p.error;
  node.querySelector(".job-error").hidden = !failed;
  if (failed) {
    const e = p.error;
    node.querySelector(".job-message").textContent = (e.stage_text ? `At "${e.stage_text}": ` : "") + e.message;
    node.querySelector(".job-more").replaceChildren(...e.more.map((m) => li(m)));
    const detail = node.querySelector(".job-detail");
    const lines = [];
    let title = "";
    if (e.task) {
      title = `The step that failed${e.task.task ? `: ${e.task.task}` : ""}, on ${e.task.host}`;
      lines.push(e.task.detail);
    } else if (e.tofu.length) {
      title = "What Proxmox or OpenTofu said";
      lines.push(...e.tofu);
    }
    detail.hidden = !lines.length;
    node.querySelector(".job-detail-title").textContent = title;
    node.querySelector(".job-detail-text").textContent = lines.join("\n");
    node.querySelector(".job-advice").textContent = e.advice;
    const retry = node.querySelector(".job-retry");
    retry.hidden = !["apply", "verify", "rebake-ci", "upgrade", "remove-guests"].includes(view.action);
    retry.textContent = view.action === "verify" ? "Run the checks again" : view.action === "upgrade" ? "Update again" : "Run setup again";
    node.querySelector(".job-agent").hidden = !(state && state.chat && state.chat.signed_in);
  }
  const ok = node.querySelector(".job-ok");
  ok.hidden = view.state !== "done";
  const next = node.querySelector(".job-ok-next");
  next.replaceChildren();
  if (view.state === "done") {
    const text = {
      probe: ["The hardware is read", "Choose what to run next."],
      apply: ["Setup finished", "Everything in the plan is in place. The checks ran at the end; their results are below."],
      verify: ["The checks finished", "No check failed."],
      upgrade: ["NullBill is updated", "Run setup now, so the server follows the new version."],
      "close-lan": ["The home network is closing", "Open this page over Tailscale and press Keep it closed under Access."],
      "keep-lan-closed": ["The home network stays closed", "Proxmox now answers only over Tailscale."],
      "open-lan": ["The home network is open again", "Proxmox answers on your home network."],
      "rebake-ci": ["The CI images are rebuilt", ""],
      "remove-guests": ["Setup finished", "The guests you chose to delete are gone; everything else in the plan is in place."],
    }[view.action] || ["Finished", ""];
    node.querySelector(".job-ok-title").textContent = text[0];
    node.querySelector(".job-ok-text").textContent = text[1];
    if (view.action === "probe") next.append(make("a", {className: "btn secondary small", href: "#setup/choices", textContent: "Choose what to run"}));
    if (view.action === "apply") next.append(make("a", {className: "btn secondary small", href: "#services", textContent: "Open your services"}));
    if (view.action === "upgrade") {
      const run = make("button", {type: "button", className: "btn primary small", textContent: "Run setup"});
      const result = make("p", {className: "feedback"});
      run.addEventListener("click", () => startJob("apply", run, result));
      next.append(run, result);
    }
  }
  const v = p.verify;
  const box = node.querySelector(".job-verify");
  box.hidden = !v;
  if (v) {
    const c = v.counts;
    box.querySelector(".counts").replaceChildren(
      make("li", {className: "badge good", textContent: `${c.PASS} passed`}),
      make("li", {className: `badge ${c.FAIL ? "bad" : ""}`, textContent: `${c.FAIL} failed`}),
      make("li", {className: `badge ${c.DEGRADED ? "warn" : ""}`, textContent: `${c.DEGRADED} degraded`}),
      make("li", {className: "badge", textContent: `${c.SKIP} skipped`}));
    box.querySelector(".problems").replaceChildren(...v.problems.map((x) =>
      make("li", {className: x.status.toLowerCase()}, make("b", {textContent: x.status === "FAIL" ? "Failed: " : "Degraded: "}), x.text)));
  }
  const pre = node.querySelector(".job-log pre");
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent += view.text;
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

async function copyDetails(node) {
  const view = job.view;
  const e = view.progress.error;
  const log = node.querySelector(".job-log pre").textContent.trimEnd().split("\n").slice(-60).join("\n");
  const text = [
    `${view.progress.title}: ${view.state} (nullbill ${view.args.join(" ")})`,
    e ? `${e.stage ? `Stage ${e.stage}: ` : ""}${e.message}` : "",
    e && e.task ? `Failed step: ${e.task.task || "?"} on ${e.task.host}\n${e.task.detail}` : "",
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
  if (action === "remove-guests") {
    const names = andList(state.removals || []);
    const ok = await confirmDialog({title: `Delete ${names}?`, text: "Everything on their disks is erased, for good. Then setup runs.", yes: "Delete and run setup", word: "delete"});
    button.focus();
    if (!ok) return;
  }
  await press(button, feedback, STARTING[action] || "Starting…", async () => {
    const view = await api("/api/jobs", {action});
    follow(view, true, feedback);
    if (OPENS_ACTIVITY.has(action) && currentView !== "activity") go("activity");
    return OPENS_ACTIVITY.has(action) ? "Started." : "Started. The bar at the top follows it.";
  });
}

function feedbackFor(button) {
  if (button.closest(".topbar")) return $("actions-result");
  const row = button.closest(".row");
  return row ? row.querySelector(".feedback") : $("actions-result");
}

function submitter(event) { return event.submitter || event.target.querySelector("button[type=submit]"); }

// The agent's sign-in, from any of its three forms.
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
      ? "Saved, but the agent program is not installed here yet: press Update NullBill."
      : "Signed in. The agent says hello in its panel.";
  }, field);
}

// The chat with the agent.
const chat = {
  seq: 0, timer: null, running: false, busy: false, pending: [], asks: new Set(), nodes: new Map(),
  typing: null, seen: 0, latest: 0, mode: "manual", failures: 0,
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
  chat.busy = view.busy;
  chat.queued = view.queued || 0;
  chat.pending = view.pending || [];
  // A request that is no longer pending on the server has expired.
  for (const [id, node] of chat.nodes) {
    if (node.classList.contains("request") && node.dataset.state === "pending" && !chat.pending.includes(id)) settleRequest(node, "expired");
  }
  if (view.mode && state && state.chat && (view.mode !== state.chat.mode || view.auto_fix !== state.chat.auto_fix)) {
    state.chat.mode = view.mode;
    state.chat.auto_fix = view.auto_fix;
    renderMode(state.chat);
  }
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
  if (state) paintBanner();
  if (refreshNeeded || (wasBusy && !view.busy)) refreshSoon();
}

function markSeen() { chat.seen = chat.latest; paintChatBadge(); }

function paintChatBadge() {
  const waiting = chat.pending.length + [...chat.asks].length;
  const unread = chat.latest > chat.seen && !chatVisible();
  const text = waiting ? String(waiting) : unread ? "•" : "";
  const badge = $("tab-agent-badge");
  badge.hidden = !text;
  badge.textContent = text;
  badge.setAttribute("aria-label", waiting ? `${waiting} waiting for you` : unread ? "new message" : "");
  if ("setAppBadge" in navigator) (waiting ? navigator.setAppBadge(waiting) : navigator.clearAppBadge()).catch(() => {});
  setNav("agent", text, waiting ? "warn" : "agent");
}

function chatEmpty() {
  return make("div", {className: "chat-empty"}, icon("agent", "big accent-2"),
    make("b", {textContent: "Ask the agent anything about your server."}),
    make("span", {textContent: "It reads logs and runs checks by itself. It asks you before it changes anything your access mode keeps for you."}));
}

function paintChatShell() {
  if (!state) return;
  const c = state.chat || {};
  const signed = Boolean(c.signed_in);
  show("chat-signin", !signed);
  show("chat-log", signed);
  $("composer").hidden = !signed;
  const log = $("chat-log");
  const empty = log.querySelector(".chat-empty");
  const hasMessages = [...log.children].some((n) => !n.classList.contains("chat-empty") && !n.classList.contains("typing"));
  if (signed && !hasMessages && !empty) log.prepend(chatEmpty());
  if (hasMessages && empty) empty.remove();
  $("chat").classList.toggle("working", chat.busy);
  $("chat-status").textContent = !signed ? "Not signed in"
    : c.installed === false ? "Not installed: press Update NullBill"
    : chat.busy && chat.queued ? `Working… then ${chat.queued} more message${chat.queued === 1 ? "" : "s"}`
    : chat.busy ? "Working…"
    : chat.pending.length ? "Waiting for you"
    : "Ready";
  show("chat-stop", chat.busy);
}

function paintTyping() {
  const log = $("chat-log");
  if (chat.busy) {
    if (!chat.typing) {
      chat.typing = make("div", {className: "typing"}, make("i"), make("i"), make("i"), make("span", {textContent: "Working"}));
      chat.typing.setAttribute("aria-hidden", "true");
    }
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

const TIER_TEXT = {read: "Read", change: "Change", risky: "Risky", blocked: "Blocked"};

function turnMeta(e) {
  const parts = [`${e.seconds} s`];
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
    case "user":
      addToLog(make("div", {className: "msg user", textContent: e.text}));
      break;
    case "assistant":
      addToLog(make("div", {className: "msg assistant"}, ...markdown(e.text)));
      break;
    case "system":
      addToLog(make("div", {className: "msg system", textContent: e.text}));
      break;
    case "error":
      addToLog(make("div", {className: "msg error", textContent: e.text}));
      break;
    case "turn":
      if (e.state !== "end") break;
      if (e.error) {
        const node = addToLog(make("div", {className: "msg error", textContent: e.error}));
        if (/carry on/i.test(e.error)) {
          const chip = make("button", {type: "button", className: "chip", textContent: "Carry on"});
          chip.addEventListener("click", () => askAgent("Carry on."));
          node.append(make("div", {className: "chips"}, chip));
        }
      }
      if (e.seconds) addToLog(make("div", {className: "turn-meta", textContent: turnMeta(e)}));
      break;
    case "ran":
      addToLog(make("div", {className: "ran"},
        make("span", {className: `badge ${e.tier === "risky" ? "warn" : ""}`, textContent: `Ran (${MODE_NAMES[e.mode] || e.mode})`}),
        make("span", {className: "ran-title", textContent: e.title}),
        e.command ? make("code", {className: "cmd inline", textContent: e.command}) : null));
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
      if (e.output) node.append(make("pre", {tabIndex: 0, textContent: e.output}));
      else node.querySelector("summary").addEventListener("click", (ev) => ev.preventDefault());
      break;
    }
    case "proposal":
      addToLog(requestCard(e));
      break;
    case "proposal_update": {
      const node = chat.nodes.get(e.id);
      if (node) settleRequest(node, e.state, e.output);
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
        make("div", {className: "card-head"}, icon("shield"), make("b", {textContent: "Blocked by the page"})),
        make("span", {textContent: e.title}),
        e.command ? make("div", {className: "cmd", textContent: e.command}) : null,
        make("span", {className: "why", textContent: `Refused in every mode: this ${e.why}.`})));
      break;
    case "job":
      addToLog(make("div", {className: "msg system"}, `Started: ${e.title || JOB_NAMES[e.action] || e.action}. `, make("a", {href: "#activity", textContent: "Follow it"})));
      break;
    default:
      break;
  }
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
  const parts = [
    make("div", {className: "card-head"}, icon(e.tier === "risky" ? "alert" : "shield"),
      make("b", {id: titleId, className: "card-title", textContent: e.act === "command" && !e.lines ? `Run on ${where}${e.background ? " in the background" : ""}` : e.title}),
      make("span", {className: `badge ${e.tier === "risky" ? "bad" : "warn"}`, textContent: TIER_TEXT[e.tier] || e.tier})),
  ];
  if (e.act === "command") parts.push(make("div", {className: "cmd", textContent: e.command}));
  if (e.act === "steps") {
    parts.push(make("ol", {className: "steps-list"}, ...(e.steps || []).map((step) => make("li", {},
      make("span", {className: "step-where", textContent: step.where === "host" ? "Proxmox" : step.where}),
      make("span", {className: `badge ${step.tier === "risky" ? "bad" : step.tier === "change" ? "warn" : ""}`, textContent: TIER_TEXT[step.tier] || step.tier}),
      make("div", {className: "cmd", textContent: step.command})))));
  }
  if (e.act === "write" && e.diff) parts.push(diffView(e.diff));
  if (e.lines && e.lines.length) parts.push(make("ul", {className: "says"}, ...e.lines.map((l) => li(l))));
  if (e.why) parts.push(make("p", {className: "says", textContent: e.why}));
  parts.push(make("p", {className: "why", textContent: `Waits for you because this ${e.reason}.`}));
  const approve = make("button", {type: "button", className: `btn ${e.tier === "risky" ? "danger" : "primary"} small`, textContent: "Approve"});
  approve.setAttribute("aria-keyshortcuts", "Alt+Y");
  const decline = make("button", {type: "button", className: "btn ghost small", textContent: "Decline"});
  decline.setAttribute("aria-keyshortcuts", "Alt+N");
  const actions = make("div", {className: "row actions"});
  let word = null;
  if (e.tier === "risky" && e.word) {
    word = make("input", {autocomplete: "off", spellcheck: false, placeholder: e.word, className: "word"});
    word.setAttribute("aria-label", `Type ${e.word} to approve`);
    actions.append(make("label", {className: "field narrow-field"}, make("span", {className: "field-label", textContent: `Type ${e.word} to approve`}), word));
    word.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); approve.click(); } });
  }
  actions.append(approve, decline, result);
  parts.push(actions);
  card.append(...parts);
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
  running: ["Approved, running…", "run"], done: ["Done", "good"], failed: ["Failed", "bad"],
  declined: ["Declined", ""], expired: ["Expired: not answered in 30 minutes", ""],
};

function settleRequest(card, stateName, output) {
  card.dataset.state = stateName;
  if (stateName === "pending") return;
  card.classList.add("settled");
  const actions = card.querySelector(".actions");
  if (actions) actions.remove();
  let line = card.querySelector(".settled-line");
  if (!line) {
    line = make("p", {className: "settled-line"});
    card.append(line);
  }
  const [text, kind] = SETTLED[stateName] || [stateName, ""];
  line.replaceChildren(make("span", {className: `badge ${kind}`, textContent: text}));
  if (output) {
    const old = card.querySelector(".result");
    if (old) old.remove();
    card.append(make("pre", {className: "result", tabIndex: 0, textContent: output}));
  }
}

function askCard(e) {
  const card = make("div", {className: "card ask"});
  card.dataset.id = e.id;
  const result = make("p", {className: "feedback"});
  result.setAttribute("role", "status");
  const input = make("input", {autocomplete: "off", placeholder: "Your answer"});
  input.setAttribute("aria-label", "Your answer");
  const send = make("button", {type: "submit", className: "btn primary small icon-only", title: "Send answer"}, icon("send"), make("span", {className: "sr-only", textContent: "Send answer"}));
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
  card.append(make("div", {className: "card-head"}, icon("agent", "accent-2"), make("b", {textContent: "The agent asks"})),
    make("div", {className: "msg assistant"}, ...markdown(e.question)),
    (e.options || []).length ? options : null,
    make("div", {className: "answer-box"}, form, result));
  chat.nodes.set(`ask-${e.id}`, card);
  chat.asks.add(e.id);
  return card;
}

function settleAsk(card, text) {
  card.classList.add("settled");
  const box = card.querySelector(".answer-box");
  if (box) box.remove();
  const chips = card.querySelector(".chips");
  if (chips) chips.remove();
  card.append(make("p", {className: "says"}, make("b", {textContent: "You: "}), text));
}

async function sendChat(text, feedback) {
  text = text.trim();
  if (!text) return false;
  if (!(state.chat && state.chat.signed_in)) {
    openAgent(true);
    say("chat-signin-result", "info", "Sign the agent in first; then send your message again.");
    return false;
  }
  try {
    const view = await api("/api/chat", {text, after: chat.seq});
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
  return out.slice(0, 3);
}

function paintChips() {
  const items = suggestions();
  const key = JSON.stringify(items);
  for (const id of ["home-chips", "chat-chips"]) {
    const box = $(id);
    if (box.dataset.key === key) continue;
    box.dataset.key = key;
    box.replaceChildren(...items.map(([label, text]) => {
      const chip = make("button", {type: "button", className: "chip", textContent: label});
      chip.addEventListener("click", () => askAgent(text));
      return chip;
    }));
  }
}

async function saveSettings(body) {
  const result = await api("/api/chat/settings", body);
  state.chat.mode = result.mode;
  state.chat.auto_fix = result.auto_fix;
  renderMode(state.chat);
  pollChat();
  return result;
}

// Command palette.
function commands() {
  const out = [
    ...VIEWS.map((v) => ({label: $(`view-${v}`).dataset.title, kind: "Page", icon: v === "home" ? "home" : v, run: () => go(v)})),
    ...STEPS.map((s, i) => ({label: `Setup: ${i + 1}. ${$("stepper").querySelector(`[data-step="${s}"] .step-name`).textContent}`, kind: "Step", icon: "setup", run: () => go(`setup/${s}`)})),
    {label: "Run setup", kind: "Run", icon: "play", run: () => startJob("apply", document.querySelector('.topbar [data-action="apply"]'), $("actions-result"))},
    {label: "Run the checks", kind: "Run", icon: "checks", run: () => startJob("verify", document.querySelector('.topbar [data-action="verify"]'), $("actions-result"))},
    {label: "Update NullBill", kind: "Run", icon: "update", run: () => startJob("upgrade", document.querySelector('.topbar [data-action="upgrade"]'), $("actions-result"))},
    {label: "Read the hardware", kind: "Run", icon: "chip", run: () => { go("setup/hardware"); $("probe-button").click(); }},
    {label: "Open the agent", kind: "Agent", icon: "agent", run: () => openAgent(true)},
    {label: "New conversation with the agent", kind: "Agent", icon: "plus", run: () => $("chat-new").click()},
    {label: "Stop the agent", kind: "Agent", icon: "stop", run: () => api("/api/chat/stop", {}).then(pollChat).catch((e) => toast(e.message))},
    {label: "Access mode", kind: "Agent", icon: "shield", run: () => { go("agent/settings"); setTimeout(() => document.querySelector('#modes [aria-pressed="true"]').focus(), 30); }},
    {label: "What the agent did", kind: "Agent", icon: "activity", run: () => go("agent/log")},
    {label: "Tailscale", kind: "Access", icon: "access", run: () => go("access/tailscale")},
    {label: "Home network", kind: "Access", icon: "lock", run: () => go("access/lan")},
    {label: "Trust this server, install the app", kind: "Access", icon: "download", run: () => go("access/trust")},
    {label: "Light or dark", kind: "Look", icon: "moon", run: toggleTheme},
    {label: "Keyboard shortcuts", kind: "Help", icon: "keyboard", run: () => $("keys").showModal()},
    {label: "Sign out", kind: "Account", icon: "logout", run: logout},
  ];
  for (const s of (state && state.services && state.services.items) || []) {
    if (s.url) out.push({label: `Open ${s.title}`, kind: "Service", icon: SERVICE_ICONS[s.name] || "services", run: () => window.open(s.url, "_blank", "noopener")});
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

// Theme.
const lightSystem = matchMedia("(prefers-color-scheme: light)");
function effectiveTheme() {
  return document.documentElement.dataset.theme || (lightSystem.matches ? "light" : "dark");
}
// theme: "light", "dark", or null to follow the system.
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  document.querySelector('meta[name="theme-color"]').content = bg || (effectiveTheme() === "light" ? "#f4f6f9" : "#07090d");
  const button = $("theme");
  if (button) {
    button.setAttribute("aria-pressed", effectiveTheme() === "light" ? "true" : "false");
    button.title = `Light theme: ${effectiveTheme() === "light" ? "on" : "off"}${theme ? "" : " (following the system)"}`;
  }
}
function toggleTheme() {
  const next = effectiveTheme() === "light" ? "dark" : "light";
  // Back to the system's choice when that is where the owner lands.
  const follow = next === (lightSystem.matches ? "light" : "dark");
  store.set("nb-theme", follow ? null : next);
  applyTheme(follow ? null : next);
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

// The installable app.
function paintInstall() {
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const can = Boolean(installPrompt) && !standalone;
  show("install-app", can);
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
function registerWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  navigator.serviceWorker.register("/sw.js").catch(() => {
    // A browser that does not trust the certificate refuses workers.
    say("pwa-state", "info", "This browser does not trust the server's certificate yet, so it cannot install the app.");
  });
}

// Keyboard.
let gPending = 0;
const G_KEYS = {h: "home", a: "agent", s: "services", u: "setup", x: "access", l: "alerts", y: "activity"};

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
    (word || card.querySelector(".actions .btn:not(.ghost)")).focus();
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

function onKey(event) {
  if (!state || $("app").hidden) return;
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
      card.querySelector(k === "KeyY" ? ".actions .btn:not(.ghost)" : ".actions .btn.ghost").click();
      return;
    }
  }
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
  const chosen = store.get("nb-theme");
  applyTheme(chosen === "light" || chosen === "dark" ? chosen : null);
  lightSystem.addEventListener("change", () => { if (!document.documentElement.dataset.theme) applyTheme(null); });
  $("single-keys").checked = store.get("nb-single-keys") !== "off";
  $("single-keys").addEventListener("change", () => store.set("nb-single-keys", $("single-keys").checked ? null : "off"));
  document.querySelector(".skip").addEventListener("click", (event) => { event.preventDefault(); $("main").focus(); });
  $("dock-scrim").addEventListener("click", () => { setDock(false); $("toggle-dock").focus(); });
  $("banner-link").addEventListener("click", (event) => {
    if (!event.currentTarget.dataset.request) return;
    event.preventDefault();
    showRequest();
  });
  setDock(!narrow.matches && store.get("nb-dock") !== "closed");
  placeChat();
  $("toast-close").addEventListener("click", () => toast(""));
  window.addEventListener("hashchange", () => route(true));
  document.addEventListener("keydown", onKey);
  for (const media of [phone, narrow]) {
    media.addEventListener("change", () => {
      setDock(!narrow.matches && store.get("nb-dock") !== "closed");
      placeChat();
      if (state) route(false);
    });
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state) { refreshSoon(); pollChat(); }
  });
  document.addEventListener("input", (event) => { if (event.target.removeAttribute) event.target.removeAttribute("aria-invalid"); });
  // Buttons that hand a question to the agent.
  document.addEventListener("click", (event) => {
    const ask = event.target.closest("[data-ask]");
    if (ask) askAgent(ask.dataset.ask);
    const link = event.target.closest('a[href^="#"]');
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
  $("toggle-dock").addEventListener("click", () => setDock(!dockOpen(), true));
  $("chat-close").addEventListener("click", () => { setDock(false, true); $("toggle-dock").focus(); });
  $("open-palette").addEventListener("click", openPalette);
  $("shortcuts").addEventListener("click", () => $("keys").showModal());
  $("theme").addEventListener("click", toggleTheme);
  $("logout").addEventListener("click", logout);
  $("install-app").addEventListener("click", install);
  $("install-app-2").addEventListener("click", install);
  $("tab-more").addEventListener("click", () => $("more").showModal());
  $("more").addEventListener("click", (event) => {
    const target = event.target.closest("a, [data-run]");
    if (event.target === $("more")) { $("more").close(); return; }
    if (!target) return;
    $("more").close();
    const run = target.dataset.run;
    if (run === "verify" || run === "upgrade") startJob(run, document.querySelector(`.topbar [data-action="${run}"]`), $("actions-result"));
    if (run === "install") install();
    if (run === "theme") toggleTheme();
    if (run === "logout") logout();
  });
  wireTabs("agent-tabs", (tab) => `agent/${tab}`);
  wireTabs("access-tabs", (tab) => `access/${tab}`);
  $("step-agent").querySelector('a[href="#setup/hardware"]').addEventListener("click", () => store.set("nb-skip-agent", "1"));

  // Palette.
  $("palette-q").addEventListener("input", () => { palette.index = 0; paintPalette(); });
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
  $("palette").addEventListener("click", (event) => { if (event.target === $("palette")) $("palette").close(); });

  // The agent.
  for (const id of ["agent-form", "setup-agent-form", "chat-signin-form"]) {
    const field = {"agent-form": "agent-credential", "setup-agent-form": "setup-agent-credential", "chat-signin-form": "chat-credential"}[id];
    const result = {"agent-form": "agent-result", "setup-agent-form": "setup-agent-result", "chat-signin-form": "chat-signin-result"}[id];
    $(id).addEventListener("submit", (event) => signInAgent(event, field, result));
  }
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
    if (!text.trim()) return;
    composer.value = "";
    grow();
    if (!(await sendChat(text, "composer-result"))) { composer.value = text; grow(); }
  });
  $("home-ask").addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = $("home-ask-text");
    const text = field.value;
    if (!text.trim()) { say("home-ask-result", "info", "Write a question first, or pick one below."); field.focus(); return; }
    field.value = "";
    openAgent(false);
    if (!(await sendChat(text, "home-ask-result"))) field.value = text;
  });
  $("chat-stop").addEventListener("click", () => press($("chat-stop"), "composer-result", "Stopping…", async () => {
    await api("/api/chat/stop", {});
    pollChat();
    return "";
  }));
  $("chat-new").addEventListener("click", async () => {
    if (chat.busy || chat.pending.length) {
      const ok = await confirmDialog({title: "Start a new conversation?", text: "The agent stops what it is doing, and waiting requests expire.", yes: "Start over", danger: false});
      $("chat-new").focus();
      if (!ok) return;
    }
    try {
      await api("/api/chat/new", {});
      pollChat();
      composer.focus();
    } catch (error) { say("composer-result", "error", error.message); }
  });
  $("chat-mode").addEventListener("click", () => {
    go("agent/settings");
    setTimeout(() => { const button = document.querySelector('#modes [aria-pressed="true"]'); if (button) button.focus(); }, 30);
  });
  $("modes").addEventListener("click", async (event) => {
    const input = event.target.closest(".mode");
    if (!input) return;
    const mode = input.dataset.mode;
    if (mode === state.chat.mode) return;
    if (mode === "yolo") {
      const ok = await confirmDialog({title: "Let the agent act without asking?", text: "In YOLO it runs every command, script and setup run it decides on, without waiting for you, including reboots and deleting. What is always blocked stays blocked, and every action is logged.", yes: "Turn on YOLO", word: "yolo"});
      if (!ok) { input.focus(); return; }
    }
    try {
      say("mode-result", "busy", "Saving…");
      await saveSettings({mode});
      say("mode-result", "ok", `${MODE_NAMES[mode]}. The agent knows from its next step.`);
      input.focus();
    } catch (error) {
      renderMode(state.chat);
      say("mode-result", "error", error.message);
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
  choices.addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("owner-login");
    press(submitter(event), "choices-result", "Saving and making the plan…", async () => {
      if (!field.value.trim()) throw new ApiError("Enter the email address you sign in to Tailscale with.", 400);
      const result = await api("/api/choices", choicesBody());
      savedChoices = null;
      await refresh();
      go("setup/plan");
      return result.plan.errors.length ? "Saved, but the plan does not fit yet." : "Saved. The plan fits.";
    }, field);
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
    const result = await api("/api/alerts/test", {});
    return `Sent to ${result.sent.join(" and ")}. Check your phone.`;
  }));
  $("copy-topic").addEventListener("click", () => press($("copy-topic"), "test-result", "Copying…", async () => {
    try { await copyText($("ntfy-topic").textContent); } catch {
      throw new ApiError("Copying is blocked here; select the topic and copy it by hand.", 400);
    }
    return "Topic copied.";
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
  registerWorker();

  refresh().catch((error) => {
    show("loading", false);
    toast(error.message);
  });
});
