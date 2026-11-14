# ADR 0022: Latest stable, daily, with every version still pinned

Status: accepted (the owner asked for it, 2026-11-13: "I want latest stable
installs that get auto updated by NullBill systems automatically daily.
Upgrade every service not just qBittorrent", and on the one open choice
picked "Small updates auto, big ones ask")

## Context

Two rules were in place and both stay:

- What is not Debian's is pinned: a version with a checksum, a signature or
  a commit, so a changed download fails instead of running.
- Debian's own packages update by themselves every night
  (unattended-upgrades, `ansible/roles/updates`), from Debian's origins
  only.

The result of the first rule was that a pin moved when somebody thought of
it. Forgejo sat two major versions back, guests ran Debian 12 while the
host ran 13, and the download client was the one Debian 12 ships, from
2023. The owner asked why, and then for the opposite: newest stable
everywhere, kept there daily, without him.

"Just install latest at setup time" would give that and lose the first
rule: nobody could say which bytes a server runs, a vendor's bad release
would reach every server the hour it appears, and no check would have
seen the combination before it runs.

What was checked before deciding (2026-11-13), each against the vendor's
own index or file:

- Every pinned program has a place that names its newest stable release
  and gives a checksum, a signature or a commit for it: Forgejo and its
  runner (Codeberg's releases, signed; the signing key's fingerprint is
  in `tools/pins`), Grafana, Loki, step-ca, Tailscale and Jellyfin (their
  apt indexes), LiteLLM (PyPI), llama.cpp and qBittorrent (tags), the
  agent CLI (npm's `stable` tag), Minecraft, Paper, Fabric, Terraria and
  Factorio (the vendors' own lists), Debian's cloud image and Proxmox's
  container template (their checksum files).
- qBittorrent publishes no Linux build of the headless program, only
  source. 5.3.0 builds against what Debian 13 ships (Qt 6.8, libtorrent
  2.0.11, Boost 1.83) in about ten minutes with one compiler in 2 GiB.
  Its API changed since 4.5: `torrents/stop` and `start` for `pause` and
  `resume`, state `stoppedDL`, and a sign-in answered by status alone.
- Debian 13 has what the roles took from elsewhere on Debian 12: Java 25
  for Minecraft (`openjdk-25-jre-headless`), Vulkan headers new enough for
  llama.cpp, PostgreSQL 17, Python 3.13.
- A Debian 12 container with PostgreSQL 15 and data in it upgrades to
  Debian 13 in place with apt's own steps, and Debian's
  `pg_upgradecluster` moves the cluster to 17 (`tools/check-release` does
  this on every CI run).
- The pinned Debian 13 cloud image boots under QEMU with a cloud-init
  seed on a SCSI CD-ROM, as OpenTofu attaches it: the user and key are
  made, and the network comes up through netplan and systemd-networkd
  with no ifupdown installed, which is what the router role expects from
  Debian 12's image.
- NVIDIA's `latest.txt` gave different answers to the same question within
  minutes. The driver is not followed.

## Decision

One mechanism, in three places.

**1. In the repository: every pin moves daily, after every check.**
`src/nullbill/data/pins.yaml` lists each pin: the file and key that hold
it, and how big a step waits for the owner. `tools/pins bump` asks each
vendor for its newest stable release and writes version and checksum into
the place the pin lives. `.github/workflows/pins.yml` runs it once a day,
regenerates what the tests keep equal to the pins, commits to the branch
`pins`, starts the whole of `check.yml` there as a run of that branch
(every role's container check installs and runs the new versions for
real; a run of the branch shares no cache or token with main's runs),
waits for it and only then fast-forwards main. If a check fails, main keeps yesterday's pins and an
issue says so. A vendor that cannot be read keeps its pin; the others
move. Nothing ever moves back.

**2. On the server: main is followed nightly.** Between 03:00 and 06:00
the page looks for an update, installs it (`nullbill upgrade`) and, when
the update changes what setup builds, runs setup. The owner gets one ntfy
message when something was installed or something waits. A switch on the
Updates page turns it off. It does not run setup over choices the owner
saved and has not started, or over guests waiting to be deleted, and it
sets up only an update it installed itself that night: after the owner's
own update, a failed or stopped run, or a setup run that failed on an
earlier night, setup is the owner's to start. The hours are the NullBill
container's local time.

**3. Big steps wait for a yes.** `hold` in the table is the number of
leading version numbers that may not change by themselves: 1 for most
(Forgejo 17 to 18 waits, 17.0 to 17.1 does not), 2 where the second
number is the breaking one (step-ca 0.x), `all` for Terraria and
Minecraft, whose players need the exact version (and every plugin and mod
names one Minecraft; a new Minecraft is not even offered until Paper and
Fabric have a build for it). A held step that
touches a service this server runs stops the night before anything is
installed; the Updates page lists it as "Waits for your yes" and **Update
NullBill** is that yes. A server that does not run the service is not
asked; one whose last run left no plan to tell is asked about every held
step.

Debian in the guests is such a step, moved by hand in the table
(`by_hand`), since it needs work each time:

- New guests are made from Debian 13 images. A guest that exists keeps
  the image it was made from (OpenTofu ignores a newer one), so a new
  image pin never replaces a guest.
- apply's `release` stage, right after OpenTofu, upgrades each guest from
  before in place (`src/nullbill/release.py`,
  `src/nullbill/data/nb-release-upgrade`): a PVE snapshot first, or a
  backup where PVE cannot snapshot (a bind mount), and no upgrade without
  one; packages downloaded before any is installed; a unit of its own
  inside the guest; a restart; then a look at what the guest says it
  runs. A failed upgrade, or a guest that does not answer after its
  restart, puts the guest back from its snapshot and stops the run. A
  guest whose copy is a backup is not put back by NullBill; a run refuses
  to go on while a guest holds an upgrade that began and neither finished
  nor was put back, and leaves its copy alone. Snapshots are deleted a
  week later by the next run.
- Which guests are from before is asked of the guests, never taken from a
  file: a guest OpenTofu makes is written down as new when it is made,
  every other is asked once which Debian it runs.
- Only Debian's packages move in that step. Packages from a vendor's
  repository are held while it runs and let go after, so a vendor's
  newest does not come in past its pin; the role moves it afterwards.
- PostgreSQL 15's cluster is moved to 17 by Debian's `pg_upgradecluster`
  in the same step; the old cluster stays on disk, stopped.
- The container NullBill itself runs in goes last, after the run ended
  well, as a unit of its own (`tools/upgrade-controller`); the page is
  away for about ten minutes. A failure has the host roll it back, with
  the log kept on the host; it is not tried again by itself
  (`controller-release.json`), and its snapshot goes after a week too.
- A CI pool's image is made again from the new image by the ci stage.
- Services of the owner's own stay on the Debian they were made with:
  NullBill does not know their programs.
- Until the upgrade ran, a run of one stage alone is refused: the roles
  are written for one release.

The download client is built from source at a pinned tag and commit in its
guest, as llama.cpp is; apply lends the container memory for the build.
Minecraft's Java and llama.cpp's Vulkan headers are Debian's now, which
update nightly with the rest of Debian.

The router asks the home router for a port (ADR 0014) without Debian's
`natpmpc`: Debian 13's refuses a home router whose own address is not a
public one before it asks for anything, so `nb-portmap` speaks NAT-PMP
itself (one UDP request, RFC 6886) and the package is no longer
installed. UPnP stays with Debian's `upnpc`.

## Not followed

OpenTofu and its Proxmox provider, sops, Ansible and its collections,
gitleaks, the models' files, the NVIDIA driver, and Proxmox VE itself
(the host installs Debian's security updates only; a PVE major release is
the owner's step). They move by hand, as before. A model is a choice, not
a version.

## Consequences

- A server runs what main pinned the day before, and every server on the
  same commit runs the same bytes. The delay is one check run.
- A vendor's broken release reaches main only if every check passes with
  it, and reaches a server one night later. A break the checks do not see
  does reach servers; **Update NullBill** then has nothing to go back to
  by itself. Going back is a commit that moves the pin back and marks it
  `by_hand` in the table, so the daily job leaves it until somebody
  removes the mark.
- One failing bump holds all of that day's bumps, since they are one
  commit. The issue names the run. Splitting them is future work.
- The daily job pushes to main with the repository's own token. It
  changes pins, the files tests regenerate, and LiteLLM's lock; nothing
  else.
- Services restart at night when their version moved. A game server with
  players on it is restarted too; the nightly window is the mitigation.
- First use on a server built before this: the Updates page shows Debian
  and Forgejo (15 to 17) as waiting. After the owner's yes, the review
  says which guests are upgraded, and that run takes much longer than
  usual: every guest is away in turn for some minutes.

## Known limits of the Debian upgrade

Found by a review of the first version (2026-11-13) and left as they are:

- Room is measured before any guest is changed (the owner asked for it
  after the review): each guest has apt work the upgrade out against the
  new release's lists in a folder of its own and compares what it needs
  with what is free; the guests' storage must have five times what the
  guests download, for the blocks their snapshots keep. That factor is an
  estimate (archives, plus the unpacked packages at three to four times
  their archives), not a measurement on a thin pool, and what the guests
  write during the week after is not in it. The room for a backup in
  place of a snapshot is not measured; a backup that fails leaves the
  guest untouched.
- A vendor's repository in a guest that has no suite for the new release
  fails the measuring, so the run stops before any guest is changed.
- The file shares' backup before the upgrade includes the copies of kept
  disks that are that guest's own volumes, so it can be large and slow.
- The review's rows per machine (ADR 0018) do not know the release
  stage: the review's reason says that guests are upgraded, the rows do
  not say "restarts".
- A guest with a backup as its copy is restored by the owner, by hand.
- Forgejo goes from 15 to 17 in one step. Forgejo migrates its database
  at start and the role check passes on 17 with a new database; a
  database from 15 was not moved in a check. The postgres guest's
  snapshot from the same run is the way back.

## Not tested on Proxmox VE

The upgrade script is tested in a Debian 12 container, the stage against
a stand-in host. Not run on a real host: `pct snapshot` and `rollback`
around the upgrade, `qm guest exec --pass-stdin` for the router VM, an
unprivileged container's systemd going from 252 to 257 under a running
unit, the router's interfaces after its restart, and the NullBill
container restarting itself. The router role was not run on a VM made
from the Debian 13 image; its check runs in a container. qBittorrent's
build was run in a container, not in a guest with the catalog's 4 GiB
disk beside everything else a guest holds. Holding vendors' packages
through the upgrade was tried once by hand in a Debian 12 container with
Jellyfin's and Tailscale's real packages; the check uses a stand-in
repository. The first real
run is the owner's, and docs/runbooks/debian-upgrade.md says how to go
back.
