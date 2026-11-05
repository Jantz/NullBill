# CLAUDE.md

Guidance for Claude sessions working in this repository.

## What this is

NullBill turns a spare x86 machine running Proxmox VE into a self-hosted dev
server from one declarative config. `nullbill` runs on a controller the owner
names and reaches the host over SSH; it never runs on the host. Only `probe`,
`plan`, `apply` (stages so far: render, host, tofu, router, proxy, updates, ca, postgres, monitoring, forgejo, ci, agents, logs, backups, vpn, verify) and
`verify` exist.

## Layout

- `src/nullbill/data/catalog.yaml`: platform defaults (zones, roles, services, engine table).
- `src/nullbill/data/schema.yaml`: JSON Schema for `config.yaml` and for the merged model.
- `src/nullbill/probe.py`: read-only probe commands and their parser.
- `src/nullbill/plan.py`: sizing rules; every rule failure is collected and reported together.
- `tests/fixtures/<class>.yaml`: synthetic probe output for small, mid, large.
- `tests/golden/<class>.plan.yaml`: expected plans. Regenerate with `UPDATE_GOLDEN=1 python3 -m pytest` and review the diff.
- `hooks/`, `tools/denylist-scan`: privacy hooks. `tools/install-hooks` enables them.
- `src/nullbill/flows.py`: the one flow list both firewall layers are generated from.
- `src/nullbill/firewall.py`: pve-firewall files; `tools/check-firewall` parses them with the stock parser in a container.
- `src/nullbill/router.py`, `src/nullbill/proxy.py`: router (nftables, dnsmasq, interfaces) and Squid files; `tools/check-guests` checks them with Debian 12's nft, dnsmasq, ifupdown and squid.
- `src/nullbill/tailnet.py`: Tailscale policy from people and roles, the only per-person gate; plan fails past the free-plan limits in `catalog.yaml`. `routes` are the zones the vpn guest advertises.
- `ansible/roles/vpn`: pinned Tailscale in userspace networking (no tun in the container); logs in once with the owner's one-time auth key (`tailscale_auth_key`, pasted on the page, dropped by apply after login), then keeps routes and name with `tailscale set`.
- `ansible/`: host baseline (key-only SSH, auditd, no-subscription repo, security-only upgrades); `tools/check-host` runs it twice in a Debian 12 container and checks the result. `docs/runbooks/` holds steps the owner does by hand. Guest roles carry `tests/check.sh`, which `tools/check-roles` runs in a Debian 12 container.
- `ansible/roles/router`: places `build/router` on the router VM, each nft and dnsmasq file checked before it replaces the old one; reboots when interface names change. Its check uses `tests/files`, which `tests/test_role_check_files.py` keeps equal to the small render.
- `ansible/roles/updates`: unattended-upgrades, needrestart and node_exporter with Debian's apt collector, plus `nb-update-metrics` (last successful run). apply's updates stage runs it on every Linux guest that lives on; `host_baseline` includes it for the host (security only, never reboots). Its alert rules are in the monitoring role's `nullbill.rules.yml`.
- Alerts: the monitoring role's Alertmanager sends every alert to `nb-alert-relay` (localhost), which posts to the owner's ntfy topic and turns the always-firing Watchdog into Healthchecks.io pings. The page makes the topic (`ntfy_topic` in secrets) when the owner sets a password and takes the optional `healthchecks_url`.
- `ansible/roles/console`: the optional on-site screen (`host.console`), cage and Chromium on the host's tty1 showing the page, its key pinned and every other address blocked by policy. apply's host stage runs it, on or off.
- `ansible/roles/egress_proxy`: Squid with the rendered `squid.conf`, each version checked before it replaces the last.
- `ansible/roles/pbs`: Proxmox Backup Server with a backup-only token for the host; its check backs up with client-side encryption, restores, and rebuilds over a kept datastore.
- `ansible/roles/tls_cert`: a step-ca certificate at `/etc/nullbill/tls/<name>.crt|.key`, first from a one-time token apply makes, then renewed by `step ca renew --daemon` (step-ca certificates last 24 h). Service roles (monitoring, postgres) read it from there and refuse to run without it; their checks seed those files from `test_pki`.
- `ansible/roles/forgejo`: Forgejo from a checksum-pinned release behind nginx (HTTPS on 443 with its tls_cert certificate), its own SSH server on 22 (no OpenSSH on that guest), PostgreSQL with `sslmode=verify-full`, no sign-up, Actions on. `nb-forgejo` runs its CLI as `git`. Its check runs the postgres role in the same container.
- `ansible/roles/runner_dispatcher` (`nb-dispatcher`) and `ansible/roles/ci_runner`: per queued Actions job, an ephemeral runner registration and a linked clone of the pool's golden image with a one-time nonce in its SMBIOS serial; the clone fetches its registration once from the dispatcher on 8443, runs `forgejo-runner one-job --handle` as `runner` and powers off; the dispatcher destroys it. The dispatcher's check runs a real job through Forgejo with `tests/fake_pve.py` standing in for PVE; `tests/test_dispatcher.py` covers the failure paths.
- `ansible/roles/step_ca` also runs the `agent` provisioner: SSH user certificates for principal `agent` only, at most 1 h, no X.509. `ansible/roles/agent_ssh` makes agent-managed guests accept those for the unprivileged `agent` user and never for root.
- `ansible/roles/operator`: the agent user, its secrets (root-owned, agent-readable), the pinned agent CLI with root-owned managed settings, and `nb-ssh` for agent-managed guests. Its check logs in end to end through the real CA.
- Command log: `agent_ssh` records each agent SSH command (`nb-agent-command`) and the operator's managed hook records each agent CLI tool call (`nb-log-tool-use`) in the journal before it runs, failing closed. `ansible/roles/log_shipper` ships those and sshd's auth lines to Loki with a shipper certificate; the monitoring role's nginx gate takes pushes only with one, and refuses deletes. Trust the `uid` label (journald's), not the message text.
- `src/nullbill/backups.py`: the PBS storage and nightly job the OpenTofu render adds, and verify's backup-age and restore checks. The pinned disk becomes the host's `nb-pbs-disk` LVM storage (`host_baseline`'s `nb-backup-disk`, run by the tofu stage, refuses signatures without `wipe`); the pbs VM's datastore disk lives there (serial `nb-backups`, `prevent_destroy`). apply's backups stage runs the pbs role, keeps PBS's token and fingerprint and the encryption key (made on the host, kept in sops) in secrets, re-runs tofu when they change and backs up each guest that has no backup yet.
- `src/nullbill/agent.py`: the runtime agent's PVE pool, role and token; `agent_managed` services join the pool and get SSH from the operator (model.py). verify audits the token, then as the agent runs `nb-ssh` to an agent-managed guest (reaches it as `agent`) and to the router (refused), and finds that command in Loki under the uid that ran it; all SKIP until the operator has a credential.
- `src/nullbill/ci.py`: the runner dispatcher's PVE identity. Pool services' golden templates sit in no pool (VM.Clone only); clones go into the `nb-ci` pool at `clone_vmids`, which the dispatcher's token may create, start, stop, set the SMBIOS serial of and destroy. apply bakes each template, so OpenTofu ignores its `template` flag.
- `src/nullbill/tofu.py`: OpenTofu JSON for bridges, images and guests (bpg/proxmox); `tools/check-tofu` runs `tofu validate` with the pinned provider in a container and checks that state is encrypted.
- `src/nullbill/verify.py`: allowed and denied flows probed from one container per zone and from the LAN; SKIP when nothing can run, never PASS.
- `src/nullbill/pveapi.py`: API calls from inside a guest with the token it holds, and `audit`, which passes only when a token holds exactly the expected privileges. `agent.py` and `ci.py` audit their tokens with it; `forge.py` checks Forgejo (HTTPS, no sign-up, nothing anonymous, TLS to Postgres) from inside its guests.
- `src/nullbill/artifacts.py`: `apply --dry-run` output, one `build/<machine>/` per target.
- `src/nullbill/apply.py`: apply's stages, run in order through a `Reach` (ssh to the host, `ansible-playbook` with a temporary 0600 inventory and vars file). Containers via `community.proxmox.proxmox_pct_remote`, VMs as `nullbill` with the host as ProxyJump. `src/nullbill/secretfile.py`: `$NULLBILL_HOME/secrets.sops.yaml` through sops and age, saved as soon as a secret is made. `tools/check-apply` runs the real Reach against a host container (fake `pct`) and a VM container; `tests/test_apply.py` uses a recording Reach.
- `tools/check`: every check CI runs. Run it before pushing. In a TLS-intercepting sandbox set `EXTRA_CA_FILE` to the proxy CA bundle.
- Skipping CI: a line `CI-Skip: tofu, firewall` in the PR body leaves out those container checks (`apply install firewall guests host roles tofu`, or `docker` for all); editing the body re-runs CI. Lint, tests, gitleaks and the deny-list hook always run, an unknown name fails the run, and pushes to main run everything. Locally, `CI_SKIP=docker tools/check`. Skip only what the diff cannot touch, such as `docker` for a docs-only change.

## Privacy rules (public repo)

- Never commit secrets, real hostnames, IPs, MACs, usernames, emails, serials,
  licence keys or real probe output. Use `example.internal`, RFC 1918 or
  RFC 5737 addresses, placeholder names and synthetic hardware.
- Owner data lives in `$NULLBILL_HOME` outside the repo: `config.yaml`,
  `probe/`, `plan.yaml`, `denylist.txt`, later tofu state and sops files.
- The pre-push hook fails without a non-empty `$NULLBILL_HOME/denylist.txt`.
- When unsure whether something identifies the owner, leave it out and ask.

## Commits and PRs

- Author commits with the owner's GitHub noreply address; no
  `Co-Authored-By`, `Claude-Session` or "Generated with Claude Code" lines.
  `.claude/settings.json` turns that attribution off.
- Commit messages state the change and the reason.
- No stubs committed as done. Unfinished work is an issue. No figures that a
  script in this repo cannot reproduce.

## Working with the owner

- Questions go in `OPEN_QUESTIONS.md`, each with a one-line recommendation.
- Owner approval before anything public, anything that spends money, or any
  tool change (with an ADR draft in `docs/adr/`).
- Nothing touches real hardware without the owner's go for that milestone.
