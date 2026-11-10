"""What the owner turns on and off under Setup, What to run.

Each choice is a workload from catalog.yaml (`intent.workloads`) or one of
the two extras (backups, the on-site screen). For a config, saved or only
ticked on the page, `options` says what each makes, what it takes from the
host's memory by plan's own counting, what it needs from the owner, what
turning it off deletes, and which of its guests the last setup run left on
the server. Sizes come from the plan where it fits and from the catalog's
floors and the owner's overrides where it does not, so a choice that does
not fit still shows what it would take.

Besides the catalog's workloads there are the owner's own: a workload only
services config.yaml added have (nullbill/custom.py) is a choice like any
other, with what NullBill knows of it in place of the catalog's text. A
further server of a game (games.more) is a choice too, with the game's text
and its own title; every game server's row carries what the page lets the
owner set for it (`game`: its settings, admins and memory).

`power` in its answer is When it runs: each planned workload that can be
stopped (catalog.yaml's `power`, nullbill/power.py), whether it always runs
or runs on demand and how it stops again. The memory meter counts what
always runs and shows what runs on demand as sharing what that leaves.
"""

from pathlib import Path

from nullbill import backups, custom, games, power, torrents
from nullbill import model as modelling
from nullbill import plan as planning
from nullbill.errors import NullBillError
from nullbill.flows import TORRENTS_PORT
from nullbill.render import strip_notes

# NullBill's own, in the page's order; the owner's follow (custom.workloads).
WORKLOADS = ["agents", "linux-ci", "inference", "media", "torrents", "windows-ci", "windows-dev", "minecraft", "terraria", "factorio"]
BACKUPS, CONSOLE = "backups", "console"
PBS = "pbs"
# apply makes these guests but nothing in them yet (README, Not done yet).
UNFINISHED = {"windows-ci": "Not finished in this version: setup would make an empty VM that nothing uses."}

