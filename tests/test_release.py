"""Guests built on the Debian before are moved to the one guests run now:
apply's release stage (release.py), against a stand-in host whose guests
answer as pct and the guest agent do."""

import json
import os
import shlex
import socket
import subprocess
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
ROOM = (900, 3000, 100)  # what a guest's measuring answers unless a test says otherwise


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
        self.refuses = set()  # guests whose own measuring finds too little room
        self.silent = set()  # guests that do not answer after their restart
        self.racy = set()  # guests whose upgrade ends while apply looks
        self.silent_end = set()  # guests whose upgrade ends without a last word
        self.left = {}  # guest: the last line of an upgrade's result file found in it
        self.asked = []  # guests asked which Debian they run
        self.measured = []  # guests whose room was measured
        self.room = {}  # guest: (MiB needed, MiB free, MiB downloaded), else ROOM
        self.pool = 200 * 1024 * 1024  # KiB free on the guests' storage
        self.own_room = ""  # what the container's own measuring prints
        self.results = {}
        self.done = []  # (what, guest name, more)
        self.local_calls = []
        self.controller = "trixie"
        self.own_memory = 768  # MiB of the NullBill container
        self.memory = []  # (guest, MiB set, what was done to it before)
        self.stuck_memory = set()  # guests whose memory cannot be set back

    def did(self, what, vmid, *more):
        self.done.append((what, self.names[int(vmid)], *more))

    def inside(self, vmid, command, stdin):
        if command == apply.LOOK:
            self.asked.append(self.names[vmid])
            return 0, self.runs[vmid] + "\n" + self.left.get(vmid, "")
        if command == apply.CODENAME:
            return (1, "") if vmid in self.silent else (0, self.runs[vmid] + "\n")
        if command == release.PLACE:
            assert stdin == release.script()
            return 0, ""
        if command == f"{release.SCRIPT} bookworm trixie room":
            assert not self.done, "measured before anything is changed"
            self.measured.append(self.names[vmid])
            found = self.room.get(vmid, ROOM)
            return (0, "room {} {} {}\n".format(*found)) if isinstance(found, tuple) else (0, found)
        if "systemd-run" in command:
            assert command.endswith(f"{release.SCRIPT} bookworm trixie")
            self.did("upgrade", vmid)
            if vmid in self.refuses:
                self.results[vmid] = ["measuring the room for trixie\nrefused: the upgrade needs about 900 MiB free\nfailed\n"]
            elif vmid in self.silent_end:
                self.results[vmid] = ["installing trixie\nactive\n", "installing trixie\ninactive\n"]
            elif vmid in self.breaks:
                self.results[vmid] = [
                    "installing trixie\nactive\n",
                    "failed: E: Sub-process /usr/bin/dpkg returned an error code (1)\nfailed\n",
                ]
            else:
                self.results[vmid] = ["downloading trixie\nactive\n", "installing trixie\nactive\n", "done trixie\ninactive\n"]
                self.runs[vmid] = "trixie"
            return 0, ""
        if command.startswith(f"systemctl is-active {release.UNIT}"):
            assert command.endswith(f"cat {release.RESULT} 2>/dev/null")
            left = self.results[vmid]
            *result, unit = (left.pop(0) if len(left) > 1 else left[0]).splitlines()
            if vmid in self.racy and unit != "active":
                # The unit ends while it is asked: active, then the file it left.
                self.racy.discard(vmid)
                left.insert(0, "\n".join([*result, unit]))
                return 0, "active\ninstalling trixie\n"
            return 0, "\n".join([unit, *result]) + "\n"
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
        if argv[:2] == ["pvesm", "status"]:
            assert argv[2:] == ["--storage", "local-lvm"]
            if self.pool is None:
                return 1, "", "storage 'local-lvm' does not exist\n"
            head = "Name             Type     Status           Total            Used       Available        %\n"
            return (
                0,
                head + f"local-lvm     lvmthin     active       487325696       {487325696 - self.pool}       {self.pool}   50.00%\n",
                "",
            )
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
        if verb == "set":
            assert tool == "pct" and argv[3] == "--memory" and len(argv) == 5, command
            if vmid in self.stuck_memory and int(argv[4]) != release.MEMORY_MIB:
                return 255, "", "can't lock file '/run/lock/lxc/pve-config.lock'\n"
            # With what was done before it: lent after the copy, given back last.
            self.memory.append((self.names[vmid], int(argv[4]), [what for what, n, *_ in self.done if n == self.names[vmid]]))
            return 0, "", ""
        if verb == "config":
            return 0, f"cores: 1\nmemory: {self.own_memory}\nswap: 512\n", ""
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
        if argv[-1] == "room":
            return 0, self.own_room
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
    # Every guest was measured before the first was changed (the stand-in refuses otherwise).
    assert host.measured == upgraded
    assert "release: router: needs about 900 MiB free on its disk, has 3000 MiB" in ctx.log
    took = 100 * release.SNAPSHOT_TIMES * len(upgraded)
    assert f"release: the snapshots from before may take about {took} MiB on local-lvm for a week; 204800 MiB are free there" in ctx.log
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


