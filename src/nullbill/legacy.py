"""Disks kept from a previous OS (docs/adr/0007-legacy-disks.md).

`disks.<id>: {use: legacy}` keeps a disk's data as it is: apply mounts each
chosen partition on the host under LEGACY_ROOT, read-only unless pinned,
and the files guest shares it to the owner over SMB. Nothing here or in
apply formats, wipes or repairs such a disk.
"""

import re
from typing import Any

from nullbill.probe import LEGACY_ROOT

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
HOST_ID_OFFSET = 100000
# Keys of a disk pin that only mean something for a legacy disk.
LEGACY_KEYS = ("mode", "partitions", "share", "name")


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
        mounts.append(entry)
    return mounts, left


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


def note(mounts_: list[dict], left: list[str]) -> str:
    modes = {m["mode"] for m in mounts_}
    how = "read-write" if modes == {"rw"} else "read-only"
    shared = [m["share"] for m in mounts_ if "share" in m]
    where = f" and shared to the owner over the VPN as {', '.join(shared)} (docs/runbooks/legacy-disk.md)" if shared else ""
    text = f"Kept as it is: mounted {how} under {LEGACY_ROOT}{where}. Not backed up by NullBill; keep a copy elsewhere."
    if left:
        text += f" Left alone: {', '.join(left)}."
    return text
