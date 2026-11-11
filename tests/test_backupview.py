"""The Backups page: turning backups on at a place, and what it shows of
what was chosen, built and measured (web/backupview.py and its routes)."""

import json
import time
import urllib.parse
from pathlib import Path

import pytest
import yaml
from fixturedata import fixture_config
from test_web import setup, sign_in, start_site, wait

from nullbill import backups
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.web import backupview

ROLE_DEFAULTS = Path(__file__).parents[1] / "ansible/roles/pbs/defaults/main.yml"
KEPT, BLANK = "ata-SYNTHETIC_HDD_0002", "ata-SYNTHETIC_HDD_0003"
OFF = {"services": {"pbs": {"enabled": False}}}
NO_KEY = {"have": False, "fingerprint": None, "saved_at": None}


def shown(facts, fixture, config, built=None, drop=()):
    f = facts(fixture)
    f["disks"] = [d for d in f["disks"] if d["id"] not in drop]
    model = build_model(config)
    plan = strip_notes(build_plan(model, f))
    return backupview.view(config, model, f, plan, built, NO_KEY), plan


def no_erase(config, chosen):
    raise AssertionError("nothing here erases a disk")


def test_the_page_keeps_the_pbs_roles_retention():
    assert yaml.safe_load(ROLE_DEFAULTS.read_text())["pbs_prune"] == {"schedule": "daily", **backupview.KEEPS}


def test_one_disk_offers_the_servers_own_disk_and_says_what_is_covered(facts):
    view, _ = shown(facts, "small", fixture_config("small") | OFF)
    assert (view["on"], view["status"], view["where"], view["built"], view["pending"]) == (False, "disabled", None, None, False)
    assert view["places"] == [] and view["system"]["allowed"] and view["system"]["storage"] == "local-lvm"
    assert view["system"]["min_gib"] == 16 and view["system"]["default_gib"] == 64
    assert "forgejo" in view["covered"] and "pbs" not in view["covered"] and "linux-runner" not in view["covered"]
    config = backupview.save(fixture_config("small") | OFF, view, {"place": "system", "size_gib": 64}, no_erase)
    assert "services" not in config and "backups" not in config
    view, plan = shown(facts, "small", config)
    assert view["where"] == {"kind": "system", "disk": None, "storage": "local-lvm", "size_gib": 64} and view["pending"]
    # Built so: nothing waits, and the room may grow but not shrink.
    view, _ = shown(facts, "small", config, built=plan)
    assert not view["pending"] and view["built"]["kind"] == "system" and view["system"]["min_gib"] == 64
    with pytest.raises(NullBillError, match="between 64 and 4096 GiB; room with backups in it cannot shrink"):
        backupview.save(config, view, {"place": "system", "size_gib": 32}, no_erase)
    grown = backupview.save(config, view, {"place": "system", "size_gib": 96}, no_erase)
    assert grown["backups"] == {"size_gib": 96}
    for bad in (True, "64", 8, None):
        with pytest.raises(NullBillError, match="between"):
            backupview.save(config, view, {"place": "system", "size_gib": bad}, no_erase)


def test_disks_are_offered_by_what_they_hold(facts):
    config = fixture_config("large") | OFF
    view, _ = shown(facts, "large", config)
    kinds = {p["id"]: p["kind"] for p in view["places"]}
    assert kinds == {KEPT: "erase", BLANK: "blank"}
    # A blank disk is in the server: the system disk is not offered beside it.
    assert not view["system"]["allowed"] and BLANK in view["system"]["why"]
    with pytest.raises(NullBillError, match="have a disk of their own"):
        backupview.save(config, view, {"place": "system", "size_gib": 64}, no_erase)
    assert backupview.save(config, view, {"place": BLANK}, no_erase)["disks"] == {BLANK: {"use": "pbs"}}
    # One that holds something goes through the page's own erase, typed word and all.
    asked = []

    def erase(c, chosen):
        asked.append(chosen)
        return {KEPT: {"use": "pbs", "wipe": True}}

    saved = backupview.save(config, view, {"place": KEPT, "confirm": "D_0002"}, erase)
    assert asked == [{KEPT: {"choice": "erase", "for": "backups", "confirm": "D_0002"}}] and saved["disks"][KEPT]["wipe"]
    with pytest.raises(NullBillError, match="choose where the backups go"):
        backupview.save(config, view, {"place": "nope"}, no_erase)


