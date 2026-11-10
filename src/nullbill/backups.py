"""Nightly backups of the planned guests to PBS, encrypted on the PVE host.

The PVE host encrypts with a key PBS never sees, using a token that can
write and read its own snapshots but not delete them (ansible/roles/pbs);
retention runs on PBS. verify checks that each guest has a fresh, encrypted
backup and restores one file from the newest container backup.

Where the backups live (docs/adr/0017-backups-on-one-disk.md): on a disk of
PBS's own when the server has one, else in a volume of capped size on the
storage the guests are on. The second kind survives a broken guest, a bad
update and a mistake, not the disk; the plan and verify say so.
"""

import json
import shlex
from collections.abc import Callable

from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

STORAGE = "nb-pbs"
# Must match ansible/roles/pbs/defaults/main.yml; tests/test_backups.py checks.
DATASTORE = "backups"
TOKEN_ID = "nb-pve@pbs!pve"
JOB = "nb-nightly"
SCHEDULE = "02:00"
# A daily job plus slack for a long run or a missed start.
MAX_AGE_H = 26
# PVE keeps the storage's token secret (.pw) and encryption key (.enc) here.
PRIV_DIR = "/etc/pve/priv/storage"

# The disk the plan pins for PBS becomes an LVM volume group and a PVE
# storage of this name on the host (nb-backup-disk); the pbs VM's datastore
# disk lives there, and the VM finds it by its serial.
DISK_STORAGE = "nb-pbs-disk"
DISK_SERIAL = "nb-backups"
DISK_PATH = f"/dev/disk/by-id/virtio-{DISK_SERIAL}"

# plan's backups.status. OK: PBS has a disk of its own. SAME_DISK: its
# datastore is a volume beside the guests.
OK, SAME_DISK, NO_ROOM, DISABLED = "ok", "same-disk", "DEGRADED", "disabled"
# The datastore beside the guests: this large unless backups.size_gib pins
# it, smaller when the storage has less left, and never under MIN_GIB.
SAME_DISK_GIB = 64
MIN_GIB = 16
SAME_DISK_WHY = "the backups are on the disk the server runs from, so a failing disk takes both"

Clock = Callable[[], float]


def enabled(plan: dict) -> bool:
    """PBS storage and the job exist only when the datastore has a place."""
    return strip_notes(plan)["backups"]["status"] in (OK, SAME_DISK)


def separate_disk(model: dict, facts: dict) -> bool:
    """Whether plan's disk rules give PBS a disk of its own: one pinned for
    it, the one apply made its volume group on, or a blank one."""
    pins = model.get("disks", {})
    if any(pin["use"] == "pbs" for pin in pins.values()):
        return True
    made = next((d["id"] for d in facts["disks"] if DISK_STORAGE in d.get("vgs", [])), None)
    free = [d for d in facts["disks"] if not d["system"] and d["id"] not in pins]
    return any(d["id"] == made or (made is None and not d["signatures"]) for d in free)


def same_disk_gib(model: dict, room_gib: int) -> int | None:
    """The datastore's size beside the guests, given the room their floors
    leave on the storage. A pinned size is taken as it is; None when not
    even the smallest fits."""
    pinned = model.get("backups", {}).get("size_gib")
    if pinned:
        return pinned
    size = min(SAME_DISK_GIB, room_gib)
    return size if size >= MIN_GIB else None


def disk(plan: dict) -> str | None:
    """The id of the disk PBS has to itself, if it has one."""
    return next((d for d, v in strip_notes(plan)["disks"].items() if v["use"] == "pbs"), None)


def store(plan: dict) -> tuple[str, int]:
    """The PVE storage the datastore volume is on and its size in GiB."""
    section = strip_notes(plan)["backups"]
    return section["storage"], section["size_gib"]


def left_out(plan: dict) -> list[str]:
    """Planned guests the nightly job leaves out because their service says
    so (`backup: false`): setup builds them again from what it pins."""
    return strip_notes(plan)["backups"].get("left_out", [])


def datastore_disk(config: str) -> tuple[str, str, int] | None:
    """The datastore volume in `qm config`'s output, found by its serial:
    its key (virtio1), the storage it is on and its size in GiB."""
    for line in config.splitlines():
        key, _, value = line.partition(": ")
        fields = value.split(",")
        if f"serial={DISK_SERIAL}" not in fields:
            continue
        size = next((f.removeprefix("size=") for f in fields if f.startswith("size=")), "")
        units = {"G": 1, "T": 1024}
        if size[-1:] not in units or not size[:-1].isdigit():
            return None
        return key, fields[0].split(":", 1)[0], int(size[:-1]) * units[size[-1]]
    return None


