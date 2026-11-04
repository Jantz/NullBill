# NullBill

NullBill turns a spare x86 machine running Proxmox VE into a self-hosted dev
server for agentic workflows: coding agents, Linux and Windows CI, model
serving and services, from one declarative config. Running it never requires
a recurring payment; anything paid is opt-in and listed by the plan.

Status: milestones 2 and 3 are rendered and container-checked;
`docs/milestones/m2.md` and `m3.md` list what still needs a host.
`nullbill probe`, `nullbill plan` and `nullbill apply --dry-run` work
against synthetic fixtures, and `nullbill verify` reports SKIP until there
is a host to probe. The dry run renders the pve-firewall, router,
egress-proxy, tailnet policy and OpenTofu files; `ansible/` holds the host
baseline and the step-ca, Postgres, monitoring and PBS roles. None has run
on real hardware, and nothing applies changes to a host.

## Use

Run on a controller, never on the PVE host. Python 3.11 or newer.

```sh
pip install -e .
export NULLBILL_HOME=~/nullbill-home   # private, backed up, outside this repo
nullbill probe                          # read-only commands over SSH
nullbill plan                           # writes $NULLBILL_HOME/plan.yaml
nullbill apply --dry-run                # renders host files into $NULLBILL_HOME/build
nullbill verify                         # PASS/FAIL/SKIP per flow; exits 1 on FAIL
```

`$NULLBILL_HOME/config.yaml` names the host and what to plan for:

```yaml
target:
  ssh: root@pve.example.internal
intent:
  workloads: [agents, linux-ci, windows-ci, windows-dev, inference]
people:                                  # Tailscale logins; one owner is required
  owner-a: {role: owner, login: owner-a@example.internal}
  dev-b: {role: default, login: dev-b@example.internal}
overrides:
  linux-runner: {count: 2}
disks:
  ata-EXAMPLE_DISK: {use: pbs, wipe: true}   # required before a disk with signatures is formatted
gpus:
  "0000:01:00.0": {owner: host-shared, consumers: {inference: 20000}}
```

Every value in `src/nullbill/data/catalog.yaml` can be pinned the same way.
`src/nullbill/data/schema.yaml` is the reference for each key. The
`tests/golden/` plans show what `plan` prints for each hardware class.

## Develop

```sh
tools/install-hooks          # gitleaks and the deny-list on commit and push
tools/check                  # lint, tests, secret scan, config parsers (need docker)
```

Licensed under Apache-2.0.