# title, what: the row. needs: what the owner supplies. on: what setup does
# and what the owner does afterwards. off: what the next setup run deletes
# and what stays.
TEXT = {
    "agents": {
        "title": "Workspace",
        "what": "A Debian container for your work and for coding agents, with no secrets on it.",
        "needs": [],
        "on": (
            "Setup makes the container. It takes SSH keys only: add yours under Access, SSH keys, "
            "and each setup run puts them on it. The Services page has the address, under Workspace."
        ),
        "off": (
            "Deletes the workspace with everything in it: checked-out repositories, files and installed tools. "
            "Push work you want to keep to Forgejo first. The agent beside this page and the coding agent in the operator stay."
        ),
    },
    "linux-ci": {
        "title": "Linux CI runners",
        "what": "Each Forgejo Actions job runs in a fresh Linux VM that is destroyed afterwards.",
        "needs": [],
        "on": (
            "Setup makes the dispatcher and builds the runner image, and rebuilds the image every week. "
            "In a Forgejo repository, turn on Actions and add a workflow with runs-on: linux. "
            "Memory left over by your other choices goes to more runners at a time."
        ),
        "off": (
            "Deletes the dispatcher and the runner image; nothing of yours is on them. "
            "Jobs with runs-on: linux then wait in Forgejo for a runner. Repositories, workflows and old job logs stay in Forgejo."
        ),
    },
    "inference": {
        "title": "Local AI models",
        "what": "One open model on the NVIDIA graphics card, behind an OpenAI-compatible API.",
        "needs": [
            "An NVIDIA graphics card in the server.",
            "Secure Boot turned off in the firmware: the card's driver is built on the server.",
        ],
        "on": (
            "Setup installs the card's driver on Proxmox, builds llama.cpp and downloads the model; "
            "the first time, this is the longest part of setup. If it stops because the old nouveau driver holds the card, "
            "restart the server once and run setup again. The address and the key are on the Services page, under Local AI model."
        ),
        "off": (
            "Deletes both containers with the downloaded model; turning it on again builds and downloads everything anew. "
            "The API key stays the same, and the card's driver stays installed on Proxmox."
        ),
    },
    "media": {
        "title": "Media server (Jellyfin)",
        "what": "Plays the films, series and music on the disks you keep, in the browser and in Jellyfin's apps.",
        "needs": ["A disk with your media, kept under Other disks. Without one Jellyfin starts with an empty library."],
        "on": (
            "Setup installs Jellyfin, finishes its first-run setup and makes its admin. "
            "Then add your folders as libraries in Jellyfin; its sign-in is on the Services page. "
            "Jellyfin can only read the kept disks."
        ),
        "off": ("Deletes Jellyfin with its libraries, users, watch history and artwork. The files on your kept disks are not touched."),
    },
    "torrents": {
        "title": "Downloads (qBittorrent)",
        "what": "A BitTorrent client whose finished downloads land on a kept disk, where Jellyfin and the file share see them.",
        "needs": [
            "A kept disk you may change files on (Setup, Hardware, Other disks): downloads go into one folder of it.",
            "Your choice of the way downloads go, beside this tick: through a WireGuard VPN of your own, or from your home connection.",
        ],
        "on": (
            "Setup installs qBittorrent in a container of its own, which sees one folder of the kept disk and nothing else of yours. "
            "Its page and sign-in are on the Services page. Downloads pause by themselves before the disk is full."
        ),
        "off": (
            "Deletes the client with its list of downloads and its settings. "
            "The downloaded files stay on the kept disk, and so does a saved VPN file."
        ),
    },
    "windows-ci": {
        "title": "Windows CI runners",
        "what": "Forgejo Actions jobs in throwaway Windows VMs. Not finished yet.",
        "needs": ["The Windows 11 ISO uploaded in Proxmox.", "A Windows licence."],
        "on": "Setup makes the runner VM, but does not install Windows or a runner in it yet. Leave it off until NullBill does.",
        "off": "Deletes the empty runner VM. Nothing of yours is on it.",
    },
    "windows-dev": {
        "title": "Windows desktop",
        "what": "A Windows 11 VM for you, reached with Remote Desktop over Tailscale.",
        "needs": [
            "The Windows 11 ISO uploaded in Proxmox (local, ISO Images) before setup runs, with Win in its name.",
            "A Windows licence, if you want Windows activated.",
        ],
        "on": (
            "Setup makes the VM. You install Windows in it once, from its console in Proxmox, and turn on Remote Desktop; "
            "docs/runbooks/windows-desktop.md has each step."
        ),
        "off": ("Deletes the VM and its disk: Windows, your programs and every file in it. Copy out what you want to keep first."),
    },
    "minecraft": {
        "title": "Minecraft",
        "what": "A Minecraft: Java Edition world for you and the people you let in.",
        "needs": [
            "Your agreement to Minecraft's EULA, ticked beside this choice, once for every Minecraft server.",
            "A Minecraft: Java Edition account for each player, as for any server.",
        ],
        "on": (
            "Setup installs Mojang's own server in a container of its own and makes a new world. "
            "The address is on the Services page; under Access, People, tick it for each friend who may join. "
            "Its memory is for a small world and a few players; Memory in its settings gives it more."
        ),
        "off": (
            "Deletes the server with every world in its list, every player's inventory and its settings. "
            "Download a world first from the server's row on the Services page."
        ),
    },
    "terraria": {
        "title": "Terraria",
        "what": "A Terraria world for you and the people you let in.",
        "needs": ["Terraria on a computer for each player, as for any server."],
        "on": (
            "Setup installs Re-Logic's own server in a container of its own and makes a new world. "
            "The address is on the Services page; under Access, People, tick it for each friend who may join."
        ),
        "off": (
            "Deletes the server with every world in its list. Characters stay with their players. "
            "Download a world first from the server's row on the Services page."
        ),
    },
    "factorio": {
        "title": "Factorio",
        "what": "A Factorio factory for you and the people you let in.",
        "needs": ["Factorio and a factorio.com account for each player, as for any server."],
        "on": (
            "Setup installs Wube's own server in a container of its own and makes a new map of the base game. "
            "The address is on the Services page; under Access, People, tick it for each friend who may join."
        ),
        "off": "Deletes the server with every map in its list. Download a map first from the server's row on the Services page.",
    },
    BACKUPS: {
        "title": "Nightly backups",
        "what": "Every service is backed up each night, encrypted, and each day one backup is opened again to prove it can be.",
        "needs": [
            "Nothing more to start with. Without a disk of their own the backups sit on the server's disk: that brings back "
            "a broken service, a bad update or a mistake, and is lost with the disk. A second disk, USB too, takes them over later."
        ],
        "on": (
            "Setup makes the backup server and runs the first backups. The Backups page says where they go, shows each night's "
            "result and holds the key to keep off the server. Kept disks and the local AI models are not backed up."
        ),
        "off": "Deletes the backup server and every backup it holds. The guests keep running, without backups.",
    },
    CONSOLE: {
        "title": "On-site screen",
        "what": "This page on a monitor plugged into the server, with a keyboard and mouse.",
        "needs": ["A monitor on the motherboard's output when the processor has graphics, else on the graphics card."],
        "on": (
            "Setup puts a browser that shows only this page on the server's own screen. "
            "Its memory comes out of what is left to Proxmox. Ctrl+Alt+F2 still opens Proxmox's text console."
        ),
        "off": "Removes the browser from the server's screen, which shows Proxmox's text console again. Nothing is deleted.",
    },
}


