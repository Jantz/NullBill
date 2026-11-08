"use strict";

// The setup page. Every control says what it is doing next to itself:
// busy while the server works, then saved or the reason it was refused.
// Long work (probe, setup, checks) shows its stages as they run.

const LABELS = {
  "agents": ["AI agents", "The operator and a workspace container for coding agents."],
  "linux-ci": ["Linux CI runners", "Run Forgejo Actions jobs in throwaway Linux VMs."],
  "inference": ["Local AI models on the GPU", "Needs more memory."],
  "media": ["Media server (Jellyfin)", "Plays the films, series and music on the disks you keep below, on your devices over Tailscale."],
  "windows-ci": ["Windows CI runners", "Needs a Windows ISO and more memory."],
  "windows-dev": ["A Windows desktop VM", "Upload the Windows 11 ISO in Proxmox first; you install Windows in it."],
};

// Which step's card shows each kind of job.
const JOB_STEP = {"probe": 2, "apply": 8, "verify": 8, "rebake-ci": 8, "close-lan": 9, "keep-lan-closed": 9, "open-lan": 9};

const $ = (id) => document.getElementById(id);
const make = (tag, props, ...children) => {
  const node = Object.assign(document.createElement(tag), props || {});
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
};
const li = (text, cls) => make("li", {textContent: text, className: cls || ""});

let state = null;          // the last /api/state
let savedChoices = null;   // the choices form as last saved, to tell unsaved changes
let statusTimer = null;
let job = null;            // the followed job: {id, view, node, offset, timer, watched}

// A server message as a sentence: capital letter, full stop.
function sentence(text) {
  const t = String(text || "").trim();
  if (!t) return t;
  // A word, not a file or a name: router.yml stays as it is.
  const s = /^[a-z]+[\s,:;]/.test(t) ? t[0].toUpperCase() + t.slice(1) : t;
  return /[.!?]$/.test(s) ? s : s + ".";
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
  const conn = $("conn");
  conn.hidden = ok;
  conn.textContent = ok ? "" : "Not connected";
}

function toast(text) {
  $("toast-text").textContent = text;
  $("toast").hidden = !text;
}

// The line next to a control: busy, ok, error or info.
function say(id, kind, text) {
  const node = typeof id === "string" ? $(id) : id;
  node.className = `feedback ${kind || ""}`;
  node.replaceChildren();
  if (!text) return;
  const icon = {busy: make("span", {className: "spin", ariaHidden: "true"}), ok: "✓ ", error: "✕ "}[kind];
  node.append(...[icon, text].filter(Boolean));
}

// Run one press: the button waits, the line beside it says what happens.
// work() returns the success text. On a refusal the field it names, if any,
// is marked.
async function press(button, feedback, busyText, work, field) {
  if (button.disabled) return;
  button.disabled = true;
  button.classList.add("busy");
  say(feedback, "busy", busyText);
  if (field) field.removeAttribute("aria-invalid");
  try {
    const text = await work();
    say(feedback, "ok", text);
  } catch (error) {
    say(feedback, "error", error.message);
    if (field && error.status !== 0 && error.status !== 401) {
      field.setAttribute("aria-invalid", "true");
      field.focus();
    }
  } finally {
    button.classList.remove("busy");
    button.disabled = false;
    gateJobButtons();
  }
}

function show(id, visible) { $(id).hidden = !visible; }

function signedOut() {
  clearInterval(statusTimer);
  stopFollowing();
  // Signed in again, the page follows the latest job afresh.
  job = null;
  state = null;
  show("setup", false);
  show("password", false);
  show("logout", false);
  show("jobbar", false);
  show("login", true);
  $("login-hint").textContent = "You were signed out, for example because NullBill was updated. Sign in again; nothing you saved is lost.";
}

// Workloads, extras and disks.
function renderWorkloads(st) {
  const box = $("workloads");
  box.replaceChildren(make("legend", {textContent: "Workloads"}));
  for (const name of st.workloads) {
    const [title, help] = LABELS[name] || [name, ""];
    const input = make("input", {type: "checkbox", value: name, checked: st.choices.workloads.includes(name)});
    box.append(make("label", {className: "check"}, input, make("span", {}, title, " ", make("small", {textContent: help}))));
  }
}

const CHOICES = {
  alone: "Leave it alone",
  keep: "Keep its files and share them to me",
  copy: "Copy its files onto the server, check the copy, then erase the disk",
  erase: "Erase everything on it",
};