def test_a_guest_opentofu_made_is_written_down_and_every_other_is_asked(server):
    # What a saved plan creates, also in place of a guest it deletes.
    change = lambda name, *actions: {"type": apply.GUEST_RESOURCES[1], "name": name, "change": {"actions": list(actions)}}  # noqa: E731
    shown = {"resource_changes": [change("forgejo", "create"), change("vpn", "delete", "create"), change("router", "update")]}
    shown["resource_changes"].append({"type": "proxmox_virtual_environment_download_file", "name": "x", "change": {"actions": ["create"]}})
    assert apply.guests_made(shown) == ["forgejo", "vpn"] and apply.guests_made({"resource_changes": None}) == []

    # A server whose list of guests is gone (built by a version that kept
    # none): nothing is taken as new, each guest says what it runs.
    ctx, host = server(built=False)
    assert apply.built_guests(ctx.home, ctx.bare) is None
    (ctx.home / "tofu").mkdir()
    (ctx.home / "tofu/main.tf.json").write_text("{}")
    assert apply.built_guests(ctx.home, ctx.bare) == sorted(ctx.bare["guests"])
    assert release.unknown(ctx.home, ctx.bare, ctx.model, apply.built_guests(ctx.home, ctx.bare))[0] == "router"
    release.note(ctx.home, "forgejo", "trixie")
    apply.stage_release(ctx)
    upgraded = {name for what, name, *_ in host.done if what == "upgrade"}
    assert "forgejo" not in host.asked and "forgejo" not in upgraded
    assert set(host.asked) == upgraded == set(release.candidates(ctx.bare, ctx.model)) - {"forgejo"}

    # Guests already on today's release are only written down.
    ctx, host = server()
    (ctx.home / release.RECORD).unlink()
    host.runs = dict.fromkeys(host.runs, "trixie")
    apply.stage_release(ctx)
    assert host.done == [] and release.read(ctx.home)["router"]["release"] == "trixie"
    assert "copy" not in release.read(ctx.home)["router"]


def test_a_guest_that_does_not_come_back_from_its_restart_is_put_back(server, monkeypatch):
    ctx, host = server()
    monkeypatch.setattr(apply, "RESTART_S", 0)
    host.silent = {ctx.bare["guests"]["egress-proxy"]["vmid"]}
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "egress-proxy: the upgrade to Debian 13 failed: no answer within 0 minutes of its restart" in str(stopped.value)
    assert str(stopped.value).endswith("It is back as it was before, from its snapshot")
    assert [what for what, name, *_ in host.done if name == "egress-proxy"] == ["snapshot", "upgrade", "reboot", "rollback"]
    assert "egress-proxy" not in release.read(ctx.home)


def test_a_guest_left_half_upgraded_stops_the_run_before_its_copy_is_replaced(server):
    # A backup was the copy and the upgrade failed: nothing put the guest back.
    ctx, host = server()
    router = ctx.bare["guests"]["router"]["vmid"]
    host.no_snapshot, host.breaks = {router}, {router}
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "Its backup from before is on" in str(stopped.value)
    host.left[router] = "failed: E: Sub-process /usr/bin/dpkg returned an error code (1)\n"
    for runs in ("bookworm", "trixie"):
        host.runs[router] = runs
        host.done.clear()
        with pytest.raises(ApplyError) as stopped:
            apply.stage_release(ctx)
        assert "router: an upgrade to Debian 13 began in it and was neither finished nor put back" in str(stopped.value)
        assert "it stopped at: failed: E: Sub-process" in str(stopped.value) and "Put router back from its copy" in str(stopped.value)
        assert host.done == [] and "router" not in release.read(ctx.home)
    # Put back by the owner, the next run upgrades it; a finished upgrade's file is no stop.
    host.left[router], host.runs[router] = "done trixie\n", "trixie"
    host.breaks = set()
    apply.stage_release(ctx)
    assert release.read(ctx.home)["router"]["release"] == "trixie"


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


