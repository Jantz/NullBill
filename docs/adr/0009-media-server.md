# ADR 0009: Jellyfin as the media server

Status: accepted (the owner asked for Jellyfin and chose "Build it into
NullBill", 2026-11-08; the rest decided by Claude under his standing "pick,
be critical and decide")

## Context

The owner asked how to install Jellyfin. The project goal lists media
servers as home server apps for milestone 9, with these defaults: their own
zone, nothing public, LAN reach (TVs) only by an opt-in router forward,
media kept out of backups. His server has 16 GiB of memory, an i9-9900K
with integrated graphics, and an RTX 4070 the host shares with local
models.

What was checked before deciding (2026-11-08, in a Debian 12 container):

- Jellyfin's apt repository for bookworm serves `jellyfin-server` and
  `jellyfin-web` 12.4 and `jellyfin-ffmpeg8` 8.1.3, signed by the key
  `4918AABC486CA052358D778D49023CD01DE21A7B`. The server package's postinst
  keeps an existing `jellyfin` user, so it can run as uid 1000, which owns
  the kept disks' files (`nb-legacy-disk` mounts them with `dmask=0077`).
- Until its first-run setup is done, `/Startup/User` lets anyone name the
  admin and set its password. `POST /Startup/Configuration`, `/Startup/User`,
  `/Startup/RemoteAccess` and `/Startup/Complete` finish it; afterwards
  each answers 401. An admin can make an API key (`POST /Auth/Keys`).
- While Jellyfin first starts, a stand-in on port 8096 answers
  `/System/Info/Public` with 200 in camelCase, then refuses connections,
  then answers 503 until the server proper answers. Asked with
  `Accept: application/json; profile="PascalCase"`, only the server proper
  answers with `StartupWizardCompleted`.
- `jellyfin-ffmpeg8` carries Intel's iHD driver, oneVPL's runtime and the
  older Media SDK runtime (`libmfxhw64`) that 9th-generation graphics
  need, and `vainfo`.

Not checked, because no Intel graphics are reachable from here: the render
node inside the container and transcoding speed.

## Decision

- Jellyfin from its own repository at pinned versions, in an unprivileged
  container in a new `media` zone whose only way out is the egress proxy,
  open to the owner's role on 8096. Its allow-list holds Jellyfin's
  repository and the metadata services it asks by default (TMDb,
  MusicBrainz, the Cover Art Archive). Plain HTTP, as Grafana: the owner
  reaches it over Tailscale only, and Jellyfin's apps for phones and TVs
  would not trust step-ca's root.
- Its library is the disks kept from a previous OS, bind-mounted read-only
  at `/media/<share>` from the same host mount the files guest shares.
  apply takes them off before the host unmounts one and puts them back
  after, as for the files guest. The owner adds libraries in Jellyfin, since
  only he knows which folder holds films and which series.
- apply finishes the first-run setup right after Jellyfin first starts,
  with the admin `nullbill-admin` and a password the page shows, and keeps
  an API key in the guest for apply and verify.
- Transcoding on the integrated Intel graphics when the probe finds them,
  through their render node (by its `by-path` name, since `renderD`
  numbers follow probe order), with Quick Sync; on the CPU otherwise or when
  `media.transcode: cpu` is pinned. The RTX 4070 stays with local models:
  NVENC would need the card's device nodes in a second container and VRAM
  from the inference budget.
- Its settings and watch history are backed up nightly with the container;
  bind mounts never are.

## Consequences

- Jellyfin updates when its pin in `ansible/roles/jellyfin/defaults/main.yml`
  moves; unattended-upgrades takes Debian's security updates only.
- TVs on the home network cannot reach it yet. That needs the router
  forward the project goal describes, as an opt-in, and is its own change.
- With no kept disk Jellyfin has nothing to play. A media store of its own
  (a share it can write to) is a later change.
- If the owner changes the admin password in Jellyfin and the API key is
  lost, apply stops at the media stage until he puts the new password in
  `/etc/nullbill/jellyfin-admin-password` in the guest
  (docs/runbooks/media.md).