// What each choice says for this disk, and why it cannot be picked, if so.
function diskChoice(disk, value, keepable) {
  if (value === "keep" && !keepable && disk.choice !== "keep") return [CHOICES.keep, "no NTFS, exFAT or FAT partition on it"];
  if (value === "copy") {
    const size = disk.used_gib === null ? "" : ` (about ${disk.used_gib} GiB, shared to you like now, and backed up)`;
    if (disk.choice === "copy") return [CHOICES.copy + size, ""];
    if (!disk.copy_to) return [CHOICES.copy, "the server's storage is not LVM; see docs/runbooks/legacy-disk.md"];
    if (disk.used_gib === null) return [CHOICES.copy, "how much it holds is unknown; probe the server again"];
    return [CHOICES.copy + size, ""];
  }
  if (value === "erase" && disk.kept) return ["Erase everything on it without a copy (I have its files elsewhere)", ""];
  return [CHOICES[value], ""];
}

function gib(mib) { return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${mib} MiB`; }

function renderDisks(disks) {
  const list = $("disk-list");
  list.replaceChildren();
  show("disks", disks.length > 0);
  for (const disk of disks) {
    const box = make("div", {className: "disk"});
    box.dataset.id = disk.id;
    const hours = disk.power_on_hours ? `, ${disk.power_on_hours} hours in use` : "";
    box.append(make("p", {}, make("b", {textContent: disk.id}),
      ` ${disk.size_gib} GiB ${disk.rotational ? "hard disk" : "SSD"}, health ${disk.health}${hours}`));
    if (disk.health === "FAILED") {
      box.append(make("p", {className: "note error", textContent: "This disk reports that it is failing. Copy what you need off it soon."}));
    }
    const parts = make("ul");
    for (const p of disk.partitions) {
      const label = p.label ? ` "${p.label}"` : "";
      parts.append(li(`${p.what}${label}, ${gib(p.size_mib)}${p.keepable ? "" : ", left alone"}`));
    }
    if (!disk.partitions.length) parts.append(li("no partitions"));
    box.append(parts);
    const keepable = disk.partitions.some((p) => p.keepable);
    for (const value of Object.keys(CHOICES)) {
      if (value === "copy" && !disk.kept && disk.choice !== "copy") continue;
      const [text, why] = diskChoice(disk, value, keepable);
      const input = make("input", {type: "radio", name: `disk-${disk.id}`, value, checked: disk.choice === value, disabled: Boolean(why)});
      box.append(make("label", {className: "check"}, input, make("span", {}, text, why ? make("small", {textContent: ` Not possible: ${why}.`}) : null)));
    }
    const rwInput = make("input", {type: "checkbox", checked: disk.rw});
    const rw = make("label", {className: "check disk-rw"}, rwInput,
      make("span", {textContent: "Let me change files on it (NTFS only). Shut Windows down fully on that disk first, with shutdown /s /t 0, or it stays read-only."}));
    // What an erased disk becomes.
    const uses = make("div", {className: "disk-for"});
    for (const [value, text] of [["backups", "then use it for the nightly backups"],
      ["storage", "then use it as extra storage in Proxmox; a copy made first moves onto it, checked again"]]) {
      const input = make("input", {type: "radio", name: `disk-for-${disk.id}`, value, checked: disk.for === value});
      uses.append(make("label", {className: "check"}, input, make("span", {textContent: text})));
    }
    box.append(uses);
    const confirm = make("label", {className: "disk-confirm"}, `Type ${disk.confirm}, the end of its name, to erase everything on it`,
      make("input", {autocomplete: "off"}));
    box.append(rw, confirm);
    const sync = () => {
      const chosen = box.querySelector("input[type=radio]:checked");
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
    const input = make("input", {type: "number", min: "1", max: "256", step: "1",
      value: gpu.vram_mib ? Math.round(gpu.vram_mib / 1024) : ""});
    input.dataset.pci = gpu.pci;
    list.append(make("label", {}, `Memory of the ${gpu.name}, in GB`, input));
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
    const chosen = box.querySelector("input[type=radio]:checked");
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
    workloads: [...$("workloads").querySelectorAll("input:checked")].map((i) => i.value),
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
  steps();
}

function renderGateway(gateway) {
  show("gateway-info", Boolean(gateway));
  if (!gateway) return;
  $("gateway-model").textContent = gateway.model;
  $("gateway-url").textContent = gateway.url;
  $("gateway-hosts").textContent = `${gateway.ip} ${gateway.name}`;
  show("gateway-reveal", gateway.key_set);
  $("gateway-key").textContent = gateway.key_set ? "" : "Setup makes the key on its next run.";
}

function renderFiles(files) {
  show("files-info", Boolean(files));
  if (!files) return;
  $("files-user").textContent = files.user;
  $("files-shares").replaceChildren(...files.shares.map((share) =>
    li(`\\\\${files.ip}\\${share}   or   smb://${files.ip}/${share}`)));
  show("files-reveal", files.password_set);
  $("files-password").textContent = files.password_set ? "" : "Setup makes the password on its next run.";
}