def test_nullbills_own_container_is_upgraded_last_by_a_unit_of_its_own(server, monkeypatch):
    ctx, host = server()
    own = ctx.bare["guests"]["nullbill"]["vmid"]
    commit = ["a" * 40]
    monkeypatch.setattr(apply.version, "head", lambda root: commit[0])
    apply.controller_release(ctx)
    assert host.done == [] and len(host.local_calls) == 1

    host.controller = "bookworm"
    apply.controller_release(ctx)
    assert host.done == [] and "is no container of install.sh's; it is left as it is" in ctx.log[-1]

    (ctx.home / "install.json").write_text(json.dumps({"host": "192.0.2.10", "vmid": own}))
    apply.controller_release(ctx)
    # The tool snapshots once this run has ended, so the snapshot holds its
    # result; apply touches nothing on the host itself.
    assert host.done == [] and host.memory == []
    tried = ctx.home / apply.CONTROLLER_TRIED
    unit = host.local_calls[-1]
    assert unit[:5] == ["systemd-run", "--quiet", "--collect", "--unit", "nullbill-release"]
    assert unit[5].endswith("tools/upgrade-controller")
    assert unit[6:] == ["bookworm", "trixie", TARGET, str(own), SNAP, str(os.getpid()), str(tried)]
    assert "moves to Debian 13 once this run has ended" in ctx.log[-1]

    # The try is written down before the snapshot, so a container the host
    # put back holds it: no second try by itself with the same NullBill.
    assert json.loads(tried.read_text())["snapshot"] == SNAP and json.loads(tried.read_text())["commit"] == commit[0]
    before = len(host.local_calls)
    apply.controller_release(ctx)
    assert [c for c in host.local_calls[before:] if c[0] == "systemd-run"] == []
    assert "its upgrade failed before and the host put it back" in ctx.log[-1] and "controller-upgrade.log" in ctx.log[-1]
    # Once NullBill was updated, once more: the update may be the fix.
    commit[0] = "b" * 40
    apply.controller_release(ctx)
    assert "tried once more with this one" in ctx.log[-2] and host.local_calls[-1][5].endswith("tools/upgrade-controller")
    assert json.loads(tried.read_text())["commit"] == commit[0]
    # A note from before notes kept the commit counts as an earlier NullBill.
    tried.write_text(json.dumps({"snapshot": SNAP, "vmid": own, "at": time.time()}))
    before = len(host.local_calls)
    apply.controller_release(ctx)
    assert [c for c in host.local_calls[before:] if c[0] == "systemd-run" and c[-1] != "room"]

    # On the new release the snapshot the tool made goes after its week, as the guests' do.
    host.controller = "trixie"
    host.snapshots[own].add(SNAP)
    apply.controller_release(ctx)
    assert host.done == [] and tried.exists()
    tried.write_text(json.dumps(json.loads(tried.read_text()) | {"at": time.time() - release.KEEP_COPY_S - 1}))
    apply.controller_release(ctx)
    assert host.done == [("delsnapshot", "nullbill")] and not tried.exists()

    # Too little room in the container: nothing is tried, and nothing written down to stop a later try.
    host.controller = "bookworm"
    host.own_room = "room 2400 800 300\n"
    host.done.clear()
    apply.controller_release(ctx)
    assert host.done == [] and not tried.exists()
    assert "its upgrade needs about 2400 MiB free on its disk and 800 MiB are free" in ctx.log[-1]


def test_too_little_room_anywhere_stops_the_run_before_any_guest_is_changed(server):
    ctx, host = server()
    router, vpn = ctx.bare["guests"]["router"]["vmid"], ctx.bare["guests"]["vpn"]["vmid"]
    host.room = {
        router: (1800, 700, 200),
        vpn: "E: The repository 'https://packages.example.internal trixie Release' does not have a Release file.\n",
    }
    host.pool = 2 * 1024 * 1024
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    said = str(stopped.value)
    assert "too little room to upgrade the guests to Debian 13; nothing was changed: " in said
    assert "router: needs about 1800 MiB free on its disk and has 700 MiB" in said
    assert "vpn: its room could not be measured (E: The repository" in said
    assert "the server's storage local-lvm: the snapshots may take about" in said and "2048 MiB are free" in said
    assert host.done == [] and release.read(ctx.home) == {}
    # With room, the same run goes through; a storage that cannot be read is said and not a stop.
    host.room, host.pool = {}, None
    apply.stage_release(ctx)
    assert "release: the free room on local-lvm could not be read, so the room for the snapshots was not checked" in ctx.log
    assert release.read(ctx.home)["router"]["release"] == "trixie"


