# ADR 0004: One throwaway VM per CI job

Status: proposed, awaiting owner review

## Context

M5 runs Forgejo Actions for code the owner, people with the `default` role,
and the agent push. A job is arbitrary code. It must not see another job's
files or credentials, must not keep anything between jobs, and must not
reach anything a build has no business with. A long-lived runner with a
Docker socket fails all three, because a job that escapes its container
owns the runner and every later job.

## Decision

Each job runs on its own linked clone of a golden VM, which is destroyed
when the job ends.

| Piece | What it does | Where |
| --- | --- | --- |
| Golden image | Debian with `forgejo-runner` (pinned by checksum) and a boot unit. apply bakes it, then converts it to a PVE template. | `ansible/roles/ci_runner`, `tofu.py` |
| Dispatcher | Polls Forgejo for waiting jobs. Per job, it registers an ephemeral runner, clones the template into the `nb-ci` pool with a random one-time nonce in the SMBIOS serial, and starts the clone. It destroys the clone when it stops. | `ansible/roles/runner_dispatcher` (`nb-dispatcher`) |
| Handoff | At boot the clone reads its serial and fetches its registration once over TLS (`:8443`). It runs exactly that job (`one-job --handle`) as the unprivileged `runner` user, then powers off. | `nb-runner-job` |
| PVE token | Clone the templates; create, start, stop, set the serial of, and destroy VMs in `nb-ci`; disk on the guest storage; the pool zones' bridges. Nothing on `/` or any other guest. | `ci.py`, `tofu.py` |
| Network | Clones live in `build`. They reach Forgejo on 443, the dispatcher on 8443, and the internet only through the egress proxy. The dispatcher, outside `build`, alone reaches the PVE API. | `catalog.yaml`, `flows.py` |
| Limits | Clones that never fetch their job are stopped after 5 minutes. A pool has a time limit per job and a fixed VMID range no larger than the zone's DHCP range. | `nb-dispatcher`, `plan.py` |

Alternatives considered:

- **Forgejo Runner in daemon mode with Docker or LXC on one VM.** It is
  simpler, but jobs share a kernel and a host that persists.
- **Pass the registration through cloud-init.** PVE stores cloud-init
  values in the VM config, where anything with `VM.Audit` reads them, and
  that includes the agent. The serial holds only a nonce, which is useless
  once fetched.
- **Let the clone register itself with a shared token.** A job could then
  read the token and register runners of its own.

## Consequences

- A job starts after a clone boots (tens of seconds) instead of at once.
- Clones are as many as the plan sizes for (`count`). Further jobs wait.
- The golden image changes only when apply re-bakes it. Job-time tool
  installs come through the proxy's allow-list.
- The dispatcher's container holds a Forgejo admin token (scope
  `write:admin`). It sits outside `build`, takes no job code, and its only
  port is the handoff.
- `verify` proves the token's grants and the dispatcher's handoff on a
  deployed host. The dispatcher's container check runs a real job through
  a real Forgejo with a stand-in PVE API. Real clones need a PVE host.
