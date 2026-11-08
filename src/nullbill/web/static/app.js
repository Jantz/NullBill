"use strict";

// The setup and status page. A sidebar of pages (Overview, Services, Setup,
// Remote access, Agents, Alerts, Activity); the bar at the top shows the
// latest job and holds Run setup, Run checks and Update NullBill on every
// page. Every control says what it is doing next to itself: busy while the
// server works, then saved or the reason it was refused. Long work shows its
// stages as they run, at the top and in full under Activity.

const LABELS = {
  "agents": ["AI agents", "The operator and a workspace container for coding agents."],
  "linux-ci": ["Linux CI runners", "Run Forgejo Actions jobs in throwaway Linux VMs."],
  "inference": ["Local AI models on the GPU", "Needs more memory."],
  "media": ["Media server (Jellyfin)", "Plays the films, series and music on the disks you keep below, on your devices over Tailscale."],
  "windows-ci": ["Windows CI runners", "Needs a Windows ISO and more memory."],
  "windows-dev": ["A Windows desktop VM", "Upload the Windows 11 ISO in Proxmox first; you install Windows in it."],
};

const VIEWS = ["overview", "services", "setup", "access", "agents", "alerts", "activity"];
// Jobs that open Activity when they start; the others show their progress
// in the bar at the top while the owner stays where they pressed.
const OPENS_ACTIVITY = new Set(["apply", "verify", "upgrade"]);
const SERVICE_ICONS = {
  proxmox: "proxmox", grafana: "chart", forgejo: "git", files: "folder", jellyfin: "film",
  workspace: "agents", operator: "bot", "windows-dev": "window", "linux-runner": "cog", "model-gateway": "chip",
};

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

let state = null;          // the last /api/state
let savedChoices = null;   // the choices form as last saved, to tell unsaved changes
let statusTimer = null;
let lastStatus = null;     // the last /api/status
let job = null;            // the followed job: {id, view, node, offset, timer, watched, feedback}
let currentView = null;

// A server message as a sentence: capital letter, full stop.
function sentence(text) {
  const t = String(text || "").trim();
  if (!t) return t;
  // A word, not a file or a name: router.yml stays as it is.
  const s = /^[a-z]+[\s,:;]/.test(t) ? t[0].toUpperCase() + t.slice(1) : t;
  return /[.!?]$/.test(s) ? s : s + ".";
}

function announce(text, urgent) {
  const region = $(urgent ? "live-urgent" : "live");
  region.textContent = "";
  // A fresh text node after a tick, so the same words are read again.
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
    response = await fetch(path, {credentials: "same-origin", ...options});
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
  node.className = `${node.className.split(" ").filter((c) => !["ok", "error", "info", "busy"].includes(c)).join(" ")} ${kind || ""}`.trim();
  node.replaceChildren();
  if (!text) return;
  const mark = {busy: make("span", {className: "spin", ariaHidden: "true"}), ok: icon("check"), error: icon("alert")}[kind];
  node.append(...[mark, make("span", {textContent: text})].filter(Boolean));
}

// Run one press: the button waits, the line beside it says what happens.
// work() returns the success text. On a refusal the field it names, if any,
// is marked. The button keeps focus throughout (aria-disabled, not disabled).
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

function show(id, visible) { $(id).hidden = !visible; }

// Pages.
function defaultView() {
  const apply = state && state.last_jobs.apply;
  return apply && apply.state === "done" ? "overview" : "setup";
}