# A service of the owner's own: what turning it on and off does.
OWN_ON = (
    "Setup makes its guest and runs its setup script in it as root. The program is not NullBill's: NullBill keeps the guest "
    "updated, watched and backed up, opens only the ports listed here, and its checks cover those and that the script ran."
)
OWN_OFF = (
    "Deletes its guest and everything in it. What it is and its setup script stay with NullBill, so turning it on again "
    "builds it anew; Remove takes those away too."
)


def option_of(name: str, svc: dict, own: list[str] = ()) -> str | None:
    """The choice a service belongs to; None for what is always planned.
    own: the workloads config.yaml added (`added`)."""
    if name == PBS:
        return BACKUPS
    return svc["workload"] if svc["workload"] in WORKLOADS or svc["workload"] in own else None


def added(model: dict) -> list[str]:
    """The choices config.yaml adds to the catalog's, in the page's order:
    further game servers, then the owner's own services' workloads."""
    return [*games.more(model), *custom.workloads(model)]


def _text(model: dict, name: str) -> dict:
    """A choice's row: the catalog's text, or for a workload of the owner's
    own what config.yaml says of it."""
    if name in TEXT:
        return TEXT[name]
    if name in model["games"]:
        return TEXT[games.kind(model, name)] | {"title": model["games"][name]["title"]}
    members = [n for n, svc in model["services"].items() if svc["workload"] == name]
    rule = model["power"].get(name) or {}
    one = members[0] if len(members) == 1 else None
    return {
        "title": rule.get("title") or (custom.title(model, one) if one else name),
        "what": (model["services"][one].get("description") if one else "") or "A service of your own.",
        "needs": [],
        "on": OWN_ON,
        "off": OWN_OFF,
    }


def _own(model: dict, members: list[str], home: Path | None) -> dict:
    """What a row of the owner's own shows besides: each of its services
    line by line, and which have a setup script to read."""
    scripts = {n: custom.read_script(home, n) for n in members}
    return {
        "facts": [line for n in members for line in custom.describe(model, n, scripts[n])],
        "scripts": [n for n in members if scripts[n] is not None],
    }


def _size(model: dict, name: str, guest: dict | None) -> dict:
    """A guest's size: the plan's, or its floor with the owner's overrides."""
    svc = model["services"][name]
    over = model.get("overrides", {}).get(name, {})
    size = {key: (guest or {}).get(key) or over.get(key) or svc["floor"][key] for key in planning.SIZE_KEYS}
    count = (guest or {}).get("count") or over.get("count") or 1
    return {"name": name, "kind": svc["kind"], **size, "count": count if svc["kind"] == "vm-pool" else None}