def test_a_kept_disk_is_never_a_place_for_backups(facts):
    config = fixture_config("large") | OFF | {"disks": {KEPT: {"use": "legacy"}}}
    view, _ = shown(facts, "large", config, drop=(BLANK,))
    assert [(p["id"], p["kind"]) for p in view["places"]] == [(KEPT, "kept")] and view["system"]["allowed"]
    with pytest.raises(NullBillError, match="NullBill puts no backups on a disk it shares"):
        backupview.save(config, view, {"place": KEPT, "confirm": "D_0002"}, no_erase)


def test_a_disk_that_holds_the_backups_stays_their_place(facts):
    config = fixture_config("mid")
    f = facts("mid")
    disk = next(d for d in f["disks"] if not d["system"])
    disk["signatures"], disk["vgs"] = ["LVM2_member"], [backups.DISK_STORAGE]
    model = build_model(config)
    plan = strip_notes(build_plan(model, f))
    view = backupview.view(config, model, f, plan, plan, NO_KEY)
    assert [p["kind"] for p in view["places"]] == ["made"] and not view["pending"]
    assert view["where"] == {"kind": "disk", "disk": disk["id"], "storage": "nb-pbs-disk", "size_gib": 1862}
    assert view["left_out"] == ["inference"] and "inference" not in view["covered"]
    # A plan from before backups could live beside the guests names only the disk.
    old = {**plan, "backups": {"target": plan["backups"]["target"], "status": "ok"}}
    assert backupview.view(config, model, f, plan, old, NO_KEY)["built"]["kind"] == "disk"


def test_the_key_is_known_by_its_fingerprint():
    key = json.dumps({"kdf": None, "data": "c3ludGhldGlj", "fingerprint": "aa:bb"})
    assert backupview.key_view(None, {}) == NO_KEY
    assert backupview.key_view(key, {}) == {"have": True, "fingerprint": "aa:bb", "saved_at": None}
    assert backupview.key_view(key, {"at": 5, "fingerprint": "aa:bb"})["saved_at"] == 5
    # A download of an earlier key says nothing about this one.
    assert backupview.key_view(key, {"at": 5, "fingerprint": "cc:dd"})["saved_at"] is None


def prometheus(plan, now, **over):
    guests = backups.guests(plan)

    def row(value, **labels):
        return {"metric": labels, "value": [now, str(value)]}

    answers = {
        "listed": [row(1)],
        "last": [row(now - 3600, vmid=str(g["vmid"]), guest=n) for n, g in guests.items()],
        "exists": [row(1, vmid=str(g["vmid"]), guest=n) for n, g in guests.items()],
        "restore_ok": [row(1, vmid="2017", guest="forgejo")],
        "restore_at": [row(now - 1800, vmid="2017", guest="forgejo")],
        "restore_backup": [row(now - 3600, vmid="2017", guest="forgejo")],
        "store": [row(64 << 30, kind="size"), row(5 << 30, kind="used")],
        "read": [row(now - 600)],
    } | over
    return lambda expr: answers[next(k for k, v in backupview.QUERIES.items() if v == expr)]


