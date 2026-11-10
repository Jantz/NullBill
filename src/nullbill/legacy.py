"""Disks kept from a previous OS (docs/adr/0007-legacy-disks.md).

`disks.<id>: {use: legacy}` keeps a disk's data as it is: apply mounts each
chosen partition on the host under LEGACY_ROOT, read-only unless pinned,
and the files guest shares it to the owner over SMB. Nothing here or in
apply formats, wipes or repairs such a disk.
"""

import math
import re
import shlex
from typing import Any

from nullbill import power
from nullbill.probe import LEGACY_ROOT, Runner
from nullbill.render import strip_notes
from nullbill.results import Result

# Filesystems a legacy disk may hold. NTFS mounts through ntfs-3g, which,
# with ntfs-3g.probe and ntfsinfo, can tell a hibernated or unclean volume;
# exFAT and FAT carry no mark of Windows' hibernation, so they stay read-only.
FILESYSTEMS = {"ntfs": "NTFS", "exfat": "exFAT", "vfat": "FAT"}
READ_WRITE = {"ntfs"}
# Partitions a Windows disk carries besides the data: never mounted, by
# GPT type GUID or MBR type code (lsblk's PARTTYPE, lowercased).
SKIPPED_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI system",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft reserved",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows recovery",
    "0xef": "EFI system",
    "0x27": "Windows recovery",
}
# Where the files guest sees each mount.
SHARE_ROOT = "/srv/share"
# uid and gid 1000 in an unprivileged container (PVE maps 0 to 100000).
SHARE_UID = 1000
# The one Samba user the files guest knows; its password is the secret
# files_share_password, which the page shows the owner.
SHARE_USER = "nbshare"
HOST_ID_OFFSET = 100000
# Keys of a disk pin that only mean something for a legacy disk.
LEGACY_KEYS = ("mode", "partitions", "share", "name", "discard_journal")
RUNBOOK = "docs/runbooks/legacy-disk.md"
# Why an NTFS partition cannot be mounted read-write, by its probed state
# (probe.NTFS_STATES), and what the owner does about it. Only "unclean" has
# a way without Windows: discarding the journal Windows left (ADR 0007).
RW_WHY = {
    "unclean": "Windows did not shut it down fully (Fast Startup, hibernation, or it was unplugged while in use)",
    "hibernated": "it holds a hibernated Windows (hiberfil.sys)",
    "chkdsk": "Windows marked it for a disk check",
    "damaged": "its NTFS is inconsistent or the disk is failing",
}
RW_FIX = {
    "unclean": (
        "Connect the disk to a Windows PC, shut that PC down with shutdown /s /t 0 and put it back; or, when "
        "that Windows is gone for good, discard what it left unfinished"
    ),
    "hibernated": "Start that Windows and shut it down with shutdown /s /t 0",
    "chkdsk": "Connect the disk to a Windows PC and run chkdsk /f on it",
    "damaged": "Copy what you need off it, then run chkdsk /f on it in Windows",
}


def rw_problem(where: str, n: int, state: str | None, journal: str | None, discard: list[str]) -> str | None:
    """What stops partition n from being mounted read-write, if anything.
    discard holds the journal fingerprints the owner pinned: one journal,
    as one Windows session left it, each. A state nobody could read (None)
    stops nothing here; nb-legacy-disk refuses what is unsafe at the mount."""
    if state not in RW_WHY or (state == "unclean" and journal is not None and journal in discard):
        return None
    text = f"{where}: partition {n} stays read-only: {RW_WHY[state]}. {RW_FIX[state]}"
    if state != "unclean":
        return f"{text}; then probe again ({RUNBOOK})."
    if discard:
        return f"{text}. The journal you confirmed is not the one on the disk now; probe again and confirm again ({RUNBOOK})."
    pin = f"discard_journal: [{journal}]" if journal else "discard_journal, once a probe has read its journal"
    return f"{text} with {pin} ({RUNBOOK})."


def _slug(label: str | None) -> str | None:
    """A share name from a partition label: lowercase ASCII, digits and dashes."""
    text = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    text = text.lstrip("0123456789-")[:31].rstrip("-")
    return text or None


def describe(part: dict) -> str:
    kind = SKIPPED_TYPES.get(part["parttype"] or "")
    what = kind or part["fstype"] or "no filesystem"
    return f"partition {part['n']} ({what})"