function renderHardware(hw) {
  const list = $("hardware");
  list.hidden = !hw;
  if (!hw) return;
  const items = [];
  if (hw.cpu || hw.threads) items.push(`${hw.cpu || "CPU"}, ${hw.threads} threads`);
  if (hw.memory_mib) items.push(`${gib(hw.memory_mib)} memory`);
  items.push(hw.gpus.length ? `GPU: ${hw.gpus.join(", ")}` : "No separate GPU");
  items.push(`${hw.disks} disk${hw.disks === 1 ? "" : "s"}`);
  if (hw.pve_version) items.push(`Proxmox VE ${hw.pve_version}`);
  list.replaceChildren(...items.map((t) => li(t)));
}

const KINDS = {"lxc": "container", "vm": "VM", "vm-pool": "VM pool"};

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
    bar.hidden = !m.host_mib;
    if (m.host_mib) {
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
      make("td", {textContent: g.cores ?? ""}),
      make("td", {textContent: g.memory_mib ? gib(g.memory_mib) + (g.count ? " each" : "") : ""}))));
  }
  $("tailnet-policy").textContent = plan.tailnet_policy || "";
  show("tailnet-details", Boolean(plan.tailnet_policy));
  show("tailnet-hint", !plan.tailnet_policy);
}

function renderAlerts(alerts) {
  $("ntfy-topic").textContent = alerts && alerts.ntfy_url ? alerts.ntfy_url.replace("https://", "") : "";
  $("healthchecks-url").value = alerts ? alerts.healthchecks_url : "";
}

function renderAgent(kind) {
  const saved = $("agent-saved");
  saved.hidden = !kind;
  saved.textContent = kind ? `✓ ${kind === "API key" ? "An" : "A"} ${kind} is saved. Paste a new one here to replace it.` : "";
  $("agent-label").textContent = kind ? "Replace it with a new API key or subscription token" : "API key or subscription token";
}

function renderTailscaleClient(saved) { show("tailscale-client-saved", saved); }

function renderVpn(waiting) {
  const note = $("vpn-saved");
  note.hidden = !waiting;
  note.className = "note ok";
  note.textContent = waiting ? "✓ A key is saved; the next setup run uses it once to join, then forgets it." : "";
}

function renderRemoteAgents(names, ready) {
  show("remote-agent-wait", !ready);
  $("remote-agent-form").querySelector("button").disabled = !ready;
  $("remote-agents").replaceChildren(...names.map((name) => {
    const result = make("span", {className: "feedback"});
    const revoke = make("button", {type: "button", className: "link", textContent: "Revoke"});
    revoke.addEventListener("click", () => press(revoke, result, `Revoking ${name}…`, async () => {
      const left = (await api("/api/remote-agents/revoke", {name})).remote_agents;
      $("remote-agent-token").hidden = true;
      state.remote_agents = left;
      setTimeout(() => renderRemoteAgents(left, ready), 1500);
      return `${name} revoked.`;
    }));
    return make("li", {}, make("b", {textContent: name}), " ", revoke, " ", result);
  }));
}

const LAN = {
  open: "Open: Proxmox answers on your home network.",
  closing: "Closing: press Keep it closed on this page opened over Tailscale, or it opens again by itself.",
  closed: "Closed: Proxmox answers only over Tailscale.",
};

function renderLan(lan) {
  $("lan-status").textContent = LAN[lan.state] + (lan.state === "closing" && !lan.via_vpn
    ? " This page is not open over Tailscale right now." : "");
  $("lan-status").className = `note ${lan.state === "closing" ? "warn" : lan.state === "closed" ? "ok" : ""}`;
  $("lan-minutes").textContent = lan.minutes;
  show("lan-close", lan.state === "open");
  show("lan-keep", lan.state === "closing");
  show("lan-open", lan.state !== "open");
}

