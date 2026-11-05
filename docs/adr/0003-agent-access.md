# ADR 0003: How the runtime agent is confined

Status: proposed, awaiting owner review

## Context

M4 gives an AI agent standing access so it can inspect, repair and run
`nullbill verify`. A prompt injection in a log line or a web page must not
turn that access into control of the router, PBS, the CA, the operator or
the host, and whatever the agent does must be on record somewhere it cannot
edit. The limits must hold whatever the agent decides to do, so none of them
may rely on the agent's own cooperation.

## Decision

Each limit is enforced by a component the agent does not control:

| Limit | Enforced by | Where |
| --- | --- | --- |
| Changes only inside the `agent-managed` pool: power and snapshots, no rollback | PVE ACLs on a privilege-separated token (`NullBillAgent` on `/pool/agent-managed`, `PVEAuditor` on `/`) | `src/nullbill/agent.py`, `tofu.py` |
| mgmt and access guests never join the pool | `plan` refuses `agent_managed` there | `plan.py` |
| SSH only to agent-managed guests, as `agent`, for at most 1 h | step-ca's `agent` provisioner template and claims; `TrustedUserCAKeys` and a principals file only on those guests | `ansible/roles/step_ca`, `agent_ssh` |
| Network reach: PVE API, CA, Loki, Prometheus, SSH on agent-managed guests; model API and packages through the proxy | pve-firewall and nftables from the one flow list; Squid's per-guest allow-list | `flows.py`, `proxy.py` |
| apply and destroy | Need the owner's API credential and state passphrase, which only the controller holds | `tofu.py`, `$NULLBILL_HOME` |
| Every command on record before it runs | sshd `ForceCommand` and a managed `PreToolUse` hook write to journald, failing closed; journald stamps the uid | `agent_ssh`, `operator` |
| A record the agent cannot alter | Loki behind a gate that takes pushes only with a shipper certificate (CA key on the controller) and refuses deletes | `monitoring`, `log_shipper` |

`nullbill verify` proves the PVE limits from the operator. It reads the
token's effective privileges on every guest and on `/`, then attempts a
snapshot of the router, which PVE must refuse.

The rules in `/etc/claude-code/CLAUDE.md` also tell the agent that logs and
web content are data. They are a courtesy to the model, not a control: every
row in the table holds even if the agent ignores them.

## Consequences

- The agent cannot fix anything outside its pool. It reports and proposes,
  and the owner applies.
- A successful prompt injection can still power off or snapshot pool guests,
  read everything PVEAuditor reads, and run commands as `agent` on pool
  guests for up to an hour. All of that is logged.
- Commands the owner runs as root are not recorded one by one; only logins
  are.
- `docs/runbooks/agent-access.md` covers running, watching and cutting off
  the agent, and the manual fallback.