def plan_disk(disk_id: str, disk: dict, pin: dict, errors: list[str]) -> tuple[list[dict], list[str]]:
    """The partitions to mount, and a note on what is left alone."""
    where = f"disks.{disk_id}"
    if pin.get("wipe"):
        errors.append(f"{where}: use: legacy keeps the data; wipe: true makes no sense with it")
    mode = pin.get("mode", "ro")
    discard = pin.get("discard_journal", [])
    if discard and mode != "rw":
        errors.append(f"{where}.discard_journal: only with mode: rw; a read-only disk is not changed at all")
    parts = {p["n"]: p for p in disk["partitions"]}
    if not parts:
        errors.append(f"{where}: no partition table; a filesystem on the whole disk cannot be kept yet")
        return [], []
    wanted = pin.get("partitions")
    chosen = []
    left = []
    for n in wanted or sorted(parts):
        part = parts.get(n)
        if part is None:
            errors.append(f"{where}.partitions: no partition {n}; the disk has {', '.join(map(str, sorted(parts)))}")
            continue
        skipped = SKIPPED_TYPES.get(part["parttype"] or "")
        if skipped:
            if wanted:
                errors.append(f"{where}.partitions: partition {n} is a {skipped} partition, which is never mounted")
            else:
                left.append(describe(part))
            continue
        if part["fstype"] not in FILESYSTEMS:
            if wanted:
                fs = part["fstype"] or "no filesystem"
                errors.append(f"{where}.partitions: partition {n} holds {fs}; a kept disk can hold NTFS, exFAT or FAT")
            else:
                left.append(describe(part))
            continue
        if part["mounts"] and not all(m.startswith(LEGACY_ROOT + "/") for m in part["mounts"]):
            errors.append(f"{where}: partition {n} is mounted at {', '.join(part['mounts'])} outside NullBill")
            continue
        if not part["uuid"]:
            errors.append(f"{where}: partition {n} has no filesystem UUID to mount it by")
            continue
        if mode == "rw" and part["fstype"] not in READ_WRITE:
            errors.append(
                f"{where}.mode: rw is for NTFS only; partition {n} holds {FILESYSTEMS[part['fstype']]}, "
                "where a disk Windows left hibernated cannot be told, so it stays read-only"
            )
        elif mode == "rw":
            problem = rw_problem(f"{where}.mode", n, part.get("rw_state"), part.get("journal"), discard)
            if problem:
                errors.append(problem)
        chosen.append(part)
    if not chosen and not wanted:
        errors.append(f"{where}: no partition with NTFS, exFAT or FAT to keep; found {', '.join(left) or 'none'}")
    base = pin.get("name")
    mounts = []
    for part in chosen:
        if base:
            name = base if len(chosen) == 1 else f"{base}-{part['n']}"
        else:
            name = _slug(part["label"]) or f"part-{part['n']}"
        entry: dict[str, Any] = {
            "n": part["n"],
            "fstype": part["fstype"],
            "label": part["label"],
            "uuid": part["uuid"],
            "mode": mode,
            "mount": f"{LEGACY_ROOT}/{name}",
        }
        if pin.get("share", True):
            entry["share"] = name
        if mode == "rw" and part.get("rw_state") == "unclean" and part.get("journal") in discard:
            # The journal apply clears before the mount, and no other.
            entry["discard_journal"] = part["journal"]
        mounts.append(entry)
    return mounts, left


# For the page: the worst first.
WRITE_ORDER = ("other", "damaged", "chkdsk", "hibernated", "unclean", None, "clean")


def write_state(parts: list[dict], discard: list[str], mounted_rw: set[int]) -> dict:
    """Whether the partitions a disk keeps can be made read-write, for the
    page: "editable" once apply mounted them so, else the worst of their
    probed states ("other": one of them is not NTFS). discard says the pin
    already names every journal Windows left open; can_discard that each
    has a fingerprint to name."""
    other = [p for p in parts if p["fstype"] not in READ_WRITE]
    if other:
        held = ", ".join(f"partition {p['n']} holds {FILESYSTEMS[p['fstype']]}" for p in other)
        return {"state": "other", "why": held, "discard": False, "can_discard": False}
    if parts and all(p["n"] in mounted_rw for p in parts):
        return {"state": "editable", "discard": False, "can_discard": False}
    state = min((p.get("rw_state") for p in parts), key=WRITE_ORDER.index, default=None)
    journals = [p.get("journal") for p in parts if p.get("rw_state") == "unclean"]
    return {
        "state": state,
        "discard": state == "unclean" and all(j in discard for j in journals),
        "can_discard": state == "unclean" and all(journals),
    }


def journals(parts: list[dict]) -> list[str]:
    """The fingerprints of the journals Windows left open on these partitions."""
    return sorted(p["journal"] for p in parts if p.get("rw_state") == "unclean" and p.get("journal"))