// Each step's state, for its badge, its pill and the overview.
function stepStates(st) {
  const lastApply = st.last_jobs.apply;
  const running = job && job.view && job.view.state === "running" ? job.view.action : null;
  const agents = st.choices.workloads.includes("agents");
  const dirty = choicesDirty();
  const out = {
    1: ["done", "Done"],
    2: running === "probe" ? ["running", "Running"] : st.probed ? ["done", "Done"] : ["todo", "To do"],
    3: dirty ? ["attention", "Not saved"] : st.choices.owner_login ? ["done", "Saved"] : ["todo", "To do"],
    4: st.plan.errors.length ? ["attention", "Needs changes"] : st.plan.text ? ["done", "Fits"] : ["todo", "Waiting for 2 and 3"],
    5: st.alerts && st.alerts.healthchecks_url ? ["done", "Set"] : ["optional", "Recommended"],
    6: !agents ? ["na", "Not needed"] : st.agent_credential ? ["done", "Saved"] : ["attention", "Needs a key"],
    7: st.tailscale_key ? ["attention", "Run setup"] : st.tailscale_client ? ["done", "Client saved"] : ["optional", "Optional"],
    8: ["apply", "verify", "rebake-ci"].includes(running) ? ["running", "Running"]
      : !lastApply ? ["todo", "Not run yet"] : lastApply.state === "done" ? ["done", "Finished"]
      : lastApply.state === "running" ? ["running", "Running"] : ["attention", "Stopped"],
    9: JOB_STEP[running] === 9 ? ["running", "Running"]
      : st.lan.state === "closed" ? ["done", "Closed"] : st.lan.state === "closing" ? ["attention", "Confirm"] : ["optional", "Open"],
  };
  return out;
}

function steps() {
  if (!state) return;
  const states = stepStates(state);
  const list = $("overview-list");
  list.replaceChildren();
  let done = 0;
  let next = null;
  for (const section of document.querySelectorAll("section.step")) {
    const n = Number(section.dataset.step);
    const [kind, text] = states[n];
    section.dataset.state = kind;
    section.querySelector(".pill").textContent = text;
    section.querySelector(".badge").textContent = kind === "done" ? "✓" : String(n);
    if (kind === "done") done += 1;
    if (!next && ["todo", "attention"].includes(kind)) next = n;
    const link = make("a", {href: `#step-${n}`},
      make("span", {className: "badge", textContent: kind === "done" ? "✓" : String(n)}),
      make("span", {className: "o-title", textContent: section.dataset.title}),
      make("span", {className: "o-state", textContent: text}));
    list.append(make("li", {className: kind}, link));
  }
  const running = Object.entries(states).find(([, [kind]]) => kind === "running");
  $("overview-summary").textContent = running
    ? `Working on step ${running[0]}; it shows each stage as it runs.`
    : next ? `Next: step ${next}, ${document.querySelector(`#step-${next}`).dataset.title.toLowerCase()}.`
      : "Every step that is needed is done. The optional ones are up to you.";
  renderReady(states);
}

// Before Run setup: what is in place, and what is missing.
function renderReady(states) {
  const items = [];
  const ok = (good, text) => items.push(make("li", {className: good ? "ok" : "warn", textContent: (good ? "✓ " : "! ") + text}));
  ok(state.probed, state.probed ? "The hardware is probed." : "Probe the server in step 2.");
  if (choicesDirty()) ok(false, "Your choices in step 3 are not saved.");
  if (state.plan.errors.length) ok(false, "The plan does not fit yet; see step 4.");
  else if (state.plan.text) ok(true, "The plan fits.");
  else ok(false, "Save your choices in step 3 to make a plan.");
  if (states[6][0] === "attention") ok(false, "No key for the coding agent yet (step 6). Setup still runs, but the agent cannot sign in.");
  if (!state.tailscale_client) ok(false, "No Tailscale OAuth client (step 7). Setup runs, but the server stays off Tailscale.");
  $("ready-list").replaceChildren(...items);
}

// Services: what the owner opens, with how to sign in. Whether each one
// answers comes from monitoring, through refreshStatus.
let lastStatus = null;

function revealButton(name) {
  const button = make("button", {type: "button", className: "secondary", textContent: "Show password"});
  const feedback = make("p", {className: "feedback"});
  feedback.setAttribute("role", "status");
  feedback.setAttribute("aria-live", "polite");
  const secret = make("code", {className: "secret"});
  button.addEventListener("click", () => press(button, feedback, "Fetching…", async () => {
    secret.textContent = (await api("/api/password-reveal", {name})).password;
    return "";
  }));
  return [make("div", {className: "actions"}, button, feedback), secret];
}

