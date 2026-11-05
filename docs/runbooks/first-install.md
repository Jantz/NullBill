# First install on the server

The first run on real hardware. Everything below has run against stand-ins
(containers playing the host, guests and services); this is the first time
it meets Proxmox itself, so expect to stop at a stage, fix it, and press
the button again. Every stage can be run again safely.

## Before you start

- Proxmox VE 8 or 9 installed on the server, its web UI reachable from a
  computer on the same network.
- Optional, for the on-site screen: the monitor plugged into the
  motherboard's output, integrated graphics enabled in the BIOS
  (OPEN_QUESTIONS 8).
- On your phone: the free ntfy app.
- If you want coding agents: an Anthropic API key, or a Claude subscription
  token from `claude setup-token` on a computer where you use Claude.
- Optional: a free Healthchecks.io check with a 10 minute period and 10
  minute grace, to hear about power cuts.

## Steps

1. In the Proxmox web UI, select the node, open **Shell** and run:

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
5. **Alerts**: subscribe to the topic shown in the ntfy app, paste the
   Healthchecks.io URL if you made one, and press **Send a test alert**.
6. **Coding agent**: paste the key or token.
7. **Reach it from anywhere** (optional): paste the policy into Tailscale and
   a one-time auth key here, as [vpn.md](vpn.md) describes. Without a key the
   vpn stage waits and the rest still runs.
8. **Run setup**. The log shows each stage: render, host, tofu, router,
   proxy, updates, ca, postgres, monitoring, forgejo, ci, agents, logs,
   backups, vpn, verify. The first run downloads images and packages and
   takes a while; tofu, ci and the first backup are the longest.
9. With backups: make the paper copy of the backup encryption key, as
   [pbs-encryption-key.md](pbs-encryption-key.md) describes.

## If a stage fails

The log names the stage and the playbook or command. Copy the last 50
lines of the log into the chat. Leave out anything you consider private;
the log carries addresses on your network and guest names, not secrets.

## What is not in this run yet

- Closing the LAN: it keeps Proxmox's own web UI and SSH until you have
  reached them over Tailscale (#9).
- Re-baking the CI image on a schedule (OPEN_QUESTIONS 9).
- Shipping the host's own SSH logins to Loki; guests' are shipped.
