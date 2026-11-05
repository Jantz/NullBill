# ADR 0006: NullBill lives on the server and is run from a web page

Status: proposed. Replaces the separate controller of ADRs 0001 and 0005.

## Context

The owner's answers of 2026-11-05:

- No second machine should be needed. After Proxmox VE and NullBill are
  installed, the server builds itself through a guided setup that asks the
  owner for input. It is then managed remotely and securely from any
  device, and nobody needs to touch it again unless something breaks badly.
- The server is on a residential connection, so its public IP changes and
  power can fail now and then.
- Agents should be able to run long jobs on the server. That covers agents
  running on the server and remote agents connecting to it, and both need
  access that is easy to give and safe.
- The hardware is an i9-9900K (16 threads), 16 GB RAM and an RTX 4070
  (12 GB).

## Decision

**Where NullBill runs.** In its own container on the server (`nullbill`,
mgmt zone), not on the PVE host itself. The container holds the
controller's state (today's `NULLBILL_HOME`: config, probe, plan, sops
secrets, OpenTofu state) at `/var/lib/nullbill`, and an SSH key for root on
the host. apply keeps reaching the host over SSH (ADR 0005), so nothing
about the stages changes. The host stays a plain PVE install, and a bug or
exploit in the web app does not run as root on the host. Holding the root
key does make the container as powerful as the host. It is therefore in
mgmt, takes connections only from the owner's tailnet devices, and the
agent's pool can never include it.

**Install.** The owner does two things at the server:

1. Install Proxmox VE from USB. This is the only step that needs a screen
   and keyboard.
2. In the PVE web UI's shell, run the one command from the README. The
   command downloads a tagged NullBill release, checks its SHA-256 against
   the README, and creates the `nullbill` container. It then prints a LAN
   address and a one-time setup code.

From then on, everything happens in a browser on any device on the same
network.

**Setup wizard.** The `nullbill` container serves a web app. On first run
it listens only on the LAN and asks for the setup code. Its steps are:

1. Probe the hardware and show the plan, including what does not fit.
2. Ask for the choices only the owner can make: workloads, people, how
   alerts reach their phone, and the agent's credential.
3. Connect Tailscale. The VPN guest starts `tailscale up` and the page
   shows its sign-in link.
4. Run apply stage by stage, with live progress.
5. Run verify and show the results.

When the owner opens the page over Tailscale, setup closes LAN access to
the page, the web UI (8006) and SSH. That step is today's `--close-lan`
(#9). After that, NullBill is reachable only over the tailnet.

**Remote access.** Tailscale, free plan (owner, 2026-11-05). It works
behind NAT and a changing public IP, without port forwarding or dynamic
DNS. The web app is a plain HTTPS page, so any device with the Tailscale
app can use it: Windows, macOS, Linux, iOS and Android. No separate client
is needed. The page lets the owner see status and verify results, re-run
apply after a config change, and manage people, agents and alerts. It also
links to the PVE UI, Grafana and Forgejo.

**Power and IP changes.**

- Every guest starts on boot, in dependency order: router, CA, Postgres,
  then services.
- The host has a BIOS setting, "restore on AC power loss", that the owner
  sets once during install. It is the only BIOS step.
- PBS, Postgres and Forgejo come back from an unclean stop on their own.
  verify's backup checks catch a missed nightly job.
- Tailscale reconnects after an IP change without help.

**Alerts.** These need no open port and work from a residential IP:

- ntfy.sh, the free public server, with the ntfy phone app. Prometheus's
  Alertmanager sends each alert through a small relay on the monitoring
  guest to a random topic NullBill makes when the owner sets a password.
  The page shows the topic and sends a test. Alerts say what failed and
  never include secrets.
- Healthchecks.io, free plan, optional. An always-firing Watchdog alert
  pings it every 5 minutes. When the server is down or offline, for
  example after a power cut, Healthchecks alerts through its own channels,
  which a server that is off cannot do for itself. When the host starts
  again, a HostStarted alert says so.

**Agents.**

- *Agents on the server* run in the operator container (ADR 0002/0003). The
  agent CLI takes either an API key or a Claude subscription token made
  with `claude setup-token`. The owner pastes one into the wizard.
- *Remote agents* get two things, both issued from the web page:
  - a Tailscale auth key tagged `tag:agent`, ephemeral and pre-approved;
  - an SSH certificate for the `agent` user, valid for the hours the owner
    picks.

  The tailnet policy lets `tag:agent` reach SSH on agent-managed guests
  only. The certificate is limited the same way (ADR 0003), so a remote
  agent has the same confinement and command log as a local one.
  Revoking the agent in the page removes the device and stops new
  certificates.
- Long jobs run in the workspace container or on CI runner clones, never in
  the operator. They survive a dropped SSH session because they run under
  `systemd-run --user`.

**Upgrades without reinstalling.** The first install is whatever fits.
Local models, Windows and backups are switches on the page, and so are
the workloads. The owner can add a container or VM for a new service from
a short form. Flipping a switch or adding a service edits `config.yaml`
and re-runs apply, which is idempotent, so nothing is reinstalled.

**What 16 GB holds.** With today's floors, the plan fits agents, Linux CI
(one runner at a time) and every M3 to M5 service, with 0.8 GB left. It
does not fit inference: its container floor is 8 GB of RAM, although a
model that fits in 12 GB of VRAM needs far less. Windows does not fit
either (4 GB per VM). The owner chose to install at 16 GB without either,
and to add them after a RAM upgrade (2026-11-05). Local models are an
option, not a goal of the project.

## Alternatives

- **NullBill directly on the PVE host.** No container to bootstrap, but
  the web app would run as root next to PVE, and the host would carry
  packages that PVE upgrades do not expect.
- **A separate controller (ADR 0005).** The owner has no spare machine,
  and running it from Windows or macOS needs WSL or a VM.
- **Public HTTPS (Cloudflare Tunnel, Tailscale Funnel).** It puts the
  management page on the internet, and Cloudflare also needs a domain,
  which costs money. The tailnet already reaches every device the owner
  uses.
- **A native desktop or phone app.** A web page does the same everywhere,
  with nothing to install beyond Tailscale.

## Consequences

- New work:
  - the installer command;
  - the `nullbill` container and its role;
  - the web app (wizard, status, people, agents, alerts);
  - remote-agent keys;
  - start-on-boot order;
  - the Healthchecks ping;
  - the inference RAM rule.
- `refuse_on_pve_host` stays: NullBill still never runs on the host
  itself.
- `NULLBILL_HOME` defaults to `/var/lib/nullbill` in the `nullbill`
  container.
  - Losing it loses the OpenTofu state and secrets. PBS backs it up
    nightly, encrypted.
  - The wizard asks the owner to download the age key and the PBS
    encryption key once, into their password manager. Without them, a
    dead disk cannot be restored.
- If PBS shares the system disk, it does not protect against a disk
  failure. The plan already reports DEGRADED in that case, and the page
  shows it.
