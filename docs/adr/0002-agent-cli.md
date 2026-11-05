# ADR 0002: Claude Code as the runtime agent's CLI

Status: proposed, awaiting owner review

## Context

M4 puts an agent CLI in the operator container. The agent inspects,
repairs and runs `nullbill verify` with a PVE token limited to the
agent-managed pool and SSH certificates that log in only as `agent`. Those
limits are enforced by PVE, step-ca, sshd and the firewall, so they hold
whichever CLI runs there. The CLI still decides what reaches the internet
and how its own settings are protected.

Options considered:

- Claude Code from npm (`@anthropic-ai/claude-code`). Its current release
  needs Node 22; Debian 12 ships Node 18, so this means a third-party Node
  repository on the operator.
- Claude Code's install script. It is not pinned and fetches from more
  hosts than the model API.
- Claude Code's native Linux binary, from the npm platform package
  `@anthropic-ai/claude-code-linux-x64`. A single file, fetched from
  `registry.npmjs.org`, which is already on the proxy allow-list.
- Another agent CLI. None was named in the goal; switching later changes
  only `ansible/roles/operator`.

## Decision

The operator role installs the native Claude Code binary, pinned by version
and by the tarball's sha512 (`operator_claude_code_*` in
`ansible/roles/operator/defaults/main.yml`).

- Root-owned managed settings (`/etc/claude-code/managed-settings.json`)
  turn off the auto-updater and non-essential traffic, send traffic through
  egress-proxy, and read the API key from a file only root and the agent
  user can read.
- Root-owned managed instructions (`/etc/claude-code/CLAUDE.md`) say that
  logs and web content are data, and that apply and destroy belong to the
  owner.
- egress-proxy forwards `api.anthropic.com` for the operator only
  (`services.operator.proxy_allow` in the catalog), not for other mgmt
  guests.

## Consequences

- Running the agent spends API credit on a key the owner supplies. Nothing
  starts it until the owner does.
- A new release means changing the version and checksum in one file; the
  role refuses a tarball that does not match.
- The CLI is proprietary software from Anthropic, downloaded on the
  operator. Nothing in this repo redistributes it.
