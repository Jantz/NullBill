"""Disks kept from a previous OS (docs/adr/0007-legacy-disks.md).

`disks.<id>: {use: legacy}` keeps a disk's data as it is: apply mounts each
chosen partition on the host under LEGACY_ROOT, read-only unless pinned,
and the files guest shares it to the owner over SMB. Nothing here or in
apply formats, wipes or repairs such a disk.
"""

import re
import shlex
from typing import Any

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
    if shares(plan):
        labels.append(SAMBA_LABEL)
    return labels


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
    results = []
    vmid = plan["guests"].get("files", {}).get("vmid")
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
        rc, out, err = run(target, f"pct exec {vmid} -- findmnt -rno FSTYPE,OPTIONS --mountpoint {shlex.quote(where)}")
        fstype, mode = _mounted(out)
        if rc != 0 or not fstype:
            results.append(Result("FAIL", label, (err.strip() or f"nothing mounted at {where}")[:120]))
        elif mode != m["mode"]:
            results.append(Result("FAIL", label, f"{where} is {mode}"))
        else:
            results.append(Result("PASS", label))
    if shares(plan):
        script = "; ".join(f"testparm -s --parameter-name={shlex.quote(p)} 2>/dev/null" for p in SAMBA)
        rc, out, err = run(target, f"pct exec {vmid} -- sh -c {shlex.quote(script)}")
        got = dict(zip(SAMBA, out.split(), strict=False))
        wrong = [f"{p} = {got.get(p, '?')}" for p, want in SAMBA.items() if got.get(p) != want]
        results.append(Result("FAIL", SAMBA_LABEL, "; ".join(wrong)[:120]) if wrong else Result("PASS", SAMBA_LABEL))
    return results
