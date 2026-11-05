# First install on the server

The first run on real hardware. Everything below has run against stand-ins
(containers playing the host, guests and services); this is the first time
it meets Proxmox itself, so expect to stop at a stage, fix it, and press
the button again. Every stage can be run again safely.

## Before you start

- On your phone: the free ntfy app.
- If you want coding agents: an Anthropic API key, or a Claude subscription
  token: run `claude setup-token` on a computer where you use Claude Code
  with a Pro, Max, Team or Enterprise plan. It prints a token that lasts one
  year; it is not saved anywhere, so copy it then.
- Optional: a free [Healthchecks.io](https://healthchecks.io/) check, to
  hear about power cuts. Sign up, add a check, and set its schedule to a
  period of 10 minutes and a grace time of 10 minutes. Copy its ping URL
  (`https://hc-ping.com/...`). The check stays "new" until the server pings
  it for the first time. Healthchecks.io alerts the address you signed up
  with; its Integrations page adds other channels.
- Optional, for the on-site screen: the monitor plugged into the
  motherboard's output, integrated graphics enabled in the BIOS
  (OPEN_QUESTIONS 8).

## Install Proxmox VE 9

Reference: <https://pve.proxmox.com/pve-docs/chapter-pve-installation.html>.
NullBill needs Proxmox VE 9; version 8 reached its end of life in 2026-08.

1. Download the Proxmox VE 9 ISO installer from
   <https://www.proxmox.com/en/downloads> and write it to a USB stick
   (balenaEtcher or Rufus in DD mode, or `dd` on Linux).
2. Boot the server from the stick and choose **Install Proxmox VE
   (Graphical)**. Accept the licence.
3. **Target harddisk**: pick the system disk. Leave the second disk out: the
   backups stage needs it empty, and NullBill never formats a disk that
   holds data unless you pin it with `wipe: true`. The default file system
   (ext4) is fine.
4. Country, time zone and keyboard layout, then a root password (at least
   8 characters; keep it in your password manager) and an email address.
5. **Management network**: the network card on your home network, a
   hostname such as `pve.home.arpa`, and a fixed IPv4 address outside your
   router's DHCP range (or reserve it in the router). Note the address.
6. Check the summary and press **Install**. Remove the stick when it
   restarts.
7. On another computer, open `https://<that address>:8006`, accept the
   certificate warning and sign in as `root` with realm **Linux PAM
   standard authentication**. Close the "No valid subscription" notice;
   NullBill switches to the free update source for you.

## Steps

1. In the Proxmox web UI, select the node (under **Datacenter** in the tree
   on the left), open **Shell** and run:

   ```sh
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Jantz/NullBill/main/install.sh)"
   ```

   It prints the page's address, its certificate fingerprint and a setup
   code. Takes a few minutes.
2. Open the address, compare the fingerprint, enter the code and choose a
   password.
3. **Look at the hardware**: run it; it only reads.
4. **Choose what to run**: start small (coding agents and Linux CI), leave
   local models and Windows off at 16 GB. Tick backups only if the server
   has a second, empty disk. Check the plan.
5. **Alerts**: in the ntfy app, add a subscription to the topic the page
   shows (server ntfy.sh, no account needed), paste the Healthchecks.io
   ping URL if you made one, press **Save**, then **Send a test alert**.
6. **Coding agent**: paste the key or token. A subscription token lasts a
   year; make a new one and paste it here before then.
7. **Reach it from anywhere** (optional): paste the policy into Tailscale and
   a one-time auth key here, as [vpn.md](vpn.md) describes. Without a key the
   vpn stage waits and the rest still runs.
8. **Run setup**. The log shows each stage: render, host, tofu, router,
   proxy, updates, ca, postgres, monitoring, forgejo, ci, agents, logs,
   backups, vpn, verify. The first run downloads images and packages and
   takes a while; tofu, ci and the first backup are the longest.
9. Once Tailscale works: **Close the home network**, then **Keep it closed**
   from the page opened over Tailscale ([vpn.md](vpn.md)). Until then
   verify reports the open LAN as DEGRADED.
10. With backups: make the paper copy of the backup encryption key, as
   [pbs-encryption-key.md](pbs-encryption-key.md) describes.

## If a stage fails

The log names the stage and the playbook or command. Copy the last 50
lines of the log into the chat. Leave out anything you consider private;
the log carries addresses on your network and guest names, not secrets.

## What is not in this run yet

- Windows and local models: options to add after a RAM upgrade.
