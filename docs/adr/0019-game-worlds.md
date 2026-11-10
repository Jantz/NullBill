# ADR 0019: Worlds of a game server, chosen on the page

Status: accepted (the owner asked for "a game server save/world feature.
So that the admin can choose what kind of save runs on that game host
instance", 2026-11-10, after the page's agent could offer only shell
commands or unticking the game to get a new Terraria Expert world; the
design is Claude's)

## Context

ADR 0012 gave each server one world, made at the first start from the
server's settings. Everything after that was by hand in Proxmox's shell:
a different world meant stopping the unit and removing files, a world from
the owner's own game meant `pct push`, a copy out meant `tar` and `pct
pull` (docs/runbooks/games.md said how). Options that only count when a
world is made (Terraria's size and difficulty, Minecraft's seed and type)
sat among the server's settings, where changing them later did nothing.
The agent had `nb-game console` and no more, so it told the owner to type
`rm`.

What was checked before deciding (2026-11-10, the real pinned servers in
the role checks' containers):

- Each game keeps a world in one place that can be moved as a whole while
  the server is stopped: Minecraft 26.3 one folder (`world`, with every
  dimension in it), Terraria one directory of its `.wld` and backups,
  Factorio its `saves` directory. A server started over an empty place
  makes a world (Minecraft, Terraria) or is given one first (Factorio's
  `--create`).
- What shapes a new world can be passed for one start without touching
  the owner's settings for good: Minecraft reads `level-seed`,
  `level-type`, `hardcore` and `generate-structures` from
  `server.properties` only while it makes a world; Terraria takes a config
  file per start (`autocreate`, `difficulty`, `seed`, `worldname`);
  Factorio's `--create` takes `--map-gen-seed` and `--preset`. Terraria
  writes name, seed, size and mode into the `.wld` header, Minecraft
  `hardcore` into `level.dat`, Factorio answers `/seed`: the checks read
  them back.
- A file says what it is before a server opens it: a `.wld` starts with a
  version, `relogic` and the file type 2; a Minecraft world's `level.dat`
  is gzip NBT with `DataVersion`, and the server's jar names its own
  (`version.json`, `world_version`); a Factorio save is a zip of one
  folder with `level.dat0` or `level-init.dat`.
- A world a server cannot load makes it exit, which its unit answers by
  starting it again: without a rollback the server would loop.

## Decision

**A server has a list of worlds; one runs.** The running world stays where
ADR 0012 put it, so backups, the role and the checks see what they saw.
The others are kept in `<home>/kept-worlds/<id>` in the same guest: the
same filesystem, so changing worlds is a rename, and a directory of root's
alone, so the server (user `game`) cannot read or write a world that does
not run. `/var/lib/nullbill/worlds.json` names them (`w1`, `w2`, ...; a
title, when and how it was made, the options it was made with). The world
a server already has becomes "First world" the first time anything asks.

**`nb-game` in the guest does every change** (`nb-game world list | new |
use | rename | delete | export | receive | sent`, one line of JSON each),
because only the guest can stop its unit, move files and start it. A
change of the running world (`swap`) stops the unit (which saves), sets
the world aside, makes or brings the other, starts the unit and waits for
the server's answer. If anything fails, the world that ran runs again,
what was made is removed, and the answer carries the server's last lines.
One change at a time (a lock); at most 24 worlds a server.

**Nothing but Delete removes a world.** New world and Run this one keep
the world that ran. Delete is refused for the running world. An upload
joins the kept worlds and runs only when the owner says so; it never
replaces one.

**Players are not thrown out by accident.** A change that stops the
server is refused while someone plays and says how many; the page asks,
and the request comes again with `--kick`.

**New world asks what the game takes when it makes one**
(`games.NEW_WORLD`), each starting at the game's own default, not at the
server's settings for its first world: Minecraft seed, type, hardcore,
structures; Terraria size, difficulty, seed; Factorio seed and one of
Wube's presets. Each role's check holds the list against the real server.

**Uploads are checked in the guest before they count**, as root, in a
place the server cannot reach: the header or `level.dat` as above; a
Minecraft world from a newer Minecraft than the server's is refused with
both versions named (an older one is upgraded by the server when it
runs); zip members that leave their folder, links and encrypted entries
are refused; a zip that unpacks to more than it declares, or more than
the disk holds beside 512 MiB, is refused. Files end up owned by `game`,
0640.

**The page asks the guest through the host** (`pct exec <vmid> -- nb-game
...` over the SSH it already has): `web/gameroom.py`, `/api/game`. A
download is `nb-game world export` (a running server is told to write its
world out first; Minecraft's saving is paused for the copy) and then the
file through `pct exec ... cat`; an upload is the request's body passed
on as it comes to `nb-game world receive SIZE TITLE`, at most 8 GiB, read
only after the session is checked. Neither is held in memory or written
on the NullBill container.

**The Services page's row for a game** shows whether it answers, who
plays, its worlds with their buttons, New world, Upload, the end of its
log and a line for its console, and Restart. The agent has the same as
`game_server` (read), `game_world` (new, use, rename: a change; delete,
or disconnecting players: risky, with a typed word) and `game_command`
(restart, console).

## Not done

- **Start and Stop of the server inside a running guest.** `nb-busy`
  counts a server that does not answer as in use, so a stopped server
  would keep an on-demand guest on for good. The guest's own Start and
  Stop (ADR 0013) is the way.
- **Scheduled copies of a world**: the nightly backup covers the guest,
  kept worlds included.
- **Mods and plugin servers**: their own change.
- **Worlds on the host or shared between servers**: a world is one
  guest's, as its backup is.
- **Bedrock worlds**: the server is Java Edition.

## Consequences

- An existing game server gets the new `nb-game` with its next setup run;
  until then its row says the guest did not answer and to run setup.
- `worldname`, `autocreate`, `difficulty`, `seed` (Terraria) and
  `level-seed`, `hardcore` (Minecraft) in a server's settings still shape
  its first world only. The Minecraft role no longer writes the new-world
  keys once a world exists, so a run of setup does not undo what New world
  chose.
- Kept worlds use the guest's disk. The page shows what is free; the
  disk's size is `overrides.<name>.disk_gib`.
- Not tried on real hardware: the path through `pct exec` for large
  files (binary in and out is proven only against stand-ins and inside
  the role checks), and changes with real players connected.
