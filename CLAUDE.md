# CLAUDE.md

Guidance for Claude sessions working in this repository.

## What this is

NullBill turns a spare x86 machine running Proxmox VE into a self-hosted dev
server from one declarative config. `nullbill` runs on a controller the owner
names and reaches the host over SSH; it never runs on the host. Only `probe`,
`plan`, `apply --dry-run` and `verify` exist so far.

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
- `src/nullbill/tailnet.py`: Tailscale policy from people and roles, the only per-person gate; plan fails past the free-plan limits in `catalog.yaml`.
- `ansible/`: host baseline (key-only SSH, auditd, no-subscription repo, security-only upgrades); `tools/check-host` runs it twice in a Debian 12 container and checks the result. `docs/runbooks/` holds steps the owner does by hand. Guest roles carry `tests/check.sh`, which `tools/check-roles` runs in a Debian 12 container.
- `ansible/roles/pbs`: Proxmox Backup Server with a backup-only token for the host; its check backs up with client-side encryption, restores, and rebuilds over a kept datastore.
- `ansible/roles/tls_cert`: a step-ca certificate at `/etc/nullbill/tls/<name>.crt|.key`, first from a one-time token apply makes, then renewed by `step ca renew --daemon` (step-ca certificates last 24 h). Service roles (monitoring, postgres) read it from there and refuse to run without it; their checks seed those files from `test_pki`.
- `ansible/roles/forgejo`: Forgejo from a checksum-pinned release behind nginx (HTTPS on 443 with its tls_cert certificate), its own SSH server on 22 (no OpenSSH on that guest), PostgreSQL with `sslmode=verify-full`, no sign-up, Actions on. `nb-forgejo` runs its CLI as `git`. Its check runs the postgres role in the same container.
- `ansible/roles/step_ca` also runs the `agent` provisioner: SSH user certificates for principal `agent` only, at most 1 h, no X.509. `ansible/roles/agent_ssh` makes agent-managed guests accept those for the unprivileged `agent` user and never for root.
- `ansible/roles/operator`: the agent user, its secrets (root-owned, agent-readable), the pinned agent CLI with root-owned managed settings, and `nb-ssh` for agent-managed guests. Its check logs in end to end through the real CA.
- Command log: `agent_ssh` records each agent SSH command (`nb-agent-command`) and the operator's managed hook records each agent CLI tool call (`nb-log-tool-use`) in the journal before it runs, failing closed. `ansible/roles/log_shipper` ships those and sshd's auth lines to Loki with a shipper certificate; the monitoring role's nginx gate takes pushes only with one, and refuses deletes. Trust the `uid` label (journald's), not the message text.
- `src/nullbill/backups.py`: the PBS storage and nightly job the OpenTofu render adds, and verify's backup-age and restore checks.
- `src/nullbill/agent.py`: the runtime agent's PVE pool, role and token; `agent_managed` services join the pool and get SSH from the operator (model.py).
- `src/nullbill/tofu.py`: OpenTofu JSON for bridges, images and guests (bpg/proxmox); `tools/check-tofu` runs `tofu validate` with the pinned provider in a container and checks that state is encrypted.
- `src/nullbill/verify.py`: allowed and denied flows probed from one container per zone and from the LAN; SKIP when nothing can run, never PASS.
- `src/nullbill/artifacts.py`: `apply --dry-run` output, one `build/<machine>/` per target.
- `tools/check`: every check CI runs. Run it before pushing. In a TLS-intercepting sandbox set `EXTRA_CA_FILE` to the proxy CA bundle.

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
