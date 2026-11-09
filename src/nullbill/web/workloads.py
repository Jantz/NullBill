"""What the owner turns on and off under Setup, What to run.

Each choice is a workload from catalog.yaml (`intent.workloads`) or one of
the two extras (backups, the on-site screen). For a config, saved or only
ticked on the page, `options` says what each makes, what it takes from the
host's memory by plan's own counting, what it needs from the owner, what
turning it off deletes, and which of its guests the last setup run left on
the server. Sizes come from the plan where it fits and from the catalog's
floors and the owner's overrides where it does not, so a choice that does
not fit still shows what it would take.
"""

from nullbill import plan as planning
from nullbill.errors import NullBillError
from nullbill.render import strip_notes

# In the page's order.
WORKLOADS = ["agents", "linux-ci", "inference", "media", "windows-ci", "windows-dev"]
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
    BACKUPS: {
        "title": "Nightly backups",
        "what": "Every guest is backed up each night, encrypted, to a backup server on a disk of its own.",
        "needs": [
            "A second disk: a blank one is taken by itself, or choose one to erase under Other disks. "
            "Without one the backup server runs and nothing is backed up."
        ],
        "on": (
            "Setup makes the backup server and runs the first backups. Keep the encryption key somewhere off the server "
            "(docs/runbooks/pbs-encryption-key.md): without it the backups cannot be read. Kept disks are never backed up."
        ),
        "off": "Deletes the backup server and every backup on its disk. The guests keep running, without backups.",
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


def option_of(name: str, svc: dict) -> str | None:
    """The choice a service belongs to; None for what is always planned."""
    if name == PBS:
        return BACKUPS
    return svc["workload"] if svc["workload"] in WORKLOADS else None


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
    if name == BACKUPS and PBS in guests:
        if plan["backups"]["status"] == "ok":
            return f"Backs up to {plan['backups']['target'].removeprefix('pbs datastore on ')}."
        return "No disk for backups yet: nothing is backed up until you give it one under Other disks."
    if name == CONSOLE and plan["host"]["console"].get("enabled"):
        return f"Shown by the {plan['host']['console']['screen']}."
    return None


def options(model: dict, facts: dict | None, existing: set[str] | None) -> dict:
    """Each choice and the memory they come to, for `model` (a merged config)."""
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

    items = []
    for name in [*WORKLOADS, BACKUPS, CONSOLE]:
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
        items.append(
            {
                "name": name,
                "extra": name in (BACKUPS, CONSOLE),
                **TEXT[name],
                "chosen": chosen,
                "guests": [_size(model, n, plan["guests"].get(n) if plan else None) for n in members],
                "memory_mib": console["memory_mib"] if name == CONSOLE else sum(counted(s) for s in least),
                "disk_gib": sum(s["disk_gib"] * ((s["count"] or 0) + 1) for s in least),
                "on_server": known(members) if members else None,
                "blocked": _blocked(name, facts),
                "note": _note(name, plan) if chosen else None,
            }
        )

    memory = None
    if facts:
        base = [n for n, svc in planned.items() if option_of(n, svc) is None]
        optional = [n for n, svc in planned.items() if option_of(n, svc) is not None]
        reserve_mib = planning.reserve(model, facts)[0]
        base_mib = sum(counted(_size(model, n, None)) for n in base)
        chosen_mib = sum(counted(_size(model, n, None)) for n in optional)
        memory = {
            "host_mib": facts["memory_mib"],
            "reserve_mib": reserve_mib,
            "base_mib": base_mib,
            "chosen_mib": chosen_mib,
            "free_mib": facts["memory_mib"] - reserve_mib - base_mib - chosen_mib,
            "share_pct": share,
            "base": base,
        }
    return {
        "items": items,
        "memory": memory,
        "errors": errors,
        "adds": None if existing is None else sorted(set(planned) - existing),
        "removes": None if existing is None else sorted(existing - set(planned)),
    }


def loss(name: str, model: dict) -> str:
    """What deleting a guest loses, for the page's question before it does."""
    svc = model["services"].get(name)
    option = option_of(name, svc) if svc else None
    return TEXT[option]["off"] if option else "Deletes the guest and everything on its disk."
