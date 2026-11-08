# ADR 0003: How the runtime agent is confined

Status: accepted, widened by the owner on 2026-11-08 (below)

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
| Network reach: PVE API, CA, Loki, Prometheus, the NullBill page, SSH on agent-managed guests; model API and packages through the proxy | pve-firewall and nftables from the one flow list; Squid's per-guest allow-list | `flows.py`, `proxy.py` |
| apply and destroy | Need the owner's API credential and state passphrase, which only the controller holds | `tofu.py`, `$NULLBILL_HOME` |
| Starts only `nullbill verify` with the owner's credential, and reads only verify's output | The page's agent API takes the agent's token from the operator's address only and offers nothing else; the page keeps the token's hash | `web/server.py`, `nb-page` |
| Every command on record before it runs | sshd `ForceCommand` and a managed `PreToolUse` hook write to journald, failing closed; journald stamps the uid | `agent_ssh`, `operator` |
| A record the agent cannot alter | Loki behind a gate that takes pushes only with a shipper certificate (CA key on the controller) and refuses deletes | `monitoring`, `log_shipper` |

The agent runs `nullbill verify` through the page (`nb-page verify`):
verify needs the owner's SSH access to the host, which the agent must not
hold, so the page runs it and returns its output. verify only reads and
probes; the agent cannot pass it arguments or start any other job. A
prompt-injected agent can at worst run verify repeatedly, one at a time.

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

## 2026-11-08: the owner gives the agent root on the host

The owner wants the agent to diagnose, update NullBill and update the host
and services without them at a shell. Offered three ways (only through the
page's API, root on the host, or the owner approving each action), they
chose root on the host. The limits above stay as the default and hold
whenever the switch is off.

While the owner's switch on the page is on:

| What | How | Where |
| --- | --- | --- |
| Root on the PVE host | The page writes one `authorized_keys` line for the agent's key (made in the operator, never copied out), accepted from the operator's address only, through `command="nb-agent-command"`; the host firewall lets the operator reach port 22 | `hostroot.py`, `operator`, `host_baseline`, `catalog.yaml` |
| Setup and updates through the page | `/api/agent/apply` (all or one stage) and `/api/agent/upgrade` for the operator's token; remote agents never get them | `web/server.py`, `nb-page` |
| The owner hears of each | The page sends each job the agent starts, and how it ended, to the owner's ntfy topic; a job it cannot announce does not start | `web/server.py` |
| NullBill moves only forward along `main` | `nullbill upgrade` fetches `main` and refuses local changes or going back | `upgrade.py` |
| Fixes reach NullBill only by pull request | The agent's GitHub token is the owner's fine-grained token for this repository; its rules say never push to main; the pre-push hook runs gitleaks and a deny-list that apply makes from the probe and config | `operator`, `apply.agent_denylist` |
| Still on record | Every host command goes to the journal before it runs (`{job="commands", host="pve"}` in Loki), failing closed; auditd records root's execs | `nb-agent-command`, `log_shipper`, `host_baseline` |
| Cut off at once | Turning the switch off takes the line out first; the switch is off even if that fails, and the page says to stop the operator | `web/server.py` |

Consequences:

- A prompt injection that the model follows can now do anything root can
  on the host: destroy guests, read every secret (the NullBill container's
  too, through `pct`), wipe disks.
  The rules ask the agent to ask the owner before anything that loses data,
  but that is the model's cooperation, not a control.
- Root can also rewrite the host's journal and stop its log shipper, so the
  record of host commands is complete only up to the moment an agent that
  set out to hide something stops it. Loki keeps what reached it.
- verify shows `the agent has no root on the PVE host` as DEGRADED while the
  switch is on, so it is never mistaken for the confined default.
- The PVE token, agent SSH certificates and the checks that prove their
  limits are unchanged; they still bind remote agents and the operator's
  token.