def mounts(plan: dict) -> list[dict]:
    """Every kept partition in a (bare) plan, with its disk id and name."""
    out = []
    for disk_id, disk in sorted(plan["disks"].items()):
        if disk.get("use") != "legacy":
            continue
        for part in disk["partitions"]:
            out.append({**part, "disk": disk_id, "name": part["mount"].rsplit("/", 1)[1]})
    return out


def shares(plan: dict) -> list[dict]:
    return [m for m in mounts(plan) if "share" in m]


# Converting a kept disk (Phase 2 of ADR 0007). A copy is an ext4 volume
# on a PVE storage, owned by the files guest and mounted in it read-write
# at SHARE_ROOT/<name>, with backup=1 so PBS backs it up from then on.
CONVERT_TO = ("pbs", "storage")
CONVERT_KEYS = ("evacuate", "discard_data")
# Storage types nb-legacy-copy puts a copy on (an ext4 volume it makes).
COPY_STORAGE = ("lvmthin", "lvm")
GIB = 2**30


def kept_mounts(disk: dict) -> list[dict]:
    """Partitions of a disk that apply keeps mounted under LEGACY_ROOT, with their name."""
    out = []
    for part in disk.get("partitions", []):
        for mount in part["mounts"]:
            if mount.startswith(LEGACY_ROOT + "/"):
                out.append({**part, "name": mount.rsplit("/", 1)[1], "mount": mount})
    return out


def copy_size_gib(used_bytes: int) -> int:
    """The copy's volume: the files plus 5 %, plus 1 GiB for ext4 itself."""
    return math.ceil(used_bytes * 1.05 / GIB) + 1


def reserved_gib(model: dict, facts: dict) -> dict[str, int]:
    """GiB each storage gives to copies this apply makes, before the guests
    are sized (plan_convert reports what is wrong with a conversion)."""
    out: dict[str, int] = {}
    pins = model.get("disks", {})
    for disk in facts["disks"]:
        pin = pins.get(disk["id"]) or {}
        if pin.get("use") in CONVERT_TO and pin.get("evacuate") and not pin.get("discard_data"):
            used = [k["used_bytes"] for k in kept_mounts(disk) if k["used_bytes"] is not None]
            out[pin["evacuate"]] = out.get(pin["evacuate"], 0) + sum(copy_size_gib(u) for u in used)
    return out


def plan_convert(disk: dict, pin: dict, facts: dict, capacity: dict, errors: list[str]) -> dict:
    """What converting a kept disk to pin['use'] takes: a verified copy on a
    storage (evacuate), or the owner's word that the files are elsewhere
    (discard_data). Anything missing is an error before apply changes a thing."""
    where = f"disks.{disk['id']}"
    kept = kept_mounts(disk)
    at = ", ".join(k["mount"] for k in kept)
    evacuate, discard = pin.get("evacuate"), pin.get("discard_data")
    if not pin.get("wipe"):
        errors.append(f"{where}: kept as legacy and mounted at {at}; converting it to {pin['use']} needs wipe: true")
    if evacuate and discard:
        errors.append(f"{where}: evacuate or discard_data, not both")
        return {}
    if not evacuate and not discard:
        errors.append(
            f"{where}: kept as legacy and mounted at {at}; converting it to {pin['use']} erases it: set evacuate: <PVE storage> "
            "to copy its files there first, or discard_data: true if you have them elsewhere"
        )
        return {}
    unknown = [str(k["n"]) for k in kept if k["used_bytes"] is None]
    if unknown:
        errors.append(f"{where}: how much partition {', '.join(unknown)} holds is unknown; probe again")
        return {}
    used = sum(k["used_bytes"] for k in kept)
    if discard:
        return {"from": "legacy", "discard_data": True, "lost_gib": math.ceil(used / GIB)}
    storage = next((st for st in facts["storage"] if st["name"] == evacuate), None)
    if storage is None:
        errors.append(f"{where}.evacuate: no active PVE storage named {evacuate!r}")
        return {}
    if not {"images", "rootdir"} <= set(storage["content"]):
        errors.append(f"{where}.evacuate: {evacuate} cannot hold container volumes (content images and rootdir)")
        return {}
    if storage["type"] not in COPY_STORAGE:
        errors.append(f"{where}.evacuate: {evacuate} is {storage['type']}; a copy goes on LVM or LVM-thin storage (lvmthin, lvm)")
        return {}
    copies = [
        {"name": k["name"], "partition": k["n"], "used_gib": math.ceil(k["used_bytes"] / GIB), "size_gib": copy_size_gib(k["used_bytes"])}
        for k in kept
    ]
    need = sum(c["size_gib"] for c in copies)
    # The guests' disks count against the same storage, provisioned in full:
    # a thin pool that fills up stops every guest on it.
    needed = getattr(capacity.get("needed_gib"), "value", capacity.get("needed_gib")) or 0
    free = storage["avail_gib"] - (needed if capacity.get("storage") == evacuate else 0)
    if need > free:
        errors.append(
            f"{where}.evacuate: the copy needs {need} GiB (the files and 5 %, plus 1 GiB each), "
            f"{evacuate} has {max(free, 0)} GiB free after the guests' disks"
        )
        return {}
    out = {"from": "legacy", "evacuate": evacuate, "copies": copies}
    if pin["use"] == "storage":
        # The copies then move onto the disk itself, once it is storage.
        target, room = storage_name(pin), storage_room_gib(disk["size_gib"])
        if need > room:
            errors.append(f"{where}: the copies need {need} GiB, more than the {room} GiB the disk holds as storage {target}")
            return {}
        out["moves_to"] = target
    return out


