"""Game servers: what plan, apply, verify and the page need for each game in
the catalog's games table (docs/adr/0012-game-servers.md).

Each game is a service of the same name, alone in an unprivileged container
in the games zone, with one port for its players. config.yaml can name more
servers of a game (`games.<name>.game`, docs/adr/0016-more-game-servers.md):
each is a game server like the catalog's, in a guest of its own, with its
own settings and admins; `kind` says which game a name is a server of. The server is the
vendor's own download, pinned by SHA-256, run as user `game` by a systemd
unit whose console is a FIFO. `nb-game` in the guest (ansible/roles/
game_server) answers how many players are connected, types on that console
and writes the metrics monitoring reads.

The zone's only way out is the egress proxy, with each guest's own
allow-list: the server file, and the vendor's sign-in service where the
server asks it about each player who joins.
"""

import copy
import re
import shlex

from nullbill import custom, power
from nullbill.errors import NullBillError, Refused
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
# The settings the page offers by name (web/workloads.py's `game`), beside
# a box for any other of the server's own. choices are (value, label);
# without them a number has min and max, and `default` gives the kind
# (True or False: a switch, a number, else text). default is what the
# server does without the setting, where DEFAULTS above does not say.
# new_world: the server reads it only when it makes its world.
OPTIONS = {
    "minecraft": (
        {
            "key": "gamemode",
            "label": "Game mode",
            "choices": (("survival", "Survival"), ("creative", "Creative"), ("adventure", "Adventure"), ("spectator", "Spectator")),
            "default": "survival",
        },
        {
            "key": "difficulty",
            "label": "Difficulty",
            "choices": (("peaceful", "Peaceful"), ("easy", "Easy"), ("normal", "Normal"), ("hard", "Hard")),
            "default": "easy",
        },
        {"key": "max-players", "label": "Players at once", "min": 1, "max": 200, "default": 20},
        {"key": "view-distance", "label": "View distance, in chunks", "min": 3, "max": 32, "default": 10},
        {"key": "hardcore", "label": "Hardcore: one life each", "default": False, "new_world": True},
        {"key": "level-seed", "label": "World seed", "default": "", "new_world": True},
        {"key": "white-list", "label": "Only players on the server's own whitelist may join"},
        {"key": "motd", "label": "Line under the server's name in the game"},
    ),
    "terraria": (
        {"key": "worldname", "label": "World name", "new_world": True},
        {"key": "autocreate", "label": "World size", "choices": ((1, "Small"), (2, "Medium"), (3, "Large")), "new_world": True},
        {
            "key": "difficulty",
            "label": "Difficulty",
            "choices": ((0, "Classic"), (1, "Expert"), (2, "Master"), (3, "Journey")),
            "new_world": True,
        },
        {"key": "seed", "label": "World seed", "default": "", "new_world": True},
        {"key": "maxplayers", "label": "Players at once", "min": 1, "max": 255},
        {"key": "password", "label": "Password players type to join", "default": ""},
        {"key": "motd", "label": "Message for players who join", "default": ""},
    ),
    "factorio": (
        {"key": "name", "label": "Server name"},
        {"key": "description", "label": "Description"},
        {"key": "max_players", "label": "Players at once (0: no limit)", "min": 0, "max": 65535, "default": 0},
        {"key": "game_password", "label": "Password players type to join", "default": ""},
        {"key": "auto_pause", "label": "Pause while nobody plays"},
        {"key": "autosave_interval", "label": "Minutes between autosaves", "min": 1, "max": 1440, "default": 10},
        {"key": "afk_autokick_interval", "label": "Idle minutes before a player is kicked (0: never)", "min": 0, "max": 1440, "default": 0},
    ),
}
# What a server takes when it makes a world: the page's New world asks
# these, and `nb-game world new` in the guest gives them to the server for
# that one world (docs/adr/0019-game-worlds.md). Same shape as OPTIONS;
# every one has a default, the server's own.
NEW_WORLD = {
    "minecraft": (
        {"key": "level-seed", "label": "Seed (empty: a random world)", "default": ""},
        {
            "key": "level-type",
            "label": "World type",
            "choices": (
                ("minecraft:normal", "Normal"),
                ("minecraft:flat", "Flat"),
                ("minecraft:large_biomes", "Large biomes"),
                ("minecraft:amplified", "Amplified"),
            ),
            "default": "minecraft:normal",
        },
        {"key": "hardcore", "label": "Hardcore: one life each", "default": False},
        {"key": "generate-structures", "label": "Villages, temples and other structures", "default": True},
    ),
    "terraria": (
        {"key": "autocreate", "label": "World size", "choices": ((1, "Small"), (2, "Medium"), (3, "Large")), "default": 2},
        {
            "key": "difficulty",
            "label": "Difficulty",
            "choices": ((0, "Classic"), (1, "Expert"), (2, "Master"), (3, "Journey")),
            "default": 0,
        },
        {"key": "seed", "label": "Seed (empty: a random world)", "default": ""},
    ),
    "factorio": (
        {"key": "seed", "label": "Seed (empty: a random map)", "min": 0, "max": 4294967295, "default": "", "optional": True},
        {
            "key": "preset",
            "label": "Map preset",
            "choices": (
                ("", "Default"),
                ("rich-resources", "Rich resources"),
                ("marathon", "Marathon"),
                ("death-world", "Death world"),
                ("death-world-marathon", "Death world marathon"),
                ("rail-world", "Rail world"),
                ("ribbon-world", "Ribbon world"),
                ("island", "Island"),
            ),
            "default": "",
        },
    ),
}
# What a world is called in each game, for the page.
WORLD = {"minecraft": "world", "terraria": "world", "factorio": "map"}
# The file the owner uploads, and what a download is.
WORLD_FILE = {
    "minecraft": "a .zip of the world's folder, the one that holds level.dat (from .minecraft/saves, or another server)",
    "terraria": "the world's .wld file, from Documents/My Games/Terraria/Worlds",
    "factorio": "a save .zip as it is, from Factorio's saves folder",
}
MAX_WORLD_TITLE = 40
# The largest world file the page takes; the guest's free disk may say less.
MAX_WORLD_BYTES = 8 * 1024**3
WORLD_ID = re.compile(r"w[0-9]{1,6}")
MAX_CONSOLE = 200
SETTING = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,60}")
MAX_SETTINGS = 60
MAX_VALUE = 200
MAX_TITLE = 40
MAX_MEMORY_MIB = 65536
# How an admin is made on the server's console; Terraria has no admins.
ADMIN_COMMAND = {"minecraft": "op {}", "factorio": "/promote {}"}
# Minecraft asks Mojang for the player behind a name before it answers.
ADMIN_WAIT_S = 20
# The client a player needs, for the page and the plan.
CLIENT = {"minecraft": "Minecraft: Java Edition", "terraria": "Terraria (PC)", "factorio": "Factorio"}
# The game's own name, whatever a server of it is called.
TITLE = {"minecraft": "Minecraft", "terraria": "Terraria", "factorio": "Factorio"}
# The server's own file of options, which `settings` names keys of.
SETTINGS_FILE = {"minecraft": "server.properties", "terraria": "serverconfig.txt", "factorio": "server-settings.json"}
# Where a player types the server's address, in each game's own words.
JOIN = {
    "minecraft": "Multiplayer, Add Server",
    "terraria": "Multiplayer, Join via IP",
    "factorio": "Multiplayer, Connect to address",
}
# A line for the server's console that everyone in the game then sees.
SAY = {"minecraft": "say hello", "terraria": "say hello", "factorio": "hello"}


