# Your own services: a program NullBill has no built-in support for

NullBill has built-in support for a handful of services. Anything else that
runs on Debian can still run on the server, as a **service of your own**:
NullBill makes it a guest of its own and everything around that guest, and
a **setup script** you can read puts the program in. Why it is built this
way: [ADR 0015](../adr/0015-custom-services.md).

| NullBill does | The setup script does |
|---|---|
| a container (or VM) with an address and a name, `<name>.nullbill.internal` | installs the program |
| the firewall: only the ports you approved, only from whom you approved | configures it |
| a way out through the proxy only, to the usual list plus the names you approved | makes a systemd unit that starts it |
| Debian updates each night, monitoring, alerts, nightly backups | |
| on demand: off until you start it, if you choose | |
| sharing with a person under **Access, People** | |
| checks: every allowed and denied connection, that the script ran, that no unit failed | |

What NullBill does not do for it: update the program, know its sign-in, or
check that it works. A built-in service, where one fits, gets all three.

## 1. Add one with the agent

**You need:** the agent signed in (the **Agent** page), and the name of the
program.

1. On the **Setup** page, in **What to run**, under **Your own services**,
   press **Ask the agent to add one** and say what you want, for example
   "Add a service of my own: Paperless-ngx, for me and my partner".
2. The agent reads the program's install guide and answers with a request
   card. Read it:
   - the lines at the top say what guest it makes, **each port it opens
     and to whom**, and **which names it may fetch from**;
   - below is the **whole setup script**. It runs as root inside the new
     guest, nowhere else.
3. Type the word the card asks for and press **Approve**, or **Decline**
   and say what to change. This waits for you in every mode but YOLO.
4. The service is now a row under **Your own services**, ticked. Press
   **Run setup** (the agent may ask to start it). The stage **Set up your
   own services** makes the guest and runs the script.
5. Open it from the **Services** page. To let someone else use it, tick it
   for them under **Access, People**; that works only for a port the card
   listed as open to "the people you tick it for".

If the script fails, the rest of setup still finishes, and the checks at
the end say `<name>: its setup script ran as it is now` failed. The run's
log has the script's output under **Set up your own services**; the agent
reads it and proposes a changed script, which you approve like the first.

## 2. Look at one, turn it off, remove it

**You need:** the setup page.

- **What it is:** **Setup**, **What to run**, the row's **What it makes,
  needs and deletes**, then **Show its setup script**.
- **Off:** untick it and press **Save and plan**, then **Run setup**, which
  asks once more before it deletes the guest and everything in it. The
  definition and the script stay, so ticking it again builds it anew.
- **Remove:** once it is off and its guest is gone, the row has
  **Remove**. The script is set aside in `custom/.removed` beside
  `config.yaml`, not erased.
- **When it runs:** like every workload, under **When it runs**
  ([on demand](on-demand.md)). A container that listens on no TCP port
  cannot stop by itself: nothing shows NullBill that it is in use.

With many services, type part of a name in **Find by name** above the
lists; long groups show what is on and keep the rest behind **Show more**.

## 3. Write one by hand

**You need:** a shell in the NullBill container (`pct enter <its id>` on
the Proxmox host).

The agent writes two things, and so can you. In
`/var/lib/nullbill/config.yaml`:

```yaml
intent:
  workloads: [agents, linux-ci, navidrome]   # your list, plus the new name
services:
  navidrome:
    id: 64                # 64-127 or 192-253, not used by another service
    kind: lxc             # or vm
    zone: apps            # or games
    workload: navidrome
    floor: {cores: 1, memory_mib: 1024, disk_gib: 8}
    description: Music streaming
    proxy_allow: [musicbrainz.org]          # names beyond the usual list
    share: {title: Navidrome, open: "http://{host}:{port}/"}
    ports:
      - {port: 4533, proto: tcp, from: ["role:owner", "shared"]}
power:
  navidrome: {title: Navidrome, run: on-demand, stop: auto, idle_minutes: 30}
```

and the script at `/var/lib/nullbill/custom/navidrome/setup.sh`. Then
**Save and plan** on the page (or reload it) and **Run setup**.

## 4. What a setup script must do

It runs as root in a fresh Debian 12 guest with no terminal, at most an
hour, and again whenever its text changes (and after a failure).

- Start with `#!/bin/bash` and `set -eu`.
- **Safe to run again:** `apt-get install -y`, `id user || useradd`,
  write files whole, `systemctl enable --now`, then `systemctl restart`.
- **Never ask anything.** `DEBIAN_FRONTEND=noninteractive` is set.
- **The guest has no route to the internet.** apt is set up for the proxy
  already. `curl`, `wget`, `pip`, `npm` and `git` use the proxy from the
  environment (`http_proxy`, `https_proxy`). Only the usual names pass
  (Debian, GitHub, PyPI, npm, crates.io, Docker Hub, ghcr.io, Hugging
  Face) plus the service's own `proxy_allow`. A program that ignores the
  proxy variables needs its own setting for it.
- **Check what you download** when the maker publishes a checksum:
  `echo "<sha256>  file" | sha256sum -c`.
- **End with a systemd unit** that listens on the ports the definition
  lists, on all addresses (`0.0.0.0`), not only `127.0.0.1`.
- **No passwords or keys in the script.** Make them in the guest
  (`openssl rand -hex 24 > /etc/myprogram/secret`, mode 0600) and read
  them there.
- The script can read `NB_SERVICE` (its name), `NB_ADDRESS`, `NB_DOMAIN`
  and `NB_PORTS` (such as `tcp/8000`); a unit gets them with
  `EnvironmentFile=/usr/local/lib/nb-custom/env`.
- For Docker use `kind: vm`: NullBill's containers are unprivileged, and
  Docker in them is untested.

On the guest: the script is `/usr/local/lib/nb-custom/setup.sh`, its
output `/var/log/nb-custom-setup.log`, and `nb-custom-setup again` runs it
once more.

## When something is wrong

| You see | Do this |
|---|---|
| The checks say `its setup script ran as it is now` failed | Open the last setup run under **Activity** and read **Set up your own services**, or ask the agent to. Fix the script, run setup again. |
| The checks say `no unit has failed` is degraded | A unit in the guest failed after setup. `pct enter <id>` on the host, then `systemctl --failed` and `journalctl -u <unit>`. |
| The script cannot download something | The name is not on the allow-list. Ask the agent to add it (you approve the new list), or add it to `proxy_allow`. |
| The page opens but a friend cannot | Tick the service for them under **Access, People**; its port must be open to "the people you tick it for". |
| Save and plan says every id is taken | There are 126 ids for your own services. Remove one you no longer use. |
