"""Backups: names shared with the pbs role, the OpenTofu resources, and the
checks verify runs against the host."""

import json
import subprocess
import time
from pathlib import Path

import yaml
from conftest import backup_host, fixture_config

from nullbill import backups
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.tofu import render_tofu
from nullbill.verify import run_verify

ROLE_DEFAULTS = Path(__file__).parents[1] / "ansible/roles/pbs/defaults/main.yml"
TARGET = "root@pve.internal"


def planned(facts, cls="mid"):
    model = build_model(fixture_config(cls))
    return build_plan(model, facts(cls)), model


def statuses(results):
    return {r.name: (r.status, r.detail) for r in results}


def test_names_match_the_pbs_role():
    defaults = yaml.safe_load(ROLE_DEFAULTS.read_text())
    assert defaults["pbs_datastore"] == backups.DATASTORE
    assert f"{defaults['pbs_client_user']}!{defaults['pbs_client_token']}" == backups.TOKEN_ID


def test_nightly_job_covers_every_guest_but_pbs_and_templates(facts):
    plan, model = planned(facts, "large")
    config = json.loads(render_tofu(plan, model)["main.tf.json"])
    job = config["resource"]["proxmox_backup_job"]["nb_nightly"]
    names = set(backups.guests(plan))
    assert "pbs" not in names and "router" in names
    assert not any(n.endswith("-runner") for n in names)
    assert sorted(job["vmid"]) == sorted(str(g["vmid"]) for g in backups.guests(plan).values())
    storage = config["resource"]["proxmox_storage_pbs"]["nb_pbs"]
    # Nothing exists until apply has the PBS fingerprint; PVE never prunes.
    assert storage["count"] == job["count"] == '${var.pbs_fingerprint == "" ? 0 : 1}'
    assert storage["backups"] == {"keep_all": True}
    assert storage["server"] == strip_notes(plan)["guests"]["pbs"]["ip"]


def test_without_a_pbs_disk_the_datastore_is_a_volume_beside_the_guests(facts):
    plan, model = planned(facts, "small")
    config = json.loads(render_tofu(plan, model)["main.tf.json"])
    (root, datastore) = config["resource"]["proxmox_virtual_environment_vm"]["pbs"]["disk"]
    assert (datastore["datastore_id"], datastore["size"], datastore["serial"]) == ("local-lvm", 64, backups.DISK_SERIAL)
    assert datastore["backup"] is False and root["datastore_id"] == "local-lvm"
    assert config["resource"]["proxmox_virtual_environment_vm"]["pbs"]["lifecycle"] == {"prevent_destroy": True}
    assert "proxmox_backup_job" in config["resource"]
    assert backups.enabled(plan) and backups.disk(plan) is None and backups.store(plan) == ("local-lvm", 64)
    # The checks run as on a disk of PBS's own; verify adds what one disk cannot do.
    assert {r.status for r in backups.check(plan, TARGET, backup_host(plan), time.time)} == {"PASS"}
    results = run_verify(plan, model, None, lambda *a: (1, "", ""))
    assert results[0].line() == (
        "DEGRADED backups: the backups are on the disk the server runs from, so a failing disk takes both; plug in a disk for them"
    )


def test_no_backup_resources_without_room_for_the_datastore(facts):
    f = facts("small")
    model = build_model(fixture_config("small") | {"overrides": {"linux-runner": {"count": 1}}})
    floors = strip_notes(build_plan(model, f))["disk"]["needed_gib"]
    next(s for s in f["storage"] if s["name"] == "local-lvm")["avail_gib"] = floors + backups.MIN_GIB - 1
    plan = build_plan(model, f)
    assert strip_notes(plan)["backups"]["status"] == "DEGRADED" and not backups.enabled(plan)
    config = json.loads(render_tofu(plan, model)["main.tf.json"])
    assert "proxmox_storage_pbs" not in config["resource"]
    assert len(config["resource"]["proxmox_virtual_environment_vm"]["pbs"]["disk"]) == 1
    assert not any(v.startswith("pbs_") for v in config["variable"])
    assert backups.check(plan, TARGET, backup_host(plan), time.time) == []
    assert backups.skipped(plan, "no target") == []


def test_a_guest_left_out_is_not_in_the_job_and_not_asked_for_a_backup(facts):
    plan, model = planned(facts, "mid")
    assert "inference" in strip_notes(plan)["guests"] and "inference" not in backups.guests(plan)
    job = json.loads(render_tofu(plan, model)["main.tf.json"])["resource"]["proxmox_backup_job"]["nb_nightly"]
    assert str(strip_notes(plan)["guests"]["inference"]["vmid"]) not in job["vmid"]
    assert not any(r.name.startswith("inference ") for r in backups.check(plan, TARGET, backup_host(plan), time.time))