def _blocked(name: str, facts: dict | None) -> str | None:
    if name in UNFINISHED:
        return UNFINISHED[name]
    if name == "inference" and facts and not any(not g["integrated"] and g["vendor"] == "nvidia" for g in facts["gpus"]):
        return "This server has no NVIDIA graphics card."
    return None


def _note(name: str, plan: dict | None) -> str | None:
    """What the plan decided for a choice that is on, in a line."""
    if not plan:
        return None
    guests = plan["guests"]
    if name == "linux-ci" and "linux-runner" in guests:
        count = guests["linux-runner"]["count"]
        return f"{count} job{'' if count == 1 else 's'} at a time: memory your other choices leave free goes to more runners."
    if name == "inference" and plan["inference"]:
        chosen = plan["inference"]
        cards = " and ".join(plan["gpus"][pci]["name"] for pci in chosen["gpus"])
        return f"Runs {chosen['model']}, a {chosen['model_mib'] / 1024:.1f} GiB download, on the {cards}."
    if name == "media" and plan["media"]:
        on_gpu = isinstance(plan["media"]["transcode"], dict)
        how = "Transcodes on the processor's graphics." if on_gpu else "Transcodes on the processor: no integrated Intel graphics found."
        return how if plan["media"]["library"] else f"No kept disk yet, so its library starts empty. {how}"
    if name == torrents.WORKLOAD and plan["torrents"]:
        found = plan["torrents"]
        way = torrents.WHAT_VPN if found["route"] == torrents.VPN else f"From your home connection. {torrents.WHAT_HOME}"
        if "port" in found:
            way += (
                f" The server asks your home router to open port {found['port']} for it."
                if found["port_mapping"]
                else f" Forward port {found['port']} (TCP and UDP) on your home router yourself, or others cannot connect to it."
            )
        return f"Downloads land in {found['store']['folder']} on the kept disk {found['store']['share']}. {way}"
    if name in plan["games"]:
        game = plan["games"][name]
        note = f"Players need {games.CLIENT[game.get('game', name)]} {game['version']}, the same version as the server."
        if "heap_mib" in game:
            note += f" Java gets {game['heap_mib'] / 1024:.1f} GiB of the container's memory for the world."
        if "space_age" in game:
            note += (
                " Every player needs the Space Age expansion."
                if game["space_age"]
                else " The base game: nobody needs the Space Age expansion."
            )
        return note
    if name == BACKUPS and PBS in guests:
        found = plan["backups"]
        if found["status"] == backups.OK:
            return f"Backs up to {found['target'].removeprefix('pbs datastore on ')}, a disk of their own."
        if found["status"] == backups.SAME_DISK:
            return f"Backs up to the server's own disk, up to {found['size_gib']} GiB. The Backups page says how to give them a disk."
        return f"Nothing is backed up: no disk for backups, and under {backups.MIN_GIB} GiB left on the server's disk."
    if name == CONSOLE and plan["host"]["console"].get("enabled"):
        return f"Shown by the {plan['host']['console']['screen']}."
    return None


def _game(model: dict, name: str) -> dict | None:
    """What the page asks about a game server beside its tick: the
    agreement the game needs, who its admins are, its settings and its
    memory. None for every other choice."""
    game = model["games"].get(name)
    if game is None:
        return None
    of = games.kind(model, name)
    out: dict = {
        "kind": of,
        "kind_title": games.TITLE[of],
        "more": of != name,
        "file": games.SETTINGS_FILE[of],
        "options": games.options(model, name),
        "other": games.other_settings(model, name),
        "memory_mib": model.get("overrides", {}).get(name, {}).get("memory_mib") or games.floor_mib(model, name),
        "floor_mib": games.floor_mib(model, name),
        "max_mib": games.MAX_MEMORY_MIB,
    }
    if "eula" in game:
        out["eula"] = {"agreed": game["eula"], "url": games.EULA, "text": f"I agree to the {games.TITLE[of]} EULA"}
    if "admins" in game:
        out["admins"] = game["admins"]
    return out


