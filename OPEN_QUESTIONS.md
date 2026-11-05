# Open questions

Each question has a recommendation. Answer in chat or edit this file;
answered items move to the decisions list at the bottom.

1. **Second disk.** PBS needs its own disk to survive a system disk
   failure. Is there a second drive, any size? Recommendation: an old HDD or
   SSD of at least the system disk's used size; without one the plan says
   DEGRADED and backups only guard against mistakes. Backups can also be
   switched off.
2. **Accounts.** Tailscale (free) and, for the offline alert,
   Healthchecks.io (free) need sign-ups by you during setup; ntfy needs only
   its phone app. Recommendation: sign up when the wizard asks.
3. **Design records.** ADRs 0001 to 0006 are proposed; you asked me to
   decide, so they stand unless you object.

## Defaults picked for cosmetic choices

- Zone subnets: `10.42.<index>.0/24`, pinnable through `network.base`.
- Zone bridges: `vmbr<100 + index>`.
- Addresses: router `.1`, service `.<id>`, pool clones `.128/26`, PVE host
  `.254` on mgmt. VMIDs are `2000 + id`. DNS domain `nullbill.internal`.
- Zone egress: services and access go out directly (never into private
  ranges); mgmt, build, inference and windows only through the proxy.
- Proxy allow-list: Debian, Proxmox, Ubuntu, PyPI, npm, crates.io, Go,
  GitHub, Docker Hub, GHCR, Hugging Face and Windows Update, in
  `catalog.yaml` under `proxy.allow`. mgmt guests use the proxy too, so they
  can install updates.
- Generated MACs: `02:4e:42:00:<zone index>:<host>`, locally administered;
  the router's uplink is zone 0. Router interfaces are named `nb-<zone>`.
- DNS: dnsmasq on the router, one instance for direct zones that forwards
  upstream and one for the other zones that answers internal names only.
- Tailnet access: the owner reaches the PVE host (8006, 22), router SSH,
  Forgejo, the workspace, Grafana, the model gateway and Windows RDP; the
  default role reaches Forgejo (443, 22) and workspace SSH. Each grant
  follows a service port, and the policy carries tests that Tailscale runs
  on every push, so a policy that lets a default user reach the host is
  rejected.
- Guest images: Debian 12 for every container (Proxmox's standard
  template) and Linux VM (Debian's dated generic cloud image), each pinned
  by SHA-512 in `catalog.yaml`. PBS installs from its apt repository onto
  the Debian VM. Windows installs from the ISO you supply.
- OpenTofu 1.13.2 with bpg/proxmox 0.118.1; the API token comes from the
  environment, never a file. State and saved plans are encrypted with
  OpenTofu's own AES-GCM and a passphrase from the environment
  (`TF_VAR_state_passphrase`); unencrypted state is refused.
- Backups: PBS keeps 7 daily, 4 weekly and 6 monthly snapshots, runs
  garbage collection daily and re-verifies each snapshot after 30 days. The
  PVE host's token may write backups but not delete them, so retention is
  enforced on PBS, out of the host's reach. All in `ansible/roles/pbs`.
- Agent access: the agent-managed pool holds the workspace, the inference
  container and the Linux and Windows runners. The runner dispatcher and
  the model gateway hold credentials, and windows-dev is your desktop, so
  they stay out; mgmt and access guests can never be in it. The agent's
  token may read everything, and power-manage and snapshot (not roll back)
  pool guests. The operator reaches the PVE API (not host SSH), the CA,
  Loki, Prometheus and SSH on agent-managed Linux guests. Set
  `services.<name>.agent_managed` to change the pool.
- Command log: every command the agent runs over SSH and every agent CLI
  tool call is written to the guest's journal before it runs, and refused
  if that fails. A small shipper (`ansible/roles/log_shipper`, Python
  standard library only, chosen over Grafana Alloy at about 550 MB per
  guest) sends those and sshd's auth lines to Loki. Only a certificate from
  a shipper CA whose key stays in `NULLBILL_HOME` may push; anyone the
  firewall admits may query; nobody may delete.
- Forgejo: the v15 LTS line. Sign-up is off and nothing is visible without
  signing in. Git over SSH uses Forgejo's own server on port 22, so the
  forgejo guest runs no OpenSSH; reach it with `pct enter` on the host. The
  first admin is `nullbill-admin`, with a random password in
  `/etc/forgejo/admin-password` (root only) that must be changed at first
  login.
- CI: a job runs on its own throwaway clone, directly as the user `runner`
  (no Docker), with the label `linux`. A job may run for at most 6 hours.
  Set both per pool through the dispatcher's pool entries (#22).
- Inference engine: llama.cpp for one GPU, vLLM for several; pinnable.
- Plan output: YAML with a comment above each decision.

## Decisions

- Server: i9-9900K, 16 GB, RTX 4070 12 GB, mid class (owner, 2026-11-05).
- First real test on that server once the owner has access; until then
  everything is tested against stand-ins (owner, 2026-11-05).
- NullBill runs in a container on the server and is used from a web page
  over Tailscale; a guided setup builds the rest (owner, 2026-11-05;
  docs/adr/0006-nullbill-on-the-server.md).
- Tailscale's free plan fits: personal, non-commercial (owner, 2026-11-05).
- Guest floors in `catalog.yaml` accepted until measured (owner, 2026-11-05).
- Alerts: free, no open port, survive IP changes and power cuts: ntfy.sh
  and a Healthchecks.io ping (owner asked, 2026-11-05; ADR 0006).
- Agent CLI: API key or Claude subscription token (owner, 2026-11-05).
- Tools for apply and the design records: Claude decides (owner,
  2026-11-05).
- Windows (M6): later; the owner cannot supply an ISO yet (2026-11-05).
- 16 GB now: install without Windows and local models, add them after a
  RAM upgrade. Local models, Windows and backups are options; services can
  be added later without reinstalling (owner, 2026-11-05).

- Licence: Apache-2.0 (owner, 2026-11-04).
- Commits: owner's GitHub noreply author, no Claude attribution (owner, 2026-11-04).
- CI: GitHub's standard runners, free for this public repo; they ran the
  first PRs, so Cloudflare runners are not needed (2026-11-04).
- Merges: fast-forward, so GitHub adds no merge commits (owner, 2026-11-04).