def test_the_datastore_volume_is_found_in_a_vm_config_by_its_serial():
    config = (
        "boot: order=virtio0\nmemory: 2048\n"
        "virtio0: local-lvm:vm-2015-disk-0,discard=on,iothread=1,size=8G\n"
        "virtio1: local-lvm:vm-2015-disk-1,backup=0,discard=on,iothread=1,serial=nb-backups,size=64G\n"
    )
    assert backups.datastore_disk(config) == ("virtio1", "local-lvm", 64)
    assert backups.datastore_disk(config.replace("local-lvm:vm-2015-disk-1", "nb-pbs-disk:vm-2015-disk-0").replace("64G", "2T")) == (
        "virtio1",
        "nb-pbs-disk",
        2048,
    )
    assert backups.datastore_disk("name: pbs\nvirtio0: local-lvm:vm-2015-disk-0,size=8G\n") is None
    assert backups.datastore_disk(config.replace("size=64G", "size=65536M")) is None


def test_fresh_encrypted_backups_and_restore_pass(facts):
    plan, _ = planned(facts)
    results = backups.check(plan, TARGET, backup_host(plan), time.time)
    assert {r.status for r in results} == {"PASS"}
    assert results[-1].name == "restore test"


def test_stale_backups_fail_and_skip_the_restore(facts):
    plan, _ = planned(facts)
    results = statuses(backups.check(plan, TARGET, backup_host(plan, age_h=40), time.time))
    status, detail = results["forgejo has an encrypted backup under 26 h old"]
    assert status == "FAIL" and detail.startswith("newest is 4")
    assert results["restore test"][0] == "SKIP"


def test_unencrypted_backup_fails(facts):
    plan, _ = planned(facts)
    results = backups.check(plan, TARGET, backup_host(plan, encrypted=False), time.time)
    assert {r.status for r in results if r.name != "restore test"} == {"FAIL"}
    assert all("is not encrypted" in r.detail for r in results if r.status == "FAIL")


def test_missing_backup_and_wrong_restore_fail(facts):
    plan, _ = planned(facts)
    host = backup_host(plan, hostname="something-else")

    def without_router(target, command):
        rc, out, err = host(target, command)
        if "/content" in command:
            vmid = strip_notes(plan)["guests"]["router"]["vmid"]
            out = json.dumps([e for e in json.loads(out) if e["vmid"] != vmid])
        return rc, out, err

    results = statuses(backups.check(plan, TARGET, without_router, time.time))
    assert results["router has an encrypted backup under 26 h old"] == ("FAIL", "no backup")
    status, detail = results["restore test"]
    assert status == "FAIL" and "expected" in detail


def test_unreadable_storage_fails(facts):
    plan, _ = planned(facts)
    results = backups.check(plan, TARGET, lambda t, c: (2, "", "storage 'nb-pbs' does not exist"), time.time)
    assert [(r.status, r.detail) for r in results] == [("FAIL", "storage 'nb-pbs' does not exist")]


def test_without_target_backups_skip(facts):
    plan, _ = planned(facts)
    results = backups.skipped(plan, "no target.ssh in config.yaml")
    assert results and {r.status for r in results} == {"SKIP"}


def test_restore_command_runs_in_bash(tmp_path, monkeypatch):
    """The restore command against a stand-in client that checks what PVE
    would pass it: the token secret, the key file and one file pattern."""
    priv = tmp_path / "priv"
    priv.mkdir()
    (priv / f"{backups.STORAGE}.pw").write_text("s3cret'with quote\n")
    (priv / f"{backups.STORAGE}.enc").write_text("{}")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    client = bindir / "proxmox-backup-client"
    client.write_text(
        "#!/usr/bin/env bash\n"
        'set -e; [ "$PBS_PASSWORD" = "s3cret\'with quote" ]; [ "$PBS_FINGERPRINT" = "aa:bb" ]\n'
        'args=" $* "; [[ $args == *" --pattern etc/hostname "* ]]; [[ $args == *" --keyfile "*".enc "* ]]\n'
        '[[ $args == *" --repository nb-pve@pbs!pve@10.42.1.15:backups "* ]]\n'
        'dest=$6; mkdir -p "$dest/etc"; echo forgejo >"$dest/etc/hostname"\n'
    )
    client.chmod(0o755)
    monkeypatch.setattr(backups, "PRIV_DIR", str(priv))
    storage = {"server": "10.42.1.15", "datastore": "backups", "username": backups.TOKEN_ID, "fingerprint": "aa:bb"}
    command = backups._restore_command(storage, "ct/2017/2027-01-15T02:00:00Z")
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env=env)
    assert (done.returncode, done.stdout) == (0, "forgejo\n"), done.stderr


def test_backups_can_be_switched_off(facts):
    config = fixture_config("mid") | {"services": {"pbs": {"enabled": False}}}
    model = build_model(config)
    plan = strip_notes(build_plan(model, facts("mid")))
    assert "pbs" not in plan["guests"]
    assert plan["backups"] == {"target": None, "status": "disabled"}
    assert "pbs" not in {d["use"] for d in plan["disks"].values()}
    assert not backups.enabled(plan)
    config = json.loads(render_tofu(plan, model)["main.tf.json"])
    assert "proxmox_storage_pbs" not in config["resource"]
    assert not any(v.startswith("pbs_") for v in config["variable"])
    results = run_verify(plan, model, None, lambda *a: (1, "", ""))
    assert results[0].line() == "DEGRADED backups: switched off; nothing is backed up"
