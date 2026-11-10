# ADR 0016: Several servers of one game, each with its own settings

Status: accepted (the owner asked that service setup be "smooth and error
free. Like multiple Minecraft servers with customizations etc.",
2026-11-10, in answer to ADR 0015's "a hundred servers of one game are a
hundred services of the owner's own"; the design is Claude's)

## Context

ADR 0012 made each game one service, one container, one world. ADR 0015
left a second server of a built-in game to a service of the owner's own,
with a setup script the agent writes. That was the wrong side of the line:
a second Minecraft is not a program NullBill does not know. It has a role
with a check that runs the real server, the tunnel to Mojang, a pinned
download, admins, a count of players for stopping on demand, and checks in
`verify`. A script would give up all of it and ask the owner to read code
to get what a tick already gives.

A game's settings were reachable only by editing `config.yaml` in the
NullBill container by hand.

What was checked before deciding (2026-11-10):

- Everything around a game server is already generated from its `services`
  entry, as for a service of the owner's own (ADR 0015): a second entry
  made from the first plans, renders both firewall layers and the Tailscale
  policy, and a friend given the second server gets its address and port
  and not the first's (`tests/test_more_games.py`). Twenty Minecraft
  servers plan and render on the large fixture.
- The roles take everything from variables and write nothing that names
  the guest, so the same role sets up each server (`games.variables` gives
  a second server the same variables by name).
- Every guest has an address of its own, so each server listens on the
  game's usual port. Nothing has to be renumbered and players type no port
  they do not know.
- The settings the page offers by name exist in the pinned servers: each
  game's role check looks its list up in the real server (Minecraft's
  rewritten `server.properties`, the example files in Re-Logic's and Wube's
  archives). Minecraft 26.3 no longer has `pvp` as a server option; it is
  not offered.
- The page in Chromium: adding a server, changing its settings, a refused
  setting, saving, and removing one.

Not checked, because nothing here can: any of it on Proxmox VE; how many
Minecraft servers a real 16 GiB host holds on demand.

## Decision

- **A further server is a game server.** `games.<name>` in `config.yaml`
  with `game: minecraft|terraria|factorio` and an `id` is one more server
  of that game. `model.build_model` makes its service, its workload and its
  rule for when it runs from the game's own, all of that name, so plan,
  both firewall layers, the proxy, updates, monitoring, backups, power,
  people, apply's games stage and verify treat it as they treat the first.
  It is not a service of the owner's own and has no setup script.
- **Its own guest, world, settings, admins, memory and friends.** What is
  the game's is shared: the pinned download (unless the server pins its
  own), Java, the proxy allow-list, the port. Minecraft's EULA is agreed
  to once, as `games.minecraft.eula`, for every Minecraft server; a
  further server cannot carry an agreement of its own.
- **Addresses from the owner's range** (64 to 127 and 192 to 253, ADR
  0015), shared with services of the owner's own: 126 in all.
- **The name comes from the title** (`Creative world` becomes
  `minecraft-creative-world`), and is the guest's name on the network.
  Titles are unique, since they are what the owner ticks for a friend.
- **Settings on the page, by name.** Each game server's row has the
  settings most people change as fields with the game's own words (game
  mode, difficulty, players at once, world size, seed, password), a box
  for any other of the server's options as `name = value`, and its memory.
  What equals the server's start is not written down. What NullBill sets
  itself (the port, the account check, anything that lists the server or
  opens a second port) is refused there as plan refuses it, with every
  problem said at once, a line each.
- **Adding one is a change, not a risk.** The page's **Add a game server**
  and the agent's `add_game_server` save a new server and tick it. No code
  comes with it and it opens what the game's own server opens, so the
  agent's request is a `change` like saving the choices: asked in Manual,
  unasked in Auto. The agent never agrees to an EULA.
- **Removing follows ADR 0015.** Unticking deletes the guest at the next
  setup run, which asks first. Only a server that is off and has no guest
  can be removed from the list (the row's **Remove**, the agent's
  `remove_game_server`). A game's first server is turned off, never
  removed.

## Consequences

- A hundred servers of one built-in game are a hundred ticks, not a
  hundred scripts. A game NullBill has no role for is still a service of
  the owner's own (ADR 0015).
- A further server starts with a new world. Copying a world in is the
  runbook's, as for the first.
- Each server is a container with the game's whole memory floor, 3 GiB for
  Minecraft. Many fit only on demand (ADR 0013), where each holds memory
  only while someone plays; a friend cannot start one.
- Mods, plugin servers (Paper, Forge) and other editions are not built: a
  further server runs the vendor's own pinned file. A pin of its own
  (`version`, `url`, `sha256`) lets one server stay on another version.
- The page offers a setting only if the pinned server has it; when a game
  renames one, the role check fails and the list moves with the pin.
- Changing a setting restarts that server at the next setup run. Settings
  that shape a world (seed, size, hardcore) count only when the world is
  made, and the page says so beside each.
- Passwords a server takes (`password`, `game_password`) are in
  `config.yaml` and shown on the page to the owner, like the admins.