function renderServices(st) {
  const services = st.services || {items: []};
  show("services", Boolean(st.last_jobs.apply) && services.items.length > 0);
  $("service-list").replaceChildren(...services.items.map((s) => {
    const signIn = s.user ? make("p", {}, "Sign in as ", make("code", {textContent: s.user})) : null;
    const open = s.url ? make("a", {className: "button", href: s.url, target: "_blank", rel: "noopener noreferrer", textContent: "Open"}) : null;
    const item = make("li", {className: "service"},
      make("div", {className: "service-head"},
        make("span", {className: "dot idle", ariaHidden: "true"}),
        make("h3", {textContent: s.title}),
        make("span", {className: "service-state"}),
        open),
      make("p", {textContent: s.what}),
      s.url ? make("p", {}, make("code", {textContent: s.url})) : null,
      ...(s.addresses || []).map((a) => make("p", {}, make("code", {textContent: a}))),
      signIn,
      ...(s.password ? revealButton(s.password) : []),
      s.password_note ? make("p", {className: "hint", textContent: s.password_note}) : null,
      s.how ? make("p", {className: "hint", textContent: s.how}) : null);
    item.dataset.watch = s.watch || "";
    return item;
  }));
  $("services-background").textContent = services.background.length
    ? `Also running, with nothing to open: ${services.background.join(", ")}.`
    : "";
  paintServices();
}

function paintServices() {
  for (const node of document.querySelectorAll("#service-list .service")) {
    const watch = node.dataset.watch;
    const dot = node.querySelector(".dot");
    const label = node.querySelector(".service-state");
    const ok = lastStatus && lastStatus.monitoring === "ok";
    if (!watch || !ok) {
      dot.className = "dot idle";
      label.textContent = "";
    } else if (lastStatus.answering.includes(watch)) {
      dot.className = "dot good";
      label.textContent = "running";
    } else if (lastStatus.down.includes(watch)) {
      dot.className = "dot bad";
      label.textContent = "not answering";
    } else {
      dot.className = "dot idle";
      label.textContent = "";
    }
  }
}

// Status, from monitoring.
async function refreshStatus() {
  const s = await api("/api/status");
  lastStatus = s;
  paintServices();
  const alerts = $("status-alerts");
  const facts = $("status-facts");
  const dot = $("status-dot");
  alerts.replaceChildren();
  facts.replaceChildren();
  if (s.monitoring === "not planned") {
    dot.className = "dot idle";
    $("status-summary").textContent = "Monitoring starts once the server is built (step 8).";
    return;
  }
  if (s.monitoring !== "ok") {
    dot.className = "dot warn";
    $("status-summary").textContent = "Cannot reach monitoring yet. If setup is running or just finished, wait a minute.";
    return;
  }
  dot.className = s.alerts.length ? "dot bad" : "dot good";
  $("status-summary").textContent = s.alerts.length
    ? `${s.alerts.length} alert${s.alerts.length > 1 ? "s" : ""} need attention.`
    : "All clear.";
  alerts.replaceChildren(...s.alerts.map((a) => li(a.summary || a.name, a.severity === "page" ? "page" : "warn")));
  facts.append(li(`${s.targets - s.down.length} of ${s.targets} systems answering` +
    (s.down.length ? `; down: ${s.down.join(", ")}` : "")));
  if (s.security_waiting.length) facts.append(li(`Security updates waiting on ${s.security_waiting.join(", ")}`));
  if (s.reboot_waiting.length) facts.append(li(`Reboot needed to finish updates: ${s.reboot_waiting.join(", ")}`));
}

async function refresh() {
  const st = await api("/api/state");
  show("loading", false);
  show("logout", st.auth);
  show("login", !st.auth);
  if (!st.auth) {
    clearInterval(statusTimer);
    $("login-hint").textContent = st.has_password
      ? "Enter your password."
      : "Enter the setup code the installer printed in the Proxmox shell.";
    show("password", false);
    show("setup", false);
    $("secret").focus();
    return;
  }
  show("password", !st.has_password);
  show("setup", st.has_password);
  if (!st.has_password) {
    $("new-password").focus();
    return;
  }
  const disksChanged = !state || JSON.stringify(state.disks.map((d) => d.id)) !== JSON.stringify(st.disks.map((d) => d.id));
  state = st;
  // Never undo what the owner is changing: unsaved choices stay as they are.
  if (!choicesDirty() || disksChanged) renderChoices(st);
  renderHardware(st.hardware);
  renderFiles(st.files);
  renderGateway(st.gateway);
  renderServices(st);
  renderPlan(st.plan);
  renderAlerts(st.alerts);
  renderAgent(st.agent_credential);
  renderTailscaleClient(st.tailscale_client);
  renderVpn(st.tailscale_key);
  renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
  renderLan(st.lan);
  onChoicesChanged();
  clearInterval(statusTimer);
  refreshStatus().catch(() => {});
  statusTimer = setInterval(() => refreshStatus().catch(() => {}), 30000);
  if (st.job && (!job || job.id !== st.job.id)) follow(st.job, st.job.state === "running");
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
  const running = job && job.view && job.view.state === "running";
  for (const button of document.querySelectorAll("button[data-action], .job-retry")) {
    if (button.classList.contains("busy")) continue;
    button.disabled = Boolean(running);
    button.title = running ? "Wait until the running job finishes." : "";
  }
}

