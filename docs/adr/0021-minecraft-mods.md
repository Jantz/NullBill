# ADR 0021: Paper, Fabric and mods for a Minecraft server

Status: accepted (the owner asked for "the most needed, wanted things" for
the game servers after worlds, 2026-11-10; mods and plugins were named as
the next change in ADR 0019's pull request; the design is Claude's)

## Context

ADR 0012 runs Mojang's own server file and nothing else. Most people who
run a Minecraft server for friends want more than that within a week: a
map in the browser, chunk pre-generation, a faster server, a mod everyone
in the group plays with. Mojang's server takes none of it. The two servers
that do, and that people actually use, are Paper (plugins; players join
with the ordinary game) and Fabric (mods; some must also be in each
player's game). Until now the only way was a service of the owner's own
(ADR 0015) with a script, which loses everything the built-in server has:
worlds, the console, who plays, admins, the checks.

A plugin or mod is a program by a stranger that runs with the server's
rights. So the questions were where they come from, what pins them, and
what they can reach.

What was checked before deciding (2026-11-10):

- Paper 26.3 build 248 (one file, SHA-256 published by PaperMC's
  downloads service) patches Mojang's server file. It looks for that file
  at `cache/mojang_<version>.jar` in its working directory and downloads
  it only when it is not there; given the file the role already holds, it
  starts without reaching anything. It keeps the world in one folder
  `world` with every dimension, as Mojang's 26.3 does, so the worlds of
  ADR 0019 move and travel as before.
- **Paper changes a world for good.** The first time it opens a world it
  moves the world's own settings (`world_gen_settings.dat`, game rules,
  weather) from `world/data/minecraft/` into
  `world/dimensions/minecraft/overworld/data/minecraft/`. Mojang's server
  and Fabric look in the old place, find nothing and stop with "Overworld
  settings missing" (seen in the role's check, which first ran one world
  through all three). Mojang's server and Fabric open each other's worlds.
- Fabric's loader 0.20.1 starts Mojang's file from a class path of seven
  libraries (the loader, ASM, Mixin), each with a SHA-256 on Fabric's
  Maven: `-Dfabric.gameJarPath=<Mojang's file> -cp '<dir>/*'
  net.fabricmc.loader.impl.launch.knot.KnotServer`. No installer runs and
  nothing is downloaded at start.
- Both can be told where their plugins or mods are, outside the
  directory the server writes to: Paper `--add-plugin=<jar>` (once per
  file; its own help lists it), Fabric `-Dfabric.modsFolder=<dir>` (in the
  loader's `SystemProperties`).
- Modrinth's API (`api.modrinth.com/v2`) answers, without an account:
  a search by loader, Minecraft version and kind of project; a project's
  versions for a loader and Minecraft version, each file with its SHA-512
  and its address on `cdn.modrinth.com`; and what a version requires or
  does not work with. Chunky's Fabric file requires Fabric API; its Paper
  file requires nothing. The same project can be a plugin and a mod.
- The role's check runs all of it for real: Paper with Chunky, Fabric
  with Lithium, Chunky and Fabric API, and the worlds on the way from
  Mojang's server to Paper, to Fabric, back to Mojang's and to Paper
  again.

## Decision

**Each Minecraft server says which server runs it**:
`games.<name>.server` is `vanilla` (the default, as before), `paper` or
`fabric`. The catalog pins Paper's build and Fabric's loader with its
libraries by SHA-256 under `games.minecraft.servers`, for the Minecraft
version it pins; plan refuses a kind pinned for another version. Mojang's
file, Java, the unit, the console, the tunnel to Mojang's services,
worlds, admins and the checks are the same for all three: only the start
script's command line differs.

**Plugins and mods come from Modrinth and from nowhere else**, as
`games.<name>.mods`: one entry per project with the file's name, its
address on Modrinth's own file store (the schema takes no other host) and
the SHA-512 Modrinth gave when the owner added it. `nullbill/mods.py`, in
the NullBill container, asks Modrinth and writes the list:

- search lists what the server's kind and Minecraft version can run;
- add takes a project's newest version for that kind and version (a
  release before a beta before an alpha), then, in turn, everything that
  version requires and the list lacks, marked as there for others;
- remove takes along what was only there for the one removed, and refuses
  to remove what another still requires;
- check for updates says which have another file now; update takes them.
  Nothing updates by itself: a mod's new version is new code.

The list also keeps each file's loaders and Minecraft versions, so plan
refuses, with the words to fix it, what the server would crash on: a mod
on Paper, a plugin on Fabric, anything on Mojang's server, a file for
another Minecraft, a mod without what it requires.

