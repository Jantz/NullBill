"""host_baseline's nb-legacy-copy against pvesm, pvesh and pct stand-ins: how
a retry treats volumes an interrupted copy or move left behind. Nothing is
mounted or formatted; the stand-in pvesm refuses to allocate, so each run
stops right after it has dealt with what was left over."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ansible/roles/host_baseline/files/nb-legacy-copy"
OLD = "local-lvm:vm-2023-disk-1"
NEW = "nb-data:vm-2023-disk-0"

FAKE_PVESM = """#!/bin/sh
echo "$*" >>"$NB_FAKE/pvesm.log"
case $1 in
  list) cat "$NB_FAKE/volumes" 2>/dev/null ;;
  alloc) exit 1 ;;
  free) [ ! -e "$NB_FAKE/free-fails" ] || exit 1
        grep -v "^$2 " "$NB_FAKE/volumes" >"$NB_FAKE/volumes.new"; mv "$NB_FAKE/volumes.new" "$NB_FAKE/volumes" ;;
esac
"""


def move(tmp_path: Path, record: str, pending: str, volumes: list[str], free_fails: bool = False, name: str = "photos", old: str = OLD):
    fake = tmp_path / "fake"
    fake.mkdir()
    for tool, body in {
        "pvesm": FAKE_PVESM,
        "pct": '#!/bin/sh\necho "status: stopped"\n',
        "pvesh": '#!/bin/sh\necho \'{"type": "lvmthin", "total": 107374182400, "avail": 96636764160}\'\n',
    }.items():
        (fake / tool).write_text(body)
        (fake / tool).chmod(0o755)
    (fake / "volumes").write_text("".join(f"{v} raw images 10737418240 2023\n" for v in volumes))
    if free_fails:
        (fake / "free-fails").touch()
    records = tmp_path / "kept-copies"
    records.write_text(record + "\n")
    Path(f"{records}.pending").write_text(pending + "\n")
    env = os.environ | {
        "NB_FAKE": str(fake),
        "NB_COPIES": str(records),
        "NB_PVESM": str(fake / "pvesm"),
        "NB_PVESH": str(fake / "pvesh"),
        "NB_PCT": str(fake / "pct"),
    }
    proc = subprocess.run(["sh", str(SCRIPT), "move", name, old, "nb-data", "2023"], capture_output=True, text=True, env=env)
    log = (fake / "pvesm.log").read_text()
    left = Path(f"{records}.pending")
    return proc, log, left.read_text() if left.exists() else "", records.read_text()


RECORD = f"photos {OLD} 10 10 1000000000 ata-SYNTHETIC_HDD_0002"


def test_a_move_that_stopped_frees_its_unfinished_volume_on_retry(tmp_path):
    """The record still names the old copy; pending names the new volume the
    move never finished. The retry frees it and keeps the old copy."""
    proc, log, pending, record = move(tmp_path, RECORD, f"photos {NEW}", [OLD, NEW])
    assert f"free {NEW}" in log and f"free {OLD}" not in log
    assert f"freed {NEW}, left over from an unfinished copy of photos" in proc.stderr
    # Its room is taken again for the new try, not a second volume beside it.
    assert "alloc nb-data 2023 vm-2023-disk-0 10G" in log and pending == f"photos {NEW}\n"
    assert record == RECORD + "\n"


def test_a_volume_the_record_names_is_never_freed(tmp_path):
    """The move of photos stopped after recording its new volume: that is the
    copy now. Moving another copy must leave it alone."""
    docs_old = "local-lvm:vm-2023-disk-2"
    records = RECORD.replace(OLD, NEW) + f"\ndocs {docs_old} 5 3 1000 ata-SYNTHETIC_HDD_0002"
    proc, log, _, record = move(tmp_path, records, f"photos {NEW}", [OLD, NEW, docs_old], name="docs", old=docs_old)
    assert f"free {NEW}" not in log and "alloc" in log
    assert record == records + "\n"


def test_a_volume_that_cannot_be_freed_stays_listed(tmp_path):
    proc, log, pending, record = move(tmp_path, RECORD, f"photos {NEW}", [OLD, NEW], free_fails=True)
    assert proc.returncode == 1
    assert f"cannot free {NEW} left over from an unfinished copy" in proc.stderr
    assert pending == f"photos {NEW}\n" and "alloc" not in log
    assert record == RECORD + "\n"


def test_a_volume_that_was_never_made_is_dropped(tmp_path):
    """The run stopped between listing the volume and allocating it: there
    is nothing to free, and the retry goes on."""
    proc, log, _, _ = move(tmp_path, RECORD, f"photos {NEW}", [OLD], free_fails=True)
    assert "cannot free" not in proc.stderr and "alloc nb-data 2023 vm-2023-disk-0 10G" in log