function slotFor(action) {
  return document.querySelector(`#step-${JOB_STEP[action] || 8} .job-slot`);
}

// Follow a job: draw it in its step, poll while it runs.
function follow(first, watched) {
  stopFollowing();
  document.querySelectorAll(".job-slot").forEach((slot) => slot.replaceChildren());
  const node = $("job-template").content.firstElementChild.cloneNode(true);
  slotFor(first.action).append(node);
  job = {id: first.id, view: first, node, offset: 0, timer: null, watched, misses: 0};
  node.querySelector(".job-retry").addEventListener("click", () => startJob(first.action === "verify" ? "verify" : "apply", node.querySelector(".job-copy-result")));
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
    current.timer = setTimeout(() => tick(current), Math.min(10000, 1000 * current.misses));
    return;
  }
  if (job !== current) return;
  current.offset = view.offset;
  current.view = view;
  drawJob(current, view);
  gateJobButtons();
  steps();
  if (view.state === "running") {
    current.timer = setTimeout(() => tick(current), 1000);
  } else if (current.watched) {
    current.watched = false;
    // What the job changed shows everywhere on the page.
    refresh().catch((error) => toast(error.message));
    if (view.state === "failed") current.node.focus({preventScroll: false});
  }
}

const ICON = {done: "✓", failed: "✕", running: "", waiting: ""};