def test_a_guest_that_finds_too_little_room_itself_is_left_as_it_was(server):
    assert release.result("measuring the room for trixie\nrefused: the upgrade needs about 900 MiB\n") == (
        "refused",
        "the upgrade needs about 900 MiB",
    )
    assert release.room("noise\nroom 885 23813 96\n") == (885, 23813, 96) and release.room("room x 1 2\n") is None
    ctx, host = server()
    router = ctx.bare["guests"]["router"]["vmid"]
    host.refuses = {router}
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "router was not upgraded and not changed: the upgrade needs about 900 MiB free" in str(stopped.value)
    assert [what for what, name, *_ in host.done if name == "router"] == ["snapshot", "upgrade"]
    # Such a result file is no half upgrade: the next run tries again.
    host.left[router], host.refuses = "refused: the upgrade needs about 900 MiB free\n", set()
    host.done.clear()
    host.measured.clear()
    apply.stage_release(ctx)
    assert release.read(ctx.home)["router"]["release"] == "trixie"


def test_the_script_is_placed_from_a_stream_that_cannot_be_opened_again(tmp_path):
    # In an unprivileged container the piped script is the host root's pipe:
    # readable, but /dev/stdin cannot be opened. A socket is the same to a shell.
    ours, theirs = socket.socketpair()
    ours.sendall(release.script().encode())
    ours.shutdown(socket.SHUT_WR)
    where = tmp_path / "nb-release-upgrade"
    done = subprocess.run(["sh", "-c", release.PLACE.replace(release.SCRIPT, str(where))], stdin=theirs, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert where.read_text() == release.script() and os.access(where, os.X_OK) and not where.with_name(where.name + ".new").exists()
    refused = subprocess.run(["sh", "-c", "cat /dev/stdin"], stdin=socket.socketpair()[1], capture_output=True, text=True)
    assert refused.returncode != 0
    # Nothing sent into a guest names /dev/stdin.
    assert "/dev/stdin" not in (apply.REPO_ROOT / "src/nullbill/apply.py").read_text()


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


def test_a_small_container_has_more_memory_for_its_upgrade_and_its_own_after(server):
    """The first real run: the kernel ended dpkg in the vpn guest's 256 MiB."""
    ctx, host = server()
    guests = ctx.bare["guests"]
    small = {n for n, g in guests.items() if g["kind"] == "lxc" and g["memory_mib"] < release.MEMORY_MIB and g["vmid"] in host.kinds}
    assert "vpn" in small and guests["vpn"]["memory_mib"] < 1024
    host.breaks.add(guests["vpn"]["vmid"])
    host.stuck_memory.add(guests["egress-proxy"]["vmid"])
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "vpn: the upgrade to Debian 13 failed" in str(stopped.value)
    upgraded = [name for what, name, *_ in host.done if what == "upgrade"]
    lent = [(name, before) for name, mib, before in host.memory if mib == release.MEMORY_MIB]
    # After the copy and before the unit starts, for every small container tried.
    assert lent == [(name, ["snapshot"]) for name in upgraded if name in small]
    back = {name: (mib, before) for name, mib, before in host.memory if mib != release.MEMORY_MIB}
    # Its own again, last: after the restart, and after a guest was put back.
    assert back["vpn"] == (guests["vpn"]["memory_mib"], ["snapshot", "upgrade", "rollback"])
    assert set(back) == {name for name, _ in lent} - {"egress-proxy"}
    assert all(guests[name]["memory_mib"] == mib and before[-1] in ("reboot", "rollback") for name, (mib, before) in back.items())
    # One that cannot be set back is said, and the run goes on to the next.
    said = [line for line in ctx.log if "egress-proxy still has" in line]
    assert said and f"not its own {guests['egress-proxy']['memory_mib']}" in said[0] and "the next setup run sets it" in said[0]
    # A VM's memory is left alone.
    assert "router" in upgraded and "router" not in {name for name, *_ in host.memory}


def test_an_upgrade_that_ends_while_apply_looks_is_read_as_it_ended(server):
    """The first real run: the download client's upgrade wrote `done` and
    ended between apply's two reads, and read as ended without a result."""
    ctx, host = server()
    qbt = ctx.bare["guests"]["vpn"]["vmid"]
    host.racy.add(qbt)
    apply.stage_release(ctx)
    assert release.read(ctx.home)["vpn"]["release"] == "trixie" and not host.racy
    assert not [line for line in ctx.log if "without a result" in line]
    # A unit that ended and left no last word is still a failure, put back.
    (ctx.home / release.RECORD).unlink()
    ctx, host = server()
    router = ctx.bare["guests"]["router"]["vmid"]
    host.silent_end = {router}
    with pytest.raises(ApplyError) as stopped:
        apply.stage_release(ctx)
    assert "router: the upgrade to Debian 13 failed: the upgrade ended without a result (at: installing trixie)" in str(stopped.value)
    assert [what for what, name, *_ in host.done if name == "router"] == ["snapshot", "upgrade", "rollback"]