function route(focus) {
  if (!state) return;
  const [name, section] = location.hash.slice(1).split("/");
  const target = VIEWS.includes(name) ? name : defaultView();
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== target;
  for (const link of document.querySelectorAll("#nav a")) {
    if (link.dataset.view === target) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  const changed = currentView !== target;
  currentView = target;
  document.title = `${$(`view-${target}`).dataset.title} · NullBill`;
  if (section && $(`sec-${section}`)) {
    $(`sec-${section}`).scrollIntoView({block: "start"});
    const heading = $(`sec-${section}`).querySelector("h2");
    if (focus && heading) { heading.tabIndex = -1; heading.focus({preventScroll: true}); }
  } else if (changed) {
    window.scrollTo(0, 0);
    if (focus) $(`h-${target}`).focus({preventScroll: true});
  }
}

function go(hash) {
  if (location.hash === `#${hash}`) route(true);
  else location.hash = hash;
}

function signedOut() {
  clearInterval(statusTimer);
  stopFollowing();
  // Signed in again, the page follows the latest job afresh.
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

// What each choice says for this disk, and why it cannot be picked, if so.
function diskChoice(disk, value, keepable) {
  if (value === "keep" && !keepable && disk.choice !== "keep") return [CHOICES.keep, "Not possible: no NTFS, exFAT or FAT partition on it."];
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

function gib(mib) { return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${mib} MiB`; }

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
    // What an erased disk becomes.
    const uses = make("div", {className: "disk-sub disk-for"}, make("span", {className: "field-label", textContent: "Then use it"}));
    for (const [value, text, desc] of [["backups", "For the nightly backups", ""],
      ["storage", "As extra storage in Proxmox", "A copy made first moves onto it, checked again."]]) {
      const input = make("input", {type: "radio", name: `disk-for-${disk.id}`, value, checked: disk.for === value});
      uses.append(tile(input, text, desc));
    }
    const confirmInput = make("input", {autocomplete: "off", spellcheck: false});
    const confirm = make("label", {className: "field disk-confirm"},
      make("span", {className: "field-label", textContent: `Type ${disk.confirm}, the end of its name, to erase everything on it`}), confirmInput);
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

function gpuChoices() {
  const out = {};
  for (const input of $("gpu-list").querySelectorAll("input")) out[input.dataset.pci] = input.value;
  return out;
}

function diskChoices() {
  const out = {};
  for (const box of $("disk-list").querySelectorAll(".disk")) {
    const chosen = box.querySelector("input[data-role=choice]:checked");
    out[box.dataset.id] = {
      choice: chosen ? chosen.value : "alone",
      rw: box.querySelector(".disk-rw input").checked,
      for: box.querySelector(".disk-for input:checked").value,
      confirm: box.querySelector(".disk-confirm input").value,
    };
  }
  return out;
}

function choicesBody() {
  return {
    workloads: [...$("workload-tiles").querySelectorAll("input:checked")].map((i) => i.value),
    backups: $("backups").checked,
    console: $("console").checked,
    owner_login: $("owner-login").value.trim(),
    disks: diskChoices(),
    gpus: gpuChoices(),
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
  if (dirty) say("choices-result", "info", "Not saved yet. Press Save and plan to see what these choices build.");
  else if ($("choices-result").classList.contains("info")) say("choices-result", "", "");
  $("plan-stale").hidden = !dirty || !state || !(state.plan.text || state.plan.errors.length);
  paintStates();
}

const SPEC = (iconName, label, value) => make("li", {}, icon(iconName), make("span", {}, make("small", {textContent: label}), value));

function renderHardware(hw) {
  const list = $("hardware");
  list.hidden = !hw;
  $("probe-button").querySelector("span").textContent = hw ? "Read it again" : "Read the hardware";
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
  if (plan.summary) {
    const m = plan.summary.memory;
    const guests = plan.summary.guests;
    $("plan-ok-text").textContent = m.host_mib
      ? `${guests.length} guests count for ${gib(m.guests_mib)} of this server's ${gib(m.host_mib)} memory, ` +
        `Proxmox keeps ${gib(m.reserve_mib)} for itself and ${gib(m.unallocated_mib)} stays free.` +
        (m.limits_mib && m.limits_mib !== m.guests_mib
          ? ` VMs count in full and containers at part of their limit, since a container takes memory only as it uses it; ` +
            `all limits together come to ${gib(m.limits_mib)}.`
          : "")
      : `${guests.length} guests.`;
    const bar = $("memory");
    $("memory").parentElement.hidden = !m.host_mib;
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
  show("tailnet-hint", !plan.tailnet_policy);
}

// Alerts.
function renderAlerts(alerts) {
  $("ntfy-topic").textContent = alerts && alerts.ntfy_url ? alerts.ntfy_url.replace("https://", "") : "";
  $("healthchecks-url").value = alerts ? alerts.healthchecks_url : "";
}

// Agents.
function renderAgent(kind) {
  show("agent-saved", Boolean(kind));
  $("agent-saved-text").textContent = kind ? `${kind === "API key" ? "An" : "A"} ${kind} is saved. Paste a new one to replace it.` : "";
  $("agent-label").textContent = kind ? "Replace it with a new API key or subscription token" : "API key or subscription token";
}

function renderAgentPowers(on) {
  const sw = $("agent-powers");
  sw.setAttribute("aria-checked", on ? "true" : "false");
  $("agent-powers-text").textContent = on ? "On" : "Off";
  show("powers-confirm", false);
}

function renderAgentGithub(login) {
  show("agent-github-saved", Boolean(login));
  $("agent-github-saved-text").textContent = login ? `A token for ${login} is saved; the agent's commits use your GitHub noreply address. Paste a new one to replace it.` : "";
  show("agent-github-remove", Boolean(login));
}

function renderRemoteAgents(names, ready) {
  show("remote-agent-wait", !ready);
  const make1 = $("remote-agent-form").querySelector("button");
  make1.setAttribute("aria-disabled", ready ? "false" : "true");
  make1.dataset.why = ready ? "" : "Read the hardware and save your choices under Setup first.";
  const list = $("remote-agents");
  // Patch in place: an item being revoked keeps its focus.
  const have = new Map([...list.children].map((node) => [node.dataset.name, node]));
  for (const [name, node] of have) if (!names.includes(name)) node.remove();
  for (const name of names) {
    if (have.has(name)) continue;
    const result = make("p", {className: "feedback"});
    result.setAttribute("role", "status");
    const revoke = make("button", {type: "button", className: "btn secondary small", textContent: `Revoke ${name}`});
    revoke.addEventListener("click", () => press(revoke, result, `Revoking ${name}…`, async () => {
      const left = (await api("/api/remote-agents/revoke", {name})).remote_agents;
      show("remote-agent-token-box", false);
      state.remote_agents = left;
      setTimeout(() => renderRemoteAgents(left, ready), 1500);
      return `${name} revoked.`;
    }));
    const item = make("li", {}, icon("key"), make("span", {className: "item-name", textContent: name}), result, revoke);
    item.dataset.name = name;
    list.append(item);
  }
}

// Remote access.
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
  show("lan-banner", lan.state === "closing");
  $("lan-banner-text").textContent = lan.via_vpn
    ? `Press Keep it closed under Remote access within ${lan.minutes} minutes, or it opens again by itself.`
    : `Open this page over Tailscale and press Keep it closed under Remote access, or it opens again by itself within ${lan.minutes} minutes.`;
}

function setBadge(id, text, kind) {
  const node = $(id);
  node.textContent = text || "";
  node.className = `badge ${kind || ""}`.trim();
}

function setNav(view, text, kind) {
  const badge = document.querySelector(`#nav a[data-view="${view}"] .nav-badge`);
  badge.hidden = !text;
  badge.textContent = text || "";
  badge.className = `nav-badge ${kind || ""}`.trim();
}

function ago(seconds) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - seconds));
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} minutes ago`;
  if (s < 129600) return `${Math.round(s / 3600)} hours ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function runningAction() {
  return job && job.view && job.view.state === "running" ? job.view.action : null;
}

