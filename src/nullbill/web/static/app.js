"use strict";

const LABELS = {
  "agents": "AI agents: the operator and a workspace container",
  "linux-ci": "Linux CI runners for Forgejo",
  "inference": "Local AI models on the GPU (needs more memory)",
  "windows-ci": "Windows CI runners (needs a Windows ISO and more memory)",
  "windows-dev": "A Windows desktop VM (needs a Windows ISO and more memory)",
};

const $ = (id) => document.getElementById(id);
let polling = null;
let statusTimer = null;

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-NullBill": "1"},
    body: JSON.stringify(body),
  };
  const response = await fetch(path, {credentials: "same-origin", ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function show(id, visible) { $(id).hidden = !visible; }

function showError(error) { $("error").textContent = error ? String(error.message || error) : ""; }

function renderWorkloads(state) {
  const box = $("workloads");
  box.replaceChildren();
  const legend = document.createElement("legend");
  legend.textContent = "Workloads";
  box.append(legend);
  for (const name of state.workloads) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = name;
    input.checked = state.choices.workloads.includes(name);
    label.append(input, " " + (LABELS[name] || name));
    box.append(label);
  }
}

const CHOICES = {
  alone: "Leave it alone",
  keep: "Keep its files and share them to me",
  copy: "Copy its files onto the server, check the copy, then erase the disk and use it for backups",
  erase: "Erase everything on it and use it for backups",
};

// What each choice says for this disk, and why it cannot be picked, if so.
function diskChoice(disk, value, keepable) {
  if (value === "keep" && !keepable && disk.choice !== "keep") return [CHOICES.keep, "no NTFS, exFAT or FAT partition on it"];
  if (value === "copy") {
    const size = disk.used_gib === null ? "" : ` (about ${disk.used_gib} GiB, shared to you like now and backed up)`;
    if (disk.choice === "copy") return [CHOICES.copy + size, ""];
    if (!disk.copy_to) return [CHOICES.copy, "the server's storage is not LVM; see docs/runbooks/legacy-disk.md"];
    if (disk.used_gib === null) return [CHOICES.copy, "how much it holds is unknown; probe the server again"];
    return [CHOICES.copy + size, ""];
  }
  if (value === "erase" && disk.kept) return ["Erase everything on it without a copy (I have its files elsewhere) and use it for backups", ""];
  return [CHOICES[value], ""];
}

function gib(mib) { return mib >= 1024 ? `${(mib / 1024).toFixed(1)} GiB` : `${mib} MiB`; }

function renderDisks(disks) {
  const list = $("disk-list");
  list.replaceChildren();
  show("disks", disks.length > 0);
  for (const disk of disks) {
    const box = document.createElement("div");
    box.className = "disk";
    box.dataset.id = disk.id;
    const hours = disk.power_on_hours ? `, ${disk.power_on_hours} hours in use` : "";
    const title = document.createElement("p");
    const name = document.createElement("b");
    name.textContent = disk.id;
    title.append(name, ` ${disk.size_gib} GiB ${disk.rotational ? "hard disk" : "SSD"}, health ${disk.health}${hours}`);
    box.append(title);
    if (disk.health === "FAILED") {
      box.append(Object.assign(document.createElement("p"), {className: "errors",
        textContent: "This disk reports that it is failing. Copy what you need off it soon."}));
    }
    const parts = document.createElement("ul");
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
      const label = document.createElement("label");
      const input = Object.assign(document.createElement("input"), {type: "radio", name: `disk-${disk.id}`, value});
      input.checked = disk.choice === value;
      input.disabled = Boolean(why);
      label.append(input, " " + text + (why ? ` (${why})` : ""));
      box.append(label);
    }
    const rw = document.createElement("label");
    rw.className = "disk-rw";
    const rwInput = Object.assign(document.createElement("input"), {type: "checkbox", checked: disk.rw});
    rw.append(rwInput, " Let me change files on it (NTFS only). Shut Windows down fully on that disk first, with shutdown /s /t 0, or it stays read-only.");
    const confirm = document.createElement("label");
    confirm.className = "disk-confirm";
    confirm.append(`Type ${disk.confirm}, the end of its name, to erase everything on it`,
      Object.assign(document.createElement("input"), {autocomplete: "off"}));
    box.append(rw, confirm);
    const sync = () => {
      const chosen = box.querySelector("input[type=radio]:checked");
      rw.hidden = !chosen || chosen.value !== "keep";
      confirm.hidden = !chosen || !["erase", "copy"].includes(chosen.value) || disk.choice === chosen.value;
    };
    box.addEventListener("change", sync);
    sync();
    list.append(box);
  }
}