# host_baseline's hourly textfile metrics for the BackupTooOld alert.
METRICS = "/usr/local/sbin/nb-backup-metrics"

LIST = f"pvesh get /nodes/localhost/storage/{STORAGE}/content --content backup --output-format json"


def unbacked(plan: dict, entries: list[dict]) -> list[int]:
    """VMIDs of covered guests with no backup on the storage at all."""
    have = {int(e.get("vmid", -1)) for e in entries}
    return [g["vmid"] for g in guests(plan).values() if g["vmid"] not in have]


def guests(plan: dict) -> dict[str, dict]:
    """Guests the nightly job covers: all but PBS itself, pool templates,
    which rebuild from their image, and those their service leaves out."""
    bare = strip_notes(plan)
    skip = {"pbs", *bare["backups"].get("left_out", [])}
    return {n: g for n, g in bare["guests"].items() if n not in skip and g["kind"] != "vm-pool"}


def _snapshot(volid: str) -> str:
    # nb-pbs:backup/ct/2010/2026-11-04T02:00:01Z -> ct/2010/2026-11-04T02:00:01Z
    return volid.split(":", 1)[1].removeprefix("backup/")


def _restore_command(storage: dict, snapshot: str) -> str:
    repo = f"{storage['username']}@{storage['server']}:{storage['datastore']}"
    priv = f"{PRIV_DIR}/{STORAGE}"
    return (
        "set -e; d=$(mktemp -d); trap 'rm -rf \"$d\"' EXIT; "
        f'PBS_PASSWORD="$(cat {priv}.pw)" PBS_FINGERPRINT={shlex.quote(storage["fingerprint"])} '
        f'proxmox-backup-client restore --repository {shlex.quote(repo)} {shlex.quote(snapshot)} root.pxar "$d" '
        f"--pattern etc/hostname --keyfile {priv}.enc >/dev/null; "
        'cat "$d/etc/hostname"'
    )


def skipped(plan: dict, reason: str) -> list[Result]:
    if not enabled(plan):
        return []
    names = [f"{name} has an encrypted backup under {MAX_AGE_H} h old" for name in guests(plan)]
    return [Result("SKIP", n, reason) for n in names + ["restore test"]]


def check(plan: dict, target: str, run: Runner, now: Clock) -> list[Result]:
    if not enabled(plan):
        return []
    rc, out, err = run(target, LIST)
    if rc != 0:
        return [Result("FAIL", f"PBS storage {STORAGE} lists backups", (err or out).strip()[:120])]
    entries = json.loads(out or "[]")
    results = []
    newest_ct = None
    for name, guest in guests(plan).items():
        label = f"{name} has an encrypted backup under {MAX_AGE_H} h old"
        mine = [e for e in entries if int(e.get("vmid", -1)) == guest["vmid"]]
        if not mine:
            results.append(Result("FAIL", label, "no backup"))
            continue
        latest = max(mine, key=lambda e: e["ctime"])
        age_h = (now() - latest["ctime"]) / 3600
        if not latest.get("encrypted"):
            results.append(Result("FAIL", label, f"{_snapshot(latest['volid'])} is not encrypted"))
        elif age_h > MAX_AGE_H:
            results.append(Result("FAIL", label, f"newest is {age_h:.0f} h old"))
        else:
            results.append(Result("PASS", label))
            if guest["kind"] == "lxc" and (newest_ct is None or latest["ctime"] > newest_ct[1]["ctime"]):
                newest_ct = (name, latest)
    if newest_ct is None:
        results.append(Result("SKIP", "restore test", "no fresh encrypted container backup to restore from"))
        return results
    name, entry = newest_ct
    rc, out, err = run(target, f"pvesh get /storage/{STORAGE} --output-format json")
    if rc != 0:
        results.append(Result("FAIL", "restore test", (err or out).strip()[:120]))
        return results
    snapshot = _snapshot(entry["volid"])
    rc, out, err = run(target, f"bash -c {shlex.quote(_restore_command(json.loads(out), snapshot))}")
    if rc != 0:
        results.append(Result("FAIL", "restore test", f"{snapshot}: {(err or out).strip()[:120]}"))
    elif out.strip() != name:
        results.append(Result("FAIL", "restore test", f"{snapshot}: etc/hostname is {out.strip()!r}, expected {name!r}"))
    else:
        results.append(Result("PASS", "restore test", f"etc/hostname from {snapshot}"))
    return results