def _torrents(model: dict, facts: dict | None) -> dict:
    """What the page asks beside the download client's tick: which way its
    traffic goes (no default), the VPN's server once a file is saved (the
    file itself is a secret), and where downloads land."""
    chosen = model["torrents"]
    shares = torrents.kept(model, facts)
    can = torrents.writable(shares)
    return {
        "route": chosen.get("route"),
        "endpoint": (chosen.get("vpn") or {}).get("endpoint"),
        "notice": torrents.NOTICE,
        "vpn": f"{torrents.WHAT_VPN} The firewall lets its container reach the VPN's server and nothing else outside.",
        "home": f"{torrents.WHAT_HOME} The checks then report it as degraded.",
        "shares": can,
        "read_only": [m["share"] for m in shares if m["share"] not in can],
        "share": chosen.get("share") or (can[0] if len(can) == 1 else None),
        "folder": chosen["folder"],
        "keep_free_gib": chosen["keep_free_gib"],
        # From home only: the port others connect to, and whether the
        # server asks the home router for it.
        "port": TORRENTS_PORT,
        "port_mapping": chosen["port_mapping"],
    }


def _when(model: dict, planned: dict, counted_for: dict[str, int]) -> list[dict]:
    """When it runs: a row per planned workload other than core.
    counted_for: what the plan counts for each guest."""
    rows = []
    own = custom.workloads(model)
    order = [*WORKLOADS, *games.more(model)]
    found = dict.fromkeys(svc["workload"] for svc in planned.values() if svc["workload"] != "core")
    # In the page's order, then what is not a choice there (the kept disks' shares).
    for workload in [*(w for w in order if w in found), *(w for w in found if w not in order)]:
        members = [n for n, svc in planned.items() if svc["workload"] == workload]
        rule = {"title": workload, **modelling.POWER_DEFAULTS, **model["power"].get(workload, {})}
        kinds = {planned[n]["kind"] for n in members}
        blocked = None
        if kinds == {"vm-pool"}:
            blocked = "Nothing to stop: its VMs exist only while a job runs."
        elif workload in UNFINISHED:
            blocked = UNFINISHED[workload]
        # A container says whether it is in use and a pool's clones show
        # it; a VM alone shows only its CPU (power.groups).
        sees_use = kinds != {"vm"} or "cpu_pct" in rule.get("busy", {})
        # One of the owner's own that listens on no TCP port shows no use either.
        blind = workload in own and not any(custom.shows_use(planned[n]) for n in members)
        rows.append(
            {
                "name": workload,
                "title": rule["title"],
                "run": rule["run"],
                "stop": rule["stop"],
                "idle_minutes": rule["idle_minutes"],
                "guests": members,
                "memory_mib": sum(counted_for[n] for n in members),
                "blocked": blocked,
                "auto": sees_use and not blind,
                "auto_why": "It listens on no TCP port, so nothing shows whether someone is using it, and it never stops by itself."
                if blind
                else None
                if sees_use
                else (
                    "A VM does not say whether someone is using it, so it never stops by itself. "
                    f"To let it, set power.{workload}.busy.cpu_pct in config.yaml (docs/runbooks/on-demand.md)."
                ),
            }
        )
    return rows


