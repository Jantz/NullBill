# ADR 0013: Workloads that stay installed and run on demand

Status: accepted (the owner asked for it on 2026-11-09: "services can remain
installed but unloaded on demand. Currently my RAM is limiting me", "very
smart and customizable", "some things should be auto turned off if possible,
while others should require user input"; the design decided by Claude under
his "I trust you to make good design choices")

## Context

The owner's server has 16 GiB. The plan counted every planned guest as
running all the time (VMs in full, containers at
`host.container_share_pct` of their limit), so the Windows desktop, local
models, Jellyfin, CI and the workspace could not all be installed, although
he never uses them all at once. Unticking a workload deletes its guest and
what is on it.

What exists to build on: every guest is a PVE container or VM that
`pct`/`qm` can start and stop in seconds; guests already carry
node_exporter and the updates role; the host already runs small NullBill
scripts with systemd timers and exports metrics through node_exporter's
textfile collector; the page already runs commands on the host.

What was checked (2026-11-09, in containers and against stand-ins; no real
hardware):

- step-ca certificates last 24 h and are renewed with themselves
  (`step ca renew --daemon`), so a guest that was off for a day comes back
  with an expired certificate it cannot renew.
- `ss -Htn state established '( sport = :P )'` lists a guest's connections
  from other machines; blackbox and node_exporter scrapes are over in
  milliseconds and are not on the service's ports (9100 is left out).
- Jellyfin's `/Sessions?activeWithinSeconds=` tells a device that plays
  (`NowPlayingItem`) from one that is merely open, and `/ScheduledTasks`
  shows a library scan in progress. Its own API key makes no session.
- nginx logs nothing for a TLS handshake without a request, so the model
  gateway's access log moves only when someone asks the model something.
- `smbstatus -b` lists connected SMB sessions.
- A container's memory limit is not a reservation: a container that idles
  takes a fraction of it. A VM takes what it has touched, and
  `MemAvailable` still counts the rest of its memory as free.

## Decision

**Per workload, not per guest.** A workload is what the owner ticks, and
its guests only make sense together (the model and its gateway, the
dispatcher and its pool). `power.<workload>` in `catalog.yaml`, pinned in
`config.yaml`:

- `run: always | on-demand`. Core guests always run. Everything else may
  run on demand; the catalog's default is `always`, so nothing changes for
  a server that has the memory.