def names(model: dict) -> list[str]:
    """Every game server: the catalog's, then the owner's further ones."""
    return list(model["games"])


def kind(model: dict, name: str) -> str:
    """The game a server is one of: its own name, for the catalog's."""
    return model["games"][name].get("game", name)


def more(model: dict) -> list[str]:
    """The servers config.yaml added to the catalog's."""
    return [name for name, game in model["games"].items() if "game" in game]


def planned(plan: dict, model: dict) -> list[str]:
    """The games the plan has a guest for, in the catalog's order."""
    return [name for name in names(model) if name in plan["guests"]]


def port(model: dict, name: str) -> tuple[int, str]:
    """A game's one port for its players, and its protocol."""
    ((number, proto),) = ((p["port"], p["proto"]) for p in model["services"][name].get("ports", []) if p != NODE_EXPORTER)
    return number, proto


def defaults(model: dict, name: str) -> dict:
    """What a server starts with unless its settings say otherwise. A
    further server is told apart by its title, in the game's own list."""
    game = kind(model, name)
    named = {"minecraft": "motd", "factorio": "name"}.get(game)
    return DEFAULTS[game] | ({named: model["games"][name]["title"]} if named and name != game else {})


def settings(model: dict, name: str) -> dict:
    """The server's options: NullBill's starting values, then the owner's,
    then what NullBill always sets."""
    number, _ = port(model, name)
    game = kind(model, name)
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
    }[game]
    return {**defaults(model, name), **model["games"][name]["settings"], **fixed}


