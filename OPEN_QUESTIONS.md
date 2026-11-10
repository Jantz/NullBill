# Open questions

Each question has a recommendation. Answer in chat or edit this file;
answered items move to the decisions list at the bottom.

1. **Second disk.** PBS needs its own disk to survive a system disk
   failure. Is there a second drive, any size? Recommendation: any spare
   HDD or SSD, also on USB, plugged in when you have one; until then the
   backups live on the system disk (64 GiB), verify says DEGRADED and
   they only guard against mistakes and broken updates (ADR 0017).
   Backups can also be switched off.
2. **Accounts.** Tailscale (free) and, for the offline alert,
   Healthchecks.io (free) need sign-ups by you during setup; ntfy needs only
   its phone app. Recommendation: sign up when the wizard asks.
3. **Design records.** ADRs 0001 to 0006 are proposed; you asked me to
   decide, so they stand unless you object.
4. **Updates that wait for you.** Every Linux guest installs Debian
   security updates and point releases each night and reboots at 04:30
   when one needs it. Three things do not happen by themselves, and
   monitoring warns about each: Proxmox packages on the host (their
   upgrades restart PVE's services and want a full upgrade, which
   unattended-upgrades does not do safely), any reboot of the host (it
   takes every guest down) and reboots of agent-managed guests (agents run
   long jobs there). Recommendation: keep these manual, with an "Update and
   reboot the host" button on the setup page and an alert after 14 days of
   waiting Proxmox updates or 3 days of a waiting reboot.
5. **Debian 13.** Guests run Debian 12, which is now oldstable; its
   security updates continue through Debian LTS until mid-2028. Proxmox VE
   9 is on Debian 13. Recommendation: move every guest to Debian 13 before
   the first real install is a few months old; it touches every role check.
6. **Pinned versions.** Grafana, Loki, step-ca, Forgejo, OpenTofu, sops and
   the agent CLI install at versions this repository pins, so their fixes
   arrive only when the pin moves. apt-installed pins show as waiting
   updates in monitoring; binary pins do not yet. Recommendation: a weekly
   CI job in this repository that compares each pin with its upstream
   release and opens an issue.
7. **Windows Update.** Not managed yet; the plan lists Windows guests under
   `updates.not_managed`. Recommendation: Group Policy for automatic
   install and an exporter for update age, when Windows work starts.
8. **On-site screen.** Plug the monitor into the motherboard's output: the
   i9-9900K's integrated graphics then drive it and the RTX 4070 stays free
   for guests (a 9900KF has no integrated graphics). Many boards switch
   integrated graphics off when a graphics card is present. On ASUS boards
   turn on Advanced Mode (F7), then **Advanced → System Agent (SA)
   Configuration → Graphics Configuration → iGPU Multi-Monitor: Enabled**
   (<https://www.asus.com/support/faq/1045574/>); other makers use a
   similar name such as "IGD Multi-Monitor" or "Internal Graphics", in the
   board's manual. Recommendation: switch the screen on in the setup page
   only if you want it; it costs about 512 MB.

10. **Disks from a previous OS** (docs/adr/0007-legacy-disks.md). A disk
    with your files can be kept as it is and shared to you over the VPN.
    - Zone for the `files` guest: mgmt (internet only through the proxy) or
      services (direct)? Recommendation: mgmt, since it holds your personal
      files and only the owner reaches it.
    - Default when you choose "keep" on the page: read-only, or read-write
      when the disk checks clean? Recommendation: read-only, with a
      read-write switch for NTFS.
    - SMB only, or SFTP as well? Recommendation: SMB only for now.
    - Alerts on SMART changes for every non-system disk (smartctl is already
      on the host)? Recommendation: yes, as the next change after the share
      works; the plan already warns about a disk that is not healthy.
11. **Jellyfin on the TV** (docs/adr/0009-media-server.md). Jellyfin is
    reachable over Tailscale only. A TV without Tailscale needs the router
    guest to forward port 8096 from your home network, which lets every
    device on that network reach Jellyfin's sign-in. Recommendation: use
    Jellyfin's app with Tailscale where the TV has it (Android TV, Fire TV,
    Apple TV); ask for the forward only for a TV that cannot.
12. **Start on first use** (docs/adr/0013-on-demand.md). A workload that
    runs on demand starts when you press Start on the page. It could also
    start when something first connects to it, but then something has to
    listen in its place and the client has to wait out the start: an
    OpenAI client and a queued CI job can, an SMB or Remote Desktop client
    gives up. Recommendation: build it for the local models and CI first,
    once you have used Start for a while and know where it is in the way.
13. **A schedule for what runs on demand.** "The desktop on weekdays 9 to
    17" or "everything off at night" is a timer running `nb-power start`
    and `stop`. Recommendation: add it to the page only if the rules
    (stops by itself, or reminds you) turn out not to be enough.
14. **The backup server on demand.** It takes 2 GiB and is needed for the
    nightly backup and its own clean-up jobs. It could run only around
    those. Recommendation: not yet; a backup that silently does not run is
    worse than 2 GiB.
15. **Stopping a Windows desktop by itself.** A VM does not tell NullBill
    whether someone uses it; only its CPU shows, and a stop closes open
    programs. The page therefore offers the reminder only, and
    `power.windows-dev.busy.cpu_pct` in `config.yaml` for those who accept
    that. Recommendation: keep it that way until Windows work starts; a
    small agent in Windows could then report the signed-in session.
16. **A port at your home router** (docs/adr/0014-direct-connections.md).
    So that a phone on mobile data connects without a relay, the server
    asks your home router to send UDP 41642 to Tailscale on the vpn guest.
    It is the first port NullBill opens to the internet; Tailscale answers
    only your tailnet's devices on it. Recommendation: leave it on. To
    forward the port yourself or have none open, switch off **Ask the home
    router for the port** on the page's **Access** page.