function diskChoices() {
  const out = {};
  for (const box of $("disk-list").querySelectorAll(".disk")) {
    const chosen = box.querySelector("input[type=radio]:checked");
    out[box.dataset.id] = {
      choice: chosen ? chosen.value : "alone",
      rw: box.querySelector(".disk-rw input").checked,
      confirm: box.querySelector(".disk-confirm input").value,
    };
  }
  return out;
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

function renderPlan(plan) {
  const errors = $("plan-errors");
  errors.replaceChildren(...plan.errors.map((e) => Object.assign(document.createElement("li"), {textContent: e})));
  $("plan").textContent = plan.text || "";
  $("plan-details").hidden = !plan.text;
  $("plan-hint").hidden = Boolean(plan.text || plan.errors.length);
  $("tailnet-policy").textContent = plan.tailnet_policy || "";
  $("tailnet-details").hidden = !plan.tailnet_policy;
  $("tailnet-hint").hidden = Boolean(plan.tailnet_policy);
}

function renderAlerts(alerts) {
  $("ntfy-topic").textContent = alerts && alerts.ntfy_url ? alerts.ntfy_url.replace("https://", "") : "";
  $("healthchecks-url").value = alerts ? alerts.healthchecks_url : "";
}

function renderAgent(kind) {
  $("agent-result").textContent = kind ? `A ${kind} is saved.` : "Nothing saved yet.";
}

function renderTailscaleClient(saved) {
  $("tailscale-client-result").textContent = saved
    ? "Saved; each setup run puts the policy in place with it."
    : "None saved. Until one is, the server stays off Tailscale.";
}

function renderRemoteAgents(names) {
  const list = $("remote-agents");
  list.replaceChildren(...names.map((name) => {
    const item = document.createElement("li");
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "link";
    revoke.textContent = "Revoke";
    revoke.addEventListener("click", async () => {
      try {
        renderRemoteAgents((await api("/api/remote-agents/revoke", {name})).remote_agents);
        $("remote-agent-token").hidden = true;
      } catch (error) { showError(error); }
    });
    item.append(`${name} `, revoke);
    return item;
  }));
}

function renderVpn(waiting) {
  $("vpn-result").textContent = waiting
    ? "A key is saved; the next setup run uses it."
    : "No key waiting. Once the server has joined, it needs none.";
}

const LAN = {
  open: "Open: Proxmox answers on your home network.",
  closing: "Closing: press Keep it closed on this page opened over Tailscale, or it opens again by itself.",
  closed: "Closed: Proxmox answers only over Tailscale.",
};

function renderLan(state) {
  $("lan-status").textContent = LAN[state.state] + (state.state === "closing" && !state.via_vpn
    ? " This page is not open over Tailscale right now." : "");
  $("lan-minutes").textContent = state.minutes;
  show("lan-close", state.state === "open");
  show("lan-keep", state.state === "closing");
  show("lan-open", state.state !== "open");
}

function li(text, cls) {
  return Object.assign(document.createElement("li"), {textContent: text, className: cls || ""});
}

async function refreshStatus() {
  const s = await api("/api/status");
  const alerts = $("status-alerts");
  const facts = $("status-facts");
  alerts.replaceChildren();
  facts.replaceChildren();
  if (s.monitoring === "not planned") {
    $("status-summary").textContent = "Monitoring starts once the server is built.";
    return;
  }
  if (s.monitoring !== "ok") {
    $("status-summary").textContent = "Cannot reach monitoring. If the server was just built, wait a minute.";
    return;
  }
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
  showError("");
  const state = await api("/api/state");
  show("logout", state.auth);
  show("login", !state.auth);
  if (!state.auth) {
    clearInterval(statusTimer);
    $("login-hint").textContent = state.has_password
      ? "Enter your password."
      : "Enter the setup code the installer printed in the Proxmox shell.";
    show("password", false);
    show("setup", false);
    return;
  }
  show("password", !state.has_password);
  show("setup", state.has_password);
  if (!state.has_password) return;
  $("probed").textContent = state.probed ? "Done." : "";
  renderWorkloads(state);
  renderDisks(state.disks);
  renderFiles(state.files);
  $("backups").checked = state.choices.backups;
  $("console").checked = state.choices.console;
  $("owner-login").value = state.choices.owner_login;
  renderPlan(state.plan);
  renderAlerts(state.alerts);
  renderAgent(state.agent_credential);
  renderTailscaleClient(state.tailscale_client);
  renderVpn(state.tailscale_key);
  renderRemoteAgents(state.remote_agents);
  renderLan(state.lan);
  clearInterval(statusTimer);
  refreshStatus().catch(showError);
  statusTimer = setInterval(() => refreshStatus().catch(() => {}), 30000);
  if (state.job) follow(state.job.id, state.job);
}

function follow(id, first) {
  clearInterval(polling);
  let offset = 0;
  show("job-section", true);
  $("job-log").textContent = "";
  const tick = async () => {
    const job = await api(`/api/jobs/${id}?offset=${offset}`);
    offset = job.offset;
    $("job-title").textContent = `${job.args.join(" ")}: ${job.state}`;
    const log = $("job-log");
    log.textContent += job.text;
    log.scrollTop = log.scrollHeight;
    if (job.state !== "running") {
      clearInterval(polling);
      if (job.action === "probe" || job.action.endsWith("-lan") || job.action === "keep-lan-closed") refresh();
    }
  };
  tick().catch(showError);
  if (!first || first.state === "running") polling = setInterval(() => tick().catch(showError), 1000);
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/login", {secret: $("secret").value});
      $("secret").value = "";
      await refresh();
    } catch (error) { showError(error); }
  });
  $("password-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/password", {password: $("new-password").value});
      $("new-password").value = "";
      await refresh();
    } catch (error) { showError(error); }
  });
  $("choices-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const workloads = [...$("workloads").querySelectorAll("input:checked")].map((i) => i.value);
    showError("");
    try {
      const result = await api("/api/choices", {workloads, backups: $("backups").checked, console: $("console").checked,
        owner_login: $("owner-login").value, disks: diskChoices()});
      renderPlan(result.plan);
      $("plan-details").open = true;
      const state = await api("/api/state");
      renderDisks(state.disks);
      renderFiles(state.files);
    } catch (error) { showError(error); }
  });
  $("alerts-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/alerts", {healthchecks_url: $("healthchecks-url").value});
      renderAlerts(result.alerts);
      $("alerts-result").textContent = "Saved.";
    } catch (error) { showError(error); }
  });
  $("agent-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/agent", {credential: $("agent-credential").value});
      $("agent-credential").value = "";
      renderAgent(result.agent_credential);
    } catch (error) { showError(error); }
  });
  $("tailscale-client-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/tailscale-client",
        {client_id: $("tailscale-client-id").value, secret: $("tailscale-client-secret").value});
      $("tailscale-client-id").value = "";
      $("tailscale-client-secret").value = "";
      renderTailscaleClient(result.tailscale_client);
    } catch (error) { showError(error); }
  });
  $("remote-agent-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const made = await api("/api/remote-agents", {name: $("remote-agent-name").value});
      $("remote-agent-name").value = "";
      $("remote-agent-token").textContent = [
        `Agent ${made.name}: shown once, keep it like a password.`,
        `NB_PAGE_URL=${made.url}`,
        `NB_PAGE_SPKI=${made.spki}`,
        `NB_PAGE_TOKEN=${made.token}`,
      ].join("\n");
      $("remote-agent-token").hidden = false;
      renderRemoteAgents((await api("/api/state")).remote_agents);
    } catch (error) { showError(error); }
  });
  $("vpn-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/vpn", {key: $("tailscale-key").value});
      $("tailscale-key").value = "";
      renderVpn(result.tailscale_key);
    } catch (error) { showError(error); }
  });
  $("files-reveal").addEventListener("click", async () => {
    try {
      $("files-password").textContent = (await api("/api/files-password", {})).password;
    } catch (error) { showError(error); }
  });
  $("test-alerts").addEventListener("click", async () => {
    try {
      const result = await api("/api/alerts/test", {});
      $("alerts-result").textContent = `Sent to ${result.sent.join(" and ")}.`;
    } catch (error) { showError(error); }
  });
  for (const button of document.querySelectorAll("button[data-action]")) {
    button.addEventListener("click", async () => {
      try {
        const job = await api("/api/jobs", {action: button.dataset.action});
        follow(job.id);
      } catch (error) { showError(error); }
    });
  }
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", {});
    await refresh();
  });
  refresh().catch(showError);
});