def managed(game: str, key: str) -> bool:
    """Whether a setting is one NullBill sets itself for that game."""
    return key in MANAGED[game] or key.startswith(MANAGED_PREFIX.get(game, ()))


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
        game, svc, of = games[name], model["services"][name], kind(model, name)
        if errors and any(e.startswith(f"services.{name}") for e in errors):
            continue
        for key in game["settings"]:
            if managed(of, key):
                errors.append(f"games.{name}.settings.{key}: NullBill sets this itself")
        number, proto = port(model, name)
        why = f"Players need {CLIENT[of]} {game['version']}, the same version as the server."
        entry: dict = {"title": game["title"], "version": game["version"], "port": Note(f"{proto}/{number}", why)}
        if of != name:
            entry["game"] = of
        allow = [*model["proxy"]["allow"], *svc.get("proxy_allow", [])]
        if of == "minecraft":
            agree = f"games.minecraft.eula: set it to true once you agree to Minecraft's EULA ({EULA}); the server does not start without"
            # Agreed once for every Minecraft server, so said once.
            if not game["eula"] and agree not in errors:
                errors.append(agree)
            for host in MOJANG:
                if not allows(allow, host):
                    errors.append(f"services.{name}.proxy_allow: {host} is missing; the server checks each player's account there")
            entry["heap_mib"] = Note(
                heap_mib(sizes[name]["memory_mib"]),
                f"Java's heap: the container's memory less {JVM_OVERHEAD_MIB} MiB for Java itself and the system. "
                f"Raise overrides.{name}.memory_mib for a larger world or more players.",
            )
        if of == "factorio":
            if not allows(allow, FACTORIO_AUTH):
                errors.append(f"services.{name}.proxy_allow: {FACTORIO_AUTH} is missing; the server checks each player's account there")
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
    """The role's variables for one game server (ansible/roles/game_<kind>)."""
    game, of = model["games"][name], kind(model, name)
    number, _ = port(model, name)
    proxy = f"{plan['guests'][PROXY]['ip']}:{proxy_port(model)}"
    prefix = f"game_{of}_"
    out = {"version": game["version"], "url": game["url"], "sha256": game["sha256"], "port": number, "settings": settings(model, name)}
    # Minecraft reads its world options from the file its settings are in,
    # so the role needs all of them; the others only what New world asks.
    # What a new world takes, each with the game's own start, as the server reads it.
    out["new_world"] = {o["key"]: str(o["default"]).lower() if isinstance(o["default"], bool) else str(o["default"]) for o in NEW_WORLD[of]}
    if of == "minecraft":
        out |= {f"java_{k}": game["java"][k] for k in ("version", "url", "sha256")}
        out |= {"heap_mib": plan["games"][name]["heap_mib"], "eula": game["eula"], "proxy": proxy, "services": list(MOJANG)}
    if of == "factorio":
        out |= {"space_age": game["space_age"], "proxy": f"http://{proxy}", "rcon_port": FACTORIO_RCON_PORT}
    return {prefix + key: value for key, value in out.items()}


def playbook(model: dict, name: str) -> str:
    return f"game-{kind(model, name)}.yml"


def missing_admins(model: dict, name: str, listed: str) -> list[str]:
    """config.yaml's admins that `nb-game admins` (listed) does not name.
    Names compare without case, as the games do. Admins the owner made on
    the console stay: NullBill only adds."""
    have = set(listed.split())
    return [admin for admin in model["games"][name].get("admins", []) if admin.lower() not in have]


def admin_command(model: dict, name: str, admin: str) -> str:
    """The console line that makes a player an admin."""
    return ADMIN_COMMAND[kind(model, name)].format(admin)


