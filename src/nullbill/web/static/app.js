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

function renderVpn(waiting) {
  $("vpn-result").textContent = waiting
    ? "A key is saved; the next setup run uses it."
    : "No key waiting. Once the server has joined, it needs none.";
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
  $("backups").checked = state.choices.backups;
  $("console").checked = state.choices.console;
  $("owner-login").value = state.choices.owner_login;
  renderPlan(state.plan);
  renderAlerts(state.alerts);
  renderAgent(state.agent_credential);
  renderVpn(state.tailscale_key);
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
      if (job.action === "probe") refresh();
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
    try {
      const result = await api("/api/choices", {workloads, backups: $("backups").checked, console: $("console").checked, owner_login: $("owner-login").value});
      renderPlan(result.plan);
      $("plan-details").open = true;
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
  $("vpn-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const result = await api("/api/vpn", {key: $("tailscale-key").value});
      $("tailscale-key").value = "";
      renderVpn(result.tailscale_key);
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