// The setup checklist: what is needed, then what is recommended, each with
// where it is done. Kinds: done, attention, running, todo.
function checklist(st) {
  const apply = st.last_jobs.apply;
  const running = runningAction();
  const agents = st.choices.workloads.includes("agents");
  const dirty = choicesDirty();
  const hw = st.hardware;
  const items = [
    ["setup/hardware", "Read the hardware",
      running === "probe" ? ["running", "Reading…"]
        : st.probed ? ["done", hw ? `${hw.cpu || "CPU"}, ${hw.memory_mib ? gib(hw.memory_mib) : ""} memory` : "Done"] : ["todo", "NullBill looks at the CPU, memory, disks and graphics."]],
    ["setup/choices", "Choose what to run",
      dirty ? ["attention", "Changed but not saved."] : st.choices.owner_login ? ["done", "Saved."] : ["todo", "Workloads, disks and your Tailscale login."]],
    ["setup/plan", "Check that the plan fits",
      st.plan.errors.length ? ["attention", "The plan does not fit yet."] : st.plan.text ? ["done", "The plan fits."] : ["todo", "Shows once the hardware is read and the choices saved."]],
  ];
  if (agents) {
    items.push(["agents/agent", "Sign in the coding agent",
      st.agent_credential ? ["done", `Signed in with ${st.agent_credential === "API key" ? "an" : "a"} ${st.agent_credential}.`] : ["attention", "Needs an API key or subscription token."]]);
  }
  items.push(["activity", "Run setup",
    ["apply", "rebake-ci"].includes(running) ? ["running", "Running now."]
      : !apply ? ["todo", "Press Run setup at the top."]
      : apply.state === "done" ? ["done", `Finished ${ago(apply.started)}.`]
      : apply.state === "running" ? ["running", "Running now."] : ["attention", "Stopped. See what to do under Activity."]]);
  const needed = items.length;
  items.push(["alerts", "Get alerts on your phone",
    st.alerts && st.alerts.healthchecks_url ? ["done", "ntfy topic and Healthchecks.io set."] : ["todo", "Subscribe to your ntfy topic; add a Healthchecks.io check."]]);
  const joined = st.lan.via_vpn || st.lan.state === "closed";
  items.push(["access/tailscale", "Reach it over Tailscale",
    joined ? ["done", "This page answers over Tailscale."]
      : st.tailscale_key ? ["attention", "Auth key saved. Run setup so the server joins."]
      : st.tailscale_client ? ["todo", "OAuth client saved. Add an auth key next."] : ["todo", "Add an OAuth client, run setup, then an auth key."]]);
  items.push(["access/lan", "Close Proxmox to the home network",
    st.lan.state === "closed" ? ["done", "Proxmox answers only over Tailscale."]
      : st.lan.state === "closing" ? ["attention", `Press Keep it closed within ${st.lan.minutes} minutes.`] : ["todo", "After Tailscale works."]]);
  return items.map(([href, title, [kind, sub]], i) => ({href, title, kind, sub, recommended: i >= needed}));
}

function renderChecklist(items) {
  const list = $("checklist");
  list.replaceChildren(...items.map((item) => make("li", {className: item.kind},
    make("a", {href: `#${item.href}`},
      make("span", {className: "check-mark", ariaHidden: "true"}, item.kind === "done" ? icon("check") : item.kind === "attention" ? icon("alert") : null),
      make("span", {className: "check-title", textContent: item.title}),
      make("span", {className: "sr-only", textContent: `: ${{done: "done", attention: "needs you", running: "running", todo: "to do"}[item.kind]}. `}),
      make("span", {className: "check-sub", textContent: item.sub}),
      make("span", {className: "check-tag"}, item.recommended && item.kind !== "done" ? make("span", {className: "badge", textContent: "Recommended"}) : null)))));
  const neededLeft = items.filter((i) => !i.recommended && i.kind !== "done").length;
  const done = items.filter((i) => i.kind === "done").length;
  $("checklist-meter").style.width = `${Math.round((100 * done) / items.length)}%`;
  setBadge("checklist-count", `${done} of ${items.length}`, done === items.length ? "good" : "");
  $("checklist-summary").textContent = neededLeft
    ? `${neededLeft} step${neededLeft > 1 ? "s" : ""} left before the server is built.`
    : done === items.length ? "Everything is set up." : "The server is built. The recommended steps make it safer to use from anywhere.";
  // Once built, the services come first on the overview.
  $("checklist-card").style.order = neededLeft ? "0" : "1";
  return neededLeft;
}

