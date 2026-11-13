"""Guests built on the Debian before are moved to the one guests run now:
apply's release stage (release.py), against a stand-in host whose guests
answer as pct and the guest agent do."""

import json
import shlex
import time

import pytest
from fixturedata import fixture_config
from test_apply import TARGET, MemoryCodec

from nullbill import apply, backups, release
from nullbill.apply import ApplyError, Context
from nullbill.artifacts import render_all
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

SNAP = "nb-before-debian-13"


class Server:
    """The PVE host: each guest's release, its snapshots, and what was run."""

    def __init__(self, plan, runs="bookworm"):
        self.kinds = {g["vmid"]: ("pct" if g["kind"] == "lxc" else "qm") for g in plan["guests"].values() if g.get("vmid")}
        self.names = {g["vmid"]: n for n, g in plan["guests"].items() if g.get("vmid")}
        self.runs = dict.fromkeys(self.kinds, runs)
        self.snapshots = {vmid: set() for vmid in self.kinds}
        self.no_snapshot = set()  # guests PVE cannot snapshot
        self.no_backup = set()  # storages vzdump fails on
        self.breaks = set()  # guests whose upgrade fails
        self.results = {}
        self.done = []  # (what, guest name, more)
        self.local_calls = []
        self.controller = "trixie"

    def did(self, what, vmid, *more):
        self.done.append((what, self.names[int(vmid)], *more))

    def inside(self, vmid, command, stdin):
        if command == apply.CODENAME:
            return 0, self.runs[vmid] + "\n"
        if command.startswith("install -m 0755 /dev/stdin"):
            assert stdin == release.script()
            return 0, ""
        if "systemd-run" in command:
            assert command.endswith(f"{release.SCRIPT} bookworm trixie")
            self.did("upgrade", vmid)
            if vmid in self.breaks:
                self.results[vmid] = [
                    "installing trixie\nactive\n",
                    "failed: E: Sub-process /usr/bin/dpkg returned an error code (1)\nfailed\n",
                ]
            else:
                self.results[vmid] = ["downloading trixie\nactive\n", "installing trixie\nactive\n", "done trixie\ninactive\n"]
                self.runs[vmid] = "trixie"
            return 0, ""
        if command.startswith(f"cat {release.RESULT}"):
            left = self.results[vmid]
            return 0, left.pop(0) if len(left) > 1 else left[0]
        if command.startswith("tail -n 25"):
            return 0, "Setting up libc6 ...\ndpkg: error processing package libc6\n"
        raise AssertionError(command)

    def host(self, command, stdin=None, timeout=900):
        argv = shlex.split(command)
        if argv[:2] == ["pct", "exec"]:
            rc, out = self.inside(int(argv[2]), argv[-1], stdin)
            return rc, out, ""
        if argv[:3] == ["qm", "guest", "exec"]:
            assert self.kinds[int(argv[3])] == "qm" and ("--pass-stdin" in argv) == (stdin is not None)
            rc, out = self.inside(int(argv[3]), argv[-1], stdin)
            return 0, json.dumps({"exitcode": rc, "exited": 1, "out-data": out}), ""
        tool, verb, vmid = argv[0], argv[1], int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        if argv[0] in ("!", "{"):
            tool, verb, vmid = argv[1], argv[2], int(argv[3])
        if tool == "vzdump":
            storage = argv[argv.index("--storage") + 1]
            if storage in self.no_backup:
                return 1, "", f"ERROR: storage '{storage}' does not exist\n"
            self.did("backup", argv[1], storage)
            return 0, "", ""
        assert self.kinds[vmid] == tool, command
        if verb == "listsnapshot":
            name = command.split("grep -qw ")[1].split()[0]
            if name in self.snapshots[vmid] and "delsnapshot" in command:
                self.snapshots[vmid].discard(name)
                self.did("delsnapshot", vmid)
            return 0, "", ""
        if verb == "snapshot":
            if vmid in self.no_snapshot:
                return 255, "", "snapshot feature is not available\n"
            self.snapshots[vmid].add(argv[3])
            self.did("snapshot", vmid)
            return 0, "", ""
        if verb == "reboot":
            self.did("reboot", vmid)
            return 0, "", ""
        if verb == "stop" and "rollback" in command:
            assert f"{tool} rollback {vmid} {SNAP} && {tool} start {vmid}" in command
            self.runs[vmid] = "bookworm"
            self.did("rollback", vmid)
            return 0, "", ""
        raise AssertionError(command)

    def local(self, argv, cwd, env, capture=False):
        self.local_calls.append(argv)
        return (0, self.controller + "\n") if argv[:2] == ["sh", "-c"] else (0, "")

    def playbook(self, playbook, hosts, variables):
        raise AssertionError(playbook)