def address(plan: dict, name: str, model: dict, by_name: bool = False) -> str:
    """What a player types into the game: the server and its port."""
    host = f"{name}.{plan['domain']}" if by_name else plan["guests"][name]["ip"]
    return f"{host}:{port(model, name)[0]}"


def _checks(plan: dict, model: dict, name: str) -> list[tuple[str, str, str]]:
    """(label, command in the game's guest, expected output)."""
    game = model["games"][name]
    field = 'python3 -c \'import json, sys; s = json.load(sys.stdin); print(s["version"], s["running"])\''
    out = [(f"{name} runs version {game['version']} and answers", f"{NB_GAME} status | {field}", f"{game['version']} True")]
    if kind(model, name) == "minecraft":
        for host in MOJANG:
            out.append((f"{name} reaches {host} through the proxy", f"/usr/local/sbin/nb-tls-tunnel check {host} 2>&1", "ok"))
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
        try:
            rc, out, err = run(target, command)
        except power.Off as asleep:
            # One game off on demand says nothing of the others.
            results.append(Result("SKIP", label, power.off_reason(asleep.title)))
            continue
        got = out.strip()
        results.append(Result("PASS", label) if got == want else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:120]))
    return results


# What the page and its agent change (web/server.py): each server's
# settings, and which further servers there are.


def _field(option: dict, default: object, value: object) -> dict:
    """An option as the page's form takes it."""
    how = "choice" if "choices" in option else "switch" if isinstance(default, bool) else "number" if "min" in option else "text"
    return {
        "key": option["key"],
        "label": option["label"],
        "type": how,
        "value": value,
        "default": default,
        "new_world": option.get("new_world", False),
        **({"choices": [list(c) for c in option["choices"]]} if how == "choice" else {}),
        **({"min": option["min"], "max": option["max"]} if how == "number" else {}),
        **({"optional": True} if option.get("optional") else {}),
    }


def options(model: dict, name: str) -> list[dict]:
    """A server's OPTIONS for the page, each with its value now."""
    have, start = model["games"][name]["settings"], defaults(model, name)
    out = []
    for option in OPTIONS[kind(model, name)]:
        default = start.get(option["key"], option.get("default"))
        out.append(_field(option, default, have.get(option["key"], default)))
    return out


def new_world_keys(game: str) -> list[str]:
    """Every setting that counts only when a world is made: what New world
    asks, and what the settings say of the server's first world."""
    return list(dict.fromkeys([o["key"] for o in NEW_WORLD[game]] + [o["key"] for o in OPTIONS[game] if o.get("new_world")]))


def new_world_options(model: dict, name: str) -> list[dict]:
    """What the page's New world asks for a server, each starting at the
    game's own default: a new world is not the first one over again."""
    return [_field(option, option["default"], option["default"]) for option in NEW_WORLD[kind(model, name)]]


def read_world_title(value: object) -> str:
    title = " ".join(str(value or "").split())
    if not 1 <= len(title) <= MAX_WORLD_TITLE or re.search(r"[\x00-\x1f\x7f]", title):
        raise NullBillError(f"give the world a name of 1 to {MAX_WORLD_TITLE} characters")
    return title


def read_new_world(model: dict, name: str, chosen: object) -> list[str]:
    """New world's form as `nb-game world new` takes it: KEY=VALUE for
    each option that differs from the game's default. Raises everything
    wrong with it together."""
    if chosen is not None and not isinstance(chosen, dict):
        raise NullBillError("options are the new world's own, by name")
    known = {o["key"]: o for o in new_world_options(model, name)}
    problems, out = [], []
    for key, value in (chosen or {}).items():
        if key not in known:
            problems.append(f"{str(key)[:40]!r} is not something a new {TITLE[kind(model, name)]} world takes; it takes {', '.join(known)}")
            continue
        option = known[key]
        if option.get("optional") and str(value).strip() == "":
            continue
        try:
            value = _option_value(option, value)
        except ValueError as exc:
            problems.append(f"{option['label']} is {exc}")
            continue
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if len(text) > MAX_VALUE or re.search(r"[\x00-\x1f\x7f]", text):
            problems.append(f"{option['label']} is one line of at most {MAX_VALUE} characters")
        elif value != option["default"]:
            out.append(f"{key}={text}")
    if problems:
        raise Refused(f"The new {WORLD[kind(model, name)]} was not made", problems)
    return out


