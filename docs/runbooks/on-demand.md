# Run something only while you use it (on demand)

A workload that runs **on demand** stays installed, with everything on its
disk, and is off until you start it. While it is off it holds no memory, so
a server can have more installed than it can run at once: the Windows
desktop, the local models and Jellyfin on 16 GiB, for example. Why it is
built this way: [ADR 0013](../adr/0013-on-demand.md).

| What | Where |
|---|---|
| Which workloads run on demand, and how each stops | the page's **Setup**, **What to run**, **When they run** |
| Start, stop, keep on; what is in use | the page's **Services**, in each service's row |
| The same from a shell | `nb-power` on the Proxmox host |

How each workload stops again, unless you change it:

| Workload | Stops | It counts as in use while |
|---|---|---|
| Jellyfin | by itself, 30 min unused | something plays, a device asked for something in the last 5 minutes, or a library scan runs |
| Local AI models | by itself, 30 min unused | the gateway answered a request in the last 2 minutes or holds a connection |
| File shares | by itself, 30 min unused | a device is connected to a share |
| Linux CI runners | by itself, 30 min unused | a job's VM runs |
| Minecraft, Terraria, Factorio | by itself, 30 min unused | someone plays, or the server does not answer (it may be making its world) |
| Workspace | only by you; a reminder after 2 h unused | someone is signed in over SSH, or a tmux or screen session is left open |
| Windows desktop | only by you; a reminder after 4 h | nothing in Windows tells NullBill, so it is never judged unused |

In every Linux guest an SSH session, a tmux or screen session and updates
being installed also count as use. When NullBill cannot ask a guest whether
it is in use, it counts as in use: nothing is stopped on a guess.

## 1. Choose what runs on demand

**You need:** the setup page.

1. Open **Setup**, **What to run**, **When they run**. Each
   workload you ticked has a row.
2. Set **Runs** to **On demand** for what you do not need all the time.
   The memory meter at the top moves what it takes out of **Your choices**
   and into **On demand**, the striped part: it shares the memory the rest
   leaves. Each on-demand workload has to fit there by itself. When not all
   of them fit at once, the meter says **Fits, but not all at once**.
3. Choose under **Stops** how it stops again:
   - **By itself when unused** stops it once nobody has used it for the
     minutes beside it.
   - **When I stop it, with a reminder** never stops it; you get one alert in
     ntfy, and a line on the **Overview** page, once it has gone unused
     that long.
   - **When I stop it** runs until you stop it.
4. Press **Save and plan**, then **Run setup**. A workload that was
   running stays on after the run and stops by its rule; one that setup
   makes new is left off.

Core guests (the router, Forgejo, monitoring, the backup server and so on)
always run. A workload that only makes VMs per job has nothing to stop.

## 2. Start and stop

**You need:** the page, from a device on your tailnet.

1. Open **Services**. A service that is off says **Off, on demand** and has
   **Start** where **Open** would be. One that is on says under its status
   why it is in use or when it stops, with **Stop** and **Keep on** beside
   it. Under the list a bar shows how much of the shared memory is taken.
2. Press **Start**. A container is up in well under a minute; the local
   model then loads into the graphics card, and Windows boots.
3. When it does not fit beside what is running, NullBill first stops
   workloads that stop by themselves and are unused. If that is not enough
   it changes nothing and asks you, naming what would have to stop and
   whether each is in use. Nothing in use, and nothing that only you stop,
   is ever stopped without that yes.
4. **Stop** shuts a workload down cleanly; if it is in use the page asks
   first. **Keep on** keeps it from being stopped, and you from being
   reminded, until you stop it.

Stopping deletes nothing. A container gets two minutes to shut down; a VM
is never cut off, and the page says so when Windows does not shut down
(sign in and shut it down, or use Proxmox).

## 3. What changes for something that is off

- **Setup** starts a workload that is off only to configure it, and stops
  it again at the end unless you started using it meanwhile. When not
  everything fits at once it does them one after another. If the server has
  no memory even for that, the run stops and says what to stop first.
- **The checks** skip what is off, with the reason; start it and run them
  again to check it.
- **Alerts** keep quiet about a guest that is off on purpose. New ones:
  the reminder above, more running than the plan counts, and the host no
  longer watching what runs on demand.
- **Updates**: a guest that is off installs none. When you start it, it
  looks for security updates at once and is not stopped while they install.
- **Backups**: Proxmox backs up a stopped guest too.
- **A restart of the server** leaves on-demand workloads off.
- **People** you gave a service to cannot start it; they reach it while you
  have it on. A game server that runs on demand is off once the last
  player has been gone for its minutes, and a friend cannot join until you
  start it again.

## 4. From the Proxmox shell

```
nb-power status                  # what is on, in use, and when it stops
nb-power start media             # as the page's Start; exit 3 when it does not fit
nb-power start inference --stop media,windows-dev
nb-power stop windows-dev
nb-power keep agents on          # or: off
journalctl -t nb-power           # what was started and stopped, and why
```

The names are the workloads': `agents`, `linux-ci`, `inference`, `media`,
`files`, `windows-dev`, `minecraft`, `terraria`, `factorio`. Start and stop
on-demand guests with `nb-power`, not with `pct start` or Proxmox's
buttons: it counts the memory. A guest started in Proxmox anyway is
noticed within a minute and treated like the rest.

## 5. In config.yaml

The page writes `power` in `config.yaml`; only what differs from the
table above is kept there.

```yaml
power:
  media: {run: on-demand, idle_minutes: 60}
  agents: {run: on-demand, stop: manual}
  windows-dev:
    run: on-demand
    stop: auto          # a VM needs busy.cpu_pct for this
    idle_minutes: 120
    busy: {cpu_pct: 15} # CPU of one core, over a minute, that counts as use
```

`busy.cpu_pct` is the one setting the page does not show. It is the only
sign of use a VM gives, and a crude one: someone reading in a Windows
session uses almost no CPU, and a stop closes their programs. Set it only
for a VM where that is acceptable.

## If something is not as expected

| You see | Do |
|---|---|
| **No status** on a service that runs on demand, or "the server does not say what is on" | Run setup: the host gets `nb-power` and its rules with the first run after you chose something on demand. |
| A workload never stops by itself | Its row on **Services** says why it is in use. A phone with the Jellyfin app open, a mounted share or a forgotten SSH session all count. |
| "needs N MiB and does not fit, even with everything else on demand stopped" | The host's rules are older than your choices: run setup. If it stays, let more run on demand. |
| Setup stops with "setup has to start ... to configure it" | Stop one of the workloads it names on the **Services** page and run setup again. |
| A started service answers with a certificate error for a minute | Its certificate ran out while it was off; starting it gets a new one. If it stays, run setup. |
