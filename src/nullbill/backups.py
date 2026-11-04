"""Nightly backups of every planned guest to PBS, encrypted on the PVE host.

The PVE host encrypts with a key PBS never sees, using a token that can
write and read its own snapshots but not delete them (ansible/roles/pbs);
retention runs on PBS. verify checks that each guest has a fresh, encrypted
backup and restores one file from the newest container backup.
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

Clock = Callable[[], float]


def enabled(plan: dict) -> bool:
    """PBS storage and the job exist only when PBS has a disk of its own."""
    return strip_notes(plan)["backups"]["status"] == "ok"


def guests(plan: dict) -> dict[str, dict]:
    """Guests the nightly job covers: all but PBS itself and pool templates,
    which rebuild from their image."""
    bare = strip_notes(plan)
    return {n: g for n, g in bare["guests"].items() if n != "pbs" and g["kind"] != "vm-pool"}


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
    rc, out, err = run(target, f"pvesh get /nodes/localhost/storage/{STORAGE}/content --content backup --output-format json")
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