17. **The download client** (docs/adr/0017-download-client.md), four
    things left out on purpose. (a) A VPN provider's port forwarding, so
    others can connect to the client and seeding is faster: it differs per
    provider. Recommendation: say which provider you use, and I build
    that one. (b) A place for downloads other than a kept disk.
    Recommendation: not until someone has no kept disk. (c) Sonarr,
    Radarr and Prowlarr beside it. Recommendation: as services of your
    own first (ADR 0015), built in once you use them. (d) HTTPS for its
    page. Recommendation: together with Jellyfin's, since both are plain
    HTTP inside Tailscale today.

## Defaults picked for cosmetic choices

- Zone subnets: `10.42.<index>.0/24`, pinnable through `network.base`.
- Zone bridges: `vmbr<100 + index>`.
- Addresses: router `.1`, service `.<id>`, pool clones `.128/26`, PVE host
  `.254` on mgmt. VMIDs are `2000 + id`. DNS domain `nullbill.internal`.
- Zone egress: services and access go out directly (never into private
  ranges); mgmt, build, inference, windows, media and games only through
  the proxy.
- Proxy allow-list: Debian, Proxmox, Ubuntu, PyPI, npm, crates.io, Go,
  GitHub, Docker Hub, GHCR, Hugging Face and Windows Update, in
  `catalog.yaml` under `proxy.allow`. mgmt guests use the proxy too, so they
  can install updates.
- Generated MACs: `02:4e:42:00:<zone index>:<host>`, locally administered;
  the router's uplink is zone 0. Router interfaces are named `nb-<zone>`.
- DNS: dnsmasq on the router, one instance for direct zones that forwards
  upstream and one for the other zones that answers internal names only.
- Tailnet access: the owner reaches the PVE host (8006, 22), router SSH,
  Forgejo, the workspace, Grafana, the model gateway, Jellyfin and Windows RDP; the
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
- Inference engine: llama.cpp with its Vulkan backend, the model picked from
  catalog.yaml's `models` by VRAM (docs/adr/0008-local-models.md); vLLM is
  not built.
- Media server: Jellyfin's latest release (12.4) from its repository, admin
  `nullbill-admin`, the kept disks read-only, Quick Sync on integrated
  Intel graphics, libraries added by you (docs/adr/0009-media-server.md).
- Game servers: the makers' own servers (Minecraft: Java Edition 26.3,
  Terraria 1.4.5.8, Factorio 2.1.24 without the Space Age expansion), a
  new world each, no mods, players checked against their accounts, 3 GiB
  for Minecraft and 1.5 GiB each for the others
  (docs/adr/0012-game-servers.md, docs/runbooks/games.md).
- Plan output: YAML with a comment above each decision.
- Updates: unattended-upgrades on the host (Debian security only) and on
  every Linux guest (Debian security and point releases, as Debian's own
  default), with needrestart restarting services on guests. node_exporter
  and Debian's apt collector on each report waiting updates, the last
  successful run and reboots needed; Prometheus alerts when security
  updates wait two days, no run succeeds for three, package lists go stale
  for two, upgrades are switched off, or a reboot waits three days. All in
  `ansible/roles/updates`. Alerts reach your ntfy topic with high priority
  for "page" alerts and normal priority for "warn" ones. Pool clones live
  for one job and start from a golden image baked with every pending
  update and baked again weekly (decisions, question 9).

## Decisions

- The review before a setup run, and the Updates page for a waiting
  update, say for each machine whether the run leaves it alone, sets it up
  again and what may restart, creates or deletes it, and how long its
  setup took last time; worked out on the controller, the server is asked
  nothing (owner asked what an update does to his Minecraft server,
  2026-11-10, then left the choice of what to build to Claude; design by
  Claude, docs/adr/0018-setup-preview.md).
- Many services, and ones NullBill has no support for: built-in services
  stay; anything else is a service of the owner's own, a guest NullBill
  makes plus a setup script, which the page's agent may add once the owner
  has read and approved it. The page's lists fold and filter for many
  (owner asked, 2026-11-10; decided by Claude as asked,
  docs/adr/0015-custom-services.md).
- Several servers of one built-in game, each with its own world, settings,
  admins, memory and friends, added on the page or by the agent; every
  game server's settings are on the page (owner asked, 2026-11-10: "multiple
  Minecraft servers with customizations"; design by Claude,
  docs/adr/0016-more-game-servers.md). Not built: mods and plugin servers.
- Worlds per game server: a list on the Services page, new world with
  the game's own options, run a kept one, upload, download, rename,
  delete; console, log and who plays in the same row; the agent has tools
  for it (owner asked, 2026-11-10: "a game server save/world feature";
  design by Claude, docs/adr/0019-game-worlds.md). Not tried on real
  hardware: large files through `pct exec`.
- Direct connections from mobile data: built, on by default, with a
  switch on the page (owner asked, 2026-11-09; design by Claude,
  docs/adr/0014-direct-connections.md; question 16 stays open for the
  default).
- Game servers for Minecraft, Terraria and Factorio, built into NullBill
  (owner, 2026-11-09).
- Jellyfin, built into NullBill (owner, 2026-11-08).
- Disks from a previous OS (question 10): `ntfs-3g` on the host and Samba
  in a `files` guest, approved (owner, 2026-11-07;
  docs/adr/0007-legacy-disks.md).
- CI image age (question 9): the recommendation, built: apply bakes each
  golden image again once it is a week old, the page runs that weekly, and
  an alert fires past 14 days. Jobs wait in Forgejo while it bakes; say if
  you want a second template so they never wait (Claude, 2026-11-05).
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
