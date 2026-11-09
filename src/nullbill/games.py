"""Game servers: what plan, apply, verify and the page need for each game in
the catalog's games table (docs/adr/0012-game-servers.md).

Each game is a service of the same name, alone in an unprivileged container
in the games zone, with one port for its players. The server is the
vendor's own download, pinned by SHA-256, run as user `game` by a systemd
unit whose console is a FIFO. `nb-game` in the guest (ansible/roles/
game_server) answers how many players are connected, types on that console
and writes the metrics monitoring reads.

The zone's only way out is the egress proxy, with each guest's own
allow-list: the server file, and the vendor's sign-in service where the
server asks it about each player who joins.
"""

import shlex

from nullbill.model import NODE_EXPORTER
from nullbill.probe import Runner
from nullbill.proxy import PROXY, allows, proxy_port
from nullbill.render import Note, strip_notes
from nullbill.results import Result

NB_GAME = "/usr/local/sbin/nb-game"
# A new world is made at the first start; Terraria's largest takes longest.
START_WAIT_S = 600
# What Java takes besides its heap, and the guest's own system, in MiB.
JVM_OVERHEAD_MIB = 1024
EULA = "https://aka.ms/MinecraftEULA"
# Mojang's services, by the names its discovery document gives
# (https://discovery.minecraftservices.com/minecraft/client). The server
# reaches them through nb-tls-tunnel, since it ignores proxy settings.
MOJANG = ("discovery.minecraftservices.com", "api.minecraftservices.com", "sessionserver.mojang.com", "api.mojang.com")
FACTORIO_AUTH = "auth.factorio.com"
TERRARIA_WORLDS = "/var/lib/terraria/worlds"
FACTORIO_RCON_PORT = 27015

# What NullBill sets and `settings` cannot: where the server listens, and
# what would open it to people the tailnet policy does not let in.
MANAGED = {
    "minecraft": ("server-port", "server-ip", "online-mode", "enable-rcon", "rcon.port", "rcon.password", "enable-query", "query.port"),
    "terraria": ("world", "worldpath", "port", "upnp", "banlist"),
    "factorio": ("visibility", "username", "password", "token", "require_user_verification"),
}
# Minecraft's management server is off by default; its options stay so.
MANAGED_PREFIX = {"minecraft": ("management-server-",)}
# What a new server starts with, unless `settings` says otherwise.
DEFAULTS = {
    # The tailnet policy decides who reaches the server, so its own list of
    # allowed players starts off (the server's default is on).
    "minecraft": {"white-list": False, "motd": "Minecraft on NullBill"},
    # autocreate 2: a medium world.
    "terraria": {"worldname": "NullBill", "autocreate": 2, "difficulty": 0, "maxplayers": 8, "secure": 1},
    "factorio": {"name": "Factorio on NullBill", "description": "", "auto_pause": True},
}
# How an admin is made on the server's console; Terraria has no admins.
ADMIN_COMMAND = {"minecraft": "op {}", "factorio": "/promote {}"}
# Minecraft asks Mojang for the player behind a name before it answers.
ADMIN_WAIT_S = 20
# The client a player needs, for the page and the plan.
CLIENT = {"minecraft": "Minecraft: Java Edition", "terraria": "Terraria (PC)", "factorio": "Factorio"}
# Where a player types the server's address, in each game's own words.
JOIN = {
    "minecraft": "Multiplayer, Add Server",
    "terraria": "Multiplayer, Join via IP",
    "factorio": "Multiplayer, Connect to address",
}
# A line for the server's console that everyone in the game then sees.
SAY = {"minecraft": "say hello", "terraria": "say hello", "factorio": "hello"}


def names(model: dict) -> list[str]:
    return list(model["games"])


def planned(plan: dict, model: dict) -> list[str]:
    """The games the plan has a guest for, in the catalog's order."""
    return [name for name in names(model) if name in plan["guests"]]


def port(model: dict, name: str) -> tuple[int, str]:
    """A game's one port for its players, and its protocol."""
    ((number, proto),) = ((p["port"], p["proto"]) for p in model["services"][name].get("ports", []) if p != NODE_EXPORTER)
    return number, proto


def settings(model: dict, name: str) -> dict:
    """The server's options: NullBill's starting values, then the owner's,
    then what NullBill always sets."""
    number, _ = port(model, name)
    fixed = {
        "minecraft": {"server-port": number, "online-mode": True, "enable-rcon": False, "enable-query": False},
        "terraria": {
            "world": f"{TERRARIA_WORLDS}/world.wld",
            "worldpath": TERRARIA_WORLDS,
            "banlist": "/var/lib/terraria/banlist.txt",
            "port": number,
            "upnp": 0,
        },
        "factorio": {"visibility": {"public": False, "lan": False}, "require_user_verification": True},
    }[name]
    return {**DEFAULTS[name], **model["games"][name]["settings"], **fixed}


def heap_mib(memory_mib: int) -> int:
    return memory_mib - JVM_OVERHEAD_MIB


