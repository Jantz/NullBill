"""Setup, Review: what a setup run started now would change on the server.

The page keeps the plan each setup run started with (jobs/<id>.plan.yaml);
the plan of the last run that finished is what the server was built from.
Compared with the plan of the saved choices, it says which guests this run
creates, deletes and changes, what happens to each disk, and which other
parts of the plan differ. Which guests exist comes from apply's own record
(apply.GUESTS) where there is one, since a run can stop half way.

Nothing here decides anything: apply builds from the config and the probe
as before. This is what the owner reads before starting it.
"""

import difflib

from nullbill import backups

# The plan's sections other than guests and disks, as the page names them.
SECTIONS = {
    "class": "the hardware class",
    "host": "the host's memory and screen",
    "disk": "guest storage",
    "domain": "the name domain",
    "zones": "network zones",
    "gpus": "graphics cards",
    "inference": "the local AI model",
    "media": "the media server",
    "games": "game servers",
    "backups": "backups",
    "flows": "firewall rules",
    "people": "people and what each may open",
    "updates": "automatic updates",
    "tailnet": "the Tailscale policy",
    "power": "what runs on demand",
    "owner_supplies": "what you supply",
}
# A guest's plan keys with a name and a unit; the rest are named as they are.
SIZES = {"memory_mib": "memory", "cores": "cores", "disk_gib": "disk"}
OTHER = {"ip": "its address changes", "ips": "its network legs change", "vmid": "its id in Proxmox changes"}
DIFF_LINES = 400


def _size(key: str, value: object) -> str:
    if key == "memory_mib" and isinstance(value, int):
        return f"{value / 1024:.1f} GiB" if value >= 1024 else f"{value} MiB"
    if key == "disk_gib":
        return f"{value} GiB"
    return str(value)


def _changed(was: dict, now: dict) -> list[str]:
    """How a guest's plan entry differs, a phrase per key."""
    out = []
    for key in dict.fromkeys([*SIZES, *now, *was]):
        before, after = was.get(key), now.get(key)
        if before == after:
            continue
        if key in SIZES and before is not None and after is not None:
            out.append(f"{SIZES[key]} {_size(key, before)} to {_size(key, after)}")
        elif key == "count" and before and after:
            out.append(f"{after} at a time, {before} before")
        elif key == "run":
            out.append("runs on demand from now on" if after else "always runs from now on")
        elif key == "zone":
            out.append(f"moves from the {before} zone to {after}")
        else:
            out.append(OTHER.get(key) or f"{key.replace('_', ' ')} changes")
    return out


def guests(now: dict, was: dict | None, existing: set[str] | None, losses: dict[str, str]) -> list[dict]:
    """A row per guest, in the plan's order, then those this run deletes.
    change: create, delete, change or keep. existing: the guests on the
    server, None when nothing says. losses: what deleting each one loses."""
    planned = now.get("guests") or {}
    built = (was or {}).get("guests") or {}
    if existing is None:
        existing = set(built)
    rows = []
    for name, guest in planned.items():
        detail: list[str] = []
        if name not in existing:
            change = "create"
        elif name in built and (detail := _changed(built[name], guest)):
            change = "change"
        else:
            change = "keep"
        rows.append(
            {
                "name": name,
                "change": change,
                "detail": detail,
                **{k: guest.get(k) for k in ("kind", "zone", "cores", "memory_mib", "count")},
            }
        )
    for name in sorted(existing - set(planned)):
        old = built.get(name) or {}
        note = losses.get(name) or "Deletes the guest and everything on its disk."
        rows.append(
            {
                "name": name,
                "change": "delete",
                "detail": [note],
                **{k: old.get(k) for k in ("kind", "zone", "cores", "memory_mib", "count")},
            }
        )
    return rows


def _modes(entry: dict) -> str:
    modes = {p.get("mode", "ro") for p in entry.get("partitions") or []}
    return "its files can be changed" if modes == {"rw"} else "read-only" if modes <= {"ro"} else "some partitions read-only"


def disks(now: dict, was: dict | None, facts: dict) -> list[dict]:
    """A row per disk this run does something to, or keeps sharing.
    erases: the run wipes it."""
    probed = {d["id"]: d for d in facts.get("disks", [])}
    before = (was or {}).get("disks") or {}
    rows = []
    for disk_id, entry in (now.get("disks") or {}).items():
        use, old = entry["use"], before.get(disk_id)
        if use == "system" or (old is not None and old == entry):
            continue
        vgs = probed.get(disk_id, {}).get("vgs", [])
        erases = False
        if use in ("pbs", "storage"):
            what = "the backup disk" if use == "pbs" else f"the storage {entry.get('storage', '')}".rstrip()
            made = (backups.DISK_STORAGE if use == "pbs" else entry.get("storage")) in vgs
            if made:
                if old is None or old.get("use") == use:
                    continue  # apply made it already; nothing happens to it
                text = f"Stays {what}."
            elif entry.get("signatures"):
                erases = True
                converts = entry.get("converts") or {}
                if converts.get("copies"):
                    text = f"Its files are copied to {converts['evacuate']} and checked first; then it is erased and becomes {what}."
                else:
                    text = f"It is erased and becomes {what}. Everything on it is lost."
            else:
                text = f"Blank; it becomes {what}."
        elif use == "legacy":
            shared = f"its files are shared, {_modes(entry)}"
            if old is not None and old.get("use") == "legacy":
                text = f"Stays kept; from now on {shared}."
            elif was is not None:
                text = f"Kept from now on: {shared}. Nothing on it is erased."
            else:
                text = f"Kept: {shared}. Nothing on it is erased."
        elif old is not None and old.get("use") not in (use, "untouched", "unused", "ignore"):
            text = "Left alone from now on. What is on it stays."
        else:
            continue
        rows.append({"id": disk_id, "size_gib": entry.get("size_gib"), "text": text, "erases": erases})
    return rows


def sections(now: dict, was: dict | None) -> list[str]:
    """The other parts of the plan that differ from what was built."""
    if was is None:
        return []
    return [title for key, title in SECTIONS.items() if now.get(key) != was.get(key)]


def difference(was_text: str | None, now_text: str) -> str | None:
    """Every changed line of the plan, as a unified diff; None when the
    plan the server was built from is not known or nothing differs."""
    if was_text is None:
        return None
    body = list(difflib.unified_diff(was_text.splitlines(), now_text.splitlines(), "built", "planned", n=2, lineterm=""))
    if not body:
        return None
    if len(body) > DIFF_LINES:
        body = [*body[:DIFF_LINES], f"... and {len(body) - DIFF_LINES} more lines"]
    return "\n".join(body)