// Each section's badge, the nav and the checklist, from state and the job.
function paintStates() {
  if (!state) return;
  const st = state;
  const running = runningAction();
  const items = checklist(st);
  renderChecklist(items);
  const by = Object.fromEntries(items.map((i) => [i.href, i]));
  const KIND = {done: "good", attention: "warn", running: "run", todo: ""};
  const badge = (item, todo) => [item.kind === "done" ? "Done" : item.kind === "attention" ? "Needs you" : item.kind === "running" ? "Running" : todo, KIND[item.kind]];
  setBadge("hardware-state", ...badge(by["setup/hardware"], "To do"));
  setBadge("choices-state", ...(choicesDirty() ? ["Not saved", "warn"] : badge(by["setup/choices"], "To do")));
  setBadge("plan-state", ...(st.plan.errors.length ? ["Does not fit", "bad"] : st.plan.text ? ["Fits", "good"] : ["Waiting", ""]));
  const setupLeft = ["setup/hardware", "setup/choices", "setup/plan"].filter((h) => by[h].kind !== "done").length;
  setNav("setup", setupLeft ? `${setupLeft} left` : "", setupLeft ? "warn" : "");
  const agents = st.choices.workloads.includes("agents");
  show("agents-off", !agents);
  setBadge("agent-state", ...(st.agent_credential ? ["Signed in", "good"] : agents ? ["Needs a key", "warn"] : ["Not needed", ""]));
  setBadge("powers-state", ...(st.agent_powers ? ["Root on", "bad"] : ["Off", ""]));
  setBadge("github-state", ...(st.agent_github ? ["Saved", "good"] : ["Optional", ""]));
  setNav("agents", agents && !st.agent_credential ? "Needs key" : st.agent_powers ? "Root on" : "", agents && !st.agent_credential ? "warn" : "bad");
  setBadge("hc-state", ...(st.alerts && st.alerts.healthchecks_url ? ["Set", "good"] : ["Optional", ""]));
  const ts = by["access/tailscale"];
  setBadge("tailscale-state", ...(ts.kind === "done" ? ["Connected", "good"] : st.tailscale_key ? ["Run setup", "warn"] : st.tailscale_client ? ["Client saved", ""] : ["Optional", ""]));
  $("ts-step-client").classList.toggle("done", st.tailscale_client);
  $("ts-step-key").classList.toggle("done", ts.kind === "done");
  show("tailscale-client-saved", st.tailscale_client);
  show("vpn-saved", st.tailscale_key);
  setNav("access", st.lan.state === "closing" ? "Confirm" : "", "warn");
  setNav("activity", running ? "Running" : st.last_jobs.apply && st.last_jobs.apply.state === "failed" ? "Stopped" : "", running ? "run" : "bad");
  const alerts = lastStatus && lastStatus.monitoring === "ok" ? lastStatus.alerts.length : 0;
  setNav("overview", alerts ? String(alerts) : "", "bad");
  show("pending-dot", Boolean(st.config_changed || st.tailscale_key));
  renderReady();
  paintHealth();
}

// Before Run setup: what is in place, and what is missing.
function renderReady() {
  const items = [];
  const ok = (good, text) => items.push(make("li", {className: good ? "ok" : "warn"}, icon(good ? "check" : "alert"), make("span", {textContent: text})));
  ok(state.probed, state.probed ? "The hardware is read." : "Read the hardware above.");
  if (choicesDirty()) ok(false, "Your choices are not saved.");
  if (state.plan.errors.length) ok(false, "The plan does not fit yet; see Plan above.");
  else if (state.plan.text) ok(true, "The plan fits.");
  else ok(false, "Save your choices to make a plan.");
  if (state.choices.workloads.includes("agents") && !state.agent_credential) ok(false, "No key for the coding agent yet (Agents). Setup still runs, but the agent cannot sign in.");
  if (!state.tailscale_client) ok(false, "No Tailscale OAuth client (Remote access). Setup runs, but the server stays off Tailscale.");
  if (state.config_changed) ok(false, "You changed the choices since the last setup run; run it so the server follows.");
  $("ready-list").replaceChildren(...items);
}

// Overview: health from monitoring, and the facts around it.
function stat(label, value, sub, dot, href) {
  const body = [make("span", {className: "overline", textContent: label}),
    make("span", {className: "stat-value"}, dot ? make("span", {className: `dot ${dot}`, ariaHidden: "true"}) : null, value),
    sub ? make("span", {className: "stat-sub", textContent: sub}) : null];
  return make("li", {className: "stat"}, ...(href ? [make("a", {href, className: "stat-link"}, ...body)] : body));
}

