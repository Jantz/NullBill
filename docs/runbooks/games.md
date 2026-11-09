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

The page asks only for what every server needs. Everything else is the
server's own option, by its own name, under `games.<name>.settings` in
`config.yaml` in the NullBill container (`pct enter 2002`,
`/var/lib/nullbill/config.yaml`):

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

Then press **Run setup**. A changed setting restarts that server, which
saves first; players are disconnected for a moment.

- NullBill sets a few options itself and plan refuses them in `settings`:
  the port, anything that lists the server publicly or opens a second
  port, and the check of each player's account (`online-mode`,
  `require_user_verification`).
- Options that shape a new world (`level-seed`, `autocreate`, `seed`)
  count only when the world is made.
- **Factorio's Space Age.** With `space_age: false`, the default, the
  server runs the base game and anyone who owns Factorio can join. With
  `true`, every player must own the expansion. Choose before the first
  setup run: switching it later loads the existing map with other mods
  than it was made with.
- **Memory.** The defaults suit a small world and a few players. Raise
  `overrides.<name>.memory_mib` when the server lags or its log says it
  ran out of memory; the **Plan** then shows whether that fits.

## 4. The server's console

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

(`-u terraria` in 2026, `-u factorio` in 2027.) Two more questions the
same tool answers: `pct exec 2025 -- nb-game status` (version, whether it
answers, how many players) and `nb-game admins`.

Admins you name on the page are added at each setup run. NullBill never
takes one away; do that on the console (`deop NAME`, `/demote NAME`).

## 5. Worlds: where they are, copying them out and in

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
each server's file by its checksum, and **Update NullBill** followed by
**Run setup** moves to the version NullBill pins now. Worlds are kept.

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
- **A friend cannot connect.** Check, in this order: their game's version
  against the **Services** page; that their Tailscale is on and
  **People** shows their device connected; that the game is ticked for
  them. Then `pct exec 2025 -- nb-game status` on the server.
- **Minecraft turns everyone away as they join**, you included: the
  server cannot ask Mojang who they are. **Run the checks** names which of
  Mojang's services does not answer through the proxy.
- **The alert GameServerDown**: the server has not answered for ten
  minutes; its unit keeps restarting it. The log, as above, says why. A
  server that runs out of memory needs more (`overrides`, above).
