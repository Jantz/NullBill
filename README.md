# NullBill

NullBill turns a spare PC into a private server for software work: a Git
server with CI, coding agents that run on the server, and the monitoring
and updates that keep it all healthy. You install Proxmox VE on the PC, run
one command, and finish the setup in a web page. NullBill builds the rest.

Everything it installs is free software, and running it never needs a
subscription. Anything paid (an AI model API key, for example) is your
choice and the plan lists it.

**Status.** Every part has been built and tested against stand-ins:
containers that play the Proxmox host, the guests and the services. None of
it has run on real hardware yet. The [first install](docs/runbooks/first-install.md)
is that first run.

## What you get

- **Forgejo**, a Git server like a small GitHub, with CI. Each CI job runs
  in a fresh copy of a virtual machine that is deleted when the job ends.
- **Coding agents** (Claude Code) that run on the server in a locked-down
  container. Every command an agent runs is logged where the agent cannot
  change or delete it.
- **Monitoring**: Prometheus and Grafana for metrics, Loki for logs, and
  alerts on your phone through the free ntfy app.
- **Updates**: every Linux system installs security updates each night.
  You get an alert when something waits for you.
- **An on-site screen**, optional: the setup page on a monitor plugged into
  the server.

## How it works, in short

- **One machine, many small guests.** [Proxmox VE](https://www.proxmox.com/en/proxmox-virtual-environment)
  is a free system for running containers and virtual machines. NullBill
  gives each service its own guest, so a bug or break-in stays in one.
- **Separate networks.** Guests sit in zones (management, services, build,
  and so on). The zones have no direct link to your home network. A small
  router guest connects them, and the firewall blocks every connection that
  is not on one list ([`flows.py`](src/nullbill/flows.py)).
- **No open ports.** You reach the server from anywhere through Tailscale, a
  free VPN that works behind a home router and a changing IP address.
  Nothing is exposed to the internet.
- **Agents get little power.** An agent can restart and snapshot only the
  guests it is given, and log into them as a normal user with an SSH
  certificate that lasts at most an hour. It cannot touch the router, the
  backups, the certificate authority or NullBill itself.
- **Sized to your hardware.** NullBill reads the hardware first, then plans
  what fits. A plan that does not fit fails with the reason, before anything
  is changed. On 16 GB of memory it fits the services, the agents, one
  Linux CI runner, a local AI model on the graphics card and a Windows
  desktop.
- **Checked, not assumed.** `verify` tries every allowed connection and
  every forbidden one from inside each zone. Allowed must work, forbidden
  must fail. A check that cannot run says SKIP, never PASS.

The design records in [`docs/adr/`](docs/adr/) explain each choice and what
it costs. [ADR 0006](docs/adr/0006-nullbill-on-the-server.md) describes the
setup page and remote access.

## What you need

- A spare x86 PC on your home network, with
  [Proxmox VE](https://www.proxmox.com/en/downloads) 9 installed (PVE 8 is past its end of life).
  A second, empty disk is needed only for backups. A disk from your old
  PC can go in as it is: NullBill keeps its files and shares them to you,
  and can later copy them onto the server before it reuses the disk for
  backups or as extra storage
  ([legacy-disk.md](docs/runbooks/legacy-disk.md)).
- Another device with a web browser, on the same network.
- On your phone: the free [ntfy](https://ntfy.sh/) app, for alerts.
- For coding agents: an Anthropic API key, or a Claude subscription token
  (`claude setup-token` on a computer where you use Claude Code).
- Optional: a free [Healthchecks.io](https://healthchecks.io/) check. It
  tells you when the server goes silent, for example after a power cut.

## Install

1. In the Proxmox web page, select the node, open **Shell** and run:

   ```sh
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Jantz/NullBill/main/install.sh)"
   ```

   It creates a small `nullbill` container on the server and prints a web
   address, a certificate fingerprint and a one-time setup code.
2. Open the address. Your browser warns about the certificate; check that
   the fingerprint matches, then continue. Enter the code and choose a
   password.
3. Follow the page from top to bottom: look at the hardware, choose what to
   run, check the plan, set up alerts, paste the agent's key and, to reach
   the server from anywhere, a Tailscale auth key ([docs/runbooks/vpn.md](docs/runbooks/vpn.md)).
4. Press **Run setup** and watch the log. The first run downloads images
   and packages and takes a while.

[docs/runbooks/first-install.md](docs/runbooks/first-install.md) has the
same steps in more detail, and what to do when a step fails. Every step is
safe to run again.

## What Run setup does

It runs these stages in order and stops at the first one that fails.

| Stage | What it does |
|---|---|
| render | Writes every config file from the plan. |
| host | Hardens Proxmox: SSH by key only, audit log, the free update source, nightly security updates, the firewall. Sets up the on-site screen if chosen. |
| tofu | Mounts any disk you kept from an old PC (or copies a kept disk onto the server and checks the copy before reusing the disk), then creates the zone networks and every container and VM ([OpenTofu](https://opentofu.org/)). |
| router | Sets up the router guest: routing, firewall and DNS between zones. |
| proxy | A web proxy that lets build guests reach only listed sites (package mirrors, GitHub and similar). |
| updates | Nightly security updates and update monitoring on each guest. |
| ca | A private certificate authority, for the services' HTTPS and the agents' SSH certificates. |
| postgres | The database Forgejo uses. |
| monitoring | Prometheus, Grafana, Loki and the alerts to ntfy. |
| forgejo | The Git server. |
| files | With a kept disk: shares its files to you over SMB 3, encrypted, with one password. |
| ci | Builds the CI runner image, then the service that gives each job its own copy. |
| agents | The agent's container and CLI, and its SSH access to the guests it may manage. |
| logs | Sends agent commands and SSH logins to Loki. |
| backups | With a second disk: Proxmox Backup Server on it, a first backup of every guest, then one each night, encrypted before it leaves Proxmox. |
| vpn | Joins your Tailscale network, once you paste an auth key. |
| verify | Checks every allowed and forbidden connection. |

## After setup

- **Change something**: change your choices on the page and press **Run
  setup** again. Only what changed is changed.
- **Alerts** arrive in ntfy: a guest that stops answering, a nearly full
  disk, an expiring certificate, updates that fail or wait, a guest whose
  nightly backup did not happen, a kept disk that is missing, a server
  that restarted, and (with
  Healthchecks.io) a server that went quiet.
- **Updates**: guests install security updates each night and restart at
  04:30 when one needs it. Proxmox's own packages and restarts of the
  server itself wait for you, with an alert while they wait.
- **Use the coding agent**: [docs/runbooks/agent-access.md](docs/runbooks/agent-access.md)
  shows how to start it, read what it did, and cut it off.
- **More step-by-step guides** are in [`docs/runbooks/`](docs/runbooks/):
  adding a person, CI runners, restoring a guest, keeping a disk from your
  old PC, and keeping the keys you must not lose.

## Not done yet

- Windows CI runners. Local AI models run one model on one NVIDIA card
  (several cards split it); vLLM and paid cloud models behind the gateway
  are not built.

[`OPEN_QUESTIONS.md`](OPEN_QUESTIONS.md) lists the decisions still open, and
[`docs/milestones/`](docs/milestones/) what each milestone finished.

## Command line

The page runs the `nullbill` command for you. You can also run it yourself,
from the `nullbill` container or from another Linux machine with SSH access
to the server, never on the Proxmox host itself. It needs Python 3.11 or
newer; [docs/runbooks/controller.md](docs/runbooks/controller.md) sets up
the rest.

```sh
pip install -e .
export NULLBILL_HOME=~/nullbill-home   # your private settings and secrets; keep it out of this repo
nullbill probe            # read the server's hardware over SSH; changes nothing
nullbill plan             # decide what fits; writes $NULLBILL_HOME/plan.yaml
nullbill apply --dry-run  # write the config files to $NULLBILL_HOME/build; changes nothing
nullbill apply            # build it, stage by stage
nullbill verify           # PASS, FAIL or SKIP for each check; exits 1 on FAIL
```

`$NULLBILL_HOME/config.yaml` says which server to use and what to run. The
page writes it for you. By hand it looks like this:

```yaml
target:
  ssh: root@pve.internal
intent:
  workloads: [agents, linux-ci]
people:                                  # Tailscale logins; one owner is required
  owner-a: {role: owner, login: owner-a@example.internal}
  dev-b: {role: default, login: dev-b@example.internal}
overrides:
  linux-runner: {count: 2}
disks:
  ata-EXAMPLE_DISK: {use: pbs, wipe: true}   # NullBill never formats a used disk without this
  ata-EXAMPLE_OLD: {use: legacy}             # keep its files as they are and share them, read-only
  ata-EXAMPLE_BIG: {use: storage, wipe: true} # an LVM-thin storage of its own (nb-data)
```

Every default in [`catalog.yaml`](src/nullbill/data/catalog.yaml) can be
changed the same way, and [`schema.yaml`](src/nullbill/data/schema.yaml)
describes each key. The plans in [`tests/golden/`](tests/golden/) show what
`plan` prints for a small, a mid-size and a large machine.

## Develop

```sh
tools/install-hooks   # secret and deny-list checks on commit and push
tools/check           # everything CI runs (needs docker)
```

[`CLAUDE.md`](CLAUDE.md) maps the code and
[`CONTRIBUTING.md`](CONTRIBUTING.md) has the rules for a change. Licensed
under Apache-2.0.