@pytest.fixture
def server(facts, tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)

    def make(built=True, config=None):
        model = build_model(config or fixture_config("mid"))
        plan = build_plan(model, facts("mid"))
        host = Server(strip_notes(plan))
        ctx = Context(plan, model, tmp_path, TARGET, host, MemoryCodec(), render_all(plan, model))
        if built:
            (tmp_path / apply.GUESTS).write_text(json.dumps({"guests": sorted(ctx.bare["guests"])}))
        return ctx, host

    return make


def test_guests_from_before_are_copied_upgraded_and_restarted_one_at_a_time(server):
    ctx, host = server()
    apply.stage_release(ctx)
    # The two every download goes through come first; Windows, the CI pools'
    # images, PBS and NullBill's own container are not this stage's.
    upgraded = [name for what, name, *_ in host.done if what == "upgrade"]
    assert upgraded[:2] == ["router", "egress-proxy"]
    assert set(upgraded) == set(ctx.bare["guests"]) - {"nullbill", "pbs", "linux-runner", "windows-runner", "windows-dev"}
    for name in upgraded:
        assert [what for what, n, *_ in host.done if n == name] == ["snapshot", "upgrade", "reboot"]
    record = release.read(ctx.home)
    assert record["postgres"]["release"] == "trixie" and record["postgres"]["was"] == "bookworm"
    assert record["postgres"]["copy"] == {"kind": "snapshot", "name": SNAP}
    assert "release: postgres: installing trixie" in ctx.log
    assert "release: router runs Debian 13 now; its snapshot from before is kept for a week" in ctx.log
    assert release.unknown(ctx.home, ctx.bare, ctx.model, sorted(ctx.bare["guests"])) == []

    # The next run asks no guest again.
    host.done.clear()
    apply.stage_release(ctx)
    assert host.done == []


def test_a_guest_this_run_made_and_one_already_there_are_only_written_down(server):
    ctx, host = server(built=False)
    assert release.unknown(ctx.home, ctx.bare, ctx.model, None) == []
    apply.stage_release(ctx)
    assert host.done == [] and release.read(ctx.home)["forgejo"]["release"] == "trixie"
    assert "copy" not in release.read(ctx.home)["forgejo"]

    ctx, host = server()
    (ctx.home / release.RECORD).unlink()
    host.runs = dict.fromkeys(host.runs, "trixie")
    apply.stage_release(ctx)
    assert host.done == [] and release.read(ctx.home)["router"]["release"] == "trixie"


def test_a_failed_upgrade_puts_the_guest_back_and_stops_before_the_next(server):
    ctx, host = server()
    host.breaks = {ctx.bare["guests"]["egress-proxy"]["vmid"]}
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    said = str(stopped.value)
    assert "egress-proxy: the upgrade to Debian 13 failed: E: Sub-process /usr/bin/dpkg returned an error code (1)" in said
    assert said.endswith("It is back as it was before, from its snapshot")
    assert [d for d in host.done if d[1] == "egress-proxy"] == [
        ("snapshot", "egress-proxy"),
        ("upgrade", "egress-proxy"),
        ("rollback", "egress-proxy"),
    ]
    assert {name for _, name, *_ in host.done} == {"router", "egress-proxy"}
    assert "release: egress-proxy: dpkg: error processing package libc6" in ctx.log
    assert "egress-proxy" not in release.read(ctx.home)
    assert release.unknown(ctx.home, ctx.bare, ctx.model, sorted(ctx.bare["guests"]))[0] == "egress-proxy"