function paintHealth() {
  if (!state) return;
  const s = lastStatus;
  const head = $("health-title");
  const sub = $("health-sub");
  const iconBox = $("health-icon");
  const setIcon = (kind, name) => { iconBox.dataset.state = kind; iconBox.replaceChildren(icon(name)); };
  const alerts = $("health-alerts");
  alerts.replaceChildren();
  if (!s) {
    head.textContent = "Checking…";
    sub.textContent = "";
    setIcon("idle", "circle");
  } else if (s.monitoring === "not planned") {
    head.textContent = "Not built yet";
    sub.textContent = "Server health shows here once setup has built monitoring.";
    setIcon("idle", "circle");
  } else if (s.monitoring !== "ok") {
    head.textContent = "Monitoring does not answer";
    sub.textContent = "If setup is running or just finished, wait a minute.";
    setIcon("warn", "alert");
  } else {
    const n = s.alerts.length;
    if (n) {
      head.textContent = `${n} alert${n > 1 ? "s" : ""} need${n > 1 ? "" : "s"} attention`;
      setIcon(s.alerts.some((a) => a.severity === "page") ? "bad" : "warn", "alert");
    } else if (s.down.length) {
      head.textContent = `${s.down.length} system${s.down.length > 1 ? "s" : ""} not answering`;
      setIcon("warn", "alert");
    } else {
      head.textContent = "All clear";
      setIcon("good", "check");
    }
    sub.textContent = n || s.down.length ? "Your phone gets these as alerts too." : "Every system answers and no alert is firing.";
    alerts.replaceChildren(...s.alerts.map((a) => make("li", {className: a.severity === "page" ? "page" : ""}, icon("alert"), make("span", {textContent: a.summary || a.name}))));
  }
  const stats = [];
  if (s && s.monitoring === "ok") {
    stats.push(stat("Systems answering", `${s.targets - s.down.length} of ${s.targets}`, s.down.length ? `Down: ${s.down.join(", ")}` : "All of them", s.down.length ? "bad" : "good"));
    stats.push(stat("Security updates", s.security_waiting.length ? `Waiting on ${s.security_waiting.length}` : "Up to date",
      s.security_waiting.length ? s.security_waiting.join(", ") : "Installed nightly", s.security_waiting.length ? "warn" : "good"));
    if (s.reboot_waiting.length) stats.push(stat("Reboot needed", `${s.reboot_waiting.length}`, s.reboot_waiting.join(", "), "warn"));
  }
  const apply = state.last_jobs.apply;
  stats.push(stat("Last setup run", !apply ? "Not run yet" : apply.state === "done" ? "Finished" : apply.state === "running" ? "Running" : "Stopped",
    apply ? ago(apply.started) : "Run setup at the top", !apply ? "" : apply.state === "done" ? "good" : apply.state === "running" ? "" : "bad", "#activity"));
  const lan = state.lan.state;
  stats.push(stat("Home network", lan === "closed" ? "Closed" : lan === "closing" ? "Closing" : "Open",
    lan === "closed" ? "Proxmox only over Tailscale" : "Proxmox answers on your LAN", lan === "closed" ? "good" : "warn", "#access/lan"));
  if (state.agent_powers) stats.push(stat("Agent", "Root on Proxmox", "Turn it off under Agents", "bad", "#agents/powers"));
  $("health-stats").replaceChildren(...stats);
}

// Services: what the owner opens, with how to sign in. Whether each one
// answers comes from monitoring, through refreshStatus.
const secrets = {};   // password name -> the value, once fetched

async function secret(name) {
  if (!(name in secrets)) secrets[name] = (await api("/api/password-reveal", {name})).password;
  return secrets[name];
}

async function copyText(text) {
  await navigator.clipboard.writeText(text);
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
  const built = Boolean(st.last_jobs.apply && st.last_jobs.apply.state === "done");
  show("services-empty", services.items.length === 0);
  show("services-unbuilt", services.items.length > 0 && !built);
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
        if (shown) {
          value.textContent = "••••••••••";
          value.className = "masked";
        } else {
          value.textContent = await secret(s.password);
          value.className = "";
        }
        reveal.setAttribute("aria-pressed", shown ? "false" : "true");
        reveal.replaceChildren(icon(shown ? "eye" : "eye-off"));
        reveal.title = shown ? `Show ${word}` : `Hide ${word}`;
        announce(shown ? `${word[0].toUpperCase()}${word.slice(1)} hidden.` : `${word[0].toUpperCase()}${word.slice(1)} shown.`);
        return "";
      }));
      rows.push(kvRow(word === "key" ? "API key" : "Password", value, reveal, copyButton(`Copy ${s.title} ${word}`, () => secret(s.password), feedback)));
    }
    const [dot, label] = serviceState(s.watch);
    const open = s.url ? make("a", {className: "btn primary", href: s.url, target: "_blank", rel: "noopener noreferrer"}, "Open ", make("span", {className: "sr-only", textContent: s.title}), icon("external")) : null;
    const item = make("li", {className: "card service"},
      make("div", {className: "service-head"},
        make("span", {className: "service-icon", ariaHidden: "true"}, icon(SERVICE_ICONS[s.name] || "services")),
        make("div", {className: "service-title"},
          make("h2", {textContent: s.title}),
          make("span", {className: "service-state"}, make("span", {className: `dot ${dot}`, ariaHidden: "true"}), make("span", {className: "state-text", textContent: label || (s.watch ? "No status yet" : "No status")})))),
      make("p", {className: "service-what", textContent: s.what}),
      rows.length ? make("dl", {className: "kv"}, ...rows) : null,
      s.password_note ? make("p", {className: "service-note", textContent: s.password_note}) : null,
      s.how ? make("p", {className: "service-note", textContent: s.how}) : null,
      feedback,
      open ? make("div", {className: "service-foot"}, open) : null);
    item.dataset.watch = s.watch || "";
    return item;
  }));
  $("services-background").textContent = services.background.length
    ? `Also running, with nothing to open: ${services.background.join(", ")}.`
    : "";
  // At a glance, on the overview.
  show("glance-empty", services.items.length === 0);
  $("glance").replaceChildren(...services.items.map((s) => {
    const item = make("li", {},
      make("span", {className: "dot", ariaHidden: "true"}),
      make("span", {className: "glance-name"}, make("b", {textContent: s.title}), make("span", {className: "glance-state state-text"})),
      s.url ? make("a", {className: "btn secondary small", href: s.url, target: "_blank", rel: "noopener noreferrer"}, "Open", make("span", {className: "sr-only", textContent: ` ${s.title}`}), icon("external")) : null);
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
    node.querySelector(".state-text").textContent = label || (watch ? "No status yet" : "No status");
  }
}

// Status, from monitoring.
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
    show("app", false);
    show("gate", true);
    show("login", !st.auth);
    show("password", st.auth && !st.has_password);
    if (!st.auth) {
      $("login-hint").textContent = st.has_password
        ? "Enter your password."
        : "Enter the setup code the installer printed in the Proxmox shell.";
      $("secret").focus();
    } else {
      $("new-password").focus();
    }
    return;
  }
  const first = !state;
  const disksChanged = !state || JSON.stringify(state.disks.map((d) => d.id)) !== JSON.stringify(st.disks.map((d) => d.id));
  state = st;
  show("gate", false);
  show("app", true);
  // Never undo what the owner is changing: unsaved choices stay as they are.
  if (!choicesDirty() || disksChanged) renderChoices(st);
  renderHardware(st.hardware);
  renderServices(st);
  renderPlan(st.plan);
  renderAlerts(st.alerts);
  renderAgent(st.agent_credential);
  renderAgentPowers(st.agent_powers);
  renderAgentGithub(st.agent_github);
  renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
  renderLan(st.lan);
  renderHistory(st.last_jobs);
  onChoicesChanged();
  if (first) route(false);
  clearInterval(statusTimer);
  refreshStatus().catch(() => {});
  statusTimer = setInterval(() => refreshStatus().catch(() => {}), 30000);
  if (st.job && (!job || job.id !== st.job.id)) follow(st.job, st.job.state === "running");
  if (!st.job) paintStrip(null);
  show("job-none", !st.job);
  gateJobButtons();
}

