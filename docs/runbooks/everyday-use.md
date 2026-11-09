# Use and manage the server

The **Services** page, in the setup page's sidebar, lists each service
the plan runs with its address, an **Open** button, the user to sign in
as, **Show password** where setup made the password, and whether its guest
answers monitoring. This guide is the same list for reading away from the
page.

Addresses and guest numbers below are for the default `network.base`
(10.42.0.0/16) and `vmid_base` (2000); **The full plan** in the **Plan**
card on the page's **Setup** page lists the real ones under `guests`
(`ip`, `vmid`). A guest's address is `10.42.<zone index>.<service id>`;
its number is `2000 + <service id>`.

## Where things are

Over Tailscale each guest answers to `<guest>.nullbill.internal` once a
setup run has set Tailscale's DNS ([vpn.md](vpn.md#names)); its address
works too, and the **Services** page shows both.

| What | Name over Tailscale (address) | First sign-in |
|---|---|---|
| NullBill page | `https://nullbill.nullbill.internal:8443` (`10.42.1.2`) | your page password (chosen at first sign-in) |
| Proxmox web UI | `https://pve.nullbill.internal:8006` (`10.42.1.254`) | `root`, realm Linux PAM, the password from the Proxmox installer |
| Grafana (metrics, logs) | `https://monitoring.nullbill.internal:3000` (`10.42.1.14`) | `admin`, **Show password** on the page |
| Forgejo (Git, CI) | `https://forgejo.nullbill.internal`, Git over SSH on `forgejo.nullbill.internal` port 22 (`10.42.2.17`) | `nullbill-admin`, **Show password** on the page; Forgejo asks for a new one |
| Kept disk share | `\\files.nullbill.internal\<share>` (`10.42.1.23`) | `nbshare`, **Show password** on the page |
| Workspace | SSH to `workspace.nullbill.internal` (`10.42.3.19`) | see [Workspace](#workspace) |
| Coding agent | inside the operator guest (2016) | see [Coding agent](#coding-agent) |
| Model gateway (local AI) | `https://10.42.4.21:4000/v1` by address (from the workspace `https://model-gateway.nullbill.internal:4000/v1`) | the API key from **Show key** under **Local AI model** on the **Services** page; see [local-models.md](local-models.md) |
| Jellyfin (media) | `http://jellyfin.nullbill.internal:8096` (`10.42.7.24`) | `nullbill-admin`, **Show password** on the page; see [media.md](media.md) |
| Windows desktop | Remote Desktop to `windows-dev.nullbill.internal` (`10.42.5.22`) | the account you make when installing ([windows-desktop.md](windows-desktop.md)) |

The other guests have nothing for you to open: the router, the internet
proxy (10.42.2.10), step-ca (10.42.1.12), PostgreSQL (10.42.2.13), the CI
dispatcher (10.42.2.18) and the vpn guest. Prometheus and Alertmanager
answer only inside the server; Grafana shows their data.

Your browser warns about the page's, Proxmox's, Forgejo's and Grafana's
certificates until the device trusts NullBill's browser CA: on the page,
**Access**, **Trust this server**, download the root certificate and
install it ([trust-this-server.md](trust-this-server.md)). That root is
limited to your server's addresses and to `nullbill.internal`. Do not add
step-ca's root to your devices' trusted certificates instead: it is not
limited, so a device that trusts it would accept a certificate it issued for
any site.

Forgejo's clone and mail links use `forgejo.nullbill.internal`. If a
device does not resolve it, check that Tailscale's DNS is on there
([vpn.md](vpn.md#names)); the page's Services entry has the address as a
fallback.

## Passwords

Setup makes Grafana's, Forgejo's and the share's passwords and keeps them
in its encrypted secrets; **Show password** on the **Services** page shows each.
Without the page, read the first two in the Proxmox Shell:

```sh
pct exec 2014 -- cat /etc/grafana/admin-password    # Grafana, user admin
pct exec 2017 -- cat /etc/forgejo/admin-password    # Forgejo, user nullbill-admin
```

Keep them in your password manager. Forgejo makes you change its password
at the first sign-in; the page and the file keep the first one, which then
no longer works.

## Every day

- **The page** is where you manage NullBill. Its **Overview** page lists
  what needs you (firing alerts, guests that stopped answering, security
  updates waiting, a request from the agent), then health, services and
  recent runs. **Run setup**, at the top of every page, applies a change
  you made on the **Setup**, **Alerts**, **Agent** or **Access** page and
  replaces certificates that expired while the server was off; the arrow
  beside it has **Run the checks**, which runs every check again, and
  **Update NullBill**, which moves the page and setup to NullBill's newest
  version (press **Run setup** after it). The page installs as an app on your phone once it trusts the
  server (**Access**, **Trust this server**).
- **The agent** sits beside every page (on a phone, the **Agent** tab).
  Ask it why something broke, to update everything, or what to do next.
  It reads logs and runs read-only commands by itself; what it would
  change shows as a card with **Approve** and **Decline**, by the access
  mode on the **Agent** page, **Settings**: **Manual** asks before every
  change, **Auto** lets changes run and asks before risky ones (deleting,
  rebooting; you type a word to approve), **YOLO** asks nothing. Erasing
  disks, deleting backups, reading secrets, silencing the command log and
  touching the NullBill container are refused in every mode. Every action
  is listed under **Agent**, **Log**. After a run fails, it looks into it
  by itself. The top bar shows a run's progress; press it, or open the
  run on the **Activity** page, to see it in full. You no longer need to run
  `install.sh` again for that.
- **A run that hangs**: the **Activity** page says when a run has printed
  nothing for half an hour. **Stop this run** under its progress ends it;
  what it finished stays done. Press **Update NullBill**, in case a fix is
  out, then start it again. A package download it left waiting in a guest
  is ended by the next run.
- **Alerts** come to the ntfy topic the page's **Alerts** page shows. Each
  names what fired; Grafana and the page say more.
- **Proxmox** is for what NullBill does not do: a guest's console, start
  and stop, snapshots and the host's own updates (Proxmox packages update
  by hand: **Updates**, **Refresh**, **Upgrade**, or ask the coding agent
  once it may manage the server). Leave NullBill's guests' network,
  firewall and disks to setup; change them through the page.
- **Grafana**: no dashboards ship yet. **Explore** with **Prometheus**
  (for example `node_memory_MemAvailable_bytes` or `up`) or with **Loki**
  (`{job="commands"}` for every agent command, `{job="auth"}` for SSH
  sign-ins).

## Remove a workload

On the **Setup** page, untick it under **What to run**. Its row turns red
and says what deleting it loses, such as the Windows desktop with Windows
and your files in it; nothing is deleted yet. Press **Save and plan**, then
**Run setup**: the page names the guests the run deletes and asks you to
type `delete` before it starts (**Build** on the Setup page shows the same
under **Delete and run setup**). To keep them, cancel, tick the workload
again and press **Save and plan**. Setup never deletes a guest you did not
confirm: if a run would delete one the page did not name, such as one it
would have to make again after a change, it stops before deleting and asks.

## Turn it off and on

In the Proxmox web UI select the node and press **Shutdown** or
**Reboot** at the top right (or `poweroff` / `reboot` in its Shell). A short
press of the case's power button also shuts it down cleanly. Proxmox stops
the guests first; at the next start it starts NullBill's guests again in
order, and certificates that expired while it was off are replaced by
**Run setup**. Pull the plug only when it no longer answers.

## Forgejo

Setup makes your own Forgejo user, an administrator named after your
Tailscale login (the part before the @), with a password the **Services**
page shows under **Forgejo**; Forgejo does not ask you to change it. Paste
your SSH public key on the page under **Access**, **SSH keys** and press
**Run setup**: the key goes on that user, and a key you take off there goes
too. Keys you add in Forgejo yourself (**Settings, SSH / GPG keys**) stay.
`nullbill-admin` is a spare administrator you do not need. Clone and push
over SSH:
`git clone git@forgejo.nullbill.internal:<user>/<repo>.git` with the hosts
line above. Git over HTTPS from a laptop fails on the certificate. With
Linux CI on, [ci-runners.md](ci-runners.md) says how a repository runs
jobs.

To bring a project you already have: make an empty repository in Forgejo
(**+**, **New repository**, no README), then in your local clone
`git remote add nullbill git@forgejo.nullbill.internal:<user>/<repo>.git`
and `git push nullbill --all`. Or let Forgejo fetch it: **+**, **New
migration**, **GitHub**, the repository's URL and, for a private one, a
GitHub token that may read it; tick **This repository will be a mirror** to
keep it following GitHub.

## Workspace

A Debian container for work and for the coding agent, with nothing secret
on it. It reaches the internet only through the proxy, and only the names
on its allow-list (Debian, PyPI, npm, GitHub and the others under
`proxy.allow` in `catalog.yaml`). apt uses it already; for other tools run
`export http_proxy=http://10.42.2.10:3128 https_proxy=http://10.42.2.10:3128`
first. It takes SSH as `root` with keys only: paste your public key on the
page under **Access**, **SSH keys** and press **Run setup**, then
`ssh root@workspace.nullbill.internal` from your device. Setup keeps those
keys between two `# nullbill:` lines in `/root/.ssh/authorized_keys`; keys
you add outside them stay. `pct enter 2019` in the Proxmox Shell works
without a key.

## Operator agent

For long work there is a second agent in the operator guest. In the Proxmox Shell:
`pct enter 2016`, `su - agent`, `tmux new -s agent`, `claude`. Detach with
Ctrl-b d and come back with `tmux attach -t agent`; it keeps running while
the Shell is closed. What it may do, how to read what it did and how to cut
it off: [agent-access.md](agent-access.md). With the switch **Root on Proxmox** on (the
**Agent** page, **Settings**, **Operator agent**), it
has root on Proxmox and runs setup and updates itself; ask it to "check the
server and fix what you find" or "update everything". Agents on your other
computers get a token under **Agents on other computers** on the same
page.

## Files

Open the share as in [first-install.md](first-install.md) part 13. The share
is read-only unless you chose **Let me change files on it**. It is not
backed up.