def guest_command(plan: dict, name: str, *args: str) -> str:
    """nb-game in a game server's guest, as the host runs it."""
    return shlex.join(["pct", "exec", str(plan["guests"][name]["vmid"]), "--", NB_GAME, *args])


def read_console(value: object) -> str:
    """One line for a server's console."""
    line = str(value or "").strip()
    if not line or len(line) > MAX_CONSOLE or re.search(r"[\x00-\x1f\x7f]", line):
        raise NullBillError(f"a console line is one line of at most {MAX_CONSOLE} characters")
    return line


def other_settings(model: dict, name: str) -> str:
    """The settings OPTIONS does not name, a line each, as the page's box
    for them holds them."""
    known = {o["key"] for o in OPTIONS[kind(model, name)]}

    def text(value: object) -> str:
        return str(value).lower() if isinstance(value, bool) else str(value)

    return "".join(f"{key} = {text(value)}\n" for key, value in model["games"][name]["settings"].items() if key not in known)


def _typed(text: str) -> str | int | float | bool:
    """A value typed into the box, as config.yaml would hold it."""
    text = text.strip()
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if re.fullmatch(r"-?[0-9]{1,15}", text):
        return int(text)
    if re.fullmatch(r"-?[0-9]{1,15}\.[0-9]{1,9}", text):
        return float(text)
    return text


def _option_value(option: dict, value: object) -> object:
    """A named option's value in its own type; ValueError says what it takes."""
    if option["type"] == "choice":
        for choice, _ in option["choices"]:
            if str(value).strip().lower() == str(choice).lower():
                return choice
        raise ValueError("one of " + ", ".join(str(c) for c, _ in option["choices"]))
    if option["type"] == "switch":
        if isinstance(value, bool) or str(value).strip().lower() in ("true", "false"):
            return value if isinstance(value, bool) else str(value).strip().lower() == "true"
        raise ValueError("true or false")
    if option["type"] == "number":
        try:
            number = int(str(value).strip())
        except ValueError:
            number = None
        if number is None or isinstance(value, bool) or not option["min"] <= number <= option["max"]:
            raise ValueError(f"a whole number from {option['min']} to {option['max']}")
        return number
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value).strip()