// Jobs.
function duration(s) {
  if (s === null || s === undefined) return "";
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${s % 60} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

function stopFollowing() {
  if (job) clearTimeout(job.timer);
}

function gateJobButtons() {
  const running = Boolean(runningAction());
  for (const button of document.querySelectorAll("button[data-action], .job-retry, #agent-powers")) {
    if (button.classList.contains("busy")) continue;
    button.setAttribute("aria-disabled", running ? "true" : "false");
    button.dataset.why = running ? "Wait until the running job finishes; the bar at the top shows it." : "";
  }
}

const JOB_NAMES = {
  probe: "Reading the hardware", apply: "Setup", verify: "Checks", upgrade: "Update of NullBill",
  "close-lan": "Closing the home network", "keep-lan-closed": "Keeping the home network closed",
  "open-lan": "Reopening the home network", "rebake-ci": "Rebuild of the CI images",
};

function renderHistory(lastJobs) {
  const entries = Object.entries(lastJobs).sort((a, b) => b[1].started - a[1].started);
  show("history-card", entries.length > 0);
  $("history").replaceChildren(...entries.map(([action, j]) => {
    const kind = j.state === "done" ? ["Finished", "good"] : j.state === "running" ? ["Running", "run"] : ["Stopped", "bad"];
    const button = make("button", {type: "button", className: "btn ghost small", textContent: "Show"});
    button.addEventListener("click", async () => {
      try {
        const view = await api(`/api/jobs/${j.id}`);
        follow(view, view.state === "running");
        job.node.querySelector(".job-title").focus();
      } catch (error) { toast(error.message); }
    });
    return make("li", {}, make("span", {className: "item-name", textContent: JOB_NAMES[action] || action}),
      make("span", {className: "muted small", textContent: new Date(j.started * 1000).toLocaleString()}),
      make("span", {className: `badge ${kind[1]}`, textContent: kind[0]}), button);
  }));
}

// Follow a job: draw it under Activity, poll while it runs.
function follow(first, watched, feedback) {
  stopFollowing();
  // The last job's "Finished" beside another button is old news now.
  if (job && job.feedback && job.feedback !== feedback && !job.feedback.classList.contains("error")) say(job.feedback, "", "");
  const node = $("job-template").content.firstElementChild.cloneNode(true);
  $("job-slot").replaceChildren(node);
  show("job-none", false);
  job = {id: first.id, view: first, node, offset: 0, timer: null, watched, misses: 0, feedback, stage: null};
  const again = ["verify", "upgrade"].includes(first.action) ? first.action : "apply";
  const retry = node.querySelector(".job-retry");
  if (again === "upgrade") retry.textContent = "Update again";
  retry.addEventListener("click", () => startJob(again, retry, node.querySelector(".job-copy-result")));
  node.querySelector(".job-copy").addEventListener("click", () => copyDetails(node));
  tick(job);
}

async function tick(current) {
  let view;
  try {
    view = await api(`/api/jobs/${current.id}?offset=${current.offset}`);
    current.misses = 0;
  } catch (error) {
    if (job !== current) return;
    if (error.status === 401) return;
    // The page's server restarts during an update; keep trying.
    current.misses += 1;
    current.node.querySelector(".job-now").textContent = `Lost contact with the server; trying again… (${error.message})`;
    $("jobstrip-sub").textContent = "Lost contact with the server; trying again…";
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
      say(current.feedback, view.state === "done" ? "ok" : "error",
        view.state === "done" ? "Finished." : "Stopped. Activity says why and what to do.");
    }
    // What the job changed shows everywhere on the page.
    refresh().catch((error) => toast(error.message));
    if (view.state === "failed" && currentView === "activity") current.node.querySelector(".job-error-title").focus();
  }
}

