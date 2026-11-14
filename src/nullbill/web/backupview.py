"""The page's Backups page (docs/adr/0017-backups-on-one-disk.md).

Three things, kept apart: what the owner chose (config.yaml and its plan),
what the last finished setup run built, and what the server measured since
(the host's nb-backup-metrics, read from Prometheus with when). The page
turns backups on and says where they go; turning them off deletes them and
stays with Setup, What to run, beside its question.
"""

import copy
import json
import time
from collections.abc import Callable

from nullbill import backups, legacy
from nullbill.errors import NullBillError
from nullbill.render import strip_notes

# When the owner last downloaded the key, and which key that was.
KEY_SAVED = "web/backup-key.json"
KEY_FILE = "nullbill-backup-key.json"
SYSTEM = "system"
MAX_GIB = 4096
# The pbs role's retention (ansible/roles/pbs/defaults/main.yml); a test
# keeps the two equal.
KEEPS = {"keep-daily": 7, "keep-weekly": 4, "keep-monthly": 6}

KEPT_WHY = (
    "It holds files you keep, and NullBill puts no backups on a disk it shares. "
    "Setup, What to run, Disks can copy its files off and erase it."
)


def chosen(config: dict | None) -> bool:
    return ((config or {}).get("services", {}).get("pbs") or {}).get("enabled", True) is not False


def where(plan: dict | None) -> dict | None:
    """Where a plan keeps the backups; None when it keeps none. A plan from
    before backups could live beside the guests names only its disk."""
    section = strip_notes(plan or {}).get("backups") or {}
    if section.get("status") not in (backups.OK, backups.SAME_DISK):
        return None
    disk = backups.disk(plan)
    return {
        "kind": "disk" if disk else SYSTEM,
        "disk": disk,
        "storage": section.get("storage", backups.DISK_STORAGE),
        "size_gib": section.get("size_gib"),
    }


def places(facts: dict | None, config: dict | None) -> list[dict]:
    """Each disk besides the system disk, as a place for the backups: made
    (it holds them), chosen (pinned for them), blank, erase (it holds
    something, and only the owner's typed word erases it), kept or storage
    (in use for something else, not offered)."""
    pins = (config or {}).get("disks") or {}
    disks = [d for d in (facts or {}).get("disks", []) if not d.get("system")]
    made = next((d["id"] for d in disks if backups.DISK_STORAGE in d.get("vgs", [])), None)
    out = []
    for disk in disks:
        use = (pins.get(disk["id"]) or {}).get("use")
        if disk["id"] == made:
            kind = "made"
        elif use == "pbs":
            kind = "chosen"
        elif use == "legacy" or legacy.kept_mounts(disk):
            kind = "kept"
        elif use == "storage":
            kind = "storage"
        else:
            kind = "erase" if disk["signatures"] else "blank"
        out.append(
            {
                "id": disk["id"],
                "kind": kind,
                "size_gib": disk["size_gib"],
                "rotational": disk.get("rotational", False),
                "health": (disk.get("smart") or {}).get("health", "unknown"),
                "holds": disk["signatures"],
                "confirm": disk["id"][-6:],
            }
        )
    return out


def key_view(secret: str | None, record: dict) -> dict:
    """The key backups are encrypted with: whether setup has made it, its
    fingerprint, and when this very key was last downloaded from the page."""
    if not secret:
        return {"have": False, "fingerprint": None, "saved_at": None}
    try:
        fingerprint = json.loads(secret).get("fingerprint")
    except (ValueError, AttributeError):
        fingerprint = None
    same = record.get("fingerprint") == fingerprint
    return {"have": True, "fingerprint": fingerprint, "saved_at": record.get("at") if same else None}


def view(config: dict | None, model: dict | None, facts: dict | None, plan: dict | None, built: dict | None, key: dict) -> dict:
    """What the Backups page shows without asking the server anything.
    plan: the saved choices' plan, None while it has problems. built: the
    plan the last finished setup run started with."""
    bare = strip_notes(plan) if plan else None
    on = chosen(config)
    want, have = where(bare) if on else None, where(built)
    found = places(facts, config)
    own = next((p for p in found if p["kind"] in ("made", "chosen", "blank")), None)
    section = (bare or {}).get("backups") or {}
    disk = (bare or {}).get("disk") or {}
    guests = (bare or {}).get("guests") or {}
    services = (model or {}).get("services") or {}
    skip = [n for n in guests if services.get(n, {}).get("backup") is False]
    covered = [n for n, g in guests.items() if n != "pbs" and g["kind"] != "vm-pool" and n not in skip]
    pinned = ((config or {}).get("backups") or {}).get("size_gib")
    return {
        "on": on,
        "status": section.get("status") if on else backups.DISABLED,
        "where": want,
        "built": have,
        # Chosen and not built, or built and since turned off or moved.
        "pending": (want or None) != (have or None) if on or have else False,
        "places": found,
        "system": {
            "allowed": own is None,
            "why": None if own is None else f"The backups have a disk of their own, {own['id']}.",
            "storage": disk.get("storage"),
            "free_gib": disk.get("free_gib"),
            "size_gib": pinned or (want or {}).get("size_gib") if (want or {}).get("kind", SYSTEM) == SYSTEM else pinned,
            "default_gib": backups.SAME_DISK_GIB,
            "min_gib": max(backups.MIN_GIB, (have or {}).get("size_gib") or 0) if (have or {}).get("kind") == SYSTEM else backups.MIN_GIB,
        },
        "covered": covered,
        "left_out": skip,
        "key": key,
        "schedule": backups.SCHEDULE,
        "keeps": KEEPS,
        "ready": config is not None and facts is not None,
    }