# A storage disk (phase 3): one volume group with a thin pool on 95 % of it,
# of which copies leave 5 % free, as nb-legacy-copy does on any storage.
STORAGE_DEFAULT = "nb-data"
STORAGE_KEYS = ("storage",)


def storage_name(pin: dict) -> str:
    return pin.get("storage", STORAGE_DEFAULT)


def storage_room_gib(size_gib: int) -> int:
    pool = size_gib * 95 // 100
    return pool - math.ceil(pool / 20)


def convert_note(converts: dict) -> str:
    if converts.get("discard_data"):
        return f"Converts a kept disk: its files ({converts['lost_gib']} GiB) are not copied and are lost, as discard_data: true says."
    names = ", ".join(c["name"] for c in converts["copies"])
    then = f" Once it is storage {converts['moves_to']}, the copies move onto it, checked again." if converts.get("moves_to") else ""
    return (
        f"Converts a kept disk: apply first copies its files ({names}) to {converts['evacuate']}, checks the copy and "
        f"shares it from the files guest, backed up; only then is the disk wiped.{then}"
    )


def copies(plan: dict) -> list[dict]:
    """Copies the files guest shares: made already (probed), or made by this apply."""
    out = [{**c, "made": True} for c in plan.get("copies", [])]
    for disk_id, disk in sorted(plan["disks"].items()):
        for c in (disk.get("converts") or {}).get("copies", []):
            storage = disk["converts"]["evacuate"]
            out.append({"name": c["name"], "storage": storage, "size_gib": c["size_gib"], "disk": disk_id, "made": False})
    return out


def note(mounts_: list[dict], left: list[str]) -> str:
    modes = {m["mode"] for m in mounts_}
    how = "read-write" if modes == {"rw"} else "read-only"
    shared = [m["share"] for m in mounts_ if "share" in m]
    where = f" and shared to the owner over the VPN as {', '.join(shared)} ({RUNBOOK})" if shared else ""
    text = f"Kept as it is: mounted {how} under {LEGACY_ROOT}{where}. Not backed up by NullBill; keep a copy elsewhere."
    cleared = [str(m["n"]) for m in mounts_ if "discard_journal" in m]
    if cleared:
        text += (
            f" Partition {', '.join(cleared)}: apply first discards the journal Windows left unfinished, as discard_journal says; "
            "the files as they read now stay."
        )
    if left:
        text += f" Left alone: {', '.join(left)}."
    return text


# What findmnt calls each filesystem once mounted: ntfs-3g is a FUSE driver.
MOUNTED_AS = {"ntfs": "fuseblk", "exfat": "exfat", "vfat": "vfat"}
SAMBA = {"server min protocol": "SMB3", "server smb encrypt": "required", "map to guest": "Never"}
SAMBA_LABEL = "files guest's Samba takes SMB3 with encryption only, and no guests"


def _labels(plan: dict) -> list[str]:
    labels = []
    for m in mounts(plan):
        labels.append(f"kept disk {m['name']} is mounted {m['mode']} on the host")
        if "share" in m:
            labels.append(f"files guest sees kept disk {m['name']} {m['mode']}")
    labels += [f"files guest sees the copy {c['name']} rw" for c in copies(plan)]
    if shares(plan) or copies(plan):
        labels.append(SAMBA_LABEL)
    labels += [_storage_label(name) for name in storages(plan)]
    return labels