function paintStrip(view) {
  const strip = $("jobstrip");
  if (!view) {
    strip.dataset.state = "idle";
    strip.querySelector(".jobstrip-icon").replaceChildren();
    $("jobstrip-title").textContent = "No job has run yet";
    $("jobstrip-sub").textContent = "Run setup builds what you chose under Setup.";
    return;
  }
  const p = view.progress;
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  strip.dataset.state = view.state;
  strip.querySelector(".jobstrip-icon").replaceChildren(...{done: [icon("check")], failed: [icon("x")]}[view.state] || []);
  const name = JOB_NAMES[view.action] || p.title;
  if (view.state === "running") {
    $("jobstrip-title").textContent = `${name} is running`;
    const parts = [];
    if (steps.length) parts.push(`Stage ${Math.min(steps.length, done + 1)} of ${steps.length}`);
    if (now) parts.push(now.text);
    parts.push(duration(view.seconds));
    $("jobstrip-sub").textContent = parts.join(" · ");
    strip.querySelector(".fill").style.width = steps.length ? `${Math.round((100 * done) / steps.length)}%` : "30%";
  } else if (view.state === "done") {
    $("jobstrip-title").textContent = `${name} finished`;
    $("jobstrip-sub").textContent = `${ago(view.started + view.seconds)}, took ${duration(view.seconds)}`;
  } else {
    $("jobstrip-title").textContent = `${name} stopped${p.error && p.error.stage_text ? ` at "${p.error.stage_text}"` : ""}`;
    $("jobstrip-sub").textContent = `${ago(view.started + view.seconds)}. Open Activity for what to do.`;
  }
}

function drawJob(current, view) {
  const node = current.node;
  const p = view.progress;
  node.dataset.state = view.state;
  node.querySelector(".job-icon").replaceChildren(...{done: [icon("check")], failed: [icon("x")]}[view.state] || []);
  paintStrip(view);
  node.querySelector(".job-title").textContent = p.title;
  setBadgeNode(node.querySelector(".job-badge"), ...{running: ["Running", "run"], done: ["Finished", "good"], failed: ["Stopped", "bad"]}[view.state]);
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  const sub = [];
  if (steps.length) sub.push(`Stage ${Math.min(steps.length, done + (now ? 1 : 0))} of ${steps.length}`);
  if (view.state === "running") sub.push(`running for ${duration(view.seconds)}`);
  else sub.push(`took ${duration(view.seconds)}`, `started ${new Date(view.started * 1000).toLocaleString()}`);
  node.querySelector(".job-sub").textContent = sub.join(" · ");
  // Announce a new stage, never each second's activity.
  if (view.state === "running" && now && current.stage !== now.name) {
    if (current.stage !== null) announce(`Stage ${done + 1} of ${steps.length}: ${now.text}.`);
    current.stage = now.name;
  }
  // The bar.
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
  // What runs now.
  const nowLine = node.querySelector(".job-now");
  if (view.state === "running") {
    const parts = [];
    if (now) parts.push(now.text);
    if (p.activity) parts.push(p.activity);
    let text = parts.length ? parts.join(": ") : "Starting…";
    if (p.quiet_s !== null && p.quiet_s >= 20) {
      text += ` (no new output for ${duration(p.quiet_s)}; downloads and image builds can be quiet for several minutes)`;
    }
    nowLine.textContent = text;
  } else {
    nowLine.textContent = "";
  }
  // The stages, folded away once all are done.
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
  // Why it stopped.
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
    retry.hidden = !["apply", "verify", "rebake-ci", "upgrade"].includes(view.action);
    retry.textContent = view.action === "verify" ? "Run the checks again" : view.action === "upgrade" ? "Update again" : "Run setup again";
  }
  // Finished.
  const ok = node.querySelector(".job-ok");
  ok.hidden = view.state !== "done";
  const next = node.querySelector(".job-ok-next");
  next.replaceChildren();
  if (view.state === "done") {
    const text = {
      probe: ["The hardware is read", "Choose what to run under Setup."],
      apply: ["Setup finished", "Everything in the plan is in place. The checks ran at the end; their results are below."],
      verify: ["The checks finished", "No check failed."],
      upgrade: ["NullBill is updated", "Run setup now, so the server follows the new version."],
      "close-lan": ["The home network is closing", "Open this page over Tailscale and press Keep it closed under Remote access."],
      "keep-lan-closed": ["The home network stays closed", "Proxmox now answers only over Tailscale."],
      "open-lan": ["The home network is open again", "Proxmox answers on your home network."],
      "rebake-ci": ["The CI images are rebuilt", ""],
    }[view.action] || ["Finished", ""];
    node.querySelector(".job-ok-title").textContent = text[0];
    node.querySelector(".job-ok-text").textContent = text[1];
    if (view.action === "probe") next.append(make("a", {className: "btn secondary small", href: "#setup/choices", textContent: "Choose what to run"}));
    if (view.action === "apply") next.append(make("a", {className: "btn secondary small", href: "#services", textContent: "Open your services"}));
  }
  // Checks.
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
  // The log, kept at its end unless the owner scrolled up.
  const pre = node.querySelector(".job-log pre");
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent += view.text;
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