def options(model: dict, facts: dict | None, existing: set[str] | None, home: Path | None = None) -> dict:
    """Each choice and the memory they come to, for `model` (a merged config).
    home: where the owner's own services keep their setup scripts."""
    errors: list[str] = []
    plan = None
    if facts:
        try:
            plan = strip_notes(planning.build_plan(model, facts))
        except NullBillError as exc:
            errors = exc.messages
    planned = planning.planned_services(model, facts)
    share = model["host"].get("container_share_pct", 100)
    console = model["host"]["console"]

    def counted(size: dict) -> int:
        return planning.counted_mib(size["kind"], size["memory_mib"], share, size["count"] or 1)

    def known(names: list[str]) -> list[str] | None:
        return None if existing is None else [n for n in names if n in existing]

    own = custom.workloads(model)
    more = games.more(model)
    choices = [*more, *own]
    items = []
    for name in [*WORKLOADS, *choices, BACKUPS, CONSOLE]:
        if name == CONSOLE:
            members: list[str] = []
            chosen = console["enabled"]
        elif name == BACKUPS:
            members = [PBS] if PBS in model["services"] else []
            chosen = PBS in planned
        else:
            members = [n for n, svc in model["services"].items() if svc["workload"] == name]
            chosen = any(n in planned for n in members)
        # The row's memory and disk are the least the choice takes: a pool
        # at one clone, plus its image on disk. The plan may give it more.
        least = [_size(model, n, None) for n in members]
        game = _game(model, name)
        here = known(members) if members else None
        if game and game["more"]:
            # Like a service of the owner's own: gone only once it is off
            # and a setup run has deleted its guest.
            game["removable"] = not chosen and not here
        items.append(
            {
                "name": name,
                "group": "extras" if name in (BACKUPS, CONSOLE) else "own" if name in own else "games" if game else "workloads",
                "extra": name in (BACKUPS, CONSOLE),
                "game": game,
                "torrents": _torrents(model, facts) if name == torrents.WORKLOAD else None,
                "own": _own(model, members, home) | {"removable": not chosen and not here} if name in own else None,
                **_text(model, name),
                "chosen": chosen,
                "guests": [_size(model, n, plan["guests"].get(n) if plan else None) for n in members],
                "memory_mib": console["memory_mib"] if name == CONSOLE else sum(counted(s) for s in least),
                "disk_gib": sum(s["disk_gib"] * ((s["count"] or 0) + 1) for s in least),
                "on_server": here,
                "blocked": _blocked(name, facts),
                "note": _note(name, plan) if chosen else None,
            }
        )

    least = {n: counted(_size(model, n, None)) for n in planned}
    when = _when(model, planned, least)
    # What runs on demand shares what the rest leaves (power.groups says
    # which rules count; one that cannot be stopped always runs).
    demand = [w for w in when if w["run"] == power.ON_DEMAND and not w["blocked"]]
    parked = {n for w in demand for n in w["guests"]}
    memory = None
    if facts:
        base = [n for n, svc in planned.items() if option_of(n, svc, choices) is None and n not in parked]
        optional = [n for n, svc in planned.items() if option_of(n, svc, choices) is not None and n not in parked]
        reserve_mib = planning.reserve(model, facts)[0]
        base_mib = sum(least[n] for n in base)
        chosen_mib = sum(least[n] for n in optional)
        free_mib = facts["memory_mib"] - reserve_mib - base_mib - chosen_mib
        total = sum(w["memory_mib"] for w in demand)
        memory = {
            "host_mib": facts["memory_mib"],
            "reserve_mib": reserve_mib,
            "base_mib": base_mib,
            "chosen_mib": chosen_mib,
            # With everything on demand off; what runs on demand shares it.
            "free_mib": free_mib,
            "demand_mib": total,
            "demand_largest_mib": max((w["memory_mib"] for w in demand), default=0),
            "together": total <= free_mib,
            "share_pct": share,
            "base": base,
        }
    kinds = [n for n in games.names(model) if n not in more]
    return {
        "items": items,
        # What Add a game server offers, and why it cannot when it cannot.
        "games": {
            "kinds": [{"name": n, "title": games.TITLE[n]} for n in kinds],
            "full": None if custom.free_id(model) else "Every address for further servers is taken. Remove one you no longer use.",
        },
        "power": when,
        "memory": memory,
        "errors": errors,
        "adds": None if existing is None else sorted(set(planned) - existing),
        "removes": None if existing is None else sorted(existing - set(planned)),
    }


def loss(name: str, model: dict) -> str:
    """What deleting a guest loses, for the page's question before it does."""
    svc = model["services"].get(name)
    option = option_of(name, svc, added(model)) if svc else None
    return _text(model, option)["off"] if option else "Deletes the guest and everything on its disk."