def storages(plan: dict) -> list[str]:
    """The storage disks' PVE storage names (phase 3)."""
    return sorted(d["storage"] for d in plan["disks"].values() if d.get("use") == "storage")


def _storage_label(name: str) -> str:
    return f"storage disk {name} is active LVM-thin storage"


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    return [Result("SKIP", label, reason) for label in _labels(strip_notes(plan_with_notes))]


def _mounted(out: str) -> tuple[str, str]:
    """findmnt -rno FSTYPE,OPTIONS: the filesystem and the first option."""
    fstype, _, options = out.strip().partition(" ")
    return fstype, options.split(",", 1)[0]


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    """Each kept partition mounted on the host as planned and, if shared,
    seen by the files guest with the same mode; Samba as the role set it."""
    plan = strip_notes(plan_with_notes)
    results: list[Result] = []
    vmid = plan["guests"].get("files", {}).get("vmid")

    def in_guest(label: str, command: str) -> tuple[int, str, str] | None:
        """The command's answer from inside the files guest, or None, with
        the check reported SKIP, while that guest is off on demand: the
        host's own mounts and storages are checked all the same."""
        try:
            return run(target, f"pct exec {vmid} -- {command}")
        except power.Off as asleep:
            results.append(Result("SKIP", label, power.off_reason(asleep.title)))
            return None

    for m in mounts(plan):
        label = f"kept disk {m['name']} is mounted {m['mode']} on the host"
        rc, out, err = run(target, f"findmnt -rno FSTYPE,OPTIONS --mountpoint {shlex.quote(m['mount'])}")
        fstype, mode = _mounted(out)
        if not fstype:
            why = f"not mounted; is partition {m['n']} of {m['disk']} connected? (docs/runbooks/legacy-disk.md)"
            results.append(Result("FAIL", label, why))
        elif (fstype, mode) != (MOUNTED_AS[m["fstype"]], m["mode"]):
            results.append(Result("FAIL", label, f"{fstype} {mode}"))
        else:
            results.append(Result("PASS", label))
        if "share" not in m:
            continue
        label = f"files guest sees kept disk {m['name']} {m['mode']}"
        where = f"{SHARE_ROOT}/{m['share']}"
        found = in_guest(label, f"findmnt -rno FSTYPE,OPTIONS --mountpoint {shlex.quote(where)}")
        if found is None:
            continue
        rc, out, err = found
        fstype, mode = _mounted(out)
        if rc != 0 or not fstype:
            results.append(Result("FAIL", label, (err.strip() or f"nothing mounted at {where}")[:120]))
        elif mode != m["mode"]:
            results.append(Result("FAIL", label, f"{where} is {mode}"))
        else:
            results.append(Result("PASS", label))
    for c in copies(plan):
        label = f"files guest sees the copy {c['name']} rw"
        where = f"{SHARE_ROOT}/{c['name']}"
        found = in_guest(label, f"findmnt -rno FSTYPE,OPTIONS --mountpoint {shlex.quote(where)}")
        if found is None:
            continue
        rc, out, err = found
        fstype, mode = _mounted(out)
        if rc != 0 or not fstype:
            results.append(Result("FAIL", label, (err.strip() or f"nothing mounted at {where}")[:120]))
        elif (fstype, mode) != ("ext4", "rw"):
            results.append(Result("FAIL", label, f"{where} is {fstype} {mode}"))
        else:
            results.append(Result("PASS", label))
    if shares(plan) or copies(plan):
        script = "; ".join(f"testparm -s --parameter-name={shlex.quote(p)} 2>/dev/null" for p in SAMBA)
        found = in_guest(SAMBA_LABEL, f"sh -c {shlex.quote(script)}")
        if found is not None:
            got = dict(zip(SAMBA, found[1].split(), strict=False))
            wrong = [f"{p} = {got.get(p, '?')}" for p, want in SAMBA.items() if got.get(p) != want]
            results.append(Result("FAIL", SAMBA_LABEL, "; ".join(wrong)[:120]) if wrong else Result("PASS", SAMBA_LABEL))
    for name in storages(plan):
        rc, out, err = run(target, f"pvesm status --storage {shlex.quote(name)}")
        row = next((line.split() for line in out.splitlines() if line.split()[:1] == [name]), [])
        if rc != 0 or len(row) < 3:
            results.append(Result("FAIL", _storage_label(name), (err.strip() or "PVE has no such storage; run apply")[:120]))
        elif row[1:3] != ["lvmthin", "active"]:
            results.append(Result("FAIL", _storage_label(name), f"{row[1]} {row[2]}"))
        else:
            results.append(Result("PASS", _storage_label(name)))
    return results