function setBadgeNode(node, text, kind) {
  node.textContent = text;
  node.className = `badge job-badge ${kind || ""}`.trim();
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

const STARTING = {
  probe: "Starting…", apply: "Starting setup…", verify: "Starting the checks…", upgrade: "Starting the update…",
  "close-lan": "Closing…", "keep-lan-closed": "Keeping it closed…", "open-lan": "Opening…",
};

async function startJob(action, button, feedback) {
  await press(button, feedback, STARTING[action] || "Starting…", async () => {
    const view = await api("/api/jobs", {action});
    follow(view, true, feedback);
    if (OPENS_ACTIVITY.has(action)) go("activity");
    return OPENS_ACTIVITY.has(action) ? "Started." : "Started. The bar at the top shows its progress.";
  });
}

function feedbackFor(button) {
  if (button.closest(".actionbar")) return $("actions-result");
  return button.closest(".row").querySelector(".feedback");
}

function submitter(event) {
  return event.submitter || event.target.querySelector("button[type=submit]");
}

document.addEventListener("DOMContentLoaded", () => {
  $("toast-close").addEventListener("click", () => toast(""));
  window.addEventListener("hashchange", () => route(true));
  for (const input of document.querySelectorAll("input")) {
    input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
  }

  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("secret");
    press(submitter(event), "login-result", "Signing in…", async () => {
      if (!field.value.trim()) throw new ApiError("Enter the setup code or your password.", 400);
      await api("/api/login", {secret: field.value});
      field.value = "";
      say("login-result", "", "");
      await refresh();
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
      const fits = !result.plan.errors.length;
      return fits ? "Saved. The plan fits; see Plan below." : "Saved, but the plan does not fit yet; Plan below says what to change.";
    }, field);
  });

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

  $("agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("agent-credential");
    press(submitter(event), "agent-result", "Checking and saving…", async () => {
      if (!field.value.trim()) throw new ApiError("Paste the API key or token into the field first.", 400);
      const result = await api("/api/agent", {credential: field.value});
      field.value = "";
      state.agent_credential = result.agent_credential;
      renderAgent(result.agent_credential);
      paintStates();
      return `Saved. ${result.agent_credential === "API key" ? "An" : "A"} ${result.agent_credential} is kept encrypted on the server.`;
    }, field);
  });

  const powers = (on) => press($("agent-powers"), "agent-powers-result", on ? "Giving the agent root on Proxmox…" : "Taking the agent's key off Proxmox…", async () => {
    try {
      state.agent_powers = (await api("/api/agent-powers", {enabled: on})).agent_powers;
    } finally {
      if (!on) state.agent_powers = false;  // off even when the key could not be taken off
      renderAgentPowers(state.agent_powers);
      paintStates();
    }
    return on ? "On. The agent can now manage the server." : "Off. The agent's key is off Proxmox.";
  });
  $("agent-powers").addEventListener("click", () => {
    const sw = $("agent-powers");
    if (sw.getAttribute("aria-disabled") === "true") return say("agent-powers-result", "info", sw.dataset.why);
    if (state.agent_powers) return powers(false);
    show("powers-confirm", true);
    $("powers-yes").focus();
  });
  $("powers-yes").addEventListener("click", () => { show("powers-confirm", false); $("agent-powers").focus(); powers(true); });
  $("powers-no").addEventListener("click", () => {
    show("powers-confirm", false);
    $("agent-powers").focus();
    say("agent-powers-result", "", "");
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
      paintStates();
      return `Saved: GitHub knows it as ${result.agent_github}. Run setup so the agent gets it.`;
    }, field);
  });
  $("agent-github-remove").addEventListener("click", () => press($("agent-github-remove"), "agent-github-result", "Removing…", async () => {
    state.agent_github = (await api("/api/agent-github", {token: ""})).agent_github;
    renderAgentGithub(state.agent_github);
    paintStates();
    $("agent-github-token").focus();
    return "Removed. Run setup so the agent's copy goes too; revoke it on GitHub as well.";
  }));
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
      return "Saved. Press Run setup at the top so the server joins.";
    }, field);
  });
  $("remote-agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("remote-agent-name");
    press(submitter(event), "remote-agent-result", "Making a token…", async () => {
      const made = await api("/api/remote-agents", {name: field.value.trim()});
      field.value = "";
      $("remote-agent-token").textContent = [
        `NB_PAGE_URL=${made.url}`,
        `NB_PAGE_SPKI=${made.spki}`,
        `NB_PAGE_TOKEN=${made.token}`,
      ].join("\n");
      show("remote-agent-token-box", true);
      const st = await api("/api/state");
      renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
      return `Made a token for ${made.name}. Copy it now; the page shows it only once.`;
    }, field);
  });
  $("remote-agent-copy").addEventListener("click", () => press($("remote-agent-copy"), "remote-agent-result", "Copying…", async () => {
    try { await copyText($("remote-agent-token").textContent); } catch {
      throw new ApiError("Copying is blocked here; select the text and copy it by hand.", 400);
    }
    return "Copied.";
  }));

  for (const button of document.querySelectorAll("button[data-action]")) {
    button.addEventListener("click", () => startJob(button.dataset.action, button, feedbackFor(button)));
  }
  $("logout").addEventListener("click", async () => {
    try { await api("/api/logout", {}); } catch (error) { toast(error.message); }
    state = null;
    currentView = null;
    stopFollowing();
    job = null;
    await refresh().catch((error) => toast(error.message));
  });
  refresh().catch((error) => {
    show("loading", false);
    toast(error.message);
  });
});