function drawJob(current, view) {
  const node = current.node;
  const p = view.progress;
  node.dataset.state = view.state;
  const verbs = {running: "running", done: "finished", failed: "stopped"};
  node.querySelector(".job-title").textContent = `${p.title}: ${verbs[view.state] || view.state}`;
  const steps = p.steps || [];
  const done = steps.filter((s) => s.status === "done").length;
  const now = steps.find((s) => s.status === "running" || s.status === "failed");
  const sub = [];
  if (view.state === "running") sub.push(`Running for ${duration(view.seconds)}`);
  else sub.push(`Took ${duration(view.seconds)}`, `started ${new Date(view.started * 1000).toLocaleString()}`);
  if (steps.length) sub.unshift(`Stage ${Math.min(steps.length, done + (now ? 1 : 0))} of ${steps.length}`);
  node.querySelector(".job-sub").textContent = sub.join(" · ");
  // The bar.
  const bar = node.querySelector(".bar");
  bar.hidden = view.state === "done" && !steps.length;
  const fraction = steps.length ? done / steps.length : view.state === "running" ? null : 1;
  bar.classList.toggle("indeterminate", fraction === null);
  bar.querySelector(".fill").style.width = fraction === null ? "" : `${Math.round(100 * fraction)}%`;
  if (fraction !== null) bar.setAttribute("aria-valuenow", String(Math.round(100 * fraction)));
  bar.setAttribute("aria-valuemax", "100");
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
    nowLine.hidden = false;
  } else {
    nowLine.hidden = true;
  }
  // The stages, folded away once all are done.
  const fold = node.querySelector(".job-stages");
  fold.hidden = !steps.length;
  if (view.state === "done" && !fold.dataset.folded) {
    fold.open = false;
    fold.dataset.folded = "1";
  }
  fold.querySelector("summary").textContent = view.state === "done" ? `All ${steps.length} stages` : "Stages";
  node.querySelector(".stages").replaceChildren(...steps.map((s) => make("li", {className: s.status},
    make("span", {className: "s-icon", textContent: ICON[s.status]}),
    make("span", {className: "s-text", textContent: s.text}),
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
    retry.hidden = !["apply", "verify", "rebake-ci"].includes(view.action);
    retry.textContent = view.action === "verify" ? "Run the checks again" : "Run setup again";
  }
  // Finished.
  const ok = node.querySelector(".job-ok");
  ok.hidden = view.state !== "done";
  if (view.state === "done") {
    const next = {
      probe: ["The hardware is read", "Choose what to run in step 3."],
      apply: ["Setup finished", "Everything in the plan is in place. The checks ran at the end; their results are below."],
      verify: ["The checks finished", "No check failed."],
      "close-lan": ["The home network is closing", "Open this page over Tailscale and press Keep it closed."],
      "keep-lan-closed": ["The home network stays closed", "Proxmox now answers only over Tailscale."],
      "open-lan": ["The home network is open again", "Proxmox answers on your home network."],
      "rebake-ci": ["The CI images are rebuilt", ""],
    }[view.action] || ["Finished", ""];
    node.querySelector(".job-ok-title").textContent = next[0];
    node.querySelector(".job-ok-text").textContent = next[1];
  }
  // Checks.
  const v = p.verify;
  const box = node.querySelector(".job-verify");
  box.hidden = !v;
  if (v) {
    const c = v.counts;
    box.querySelector(".counts").replaceChildren(
      make("span", {className: "c pass", textContent: `${c.PASS} passed`}),
      make("span", {className: "c fail", textContent: `${c.FAIL} failed`}),
      make("span", {className: "c degraded", textContent: `${c.DEGRADED} degraded`}),
      make("span", {className: "c skip", textContent: `${c.SKIP} skipped`}));
    box.querySelector(".problems").replaceChildren(...v.problems.map((x) =>
      make("li", {className: x.status.toLowerCase()}, make("b", {textContent: x.status === "FAIL" ? "Failed: " : "Degraded: "}), x.text)));
  }
  // The log, kept at its end unless the owner scrolled up.
  const pre = node.querySelector(".job-log pre");
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent += view.text;
  if (atEnd) pre.scrollTop = pre.scrollHeight;
  updateJobbar();
}

function updateJobbar() {
  const bar = $("jobbar");
  if (!job || !job.view || job.view.state !== "running") {
    bar.hidden = true;
    return;
  }
  const rect = job.node.getBoundingClientRect();
  const visible = rect.bottom > 0 && rect.top < window.innerHeight;
  const p = job.view.progress;
  const now = (p.steps || []).find((s) => s.status === "running");
  $("jobbar-text").textContent = `${p.title}${now ? `: ${now.text}` : "…"} · ${duration(job.view.seconds)}`;
  bar.hidden = visible;
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
    await navigator.clipboard.writeText(text);
    say(result, "ok", "Copied. It holds addresses on your network and guest names, no passwords.");
  } catch {
    const area = make("textarea", {value: text, readOnly: true, className: "copy-area"});
    node.querySelector(".job-error .actions").after(area);
    area.select();
    say(result, "info", "Select all of the text below and copy it.");
  }
}

const STARTING = {
  probe: "Starting the probe…", apply: "Starting setup…", verify: "Starting the checks…",
  "close-lan": "Closing…", "keep-lan-closed": "Keeping it closed…", "open-lan": "Opening…",
};

async function startJob(action, feedback) {
  const button = document.querySelector(`button[data-action="${action}"]`);
  await press(button, feedback, STARTING[action] || "Starting…", async () => {
    const view = await api("/api/jobs", {action});
    follow(view, true);
    job.node.scrollIntoView({behavior: "smooth", block: "start"});
    return "";
  });
}

function feedbackFor(button) {
  return button.closest(".actions").querySelector(".feedback");
}