def test_where_pve_cannot_snapshot_a_backup_is_the_copy_and_without_one_nothing_is_touched(server):
    ctx, host = server()
    router = ctx.bare["guests"]["router"]["vmid"]
    host.no_snapshot = {router}
    host.breaks = {ctx.bare["guests"]["egress-proxy"]["vmid"]}
    with pytest.raises(ApplyError):
        apply.stage_release(ctx)
    storage = backups.STORAGE if backups.enabled(ctx.plan) else "local"
    assert host.done[:3] == [("backup", "router", storage), ("upgrade", "router"), ("reboot", "router")]
    assert release.read(ctx.home)["router"]["copy"] == {"kind": "backup", "storage": storage}
    assert f"release: router runs Debian 13 now; its backup from before is on {storage}" in ctx.log

    ctx, host = server()
    (ctx.home / release.RECORD).unlink()
    host.no_snapshot = {router}
    host.no_backup = {backups.STORAGE, "local"}
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "router: no copy could be made before its upgrade (snapshot: snapshot feature is not available; backup to" in str(stopped.value)
    assert str(stopped.value).endswith("it was not changed")
    assert host.done == []


def test_a_guest_on_some_other_release_stops_the_run(server):
    ctx, host = server()
    host.runs[ctx.bare["guests"]["router"]["vmid"]] = "bullseye"
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "router runs Debian bullseye, neither bookworm nor trixie" in str(stopped.value)
    assert host.done == []


def test_a_snapshot_is_deleted_a_week_after_its_guests_upgrade(server):
    ctx, host = server()
    apply.stage_release(ctx)
    host.done.clear()
    apply.stage_release(ctx)
    assert host.done == []
    record = release.read(ctx.home)
    record["vpn"]["upgraded"] -= release.KEEP_COPY_S + 1
    release.write(ctx.home, record)
    apply.stage_release(ctx)
    assert host.done == [("delsnapshot", "vpn")]
    assert "copy" not in release.read(ctx.home)["vpn"] and "copy" in release.read(ctx.home)["router"]


def test_one_stage_alone_is_refused_while_guests_from_before_wait(server):
    ctx, host = server()
    with pytest.raises(ApplyError) as stopped:
        apply.run_apply(ctx, only="games")
    assert "apply stopped at games: router, egress-proxy" in str(stopped.value)
    assert "were built before guests ran Debian 13" in str(stopped.value) and host.done == []
    # The stage that upgrades them may run alone, and after it any other.
    apply.run_apply(ctx, only="release")
    apply.run_apply(ctx, [("games", lambda c: None)], only="games")


def test_nullbills_own_container_is_upgraded_last_by_a_unit_of_its_own(server):
    ctx, host = server()
    own = ctx.bare["guests"]["nullbill"]["vmid"]
    apply.controller_release(ctx)
    assert host.done == [] and len(host.local_calls) == 1

    host.controller = "bookworm"
    apply.controller_release(ctx)
    assert host.done == [] and "is no container of install.sh's; it is left as it is" in ctx.log[-1]

    (ctx.home / "install.json").write_text(json.dumps({"host": "192.0.2.10", "vmid": own}))
    apply.controller_release(ctx)
    assert host.done == [("snapshot", "nullbill")]
    unit = host.local_calls[-1]
    assert unit[:5] == ["systemd-run", "--quiet", "--collect", "--unit", "nullbill-release"]
    assert unit[5].endswith("tools/upgrade-controller") and unit[6:] == ["bookworm", "trixie", TARGET, str(own), SNAP]
    assert "This page is away for about ten minutes" in ctx.log[-1]

    host.no_snapshot = {own}
    before = len(host.local_calls)
    apply.controller_release(ctx)
    assert "stays on Debian bookworm: no snapshot could be made" in ctx.log[-1]
    assert [c for c in host.local_calls[before:] if c[0] == "systemd-run"] == []


def test_the_script_names_what_apply_reads_and_the_catalog_names_the_release():
    text = release.script()
    assert release.RESULT in text.replace("$state", "/var/lib/nullbill") and release.LOG in text
    assert release.result("updating bookworm\ninstalling trixie\n") == ("running", "installing trixie")
    assert release.result("installing trixie\ndone trixie\n") == ("done", "trixie")
    assert release.result("failed: no room\n") == ("failed", "no room")
    assert release.result("") == ("running", "")
    model = build_model(fixture_config("small"))
    assert model["images"]["release"] == {"version": "13", "codename": "trixie", "from": "bookworm"}
    assert "trixie" in model["images"]["debian-cloud"]["url"] and "debian-13-" in model["images"]["debian-lxc"]["url"]
