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


FAKE_CLIENT = """#!/bin/sh
# restore --repository REPO SNAPSHOT root.pxar DEST --pattern etc/hostname --keyfile KEY
echo "$*" >>"$NB_FAKE/restores"
[ "$PBS_PASSWORD" = token-secret ] && [ "$PBS_FINGERPRINT" = aa:bb ] && [ -f "${10}" ] || { echo "bad call" >&2; exit 2; }
[ -f "$NB_FAKE/broken" ] && { echo "Error: unable to decrypt" >&2; exit 255; }
mkdir -p "$6/etc"
case "$4" in ct/2010/*) cat "$NB_FAKE/hostname-2010" ;; ct/2017/*) echo forgejo ;; esac >"$6/etc/hostname"
"""
PBS = {"server": "10.42.1.15", "datastore": "backups", "username": backups.TOKEN_ID, "fingerprint": "aa:bb"}
STATUS = f"/nodes/localhost/storage/{backups.STORAGE}/status"


def ct(vmid, ctime):
    return {
        "vmid": vmid,
        "ctime": ctime,
        "encrypted": "aa:bb",
        "volid": f"{backups.STORAGE}:backup/ct/{vmid}/2027-01-15T02:00:0{ctime % 10}Z",
    }


def restoring(tmp_path, monkeypatch, entries, **more):
    priv = tmp_path / "priv"
    priv.mkdir(exist_ok=True)
    (priv / f"{backups.STORAGE}.pw").write_text("token-secret\n")
    (priv / f"{backups.STORAGE}.enc").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "proxmox-backup-client").write_text(FAKE_CLIENT)
    (bindir / "proxmox-backup-client").chmod(0o755)
    monkeypatch.setenv("NB_PRIV_DIR", str(priv))
    answers = {"/cluster/backup": [JOB], CONTENT: entries, "/cluster/resources": NAMES, f"/storage/{backups.STORAGE}": PBS, **more}
    out, text = run(tmp_path, answers)
    log = tmp_path / "fake/restores"
    return out, samples(text), log.read_text().splitlines() if log.exists() else []


def test_each_nights_backups_get_one_restore_test_and_each_container_its_turn(tmp_path, monkeypatch):
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake/hostname-2010").write_text("router\n")
    who = 'vmid="2010",guest="router"'
    out, got, calls = restoring(tmp_path, monkeypatch, [ct(2010, 1001), ct(2017, 1002)], **{STATUS: {"total": 64 << 30, "used": 5 << 30}})
    assert out.strip() == "2 guests, 2 with a backup; restore test of router passed"
    assert got[f"nb_backup_restore_ok{{{who}}}"] == "1" and got[f"nb_backup_restore_backup_timestamp_seconds{{{who}}}"] == "1001"
    assert int(got[f"nb_backup_restore_timestamp_seconds{{{who}}}"]) > 1_700_000_000
    assert got['nb_backup_store_bytes{kind="size"}'] == str(64 << 30) and got['nb_backup_store_bytes{kind="used"}'] == str(5 << 30)
    assert got['nb_backup_exists{vmid="2017",guest="forgejo"}'] == "1"
    (call,) = calls
    assert call.startswith("restore --repository nb-pve@pbs!pve@10.42.1.15:backups ct/2010/2027-01-15T02:00:01Z root.pxar ")
    assert " --pattern etc/hostname --keyfile " in call
    # The same night an hour later: tested already, and still reported.
    _, got, calls = restoring(tmp_path, monkeypatch, [ct(2010, 1001), ct(2017, 1002)])
    assert len(calls) == 1 and got[f"nb_backup_restore_ok{{{who}}}"] == "1"
    # The next night it is the other container's turn.
    out, got, calls = restoring(tmp_path, monkeypatch, [ct(2010, 2001), ct(2017, 2002)])
    assert len(calls) == 2 and "ct/2017/" in calls[1] and got['nb_backup_restore_ok{vmid="2017",guest="forgejo"}'] == "1"


def test_a_failed_restore_test_says_so_and_is_tried_again(tmp_path, monkeypatch):
    (tmp_path / "fake").mkdir()
    (tmp_path / "fake/hostname-2010").write_text("someone-else\n")
    who = 'vmid="2010",guest="router"'
    out, got, _ = restoring(tmp_path, monkeypatch, [ct(2010, 1001)])
    assert got[f"nb_backup_restore_ok{{{who}}}"] == "0"
    assert out.strip().endswith("restore test of router failed: etc/hostname holds 'someone-else', expected 'router'")
    (tmp_path / "fake/broken").write_text("")
    out, got, calls = restoring(tmp_path, monkeypatch, [ct(2010, 1001)])
    assert len(calls) == 2 and out.strip().endswith("failed: Error: unable to decrypt")
    (tmp_path / "fake/broken").unlink()
    (tmp_path / "fake/hostname-2010").write_text("router\n")
    out, got, calls = restoring(tmp_path, monkeypatch, [ct(2010, 1001)])
    assert len(calls) == 3 and got[f"nb_backup_restore_ok{{{who}}}"] == "1"


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
