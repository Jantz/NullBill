# ADR 0012: Game servers, one container each

Status: accepted (the owner asked to "host games such Minecraft, Terraria,
Factorio, etc.", 2026-11-09; the design decided by Claude under his "I
trust you to make good design choices")

## Context

The owner wants to play with friends on his own server. It has 16 GiB of
memory, most of it spoken for. Friends reach a service only when the owner
ticks it for them (ADR 0011), and nothing on the server is open to the
internet.

A game server is a vendor's binary that takes connections from other
people's computers, reads what they send and keeps running for months. The
three asked for differ in every detail: how they are fetched, how they are
configured, how they stop, how they know who a player is.

What was checked before deciding (2026-11-09, each in a Debian 12
container with the server's user cut off from everything but the
container's own address by an nftables rule, and a stand-in for the egress
proxy that lets only the game's allow-list through; these are the role
checks under `ansible/roles/game_*/tests`):

- **Minecraft: Java Edition 26.3.** Mojang's version list names each
  release's `server.jar` with its SHA-1 and the Java it needs: 25, where
  Debian 12 has 17. Eclipse Temurin publishes a JRE 25 with its SHA-256.
  The server does not start until `eula.txt` says the EULA is agreed to.
  It asks Mojang's services by name for a direct connection and ignores
  Java's proxy settings, so in a zone whose only way out is a proxy it
  finds no name and no route. Given a hosts file of its own
  (`-Djdk.net.hosts.file`) that names those services at 127.0.0.1, and a
  small relay there that reads the name from each TLS hello and asks the
  proxy for it with CONNECT, it reaches them: TLS still ends at Mojang, the
  relay sees only the name. A player who is not signed in to the account
  they claim is refused ("tried to join with an invalid session"), and the
  proxy's log shows the question to `sessionserver.mojang.com`. The server
  rewrites `server.properties` at every start, in its own order with every
  option it knows. `stop` on its console saves the world and exits.
- **Terraria 1.4.5.8.** Re-Logic's dedicated server is a zip from
  terraria.org with no published checksum. It brings its own runtime and
  needs nothing from the internet. With `autocreate` in its config file it
  makes a world at the first start. It has no accounts: whoever reaches the
  port may join, unless the owner sets a server password. `exit` on its
  console saves the world; a signal alone ends it without saving. It has no
  way to ask how many players are connected, so that is the number of
  established TCP connections on its port.
- **Factorio 2.1.24.** Wube's headless server is a tarball from
  factorio.com with a published SHA-256. `proxy=` in its config file sends
  its requests through a proxy. With `require_user_verification` it asks
  `auth.factorio.com` about each player, and does not start a multiplayer
  game without an answer from there. `--rcon-bind 127.0.0.1:27015` keeps
  its remote console on the loopback address, where `/players online count`
  answers. SIGTERM saves the map and exits. `/promote NAME` on the console
  makes an admin of a player who has never joined. Which mods are on
  decides who can join: with only `base`, anyone who owns Factorio; with
  the Space Age expansion's four, only those who own it.
- Each unit was also run under a real systemd (a container with systemd as
  its first process): the console FIFO in the unit's runtime directory, a
  save on `systemctl stop`, a restart, and port 443 for the relay as an
  unprivileged user all work.

Not checked, because nothing here can: the guests as unprivileged
containers on Proxmox VE; a player with a real account joining and
playing; the path from a friend's device through Tailscale to the port.
Factorio's port is UDP, and no probe short of its own protocol tells an
open port from a closed one, so `verify` checks that the server answers
inside its guest and that the firewall files allow the flow, not the flow
itself.

## Decision

- **One unprivileged container per game**, each a workload of its own, so
  the owner pays in memory only for the games he ticks, a broken or
  compromised server holds one world and nothing else, and a game that is
  not being played can later be stopped by itself.
- **A zone of their own, `games`**, whose only way out is the egress
  proxy. A game server parses what strangers' computers send it; from this
  zone it reaches no other guest. Each guest's allow-list holds its
  server's download and the vendor's sign-in service, nothing more.
- **Friends through ADR 0011.** Each game has one port, open to the owner
  and to `shared`. A friend plays once the owner ticks the game for them
  under Access, People; the text the page writes for them says which game
  and version they need and what to type where.
- **The vendor's own server, pinned by SHA-256 in `catalog.yaml`'s `games`
  table**, fetched with a checksum like every other download. No container
  images, no server managers, no third-party builds. A player's game must
  be the same version as the server, so the pin moves with NullBill's
  updates, and the owner can pin another file in `config.yaml` at once.
  `tools/game-pins` prints what the vendors offer now.
- **Players prove who they are to the vendor**, always: Minecraft's
  `online-mode` and Factorio's `require_user_verification` cannot be
  turned off from `config.yaml`, nor can anything that would list the
  server publicly or open a second port. Everything else in a server's own
  settings can be set under `games.<name>.settings`, by the server's own
  names.
- **Minecraft's EULA is the owner's to agree to.** `games.minecraft.eula`
  is false in the catalog; plan refuses Minecraft without it, the page asks
  beside the tick with a link to the text, and the role installs nothing
  before it is true.
- **Minecraft reaches Mojang through `nb-tls-tunnel`**, the relay
  described above. The alternative, letting the games zone out directly,
  would give a server that parses untrusted input the whole internet.
- **One tool in every game's guest, `nb-game`**: whether the server
  answers, how many players are connected, a line on its console, a clean
  stop, its admins, and metrics for monitoring (`nb_game_up`,
  `nb_game_players`, `nb_game_last_active_timestamp_seconds`). The servers
  have no remote console open to anyone: Minecraft's and Terraria's read a
  FIFO only root and the server can write, Factorio's RCON answers on the
  loopback address with a password made in the guest.
- **A stop always saves.** Each unit stops its server the way that server
  saves (`stop`, `exit`, SIGTERM) and waits for it, so a restart of the
  guest, an update or a stop of a guest nobody plays on loses nothing.
- **Each guest says whether it is in use.** `/etc/nullbill/busy.d/game`
  prints a line while a player is connected, and while the server does
  not answer (it may be making its world), and nothing once it stands
  empty. That is what a later change that stops idle guests asks; port
  traffic alone would not do, since Factorio's is UDP.
- **Admins from `config.yaml`, added only.** apply makes each listed
  player an admin on the console (`op`, `/promote`). It never takes an
  admin away: the owner may have made others in the game.
- **Memory by default is for a small world and a few players**: 3 GiB for
  Minecraft (2 GiB of it Java's heap), 1.5 GiB each for Terraria and
  Factorio. These are choices, not measurements; plan counts a container
  at half its limit, and `overrides.<name>.memory_mib` raises one.
- **Worlds are backed up nightly** with their containers, like every
  other guest.

## Consequences

- When a game updates, players' games update by themselves and no longer
  match the server. Until NullBill's pin moves, the owner pins the new
  server in `config.yaml` (docs/runbooks/games.md says how) or players
  hold their game back. Following new versions from the page is not built.
- Mods, plugins and modded servers (Paper, Fabric, tModLoader, Factorio's
  mod portal) are not offered: each is code from a further party and
  needs a way out that the allow-lists do not give.
- Minecraft: Bedrock Edition (phones, consoles) cannot join; that is
  another server.
- Terraria has no accounts, so a friend given Terraria is anyone on that
  friend's Tailscale account. A server password (`games.terraria.settings.
  password`) is the owner's choice.
- Factorio's RCON password is on the server's command line, where every
  process in that guest can read it. Nothing else runs there, and RCON
  listens on the loopback address only.
- A game that needs more than one port, or another game, needs a role and
  a catalog entry of its own; `games.py` holds what the three share.
- Removing a game's tick deletes its container and its world with the
  next setup run, after the page's question. The runbook says how to copy
  a world out first.