def save(config: dict, shown: dict, body: dict, erase: Callable[[dict, dict], dict]) -> dict:
    """config.yaml with backups turned on at the place the owner picked:
    `system` with a size, or a disk's id. erase(config, chosen) is the
    page's own pin for erasing a disk, which checks the typed word."""
    config = copy.deepcopy(config)
    place = str(body.get("place", ""))
    services = config.setdefault("services", {})
    services.get("pbs", {}).pop("enabled", None)
    if services.get("pbs") == {}:
        del services["pbs"]
    if not services:
        del config["services"]
    if place == SYSTEM:
        if not shown["system"]["allowed"]:
            raise NullBillError(shown["system"]["why"])
        size, least = body.get("size_gib"), shown["system"]["min_gib"]
        if not isinstance(size, int) or isinstance(size, bool) or not least <= size <= MAX_GIB:
            grown = "; room with backups in it cannot shrink" if least > backups.MIN_GIB else ""
            raise NullBillError(f"give the backups between {least} and {MAX_GIB} GiB{grown}")
        section = config.setdefault("backups", {})
        if size == backups.SAME_DISK_GIB:
            section.pop("size_gib", None)
        else:
            section["size_gib"] = size
        if not section:
            del config["backups"]
        return config
    disk = next((p for p in shown["places"] if p["id"] == place), None)
    if disk is None:
        raise NullBillError("choose where the backups go")
    holder = next((p for p in shown["places"] if p["kind"] in ("made", "chosen") and p["id"] != place), None)
    if holder:
        raise NullBillError(f"the backups have {holder['id']} already; one disk holds them")
    if disk["kind"] == "kept":
        raise NullBillError(f"{place}: {KEPT_WHY}")
    if disk["kind"] == "storage":
        raise NullBillError(f"{place}: it is extra storage in Proxmox; Setup, What to run, Disks changes that")
    if disk["kind"] == "blank" and (shown.get("where") or {}).get("disk") == place:
        return config  # the plan takes this blank disk already; a pin would only ask for a setup run
    if disk["kind"] == "blank":
        config["disks"] = {**(config.get("disks") or {}), place: {"use": "pbs"}}
    elif disk["kind"] == "erase":
        config["disks"] = erase(config, {place: {"choice": "erase", "for": "backups", "confirm": body.get("confirm", "")}})
    return config


QUERIES = {
    "listed": "nb_backup_status_ok",
    "last": "nb_backup_last_success_timestamp_seconds",
    "exists": "nb_backup_exists",
    "restore_ok": "nb_backup_restore_ok",
    "restore_at": "nb_backup_restore_timestamp_seconds",
    "restore_backup": "nb_backup_restore_backup_timestamp_seconds",
    "store": "nb_backup_store_bytes",
    "read": 'node_textfile_mtime_seconds{file=~".*nullbill-backups\\\\.prom"}',
}


def live(built: dict | None, query: Callable[[str], list[dict]], now: float | None = None) -> dict:
    """What the host last measured (nb-backup-metrics, hourly and at the
    end of setup's backups stage), as Prometheus holds it: each covered
    guest's newest backup, the last restore test and the datastore's room.
    `read` is when the host measured, not when the page asked."""
    now = time.time() if now is None else now
    if where(built) is None:
        return {"state": "none", "at": now}
    try:
        found = {name: query(expr) for name, expr in QUERIES.items()}
    except (OSError, ValueError, KeyError) as exc:
        return {"state": "unreachable", "at": now, "error": str(exc)[:200]}

    def value(rows: list[dict]) -> float | None:
        return float(rows[0]["value"][1]) if rows else None

    read = value(found["read"])
    if not found["listed"]:
        return {"state": "waiting", "at": now, "read": read}
    if value(found["listed"]) != 1:
        return {"state": "unlisted", "at": now, "read": read}
    last = {r["metric"].get("vmid"): float(r["value"][1]) for r in found["last"]}
    has = {r["metric"].get("vmid") for r in found["exists"] if r["value"][1] == "1"}
    guests = [
        {"name": name, "vmid": g["vmid"], "last": last.get(str(g["vmid"])) if str(g["vmid"]) in has else None}
        for name, g in backups.guests(built).items()
    ]
    restore = None
    if found["restore_ok"]:
        who = found["restore_ok"][0]["metric"]
        restore = {
            "ok": value(found["restore_ok"]) == 1,
            "guest": who.get("guest") or who.get("vmid"),
            "at": value(found["restore_at"]),
            "backup": value(found["restore_backup"]),
        }
    room = {r["metric"].get("kind"): int(float(r["value"][1])) for r in found["store"]}
    return {
        "state": "ok",
        "at": now,
        "read": read,
        "guests": guests,
        "late": [g["name"] for g in guests if g["last"] is None or now - g["last"] > backups.MAX_AGE_H * 3600],
        "restore": restore,
        "store": room if {"size", "used"} <= set(room) else None,
    }
