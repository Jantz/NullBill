"""host_baseline's nb-backup-metrics against a pvesh stand-in, and the
alert rule it feeds."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from nullbill import backups

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ansible/roles/host_baseline/files/nb-backup-metrics"
RULES = ROOT / "ansible/roles/monitoring/files/nullbill.rules.yml"

# Answers by pvesh path; a missing path fails like PVE does.
FAKE_PVESH = """#!/bin/sh
f="$NB_FAKE/$(printf %s "$2" | tr / _).json"
[ -f "$f" ] || { echo "no such path $2" >&2; exit 1; }
cat "$f"
"""


def run(tmp_path, answers):
    fake = tmp_path / "fake"
    fake.mkdir(exist_ok=True)
    for old in fake.glob("*.json"):
        old.unlink()
    for path, value in answers.items():
        (fake / (path.replace("/", "_") + ".json")).write_text(json.dumps(value))
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "pvesh").write_text(FAKE_PVESH)
    (bindir / "pvesh").chmod(0o755)
    env = os.environ | {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "NB_FAKE": str(fake),
        "NB_TEXTFILE_DIR": str(tmp_path / "textfile"),
        "NB_STATE_DIR": str(tmp_path / "state"),
    }
    out = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, check=True).stdout
    prom = tmp_path / "textfile/nullbill-backups.prom"
    return out, prom.read_text() if prom.exists() else None


def samples(text):
    return dict(re.findall(r"^(nb_\w+(?:\{[^}]*\})?) (\d+)$", text, re.M))


JOB = {"id": backups.JOB, "vmid": "2010,2017", "storage": backups.STORAGE}
CONTENT = f"/nodes/localhost/storage/{backups.STORAGE}/content"
NAMES = [{"vmid": 2010, "name": "router"}, {"vmid": 2017, "name": "forgejo"}]


def test_newest_encrypted_backup_per_guest(tmp_path):
    entries = [
        {"vmid": 2010, "ctime": 1000, "encrypted": "aa:bb"},
        {"vmid": 2010, "ctime": 3000, "encrypted": "aa:bb"},
        # Unencrypted does not count, and neither do guests outside the job.
        {"vmid": 2010, "ctime": 5000},
        {"vmid": 2099, "ctime": 4000, "encrypted": "aa:bb"},
    ]
    out, text = run(tmp_path, {"/cluster/backup": [JOB], CONTENT: entries, "/cluster/resources": NAMES})
    got = samples(text)
    assert got["nb_backup_status_ok"] == "1"
    assert got['nb_backup_last_success_timestamp_seconds{vmid="2010",guest="router"}'] == "3000"
    assert "2099" not in text
    assert out.strip() == "2 guests, 1 with a backup"
    # forgejo has none yet: it counts from when the job first listed it, and
    # keeps that time on later runs rather than restarting the clock.
    since = int(got['nb_backup_last_success_timestamp_seconds{vmid="2017",guest="forgejo"}'])
    assert since > 1_700_000_000
    (tmp_path / "state/backups-seen.json").write_text(json.dumps({"2010": 1, "2017": 42}))
    _, text = run(tmp_path, {"/cluster/backup": [JOB], CONTENT: entries, "/cluster/resources": NAMES})
    assert samples(text)['nb_backup_last_success_timestamp_seconds{vmid="2017",guest="forgejo"}'] == "42"


def test_unreachable_storage_reports_status_and_no_ages(tmp_path):
    out, text = run(tmp_path, {"/cluster/backup": [JOB]})
    assert samples(text) == {"nb_backup_status_ok": "0"}
    assert out.startswith("unavailable: ")


def test_no_job_writes_nothing_and_removes_an_old_file(tmp_path):
    run(tmp_path, {"/cluster/backup": [JOB], CONTENT: [], "/cluster/resources": NAMES})
    out, text = run(tmp_path, {"/cluster/backup": [{"id": "someone-else"}]})
    assert text is None and out.strip() == "no nightly job"


def test_names_match_backups_and_the_alert_uses_its_age():
    script = SCRIPT.read_text()
    assert f'JOB = "{backups.JOB}"' in script and f'STORAGE = "{backups.STORAGE}"' in script
    rules = {r["alert"]: r for g in yaml.safe_load(RULES.read_text())["groups"] for r in g["rules"] if "alert" in r}
    assert f"> {backups.MAX_AGE_H} * 3600" in rules["BackupTooOld"]["expr"]
