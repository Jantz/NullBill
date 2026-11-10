"""host_baseline's nb-power against pct and qm stand-ins: what starts, what
stops by itself, what is asked about, and what a setup run may do."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "ansible/roles/host_baseline/files/nb-power"

# One stand-in for both tools, by the name it is called as. The world is a
# JSON file: per VMID its kind, state and what nb-busy answers.
FAKE = r"""#!__PYTHON__
import json, os, sys
path = os.environ["NB_FAKE_WORLD"]
world = json.load(open(path))
tool, args = os.path.basename(sys.argv[0]), sys.argv[1:]
kind = "lxc" if tool == "pct" else "vm"
world.setdefault("calls", []).append([tool, *args])
rc = 0
if args[0] == "list":
    print("VMID Status Lock Name" if tool == "pct" else "VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID")
    for vmid, g in sorted(world["guests"].items()):
        if g["kind"] != kind:
            continue
        if tool == "pct":
            print(f"{vmid} {g['state']} {g['name']}")
        else:
            print(f"{vmid} {g['name']} {g['state']} {g.get('mem', 2048)} 32.00 {4000 if g['state'] == 'running' else 0}")
else:
    g = world["guests"].get(args[1])
    if g is None or g["kind"] != kind:
        print(f"Configuration file 'nodes/pve/{args[1]}.conf' does not exist", file=sys.stderr)
        rc = 2
    elif args[0] == "start":
        if g.get("start_fails"):
            print("start failed: out of memory", file=sys.stderr)
            rc = 255
        else:
            g["state"] = "running"
    elif args[0] == "shutdown":
        if g.get("hangs"):
            print("VM quit/powerdown failed - got timeout", file=sys.stderr)
            rc = 255
        else:
            g["state"] = "stopped"
    elif args[0] == "exec":
        command = args[3:]
        if g["state"] != "running":
            print("container not running", file=sys.stderr)
            rc = 255
        elif command[0] == "/usr/local/sbin/nb-busy":
            rc = g.get("busy_rc", 0)
            print("\n".join(g.get("busy", [])))
        elif command[:2] == ["test", "-s"]:
            rc = 0 if g.get("cert") else 1
        elif command[0] == "openssl":
            rc = 0 if g.get("cert") == "valid" else 1
        elif command[0] == "env" and "token" in command:
            print("one-time-token")
        elif command[:2] == ["sh", "-c"]:
            g["cert"] = "valid"
            g["issued"] = {"token": sys.stdin.read().strip(), "args": command[3:]}
