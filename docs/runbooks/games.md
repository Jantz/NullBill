# Game servers: Minecraft, Terraria, Factorio

Each game is a choice under **What to run**: a server of your own, in a
container of its own, for you and the friends you let in, over Tailscale.
Nothing is listed publicly or open to the internet. Why it is built this
way: [ADR 0012](../adr/0012-game-servers.md).

| Game | Players type, in the game | Guest | Each player needs |
|---|---|---|---|
| Minecraft | `minecraft.nullbill.internal:25565` (`10.42.8.25:25565`) under **Multiplayer, Add Server** | 2025 | Minecraft: Java Edition, signed in |
| Terraria | `terraria.nullbill.internal` (`10.42.8.26`), then port `7777`, under **Multiplayer, Join via IP** | 2026 | Terraria on a computer |
| Factorio | `factorio.nullbill.internal:34197` (`10.42.8.27:34197`) under **Multiplayer, Connect to address** | 2027 | Factorio, signed in to factorio.com |

The **Services** page shows each address as it is on your server, and the
version the server runs. A player's game must be that same version
([When a game updates](#when-a-game-updates)).

## 1. Turn a game on

**You need:** the setup page. For Minecraft, to have read its EULA
(<https://aka.ms/MinecraftEULA>): the server runs only once you agree.

1. On the **Setup** page, in **What to run**, tick the game under **Game
   servers**. The memory meter shows what it takes: about 1.5 GiB for
   Minecraft and 0.75 GiB each for Terraria and Factorio, since a container
   counts at half its limit.
2. For Minecraft, tick **I agree to the Minecraft EULA** beside it.
3. For Minecraft and Factorio, type your player name beside **Admins** if
   you want to be an admin in the game (several names: commas between
   them). Terraria has no admins.
4. Press **Save and plan**, then **Run setup**. The stage **Set up the game
   servers** installs each server, waits until it has made its world and
   answers, and makes the admins.
5. **Run the checks** shows each game's checks: the server runs the planned
   version and answers, its admins are those you named, and Minecraft
   reaches the account service it asks about each player.

Then open the game on a device with Tailscale and type the address from
the **Services** page.

## 2. Let a friend join

**You need:** the friend's Tailscale login.

On the page open **Access**, **People**, add the person or change them, and
tick the game ([add-person.md](add-person.md)). It is in force as soon as
the page says so, without a setup run. The steps the page writes for them
say which game and version they need and what to type.

A friend reaches only what you ticked: the game's one port, nothing else on
the server.

- Minecraft and Factorio ask the game's maker who each player is, so a
  friend plays under their own account's name.
- Terraria has no accounts. If you want one more lock, set a password
  (below) and tell your friends.

## 3. Settings

**You need:** the game ticked under **Setup**, **What to run**.

1. In the game's row, open **Settings of this server**.
2. Change what you want. The fields are the settings most people change,
   in the game's own words: game mode, difficulty, players at once, world
   size and seed, a password. **Memory** is the container's; give it more
   for a larger world or more players.
3. Anything else the server can do goes into **Other settings**, one on
   each line as `name = value`, by the server's own name for it
   (`server.properties` for Minecraft, the server's config file for
   Terraria, `server-settings.json` for Factorio).
4. Press **Save and plan**, then **Run setup**. A changed setting restarts
   that server, which saves first; players are disconnected for a moment.

If a setting is refused, the line under the form says which and why;
nothing is saved until every line is right.

The same settings are `games.<name>.settings` in `config.yaml` in the
NullBill container (`pct enter 2002`, `/var/lib/nullbill/config.yaml`),
for those who would rather write them:

```yaml
games:
  minecraft:
    settings:
      difficulty: hard          # server.properties, key by key
      max-players: 8
      motd: Our world
  terraria:
    settings:
      worldname: Our world      # the server's config file, key by key
      autocreate: 3             # 1 small, 2 medium, 3 large; only when the world is made
      difficulty: 1             # 0 classic, 1 expert, 2 master, 3 journey
      password: a-word-for-friends
  factorio:
    space_age: true             # the expansion; see below
    settings:
      name: Our factory         # server-settings.json, key by key
      max_players: 8
      game_password: a-word-for-friends
overrides:
  minecraft: {memory_mib: 6144} # more memory: Java gets all but 1024 MiB of it
```

- NullBill sets a few options itself and refuses them among the settings:
  the port, anything that lists the server publicly or opens a second
  port, and the check of each player's account (`online-mode`,
  `require_user_verification`).
- Settings that shape a new world (seed, world size, hardcore) count only
  when the world is made; the page says so beside each.
- **Factorio's Space Age.** With `space_age: false`, the default, the
  server runs the base game and anyone who owns Factorio can join. With
  `true`, every player must own the expansion. Choose before the first
  setup run: switching it later loads the existing map with other mods
  than it was made with.
- **Memory.** The defaults suit a small world and a few players. Raise
  **Memory** in the server's settings (`overrides.<name>.memory_mib`) when
  the server lags or its log says it ran out of memory; the meter above
  the list shows at once whether that fits.
- **Only while someone plays.** Under **When it runs**, set a game to
  **On demand** and it holds no memory while off. You start it on the
  **Services** page; it stops by itself once nobody has played for 30
  minutes, saving its world. A friend cannot start it, so start it before
  they join ([on demand](on-demand.md)).

## 4. More than one server of a game

A second Minecraft for a creative world, a Terraria in master mode beside
the first: each is a server of its own, in a container of its own, with
its own world, settings, admins and friends
([ADR 0016](../adr/0016-more-game-servers.md)).

**You need:** your choices saved, and for Minecraft the EULA agreed to
(once, for every Minecraft server).

1. Under **Setup**, **What to run**, **Game servers**, press **Add a game
   server**. Choose the game and give the server a name, such as
   `Creative world`, and press **Add**.
2. The new server is a row of its own, ticked. Open **Settings of this
   server** in it and set it up as in [Settings](#3-settings).
3. Press **Save and plan**. If the meter says it does not fit, set the
   servers you do not play at the same time to **On demand** under **When
   it runs**: they then share the memory.
4. Press **Run setup**.
5. Under **Access**, **People**, tick the new server for each friend who
   may join it. A friend ticked for one server does not reach the other.

Each server has its own address and the game's usual port, shown on the
**Services** page: a server named `Creative world` answers at
`minecraft-creative-world.nullbill.internal:25565`.

**The agent can do the same.** Ask it, for example "Add two more Minecraft
servers: one creative for the kids, one hard survival with 6 GB". It asks
you once per server in Manual mode and not at all in Auto, and never
agrees to an EULA for you.

**To remove one:** untick it, **Save and plan**, **Run setup** (which asks
before it deletes the server and its world), then **Remove** under the
row's **What it makes, needs and deletes**. The first server of a game is
only ever turned off.

In `config.yaml` a further server is an entry under `games` with the game
it is one of and an address from 64 to 127 or 192 to 253 that nothing else
uses:

```yaml
intent:
  workloads: [agents, minecraft, minecraft-creative-world]
games:
  minecraft:
    eula: true
  minecraft-creative-world:
    game: minecraft
    id: 64
    title: Creative world
    settings: {gamemode: creative}
    admins: [your_player_name]
power:
  minecraft-creative-world: {run: on-demand}
```

## 5. The server's console

Each server takes its own commands on its console. In Proxmox, select the
node, open **Shell**, and type the line after `nb-game console`:

```
pct exec 2025 -- nb-game console say Back in ten minutes
pct exec 2025 -- nb-game console whitelist add SomePlayer
pct exec 2026 -- nb-game console kick SomePlayer
pct exec 2027 -- nb-game console /ban SomePlayer
```

The answer is in the server's log:

```
pct exec 2025 -- journalctl -u minecraft -n 30
```

(`-u terraria` in 2026, `-u factorio` in 2027. A further server of a game
has a number of its own, shown with its console line on the **Services**
page, and the game's unit name.) Two more questions the
same tool answers: `pct exec 2025 -- nb-game status` (version, whether it
answers, how many players) and `nb-game admins`.

Admins you name on the page are added at each setup run. NullBill never
takes one away; do that on the console (`deop NAME`, `/demote NAME`).

## 6. Worlds: where they are, copying them out and in

Every world is in its container, which the nightly backup covers once you
have a backup disk ([restore-guest.md](restore-guest.md) brings a whole
guest back). **Unticking a game deletes its container and its world** with
the next setup run; the page asks first. To keep a world, copy it out
before.

| Game | The world, in its guest |
|---|---|
| Minecraft | the folder `/var/lib/minecraft/world` |
| Terraria | the file `/var/lib/terraria/worlds/world.wld` |
| Factorio | the newest `.zip` in `/var/lib/factorio/saves` (the server autosaves there) |

**Copy a world out** (Proxmox **Shell**; Minecraft shown). Stopping the
server saves the world first.

```
pct exec 2025 -- systemctl stop minecraft
pct exec 2025 -- tar -C /var/lib/minecraft -czf /root/world.tar.gz world
pct pull 2025 /root/world.tar.gz /root/minecraft-world.tar.gz
pct exec 2025 -- systemctl start minecraft
```

The file is then on the Proxmox host; fetch it to your computer with
`scp root@pve.nullbill.internal:/root/minecraft-world.tar.gz .` For
Terraria pull `/var/lib/terraria/worlds/world.wld` from 2026 the same way,
for Factorio a save from `/var/lib/factorio/saves` in 2027, each with its
server stopped (`systemctl stop terraria`, `factorio`).

**Put a world in.** A world from another server or from your own game
works if it is of the same or an older version of the game. Copy it to the
Proxmox host first (`scp`), then:

- Minecraft, from a `world.tar.gz` that holds one folder named `world`:

  ```
  pct push 2025 /root/world.tar.gz /root/world.tar.gz
  pct exec 2025 -- systemctl stop minecraft
  pct exec 2025 -- sh -c 'rm -rf /var/lib/minecraft/world && tar -C /var/lib/minecraft -xzf /root/world.tar.gz && chown -R game:game /var/lib/minecraft/world'
  pct exec 2025 -- systemctl start minecraft
  ```

- Terraria, from a `.wld` file:

  ```
  pct exec 2026 -- systemctl stop terraria
  pct push 2026 /root/MyWorld.wld /var/lib/terraria/worlds/world.wld
  pct exec 2026 -- chown game:game /var/lib/terraria/worlds/world.wld
  pct exec 2026 -- systemctl start terraria
  ```

- Factorio, from a save `.zip` (the server loads the newest file, so this
  one must be the newest; set `space_age` to match the save first):

  ```
  pct exec 2027 -- systemctl stop factorio
  pct push 2027 /root/MyFactory.zip /var/lib/factorio/saves/MyFactory.zip
  pct exec 2027 -- sh -c 'chown game:game /var/lib/factorio/saves/MyFactory.zip && touch /var/lib/factorio/saves/MyFactory.zip'
  pct exec 2027 -- systemctl start factorio
  ```

**Start over with a new world.** Stop the server, remove the world, start
it: Minecraft and Terraria make a new one at the start.

```
pct exec 2025 -- systemctl stop minecraft
pct exec 2025 -- rm -rf /var/lib/minecraft/world
pct exec 2025 -- systemctl start minecraft
```

For Factorio, make the map you want in your own game (its map generator
has every option) and put that save in, as above.

## When a game updates

A player's game and the server must be the same version. NullBill pins
each server's file by its checksum, and **Update NullBill** (on the
**Updates** page) followed by **Run setup** moves to the version NullBill
pins now. Worlds are kept.

When a game updates before NullBill does, either the players keep the
older version (Minecraft's launcher: **Installations**, **New
installation**, pick the version; Factorio on Steam: **Properties**,
**Betas**), or you pin the new server yourself:

1. On any computer with Python 3, fetch
   [`tools/game-pins`](../../tools/game-pins) from the repository and run
   `python3 game-pins`. It asks each game's maker what is current and
   prints version, address and SHA-256 for each.
2. Put those of the game in `config.yaml` (as in [Settings](#3-settings)):

   ```yaml
   games:
     factorio:
       version: 2.1.25
       url: https://factorio.com/get-download/2.1.25/headless/linux64
       sha256: <the SHA-256 it printed>
   ```

   For Minecraft, if the tool prints another Java than the catalog's, pin
   `java` (`version`, `url`, `sha256`) the same way.
3. Press **Run setup**. The server is replaced, the world stays. Remove
   the lines again once NullBill pins that version or a newer one.

A world opened by a newer server cannot go back to an older one. Copy it
out first if you are not sure.

## If something is wrong

- **Setup stops at Set up the game servers** with the agreement named:
  tick **I agree to the Minecraft EULA** beside Minecraft, or untick
  Minecraft.
- **"did not answer within 600 s"**: the server was installed but did not
  come up. Its log says why: `pct exec 2025 -- journalctl -u minecraft -n
  50`. Run setup again afterwards.
- **"did not make NAME an admin"**: the name is not a player the game's
  maker knows. Spell it as the game shows it.
- **A friend cannot connect.** Check, in this order: that the game is not
  **Off, on demand** on the **Services** page (press **Start**); their
  game's version against that page; that their Tailscale is on and
  **People** shows their device connected; that the game is ticked for
  them. Then `pct exec 2025 -- nb-game status` on the server.
- **Minecraft turns everyone away as they join**, you included: the
  server cannot ask Mojang who they are. **Run the checks** names which of
  Mojang's services does not answer through the proxy.
- **The alert GameServerDown**: the server has not answered for ten
  minutes; its unit keeps restarting it. The log, as above, says why. A
  server that runs out of memory needs more (`overrides`, above).
