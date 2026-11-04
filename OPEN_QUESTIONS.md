# Open questions

Each question has a recommendation. Answer in chat or edit this file;
answered items move to the decisions list at the bottom.

1. **Test target.** What can NullBill test on: a nested PVE VM, or a spare box
   reset by snapshot? Recommendation: a nested PVE VM on a machine you name,
   with a clean snapshot to roll back to.
2. **Hardware class of the real server.** small (no GPU), mid (1 GPU) or
   large (several)? Which GPU and VRAM? Recommendation: tell me the class and
   GPU model only, no serials.
3. **Controller.** Which machine runs `nullbill`? Recommendation: your PC
   (Linux, or WSL on Windows); never the PVE host.
4. **NULLBILL_HOME.** Does the directory exist on the controller, in your
   backup set, with a `denylist.txt` of your names, hostnames, domains and
   serials? Recommendation: `~/nullbill-home`, included in backups, created
   before your first push from that machine.
5. **Tailscale Personal.** Its free plan is for personal, non-commercial use.
   Is this server personal and non-commercial? Recommendation: confirm, or I
   draft an ADR for Headscale instead.
6. **Guest floors.** The sizes in `catalog.yaml` are starting values I chose,
   not measurements. Recommendation: accept them for M1; replace them with
   measured values once a test target exists.

7. **Alert channel.** Alerts for failed backups, expiring certificates,
   full disks and failed logins need somewhere to go. Recommendation: a
   self-hosted ntfy in the services zone with its phone app, which costs
   nothing; or name an email address and SMTP relay you already use.

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
  environment, never a file.
- Inference engine: llama.cpp for one GPU, vLLM for several; pinnable.
- Plan output: YAML with a comment above each decision.

## Decisions

- Licence: Apache-2.0 (owner, 2026-11-04).
- Commits: owner's GitHub noreply author, no Claude attribution (owner, 2026-11-04).
- CI: GitHub's standard runners, free for this public repo; they ran the
  first PRs, so Cloudflare runners are not needed (2026-11-04).
- Merges: fast-forward, so GitHub adds no merge commits (owner, 2026-11-04).