**The guest downloads exactly those files.** The role fetches each to
`/opt/minecraft/mods`, root's, by its SHA-512, through the egress proxy
(the Minecraft guest's allow-list gains `cdn.modrinth.com`,
`fill-data.papermc.io` and `maven.fabricmc.net`), and removes what the
list no longer names. The server is started with that directory and
cannot write to it, so what runs is what the page lists, as Modrinth
published it on the day it was added. A file swapped on Modrinth later
fails its hash and is not installed.

**Changing the kind drops the list**, after the page asked: a plugin is
no mod. What only a plugin or mod added to a world may be gone when the
world is opened without.

**A world Paper has opened is Paper's, and no world is lost over it.**
NullBill does not convert a world back: that would mean rewriting an
owner's world from what was seen of Paper's files, with nothing to check
the result against. Instead:

- `nb-game` tells such a world by where its settings are, and the list
  marks it **Paper only**. `world use` refuses it on a server that is not
  Paper, before anything stops.
- When the kind is no longer Paper, the role runs `nb-game world fit`
  before it starts the server: the world that runs is set aside, kept in
  the list like any other, and the server makes a new one ("New world
  after Paper") from the server's settings. Back on Paper, **Run this
  one** runs the kept world again.
- The page asks before a change to Paper ("only Paper opens that world
  from then on; download it first") and before a change away from it
  ("the one that runs is kept, and the server starts with a new world").
  Between Mojang's server and Fabric without a list it asks nothing.

**The page**: the Minecraft row on the Services page has **Server and
plugins** (or **mods**): Vanilla, Paper, Fabric; the list with what every
player needs in their own game marked; **Add** with a search of Modrinth;
**Remove**; **Check for updates**. A change is saved at once and needs no
server, so the list can be made before the first setup run. The server
follows when setup's games stage runs: the block says "The server does
not run this yet" from what the guest holds (`nb-game report`), and
**Apply to the server** starts that stage. The agent's `game_mods` does
the same, as a change.

**verify** asks the guest which kind it was set up as and which files it
holds (`nb-game mods`) and compares with config.yaml.

## What a plugin or mod can do

It runs as the server: user `game`, in the server's unprivileged
container, under the unit's `ProtectSystem=strict`. It can change the
world and the server's own files, read who plays and from where, and use
the server's memory and CPU. It cannot change the server's programs, its
other mods or the kept worlds (root's). Its way out is the guest's: the
egress proxy with this guest's allow-list (Mojang's services, the
download hosts above, Debian's mirrors and the platform's own list) and
nothing else, and the firewall lets the guest start no connection to
another guest. Names resolve for Java only from the role's hosts file, so
a plugin that phones home (update checks, metrics) just fails, and one
that needs the internet (a Discord bridge) does not work here.

That is why adding one is a change and not a risky action for the agent's
gate: the reach is the game server's, which the owner already runs.

## Consequences

- When NullBill's pin for Minecraft moves, a server with mods made for
  the old version fails plan until **Check for updates** took newer ones
  or they are removed, since Fabric refuses to start otherwise. Until a
  mod has a version for the new Minecraft, the owner can hold the server
  back with the old pins under `games.<name>` in config.yaml.
- Paper keeps its own files (`paper-*.yml`, `plugins/<name>/`) in the
  server's home. They stay when the kind changes, unused.
- A `.jar` put into `/var/lib/minecraft/plugins` by hand still loads on
  Paper: NullBill neither lists nor updates it, and verify does not see
  it. Fabric reads only the role's directory.
- `tools/game-pins` prints Paper's and Fabric's pins beside Mojang's;
  `tools/record-modrinth` records Modrinth's answers for the tests.

## Not built

- Taking a world back from Paper to Mojang's server or Fabric. A download
  made before the change to Paper is the way back.
- Forge, NeoForge, Quilt, Purpur, Folia; modpacks (`.mrpack`); datapacks
  and resource packs; mods from CurseForge or a file the owner uploads
  (no publisher's hash to pin).
- A mod's own settings on the page: they are files in the server's home.
- Sending players the mods they need: the page names them, each player
  installs them.
- Holding Minecraft back by itself while a mod has no version for a new
  release.
- Factorio's mod portal, which needs the owner's factorio.com token.

## Not tested on real hardware

Everything here ran in the role check's container and against a stand-in
for the page's host: the downloads through the real egress proxy, a real
player joining a Paper or Fabric server, and a mod that every player
needs have not been tried on the owner's server.