def plan_section(model: dict, planned_services: dict, sizes: dict, errors: list[str]) -> Note | dict:
    """Each planned game's version and port, and Minecraft's heap; rule
    failures go to errors."""
    games = model["games"]
    for name in games:
        svc = model["services"].get(name)
        if svc is None:
            errors.append(f"games.{name}: no service of that name")
            continue
        if svc["kind"] != "lxc" or svc.get("attach"):
            errors.append(f"services.{name}: a game server is one container")
        if len([p for p in svc.get("ports", []) if p != NODE_EXPORTER]) != 1:
            errors.append(f"services.{name}.ports: a game server has one port, for its players")
    section: dict = {}
    for name in [n for n in games if n in planned_services and n in model["services"]]:
        game, svc = games[name], model["services"][name]
        if errors and any(e.startswith(f"services.{name}") for e in errors):
            continue
        for key in game["settings"]:
            if key in MANAGED[name] or key.startswith(MANAGED_PREFIX.get(name, ())):
                errors.append(f"games.{name}.settings.{key}: NullBill sets this itself")
        number, proto = port(model, name)
        why = f"Players need {CLIENT[name]} {game['version']}, the same version as the server."
        entry: dict = {"title": game["title"], "version": game["version"], "port": Note(f"{proto}/{number}", why)}
        allow = [*model["proxy"]["allow"], *svc.get("proxy_allow", [])]
        if name == "minecraft":
            if not game["eula"]:
                errors.append(
                    f"games.minecraft.eula: set it to true once you agree to Minecraft's EULA ({EULA}); the server does not start without"
                )
            for host in MOJANG:
                if not allows(allow, host):
                    errors.append(f"services.minecraft.proxy_allow: {host} is missing; the server checks each player's account there")
            entry["heap_mib"] = Note(
                heap_mib(sizes[name]["memory_mib"]),
                f"Java's heap: the container's memory less {JVM_OVERHEAD_MIB} MiB for Java itself and the system. "
                "Raise overrides.minecraft.memory_mib for a larger world or more players.",
            )
        if name == "factorio":
            if not allows(allow, FACTORIO_AUTH):
                errors.append(f"services.factorio.proxy_allow: {FACTORIO_AUTH} is missing; the server checks each player's account there")
            entry["space_age"] = Note(
                game["space_age"],
                "Every player must own the Space Age expansion."
                if game["space_age"]
                else "The base game: anyone who owns Factorio can join.",
            )
        if game.get("admins"):
            entry["admins"] = game["admins"]
        section[name] = entry
    return section or Note({}, "Not planned.")


def variables(plan: dict, model: dict, name: str) -> dict:
    """The role's variables for one game (ansible/roles/game_<name>)."""
    game = model["games"][name]
    number, _ = port(model, name)
    proxy = f"{plan['guests'][PROXY]['ip']}:{proxy_port(model)}"
    prefix = f"game_{name}_"
    out = {"version": game["version"], "url": game["url"], "sha256": game["sha256"], "port": number, "settings": settings(model, name)}
    if name == "minecraft":
        out |= {f"java_{k}": game["java"][k] for k in ("version", "url", "sha256")}
        out |= {"heap_mib": plan["games"][name]["heap_mib"], "eula": game["eula"], "proxy": proxy, "services": list(MOJANG)}
    if name == "factorio":
        out |= {"space_age": game["space_age"], "proxy": f"http://{proxy}", "rcon_port": FACTORIO_RCON_PORT}
    return {prefix + key: value for key, value in out.items()}


def playbook(name: str) -> str:
    return f"game-{name}.yml"


def missing_admins(model: dict, name: str, listed: str) -> list[str]:
    """config.yaml's admins that `nb-game admins` (listed) does not name.
    Names compare without case, as the games do. Admins the owner made on
    the console stay: NullBill only adds."""
    have = set(listed.split())
    return [admin for admin in model["games"][name].get("admins", []) if admin.lower() not in have]


def admin_command(name: str, admin: str) -> str:
    """The console line that makes a player an admin."""
    return ADMIN_COMMAND[name].format(admin)


def address(plan: dict, name: str, model: dict, by_name: bool = False) -> str:
    """What a player types into the game: the server and its port."""
    host = f"{name}.{plan['domain']}" if by_name else plan["guests"][name]["ip"]
    return f"{host}:{port(model, name)[0]}"


def _checks(plan: dict, model: dict, name: str) -> list[tuple[str, str, str]]:
    """(label, command in the game's guest, expected output)."""
    game = model["games"][name]
    field = 'python3 -c \'import json, sys; s = json.load(sys.stdin); print(s["version"], s["running"])\''
    out = [(f"{name} runs version {game['version']} and answers", f"{NB_GAME} status | {field}", f"{game['version']} True")]
    if name == "minecraft":
        for host in MOJANG:
            out.append((f"minecraft reaches {host} through the proxy", f"/usr/local/sbin/nb-tls-tunnel check {host} 2>&1", "ok"))
    if game.get("admins"):
        want = " ".join(sorted(a.lower() for a in game["admins"]))
        # The owner may have made more admins on the console: those stay.
        missing = 'case "$have" in *" $a "*) ;; *) echo "$a is not an admin"; exit;; esac'
        script = f'have=" $({NB_GAME} admins 2>&1 | tr "\\n" " ")"; for a in {want}; do {missing}; done; echo admins'
        out.append((f"{name}'s admins are those in config.yaml", script, "admins"))
    return out


def commands(plan: dict, model: dict) -> list[tuple[str, str, str]]:
    out = []
    for name in planned(plan, model):
        vmid = plan["guests"][name]["vmid"]
        out += [(label, f"pct exec {vmid} -- bash -c {shlex.quote(command)}", want) for label, command, want in _checks(plan, model, name)]
    return out


def skipped(plan_with_notes: dict, model: dict, reason: str) -> list[Result]:
    return [Result("SKIP", label, reason) for label, _, _ in commands(strip_notes(plan_with_notes), model)]


def check(plan_with_notes: dict, model: dict, target: str, run: Runner) -> list[Result]:
    results = []
    for label, command, want in commands(strip_notes(plan_with_notes), model):
        rc, out, err = run(target, command)
        got = out.strip()
        results.append(Result("PASS", label) if got == want else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:120]))
    return results