def test_the_reading_is_the_servers_with_when_it_was_made(facts):
    plan = strip_notes(build_plan(build_model(fixture_config("small")), facts("small")))
    now = time.time()
    live = backupview.live(plan, prometheus(plan, now), now)
    assert (live["state"], live["read"], live["late"]) == ("ok", now - 600, [])
    assert [g["name"] for g in live["guests"]] == list(backups.guests(plan))
    assert live["restore"] == {"ok": True, "guest": "forgejo", "at": now - 1800, "backup": now - 3600}
    assert live["store"] == {"size": 64 << 30, "used": 5 << 30}
    # A guest with no backup at all, and one whose newest is too old, are late.
    vmid = str(plan["guests"]["forgejo"]["vmid"])
    older = [
        r if r["metric"]["vmid"] != vmid else {**r, "value": [now, str(now - 30 * 3600)]}
        for r in prometheus(plan, now)(backupview.QUERIES["last"])
    ]
    none = [
        r if r["metric"]["guest"] != "router" else {**r, "value": [now, "0"]} for r in prometheus(plan, now)(backupview.QUERIES["exists"])
    ]
    live = backupview.live(plan, prometheus(plan, now, last=older, exists=none), now)
    assert live["late"] == ["router", "forgejo"] and next(g for g in live["guests"] if g["name"] == "router")["last"] is None
    assert backupview.live(plan, prometheus(plan, now, restore_ok=[], store=[]), now)["restore"] is None
    # No reading is said as such, never shown as a good one.
    assert backupview.live(plan, prometheus(plan, now, listed=[]), now)["state"] == "waiting"
    assert backupview.live(plan, prometheus(plan, now, listed=[{"metric": {}, "value": [now, "0"]}]), now)["state"] == "unlisted"

    def down(expr):
        raise OSError("connection refused")

    assert backupview.live(plan, down, now) == {"state": "unreachable", "at": now, "error": "connection refused"}
    off = strip_notes(build_plan(build_model(fixture_config("small") | OFF), facts("small")))
    assert backupview.live(off, down, now) == {"state": "none", "at": now}


def test_backups_are_turned_on_from_the_page_and_the_key_is_the_owners_to_take(tmp_path, monkeypatch):
    asked = []

    def fetch(url):
        asked.append(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0])
        return {"data": {"result": []}}

    app, client, server = start_site(tmp_path, monkeypatch, fetch=fetch)
    try:
        assert client.call("/api/backups")[0] == 401 and client.call("/api/backups/key")[0] == 401
        sign_in(app, client)
        assert client.call("/api/state")[1]["backups"]["ready"] is False
        assert "read the hardware" in client.call("/api/backups", {"place": "system", "size_gib": 64})[1]["error"]
        wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
        choices = {"workloads": ["agents", "linux-ci"], "backups": False, "owner_login": "owner-a@example.internal"}
        assert client.call("/api/choices", choices)[0] == 200
        wait(client, setup(client)[1]["id"])
        view = client.call("/api/state")[1]["backups"]
        assert (view["ready"], view["on"], view["built"], view["pending"]) == (True, False, None, False)

        # A size the server's disk cannot hold: refused with the plan's reason, nothing saved.
        before = (app.home / "config.yaml").read_text()
        status, said = client.call("/api/backups", {"place": "system", "size_gib": 4000})
        assert status == 400 and "besides 4000 GiB for the backups" in said["error"]
        assert (app.home / "config.yaml").read_text() == before

        status, said = client.call("/api/backups", {"place": "system", "size_gib": 48})
        assert status == 200 and said["backups"]["pending"] and said["backups"]["where"]["size_gib"] == 48
        config = yaml.safe_load((app.home / "config.yaml").read_text())
        assert config["backups"] == {"size_gib": 48} and "services" not in config
        assert "pbs" in yaml.safe_load(said["plan"]["text"])["guests"]
        # Nothing is built yet, so nothing is asked of the server.
        assert client.call("/api/backups")[1]["live"]["state"] == "none" and not asked
        wait(client, setup(client)[1]["id"])
        status, got = client.call("/api/backups")
        assert not got["backups"]["pending"] and got["backups"]["built"]["size_gib"] == 48
        assert got["live"]["state"] == "waiting" and backupview.QUERIES["listed"] in asked

        # The key: none before setup made it; then a download the page remembers.
        assert client.call("/api/backups/key")[0] == 404
        key = json.dumps({"kdf": None, "data": "c3ludGhldGlj", "fingerprint": "aa:bb"})
        app.codec(app.home).save({**app.codec(app.home).load(), "pbs_encryption_key": key})
        assert client.call("/api/state")[1]["backups"]["key"] == {"have": True, "fingerprint": "aa:bb", "saved_at": None}
        status, body = client.call("/api/backups/key")
        assert status == 200 and body == json.loads(key)
        assert client.headers["Content-Disposition"] == 'attachment; filename="nullbill-backup-key.json"'
        assert client.call("/api/state")[1]["backups"]["key"]["saved_at"] > time.time() - 60
        assert key not in json.dumps(client.call("/api/state")[1])
    finally:
        server.shutdown()