- `stop: auto | ask | manual`, with `idle_minutes`. `auto` stops by itself
  once it has gone that long without use. `ask` never stops by itself: the
  owner gets one alert once it has gone that long, and decides. `manual`
  runs until stopped. Defaults: Jellyfin, local models, file shares and CI
  `auto` after 30 min; the workspace `ask` after 2 h (an agent's or a
  person's work in a shell is not something a script should judge done);
  the Windows desktop `ask` after 4 h (nothing inside Windows tells
  NullBill whether it is in use, and it may hold unsaved work).
- `busy.cpu_pct`: optionally, CPU use that counts as use. The only signal
  a VM gives, so `stop: auto` on a VM-only workload is refused without it.

**The plan counts differently, and says so.** Guests that always run are
counted as before. What is on demand shares what they leave (`power.
headroom_mib`); the plan only asks that each on-demand workload fits there
by itself, counted as the plan counts its guests. `power.together` says
whether all of them fit at once. Pools still grow only from what is left
with everything running.

**One arbiter, on the host: `nb-power`.** A script `host_baseline`
installs, with the rules apply renders from the plan
(`/etc/nullbill/power.json`) and a timer every minute. On the host because
that is where `pct` and `qm` are, and so that stopping idle guests does not
depend on the page, the controller or the network being up.

- `start <workload>`: starts it if it fits. If not, workloads that are
  `auto`, idle and not kept on are stopped to make room, longest idle
  first. If that is not enough, nothing changes and the answer names the
  fewest workloads that would have to stop (exit 3); the caller may come
  back with `--stop a,b` once the owner agreed. A busy workload, or one
  that is `ask` or `manual`, is never stopped without that.
- `stop`, `keep on|off` (not stopped for idleness and nobody asked, until
  it is next stopped), `status [--json]`.
- `tick`: sees what is in use, stops what its rule lets it stop, marks what
  to ask about, writes metrics. A guest started around it (in Proxmox) is
  seen at the next tick and managed like the rest.
- Guests are shut down cleanly. A container that does not finish in two
  minutes is stopped (a game server saves its world on the way down); a VM
  never is, since its disk may be mid-write, and the failure is reported.
- On start, a container whose certificate ran out gets a new one with a
  one-time token from the CA guest (passed on stdin), and apt is told to
  catch up at once: a guest that is only ever on for an hour would
  otherwise never reach its nightly security updates.

**Use is what the guest says.** Every Linux guest has `nb-busy` (the
updates role): SSH sessions, tmux or screen, apt at work, and the
programs in `/etc/nullbill/busy.d`, each printing a line per reason. A
service's role puts one there that knows what use of that service is:
Jellyfin (a device playing or open in the last five minutes, a scan
running), the model gateway (a request in the last two minutes, or a held
connection), Samba (a connected session), a game server (a player
connected, or the server not answering yet). Without one, a connection from
another machine to the service's own ports counts. A pool is in use while
a clone runs. A probe that fails or hangs counts as use: a guest is never
stopped because it could not be asked. This is also the contract for a new
workload such as a game server: a `power.<workload>` entry (or none, for
`ask` after 2 h) and, if it should stop by itself, a program in `busy.d`;
a UDP-only service must ship one, since connections are counted on TCP.

**OpenTofu makes the guests and leaves them off.** On-demand guests get
`started = false`, no start at boot, and `started` in
`lifecycle.ignore_changes`, so OpenTofu neither starts nor stops them
later.

**Setup starts what it must configure, and puts it back.** apply reaches a
guest through `nb-power ensure <guest>..` first, which starts it, begins a
hold (nothing is stopped for idleness while setup runs) and remembers what
was off. `setup end`, in apply's `finally`, stops again what was off and
nobody took up; a hold whose run died ends by itself after 6 h. `ensure`
uses the counted memory first; past it, it stops workloads this run
started earlier and is done with, then goes by what the host really has
free (minus what running VMs have not touched yet), keeping 512 MiB spare,
and otherwise stops the run with what to stop. Where the plan says not
everything fits at once, a playbook for many guests runs on the always-on
guests first and then on each on-demand workload by itself.

**Checks and alerts know what is off on purpose.** verify asks
`nb-power status` and reports SKIP, with the reason, for every check from,
to or inside a guest that is off; it never starts one. It checks the
timer, that the host's rules are the plan's, that no on-demand guest
starts at boot, and that what runs fits (DEGRADED otherwise).
`TargetDown` and the certificate alerts leave out guests `nb-power`
reports off; the update alerts give a started guest six hours. New
alerts: `OnDemandLeftOn` (the `ask` rule's one message),
`OnDemandOverbooked`, `OnDemandNotWatched`.

## Alternatives considered

- **Memory ballooning or lower limits instead of stopping.** A container's
  limit is already not a reservation; what the plan needed was to stop
  counting guests that do not run. A stopped guest also frees its VRAM
  (the model) and cannot be reached, which a throttled one does not.
- **Deciding in the page's server.** It has the owner's clicks, but it runs
  in a container without `pct`, and a restart or an update of the page
  must not leave a guest running all night. The page asks `nb-power`.
- **Marks of each playbook's last run kept outside the guest**, so setup
  need not start a guest to see that nothing changed. A guest restored
  from a backup, or made again by hand, would be taken for up to date.
  Setup starts it; a container is up in seconds.
- **Skipping a workload setup cannot start**, instead of stopping the run.
  Later stages assume earlier ones ran; a run that says done and is not is
  worse than one that says what to stop.
- **llama.cpp's own idle unload.** It frees VRAM but not the container's
  memory, and only helps one workload.

## Not done, on purpose

- **Start on first use** (a connection to a workload that is off starts
  it). It needs something that always listens in the workload's place (the
  router, for TCP; each protocol waits differently for the minute a start
  takes: SMB and RDP clients time out, an OpenAI client can retry).
  `OPEN_QUESTIONS.md` has the recommendation: build it for the model
  gateway and CI first, where a client or a queue waits by design.
- **Schedules** ("the desktop on weekdays 9 to 17"). `start` and `stop`
  are commands a timer can run; no page for it yet.
- **PBS on demand around the backup window.** PBS is core, and its
  garbage collection and verify jobs have their own schedules.
- **CI clones are not admitted one by one.** The workload counts all its
  clones while its dispatcher runs.

## Consequences

- A 16 GiB server can hold every workload the plan offers and run the ones
  in use; the plan stops refusing what does not run together.
- A setup run takes longer by the starts of what was off, and its checks
  skip what it did not need to start.
- A guest that is off gets no security updates until it starts; it also
  answers nobody. It catches up on start.
- The page (ADR 0010's server) asks `nb-power` over SSH as root, as it
  already does for the agent's switch: `status` when the Services page is
  open, `start`, `stop` and `keep` on the owner's press. A start that does
  not fit and a stop of something in use come back as questions; only the
  owner's yes sends them again. The page's agent may run `nb-power`
  through its gate (`status` reads, the rest is a change).
- Not checked on real hardware: how long the model takes to load after a
  start, Quick Sync after a container restart, a Windows VM's clean
  shutdown through the guest agent, and the nightly backup of a VM that is
  off (vzdump starts it paused for the copy; nb-power would see it as
  running for those minutes). The owner's first on-demand run is that
  test.