def read_settings(model: dict, name: str, chosen: object, other: object = "") -> dict:
    """A server's settings from a form: `chosen` by name, and `other` as
    lines of `name = value` for what OPTIONS does not offer. What equals
    the server's start is left out, so config.yaml holds only what the
    owner changed. Raises everything wrong with it together."""
    title, game = model["games"][name]["title"], kind(model, name)
    named = {o["key"]: o for o in options(model, name)}
    problems: list[str] = []
    found: dict = {}
    if chosen is not None and not isinstance(chosen, dict):
        raise NullBillError(f"{title}: settings are the server's own options by name")
    for number, line in enumerate(str(other or "").splitlines(), 1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        key, equals, value = line.partition("=")
        if not equals:
            problems.append(f"{title}, other settings, line {number}: write it as name = value")
            continue
        found[key.strip()] = _typed(value)
    # What the form names itself wins over a line in the box.
    found |= chosen or {}
    out: dict = {}
    for key, value in found.items():
        if not isinstance(key, str) or not SETTING.fullmatch(key):
            problems.append(f"{title}: {str(key)[:40]!r} is not the name of a setting")
        elif managed(game, key):
            problems.append(f"{title}: NullBill sets {key} itself, so that only the people you let in reach the server")
        elif isinstance(value, (dict, list)) or value is None:
            problems.append(f"{title}: {key} takes one value")
        elif len(str(value)) > MAX_VALUE or "\n" in str(value) or "\r" in str(value):
            problems.append(f"{title}: {key} is one line of at most {MAX_VALUE} characters")
        else:
            try:
                out[key] = _option_value(named[key], value) if key in named else value
            except ValueError as exc:
                problems.append(f"{title}: {named[key]['label']} ({key}) is {exc}")
    if len(out) > MAX_SETTINGS:
        problems.append(f"{title}: at most {MAX_SETTINGS} settings")
    if problems:
        raise NullBillError(*problems)
    start = defaults(model, name) | {key: o["default"] for key, o in named.items()}
    return {key: value for key, value in out.items() if key not in start or start[key] != value or type(start[key]) is not type(value)}


def floor_mib(model: dict, name: str) -> int:
    return model["services"][name]["floor"]["memory_mib"]


def read_memory(model: dict, name: str, value: object) -> int | None:
    """A server's memory from a form, in MiB; None for the game's own."""
    title, least = model["games"][name]["title"], floor_mib(model, name)
    if value in (None, ""):
        return None
    try:
        asked = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        asked = None
    if asked is None or isinstance(value, bool) or not least <= asked <= MAX_MEMORY_MIB:
        raise NullBillError(
            f"{title}: its memory is from {least / 1024:g} to {MAX_MEMORY_MIB / 1024:g} GiB ({least} to {MAX_MEMORY_MIB} MiB)"
        )
    return None if asked == least else asked


def read_title(model: dict, name: str | None, value: object) -> str:
    """A server's title: a line of its own that no other server has."""
    title = " ".join(str(value or "").split())
    if not 1 <= len(title) <= MAX_TITLE:
        raise NullBillError(f"a game server's name is 1 to {MAX_TITLE} characters")
    if any(other != name and game["title"].lower() == title.lower() for other, game in model["games"].items()):
        raise NullBillError(f"another game server is called {title} already; give this one a name of its own")
    return title


def describe(model: dict, name: str, on: bool = True) -> list[str]:
    """A game server line by line, for the agent's request to the owner."""
    game, svc, of = model["games"][name], model["services"][name], kind(model, name)
    size = svc["floor"] | model.get("overrides", {}).get(name, {})
    number, proto = port(model, name)
    lines = [
        f"A {TITLE[of]} {game['version']} server of its own, called {game['title']}, with its own world.",
        f"A container in the games zone: {size['cores']} core{'' if size['cores'] == 1 else 's'}, "
        f"{size['memory_mib'] / 1024:.1f} GiB memory, {size['disk_gib']} GiB disk.",
        f"Opens {proto}/{number} to you and the people you tick it for, as every {TITLE[of]} server does. No program comes with it "
        "but the game's own server, pinned by NullBill.",
    ]
    if game["settings"]:
        lines.append("Settings: " + ", ".join(f"{key} {value}" for key, value in game["settings"].items()) + ".")
    if game.get("admins"):
        lines.append("Admins: " + ", ".join(game["admins"]) + ".")
    lines.append("It is on: the next setup run makes it." if on else "It is off until it is ticked under Setup, What to run.")
    return lines


def add(config: dict, model: dict, game: str, title: object) -> tuple[dict, str]:
    """config.yaml with one more server of a game, and the server's name.
    The name comes from the title; the address from the range of the
    owner's own."""
    kinds = [n for n in names(model) if n not in more(model)]
    if game not in kinds:
        raise NullBillError(f"NullBill has no server for {str(game)[:40]!r}; it has {', '.join(kinds)}")
    title = read_title(model, None, title)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    stem = (slug if slug.startswith(game) else f"{game}-{slug}" if slug else game)[:28].strip("-")
    taken = {*model["services"], *model["games"], *(svc["workload"] for svc in model["services"].values()), *custom.RESERVED}
    name = next(n for n in (stem, *(f"{stem}-{i}" for i in range(2, 1000))) if n not in taken)
    number = custom.free_id(model)
    if number is None:
        raise NullBillError(
            f"all {len(custom.OWN_IDS)} addresses for further game servers and services of your own are taken; remove one you no longer use"
        )
    out = copy.deepcopy(config)
    out.setdefault("games", {})[name] = {"game": game, "id": number, "title": title}
    return out, name


def forget(config: dict, model: dict, name: str) -> dict:
    """config.yaml without one of the further servers: its entry, its rule
    for when it runs, its size, its tick and each person's tick for it."""
    if name not in more(model):
        raise NullBillError(f"{name} is not a further game server; NullBill's own games are turned off under Setup, What to run")
    out = copy.deepcopy(config)
    for section in ("games", "power", "overrides"):
        if name in (out.get(section) or {}):
            del out[section][name]
            if not out[section]:
                del out[section]
    if "workloads" in (out.get("intent") or {}):
        out["intent"]["workloads"] = [w for w in out["intent"]["workloads"] if w != name]
    for person in (out.get("people") or {}).values():
        if name in person.get("services", []):
            person["services"] = [s for s in person["services"] if s != name]
            if not person["services"]:
                del person["services"]
    return out
