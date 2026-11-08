# Use and manage the server

The page's **Services** card, under **Server status**, lists each service
the plan runs with its address, an **Open** button, the user to sign in as,
**Show password** where setup made the password, and whether its guest
answers monitoring. This guide is the same list for reading away from the
page.

Addresses and guest numbers below are for the default `network.base`
(10.42.0.0/16) and `vmid_base` (2000); the page's step 4, **The full
plan**, lists the real ones under `guests` (`ip`, `vmid`). A guest's address
is `10.42.<zone index>.<service id>`; its number is `2000 + <service id>`.

## Where things are

| What | Address over Tailscale | First sign-in |
|---|---|---|
| NullBill page | `https://10.42.1.2:8443` | your page password (step 1) |
| Proxmox web UI | `https://10.42.1.254:8006` | `root`, realm Linux PAM, the password from the Proxmox installer |
| Grafana (metrics, logs) | `http://10.42.1.14:3000` | `admin`, **Show password** on the page |
| Forgejo (Git, CI) | `https://10.42.2.17`, Git over SSH on `10.42.2.17` port 22 | `nullbill-admin`, **Show password** on the page; Forgejo asks for a new one |
| Kept disk share | `\\10.42.1.23\<share>` | `nbshare`, **Show password** on the page |
| Workspace | SSH to `10.42.3.19` | see [Workspace](#workspace) |
| Coding agent | inside the operator guest (2016) | see [Coding agent](#coding-agent) |
| Model gateway (local AI) | `https://model-gateway.nullbill.internal:4000/v1`, with a hosts line for `10.42.4.21` | the API key from **Show password** on the Services card (or **Show the key** in step 3); see [local-models.md](local-models.md) |
| Jellyfin (media) | `http://10.42.7.24:8096` | `nullbill-admin`, **Show password** on the page; see [media.md](media.md) |
| Windows desktop | Remote Desktop to `10.42.5.22` | the account you make when installing ([windows-desktop.md](windows-desktop.md)) |

The other guests have nothing for you to open: the router, the internet
proxy (10.42.2.10), step-ca (10.42.1.12), PostgreSQL (10.42.2.13), the CI
dispatcher (10.42.2.18) and the vpn guest. Prometheus and Alertmanager
answer only inside the server; Grafana shows their data.

Your browser warns about the page's, Proxmox's and Forgejo's certificates.
Proxmox and the page use their own self-signed certificates; Forgejo's
comes from NullBill's certificate authority (step-ca), which your devices
do not trust, and it names `forgejo.nullbill.internal`, not the address.
Accept the warning for these addresses only. Do not add step-ca's root to
your devices' trusted certificates: the root is not limited to
`nullbill.internal`, so a device that trusts it would accept a certificate
it issued for any site.

Names under `nullbill.internal` do not resolve on your devices yet. Forgejo
shows `forgejo.nullbill.internal` in its clone and mail links; on a laptop,
add the line `10.42.2.17 forgejo.nullbill.internal` to the hosts file
(`/etc/hosts`, or `C:\Windows\System32\drivers\etc\hosts` edited as
administrator) so those links work. Put it below
`# TailscaleHostsSectionEnd` if the file has that section: Tailscale
rewrites everything between its start and end lines.

## Passwords

Setup makes Grafana's, Forgejo's and the share's passwords and keeps them
in its encrypted secrets; **Show password** on the Services card shows each.
Without the page, read the first two in the Proxmox Shell:

```sh
pct exec 2014 -- cat /etc/grafana/admin-password    # Grafana, user admin
pct exec 2017 -- cat /etc/forgejo/admin-password    # Forgejo, user nullbill-admin
```

Keep them in your password manager. Forgejo makes you change its password
at the first sign-in; the page and the file keep the first one, which then
no longer works.

## Every day

- **The page** is where you manage NullBill. **Server status** at the top
  shows firing alerts, guests that stopped answering and guests waiting for
  a security update's reboot. **Run checks** (step 8) runs every check
  again; **Run setup** applies a change you made in steps 3 to 7 and
  replaces certificates that expired while the server was off.
- **Alerts** come to the ntfy topic from step 5. Each names what fired;
  Grafana and the page say more.
- **Proxmox** is for what NullBill does not do: a guest's console, start
  and stop, snapshots and the host's own updates (Proxmox packages update
  by hand: **Updates**, **Refresh**, **Upgrade**). Leave NullBill's guests'
  network, firewall and disks to setup; change them through the page.
- **Grafana**: no dashboards ship yet. **Explore** with **Prometheus**
  (for example `node_memory_MemAvailable_bytes` or `up`) or with **Loki**
  (`{job="commands"}` for every agent command, `{job="auth"}` for SSH
  sign-ins).

## Turn it off and on

In the Proxmox web UI select the node and press **Shutdown** or
**Reboot** at the top right (or `poweroff` / `reboot` in its Shell). A short
press of the case's power button also shuts it down cleanly. Proxmox stops
the guests first; at the next start it starts NullBill's guests again in
order, and certificates that expired while it was off are replaced by
**Run setup**. Pull the plug only when it no longer answers.

## Forgejo

Sign in as `nullbill-admin`, set its new password, then make your own
everyday user under **Site administration, User accounts** and add your SSH
public key to it (**Settings, SSH / GPG keys**). Clone and push over SSH:
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
first. It takes SSH as `root` with keys
only, and setup gave it no key of yours. Add one from the Proxmox Shell,
with your public key in place of the quoted text:

```sh
pct exec 2019 -- sh -c 'mkdir -p -m 700 /root/.ssh && echo "ssh-ed25519 AAAA... you@laptop" >> /root/.ssh/authorized_keys'
```

Then `ssh root@10.42.3.19` from your device. `pct enter 2019` in the Shell
works without a key.

## Coding agent

The agent runs in the operator guest. In the Proxmox Shell:
`pct enter 2016`, `su - agent`, `tmux new -s agent`, `claude`. Detach with
Ctrl-b d and come back with `tmux attach -t agent`; it keeps running while
the Shell is closed. What it may do, how to read what it did and how to cut
it off: [agent-access.md](agent-access.md). Agents on your other computers
get a token on the page's step 7.

## Files

Open the share as in [first-install.md](first-install.md) part 13. The share
is read-only unless you chose **Let me change files on it**. It is not
backed up.