document.addEventListener("DOMContentLoaded", () => {
  $("toast-close").addEventListener("click", () => toast(""));
  $("jobbar").addEventListener("click", () => job && job.node.scrollIntoView({behavior: "smooth", block: "start"}));
  window.addEventListener("scroll", updateJobbar, {passive: true});
  for (const input of document.querySelectorAll("input")) {
    input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
  }

  $("login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("secret");
    press(event.submitter || event.target.querySelector("button"), "login-result", "Signing in…", async () => {
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
    press(event.submitter || event.target.querySelector("button"), "password-result", "Saving the password…", async () => {
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
    press(event.submitter || choices.querySelector("button[type=submit]"), "choices-result", "Saving and making the plan…", async () => {
      if (!field.value.trim()) throw new ApiError("Enter the email address you sign in to Tailscale with.", 400);
      const result = await api("/api/choices", choicesBody());
      savedChoices = null;
      await refresh();
      const fits = !result.plan.errors.length;
      $("step-4").scrollIntoView({behavior: "smooth", block: "start"});
      return fits ? "Saved. The plan fits; see step 4." : "Saved, but the plan does not fit yet; step 4 says what to change.";
    }, field);
  });

  $("alerts-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("healthchecks-url");
    press(event.submitter || event.target.querySelector("button[type=submit]"), "alerts-result", "Saving…", async () => {
      const result = await api("/api/alerts", {healthchecks_url: field.value});
      renderAlerts(result.alerts);
      state.alerts = result.alerts;
      steps();
      return result.alerts.healthchecks_url ? "Saved. Send a test alert to check it." : "Saved without a Healthchecks.io URL.";
    }, field);
  });
  $("test-alerts").addEventListener("click", () => press($("test-alerts"), "alerts-result", "Sending a test alert…", async () => {
    const result = await api("/api/alerts/test", {});
    return `Sent to ${result.sent.join(" and ")}. Check your phone.`;
  }));
  $("copy-topic").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("ntfy-topic").textContent);
      say("alerts-result", "ok", "Topic copied.");
    } catch { say("alerts-result", "error", "Copying is blocked here; select the topic and copy it by hand."); }
  });

  $("agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("agent-credential");
    press(event.submitter || event.target.querySelector("button"), "agent-result", "Checking and saving…", async () => {
      if (!field.value.trim()) throw new ApiError("Paste the API key or token into the field first.", 400);
      const result = await api("/api/agent", {credential: field.value});
      field.value = "";
      state.agent_credential = result.agent_credential;
      renderAgent(result.agent_credential);
      steps();
      return `Saved. ${result.agent_credential === "API key" ? "An" : "A"} ${result.agent_credential} is kept encrypted on the server.`;
    }, field);
  });

  $("tailscale-client-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const id = $("tailscale-client-id");
    const secret = $("tailscale-client-secret");
    press(event.submitter || event.target.querySelector("button"), "tailscale-client-result", "Saving…", async () => {
      if (!id.value.trim() || !secret.value.trim()) {
        (id.value.trim() ? secret : id).setAttribute("aria-invalid", "true");
        throw new ApiError("Paste both the client ID and the secret.", 400);
      }
      const result = await api("/api/tailscale-client", {client_id: id.value, secret: secret.value});
      id.value = "";
      secret.value = "";
      state.tailscale_client = result.tailscale_client;
      renderTailscaleClient(result.tailscale_client);
      steps();
      return "Saved. The next setup run puts the policy in place with it.";
    }, id);
  });
  $("vpn-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("tailscale-key");
    press(event.submitter || event.target.querySelector("button"), "vpn-result", "Saving…", async () => {
      if (!field.value.trim()) throw new ApiError("Paste the auth key into the field first.", 400);
      const result = await api("/api/vpn", {key: field.value});
      field.value = "";
      state.tailscale_key = result.tailscale_key;
      renderVpn(result.tailscale_key);
      steps();
      return "Saved. Press Run setup in step 8 so the server joins.";
    }, field);
  });
  $("remote-agent-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const field = $("remote-agent-name");
    press(event.submitter || event.target.querySelector("button"), "remote-agent-result", "Making a token…", async () => {
      const made = await api("/api/remote-agents", {name: field.value.trim()});
      field.value = "";
      $("remote-agent-token").textContent = [
        `Agent ${made.name}: shown once, keep it like a password.`,
        `NB_PAGE_URL=${made.url}`,
        `NB_PAGE_SPKI=${made.spki}`,
        `NB_PAGE_TOKEN=${made.token}`,
      ].join("\n");
      $("remote-agent-token").hidden = false;
      const st = await api("/api/state");
      renderRemoteAgents(st.remote_agents, st.remote_agents_ready);
      return `Made a token for ${made.name}. Copy it now; the page shows it only once.`;
    }, field);
  });
  $("files-reveal").addEventListener("click", () => press($("files-reveal"), "files-result", "Fetching…", async () => {
    $("files-password").textContent = (await api("/api/password-reveal", {name: "files"})).password;
    return "";
  }));
  $("gateway-reveal").addEventListener("click", () => press($("gateway-reveal"), "gateway-result", "Fetching…", async () => {
    $("gateway-key").textContent = (await api("/api/password-reveal", {name: "model-gateway"})).password;
    return "";
  }));

  for (const button of document.querySelectorAll("button[data-action]")) {
    button.addEventListener("click", () => startJob(button.dataset.action, feedbackFor(button)));
  }
  $("logout").addEventListener("click", async () => {
    try { await api("/api/logout", {}); } catch (error) { toast(error.message); }
    state = null;
    stopFollowing();
    job = null;
    await refresh().catch((error) => toast(error.message));
  });
  refresh().catch((error) => {
    show("loading", false);
    toast(error.message);
  });
});