json.dump(world, open(path, "w"))
sys.exit(rc)
"""

MIB = 1024 * 1024


def guest(name, kind="lxc", state="stopped", **more):
    return {"name": name, "kind": kind, "state": state, **more}


POLICY = {
    "headroom_mib": 5000,
    "ca": {"vmid": 2012, "url": "https://step-ca.nullbill.internal"},
    "groups": {
        "media": {
            "title": "Jellyfin",
            "memory_mib": 1024,
            "stop": "auto",
            "idle_minutes": 30,
            "guests": [{"name": "jellyfin", "kind": "lxc", "vmid": 2024, "memory_mib": 2048}],
        },
        "inference": {
            "title": "Local AI models",
            "memory_mib": 1536,
            "stop": "auto",
            "idle_minutes": 20,
            "guests": [
                {"name": "inference", "kind": "lxc", "vmid": 2020, "memory_mib": 2048},
                {"name": "model-gateway", "kind": "lxc", "vmid": 2021, "memory_mib": 1024, "certs": ["model-gateway.nullbill.internal"]},
            ],
        },
        "windows-dev": {
            "title": "Windows desktop",
            "memory_mib": 4096,
            "stop": "ask",
            "idle_minutes": 240,
            "guests": [{"name": "windows-dev", "kind": "vm", "vmid": 2022, "memory_mib": 4096}],
        },
        "linux-ci": {
            "title": "Linux CI runners",
            "memory_mib": 2176,
            "stop": "auto",
            "idle_minutes": 30,
            "guests": [
                {"name": "runner-dispatcher", "kind": "lxc", "vmid": 2018, "memory_mib": 256},
                {"name": "linux-runner", "kind": "vm-pool", "first": 2300, "last": 2301, "memory_mib": 2048},
            ],
        },
    },
}


class Host:
    def __init__(self, tmp_path, policy=POLICY):
        self.dir = tmp_path
        self.world = tmp_path / "world.json"
        self.now = 1_800_000_000
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for tool in ("pct", "qm"):
            (bindir / tool).write_text(FAKE.replace("__PYTHON__", sys.executable))
            (bindir / tool).chmod(0o755)
        (tmp_path / "policy.json").write_text(json.dumps(policy))
        (tmp_path / "meminfo").write_text("MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n")
        self.set(
            {
                "2012": guest("step-ca", state="running"),
                "2018": guest("runner-dispatcher"),
                "2020": guest("inference"),
                "2021": guest("model-gateway"),
                "2022": guest("windows-dev", "vm", mem=4096),
                "2024": guest("jellyfin"),
            }
        )
        # time.time() as the test sets it: the script reads NB_FAKE_NOW
        # through a sitecustomize on its path.
        site = tmp_path / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import os, time\nif os.environ.get('NB_FAKE_NOW'):\n    time.time = lambda: float(os.environ['NB_FAKE_NOW'])\n"
        )

    def set(self, guests):
        self.world.write_text(json.dumps({"guests": guests}))

    def read(self):
        return json.loads(self.world.read_text())

    def guests(self):
        return self.read()["guests"]

    def change(self, vmid, **values):
        world = self.read()
        world["guests"][str(vmid)] |= values
        self.world.write_text(json.dumps(world))

    def running(self):
        return sorted(g["name"] for g in self.guests().values() if g["state"] == "running" and g["name"] != "step-ca")

    def run(self, *args, check=True):
        env = os.environ | {
            "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}",
            "PYTHONPATH": str(self.dir / "site"),
            "NB_FAKE_WORLD": str(self.world),
            "NB_FAKE_NOW": str(self.now),
            "NB_POWER_POLICY": str(self.dir / "policy.json"),
            "NB_STATE_DIR": str(self.dir / "state"),
            "NB_TEXTFILE_DIR": str(self.dir / "textfile"),
            "NB_CGROUP_ROOT": str(self.dir / "cgroup"),
            "NB_MEMINFO": str(self.dir / "meminfo"),
        }
        done = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True)
        if check:
            assert done.returncode == 0, done.stdout + done.stderr
        return done

    def json(self, *args, rc=0):
        done = self.run(*args, "--json", check=False)
        assert done.returncode == rc, done.stdout + done.stderr
        return json.loads(done.stdout)

    def status(self):
        found = self.json("status")
        return found | {"by_name": {g["name"]: g for g in found["groups"]}}

    def minutes(self, n):
        """n minutes later, as the timer's run finds things then. The runs
        in between would each see the same use, so one stands for them."""
        self.now += n * 60
        self.run("tick")

    def metrics(self):
        path = self.dir / "textfile/nullbill-power.prom"
        return dict(re.findall(r"^(nb_\w+(?:\{[^}]*\})?) (\d+)$", path.read_text(), re.M)) if path.exists() else None

    def cpu(self, scope, usec):
        path = self.dir / "cgroup" / scope
        path.mkdir(parents=True, exist_ok=True)
        (path / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\n")


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_everything_starts_off_and_counts_nothing(host):
    found = host.status()
    assert [g["state"] for g in found["groups"]] == ["off"] * 4
    assert (found["headroom_mib"], found["used_mib"], found["free_mib"]) == (5000, 0, 5000)


def test_start_and_stop_a_workload_with_all_its_guests(host):
    assert host.json("start", "inference") == {"ok": True, "name": "inference", "already": False, "stopped": [], "warnings": []}
    assert host.running() == ["inference", "model-gateway"]
    found = host.status()
    assert found["by_name"]["inference"]["state"] == "on"
    assert found["used_mib"] == 1536
    # The gateway, which fronts the model, stops first.
    host.json("stop", "inference")
    stops = [c for c in host.read()["calls"] if c[1] == "shutdown"]
    assert [c[2] for c in stops] == ["2021", "2020"]
    assert host.running() == []
    assert host.json("stop", "inference")["already"] is True


def test_a_container_that_hangs_is_stopped_and_a_vm_is_not(host):
    host.json("start", "media")
    host.json("stop", "media")
    host.json("start", "windows-dev")
    host.change(2022, hangs=True)
    done = host.run("stop", "windows-dev", check=False)
    assert done.returncode == 1
    assert "did not shut down" in done.stderr and "shut it down from inside" in done.stderr
    assert host.running() == ["windows-dev"]
    assert ["pct", "shutdown", "2024", "--timeout", "120", "--forceStop", "1"] in host.read()["calls"]


def test_unknown_or_unmade_workload_is_refused(host):
    assert "does not run on demand" in host.json("start", "postgres", rc=1)["error"]
    guests = host.guests()
    del guests["2024"]
    host.set(guests)
    assert host.status()["by_name"]["media"]["state"] == "missing"
    assert "not made yet; run setup" in host.json("start", "media", rc=1)["error"]


def test_idle_workload_stops_by_itself_after_its_minutes(host):
    host.json("start", "media")
    host.minutes(29)
    found = host.status()["by_name"]["media"]
    assert found["state"] == "on" and found["idle_s"] == 29 * 60 and found["stops_in_s"] == 60
    host.minutes(1)
    assert host.running() == []
    events = host.status()["events"]
    assert events[-1] | {"at": 0} == {"at": 0, "group": "media", "what": "stopped", "why": "not used for 30 min"}


def test_use_keeps_it_on_and_idle_counts_from_the_last_use(host):
    host.json("start", "media")
    host.change(2024, busy=["playing on 1 device"])
    host.minutes(45)
    found = host.status()["by_name"]["media"]
    assert found["state"] == "on" and found["busy"] == ["playing on 1 device"] and "stops_in_s" not in found
    host.change(2024, busy=[])
    host.minutes(29)
    assert host.running() == ["jellyfin"]
    host.minutes(1)
    assert host.running() == []


def test_a_probe_that_fails_counts_as_use(host):
    host.json("start", "media")
    host.change(2024, busy_rc=127)
    host.minutes(40)
    found = host.status()["by_name"]["media"]
    assert found["state"] == "on"
    assert found["busy"] == ["cannot tell whether jellyfin is in use (nb-busy is not installed yet; run setup)"]


def test_one_busy_guest_keeps_the_whole_workload(host):
    host.json("start", "inference")
    host.change(2021, busy=["answered a request 20 s ago"])
    host.minutes(30)
    assert host.running() == ["inference", "model-gateway"]


def test_keep_on_holds_it_until_it_is_stopped(host):
    host.json("start", "media", "--keep")
    host.minutes(90)
    assert host.status()["by_name"]["media"]["keep"] is True
    assert host.running() == ["jellyfin"]
    host.json("keep", "media", "off")
    host.minutes(1)
    assert host.running() == []
    assert "is off; start it first" in host.json("keep", "media", "on", rc=1)["error"]


def test_ask_never_stops_and_marks_the_owner_is_to_be_asked(host):
    host.json("start", "windows-dev")
    host.minutes(239)
    assert host.status()["by_name"]["windows-dev"]["asks"] is False
    host.minutes(2)
    found = host.status()["by_name"]["windows-dev"]
    assert found["asks"] is True and found["state"] == "on" and "stops_in_s" not in found
    metrics = host.metrics()
    assert metrics['nb_power_group_asks{group="windows-dev",title="Windows desktop"}'] == "1"
    assert metrics['nb_power_group_idle_seconds{group="windows-dev",title="Windows desktop"}'] == str(241 * 60)
    # Keep on answers the question.
    host.json("keep", "windows-dev", "on")
    host.minutes(1)
    assert host.status()["by_name"]["windows-dev"]["asks"] is False


def test_manual_neither_stops_nor_asks(tmp_path):
    policy = json.loads(json.dumps(POLICY))
    policy["groups"]["media"]["stop"] = "manual"
    host = Host(tmp_path, policy)
    host.json("start", "media")
    host.minutes(600)
    found = host.status()["by_name"]["media"]
    assert found["state"] == "on" and found["asks"] is False


def test_cpu_counts_as_use_where_the_rules_say_so(tmp_path):
    policy = json.loads(json.dumps(POLICY))
    policy["groups"]["windows-dev"] |= {"stop": "auto", "idle_minutes": 10, "cpu_pct": 20}
    host = Host(tmp_path, policy)
    host.json("start", "windows-dev")
    usec = 0
    for _ in range(4):
        usec += 5 * 30_000_000  # half a core for five minutes
        host.cpu("qemu.slice/2022.scope", usec)
        host.minutes(5)
    found = host.status()["by_name"]["windows-dev"]
    assert found["state"] == "on" and found["busy"] == ["windows-dev is working (CPU 50%)"]
    usec += 9 * 3_000_000  # 5% of a core
    host.cpu("qemu.slice/2022.scope", usec)
    host.minutes(9)
    assert host.running() == ["windows-dev"]
    host.cpu("qemu.slice/2022.scope", usec + 3_000_000)
    host.minutes(1)
    assert host.running() == []


def test_a_running_ci_job_keeps_ci_on_and_counted(host):
    host.json("start", "linux-ci")
    guests = host.guests()
    guests["2300"] = guest("linux-runner-2300", "vm", "running")
    host.set(guests)
    host.minutes(40)
    assert host.status()["by_name"]["linux-ci"]["busy"] == ["1 CI job running"]
    # The dispatcher stopped by hand, the job still running: still counted.
    host.change(2018, state="stopped")
    found = host.status()
    assert found["by_name"]["linux-ci"]["state"] == "partly" and found["used_mib"] == 2176
    guests = host.guests()
    del guests["2300"]
    host.set(guests)
    assert host.status()["used_mib"] == 0


def test_start_stops_idle_automatic_workloads_to_make_room(host):
    host.json("start", "media")
    host.json("start", "inference")
    host.minutes(5)
    # 5000 - 1024 - 1536 = 2440 free; Windows needs 4096.
    answer = host.json("start", "windows-dev")
    assert answer["stopped"] == ["Jellyfin", "Local AI models"]
    assert host.running() == ["windows-dev"]
    why = [e["why"] for e in host.status()["events"] if e["what"] == "stopped"]
    assert why == ["to make room for Windows desktop"] * 2


def test_start_stops_only_as_many_as_it_needs_longest_idle_first(host):
    host.json("start", "media")
    host.minutes(10)
    host.json("start", "linux-ci")
    host.minutes(2)
    # 5000 - 1024 - 2176 = 1800 free; local AI needs 1536: fits already.
    assert host.json("start", "inference")["stopped"] == []
    host.json("stop", "inference")
    # Windows needs 4096 of 1800: Jellyfin (idle longest) is not enough, so both go.
    assert host.json("start", "windows-dev")["stopped"] == ["Jellyfin", "Linux CI runners"]


def test_start_asks_before_stopping_what_is_in_use_or_only_the_owner_stops(host):
    host.json("start", "media")
    host.json("start", "inference")
    host.change(2024, busy=["playing on 1 device"])
    answer = host.json("start", "windows-dev", rc=3)
    assert answer["error"] == "no-room" and answer["enough"] is True
    assert (answer["need_mib"], answer["free_mib"]) == (4096, 2440)
    assert [(o["name"], o["auto"], o["busy"]) for o in answer["stop"]] == [
        ("inference", True, []),
        ("media", False, ["playing on 1 device"]),
    ]
    # Nothing was touched.
    assert host.running() == ["inference", "jellyfin", "model-gateway"]
    # The owner agrees: exactly those stop.
    assert host.json("start", "windows-dev", "--stop", "inference,media")["stopped"] == ["Jellyfin", "Local AI models"]
    assert host.running() == ["windows-dev"]
    # And the other way round: Windows is the owner's to stop, however idle.
    host.minutes(300)
    answer = host.json("start", "media", rc=3)
    assert [(o["name"], o["auto"], o["idle_s"]) for o in answer["stop"]] == [("windows-dev", False, 300 * 60)]
    assert host.running() == ["windows-dev"]


def test_agreed_stops_that_do_not_make_room_change_nothing(host):
    host.json("start", "media")
    host.json("start", "inference")
    host.change(2024, busy=["playing on 1 device"])
    host.change(2021, busy=["answered a request 5 s ago"])
    answer = host.json("start", "windows-dev", "--stop", "media", rc=3)
    assert answer["error"] == "no-room"
    assert host.running() == ["inference", "jellyfin", "model-gateway"]
    assert "frees nothing" in host.json("start", "windows-dev", "--stop", "linux-ci", rc=1)["error"]


def test_a_certificate_that_ran_out_while_off_is_issued_anew(host):
    host.change(2021, cert="expired")
    answer = host.json("start", "inference")
    assert answer["warnings"] == []
    gateway = host.guests()["2021"]
    assert gateway["cert"] == "valid"
    name = "model-gateway.nullbill.internal"
    assert gateway["issued"] == {
        "token": "one-time-token",
        "args": [name, f"/etc/nullbill/tls/{name}.crt", f"/etc/nullbill/tls/{name}.key", "https://step-ca.nullbill.internal"],
    }
    token = next(c for c in host.read()["calls"] if "token" in c)
    assert token[:3] == ["pct", "exec", "2012"] and name in token
    # The token never rides on a command line.
    assert not any("one-time-token" in " ".join(c) for c in host.read()["calls"])
    # A valid one is left alone.
    host.json("stop", "inference")
    before = len([c for c in host.read()["calls"] if "token" in c])
    host.json("start", "inference")
    assert len([c for c in host.read()["calls"] if "token" in c]) == before


def test_metrics_say_what_is_off_so_alerts_keep_quiet(host):
    host.json("start", "media")
    host.minutes(1)
    metrics = host.metrics()
    assert metrics['nb_power_guest_off{guest="jellyfin",group="media"}'] == "0"
    assert metrics['nb_power_guest_off{guest="inference",group="inference"}'] == "1"
    assert metrics['nb_power_guest_started_timestamp_seconds{guest="jellyfin",group="media"}'] == str(host.now - 60)
    assert metrics['nb_power_group_on{group="media",title="Jellyfin"}'] == "1"
    assert metrics["nb_power_tick_timestamp_seconds"] == str(host.now)
    assert (metrics["nb_power_headroom_mib"], metrics["nb_power_used_mib"]) == ("5000", "1024")
    assert 'nb_power_group_idle_seconds{group="inference",title="Local AI models"}' not in metrics


def test_no_rules_no_metrics(tmp_path):
    host = Host(tmp_path, {"headroom_mib": 0, "groups": {}})
    host.minutes(1)
    assert host.metrics() is None
    assert host.run("status").stdout == "nothing runs on demand\n"


def test_a_guest_started_in_proxmox_is_seen_and_managed(host):
    host.change(2024, state="running")
    assert host.status()["used_mib"] == 1024
    # Its idle time counts from when nb-power first saw it.
    host.minutes(1)
    host.minutes(29)
    assert host.running() == ["jellyfin"]
    host.minutes(1)
    assert host.running() == []


def test_setup_starts_what_it_configures_and_puts_it_back(host):
    host.json("start", "media")
    host.run("setup", "begin")
    assert host.run("ensure", "postgres").stdout == ""
    assert host.run("ensure", "jellyfin").stdout == ""
    assert host.run("ensure", "inference", "postgres").stdout == "started inference\n"
    assert host.run("ensure", "inference", "model-gateway").stdout == "started model-gateway\n"
    assert host.status()["setup"] is True
    assert host.status()["by_name"]["inference"]["setup"] is True
    # Nothing stops for being idle while setup runs.
    host.minutes(120)
    assert host.running() == ["inference", "jellyfin", "model-gateway"]
    out = host.run("setup", "end").stdout
    assert out == "Local AI models is off again, as it was before setup\n"
    # Jellyfin was on before, so it stays, under its own rule again.
    assert host.running() == ["jellyfin"]
    assert host.status()["setup"] is False
    host.minutes(30)
    assert host.running() == []


def test_setup_leaves_on_what_the_owner_started_or_someone_uses(host):
    host.run("setup", "begin")
    host.run("ensure", "jellyfin")
    host.run("ensure", "inference")
    host.run("ensure", "model-gateway")
    host.run("ensure", "runner-dispatcher")
    host.json("start", "media")
    host.change(2021, busy=["answered a request 5 s ago"])
    out = host.run("setup", "end").stdout.splitlines()
    assert out == ["Local AI models stays on: answered a request 5 s ago", "Linux CI runners is off again, as it was before setup"]
    assert host.running() == ["inference", "jellyfin", "model-gateway"]


def test_setup_may_pass_the_counted_memory_while_the_host_really_has_it(host):
    host.json("start", "windows-dev")
    host.run("setup", "begin")
    # 904 MiB counted free; Jellyfin counts 1024 and its limit is 2048.
    assert host.run("ensure", "jellyfin").stdout == "started jellyfin\n"
    # The host itself short: 2400 MiB available, 2048 limit, 512 to spare.
    # Someone is watching, so Jellyfin is not what makes room.
    host.change(2024, busy=["playing on 1 device"])
    (host.dir / "meminfo").write_text("MemAvailable: 2457600 kB\n")
    done = host.run("ensure", "inference", check=False)
    assert done.returncode == 1
    assert "setup has to start inference to configure it, and the host has 2400 MiB really free for its 2048 MiB" in done.stderr
    assert "beside Jellyfin, Windows desktop; stop one of them on the page" in done.stderr
    assert host.running() == ["jellyfin", "windows-dev"]


def test_setup_makes_room_with_what_it_started_before_and_is_done_with(host):
    host.json("start", "windows-dev")
    (host.dir / "meminfo").write_text("MemAvailable: 2457600 kB\n")
    # Both of a workload's guests at once: 3072 MiB of limits do not fit.
    done = host.run("ensure", "inference", "model-gateway", check=False)
    assert (
        done.returncode == 1
        and "start inference and model-gateway to configure them" in done.stderr
        and "for their 3072 MiB" in done.stderr
    )
    (host.dir / "meminfo").write_text("MemAvailable: 4194304 kB\n")
    assert host.run("ensure", "jellyfin").stdout == "started jellyfin\n"
    # Past the counted memory, the workload setup started before goes first.
    out = host.run("ensure", "runner-dispatcher").stdout
    assert out == "stopped Jellyfin\nstarted runner-dispatcher\n"
    assert host.running() == ["runner-dispatcher", "windows-dev"]
    # One the owner started since is the owner's, and stays.
    host.json("start", "linux-ci")
    assert host.run("ensure", "jellyfin").stdout == "started jellyfin\n"
    assert host.running() == ["jellyfin", "runner-dispatcher", "windows-dev"]
    assert host.run("setup", "end").stdout == "Jellyfin is off again, as it was before setup\n"


def test_memory_a_vm_has_not_touched_yet_is_not_free(host):
    host.json("start", "windows-dev")
    host.run("setup", "begin")
    (host.dir / "meminfo").write_text("MemAvailable: 6291456 kB\n")  # 6144 MiB
    scope = host.dir / "cgroup/qemu.slice/2022.scope"
    scope.mkdir(parents=True)
    (scope / "memory.current").write_text(str(512 * MIB))  # of its 4096
    # 6144 - 3584 not yet touched = 2560: Jellyfin's 2048 leaves the 512 to spare.
    host.run("ensure", "jellyfin")
    (host.dir / "meminfo").write_text("MemAvailable: 6144000 kB\n")  # 6000 MiB
    done = host.run("ensure", "inference", check=False)
    assert done.returncode == 1 and "2416 MiB really free" in done.stderr


def test_a_setup_run_that_died_is_given_up_on(host):
    host.run("setup", "begin")
    host.run("ensure", "jellyfin")
    host.minutes(6 * 60 - 1)
    assert host.running() == ["jellyfin"]
    host.minutes(2)
    assert host.running() == []
    assert host.status()["setup"] is False


def test_status_for_people(host):
    host.json("start", "media")
    host.change(2024, busy=["playing on 1 device"])
    host.minutes(1)
    host.json("start", "inference")
    host.minutes(3)
    out = host.run("status").stdout.splitlines()
    assert out[0] == "on demand: 2560 of 5000 MiB in use, 2440 MiB free"
    assert out[1] == "media          on         1024 MiB  Jellyfin: playing on 1 device"
    assert out[2] == "inference      on         1536 MiB  Local AI models: idle 3 min, stops in 17 min"
    assert out[3] == "windows-dev    off        4096 MiB  Windows desktop"


def test_a_started_guest_catches_up_on_updates_but_not_under_a_setup_run(host):
    kick = ["systemctl", "start", "--no-block", "apt-daily.service", "apt-daily-upgrade.service"]
    host.json("start", "media")
    assert ["pct", "exec", "2024", "--", *kick] in host.read()["calls"]
    # Setup's own apt tasks must not wait behind apt's timers.
    host.run("ensure", "inference")
    assert ["pct", "exec", "2020", "--", *kick] not in host.read()["calls"]
