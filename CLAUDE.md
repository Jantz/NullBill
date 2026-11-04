# CLAUDE.md

Guidance for Claude sessions working in this repository.

## What this is

NullBill turns a spare x86 machine running Proxmox VE into a self-hosted dev
server from one declarative config. `nullbill` runs on a controller the owner
names and reaches the host over SSH; it never runs on the host. Only `probe`,
`plan` and `apply --dry-run` exist so far.

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
- `ansible/`: host baseline (key-only SSH, auditd, no-subscription repo, security-only upgrades); `tools/check-host` runs it twice in a Debian 12 container and checks the result. `docs/runbooks/` holds steps the owner does by hand.
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
