import errno
import shlex
import socket
import subprocess
import threading

import pytest
from conftest import backup_host, fixture_config

from nullbill import verify
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.verify import _connect_via, _lan, _tcp, probes, run_verify, tcp_connect

PUBLIC = "192.0.2.80"  # RFC 5737 stand-in for the resolved public name


def setup(facts, cls="mid"):
    model = build_model(fixture_config(cls))
    return build_plan(model, facts(cls)), model


def firewall(plan, model, leak: bool = False):
    """A runner that answers each probe the way a correct (or fully open) host would."""
    answers = {}
    for p in probes(plan, model, PUBLIC)[0]:
        if p.kind == "proxy":
            out = ("HTTP/1.1 200 Connection established\n" if p.expect == "2" else "HTTP/1.1 403 Forbidden\n") + "rc=0\n"
        elif p.expect == "open" or leak:
            out = "rc=0\n"
        else:
            out = "rc=124\n"
        answers[f"pct exec {p.vmid} -- bash -c {shlex.quote(p.command)}"] = out

    backup = backup_host(plan)

    def run(target, command):
        if command == "pve-firewall status":
            return 0, "Status: enabled/running\n", ""
        if command in answers:
            return 0, answers[command], ""
        return backup(target, command)

    return run


def statuses(results):
    return {s: [r for r in results if r.status == s] for s in ("PASS", "FAIL", "SKIP", "DEGRADED")}


def test_correct_host_passes_everything_it_can(facts):
    plan, model = setup(facts)
    results = run_verify(plan, model, "root@pve.internal", firewall(plan, model), lambda h, p: "timeout", lambda n: PUBLIC)
    by = statuses(results)
    assert by["FAIL"] == []
    assert [r.name for r in by["SKIP"]] == ["zone windows"]
    names = [r.name for r in by["PASS"]]
    assert "workspace (build) is refused deb.debian.org directly by IP" in names
    assert "workspace (build) is refused a bare IP by the proxy" in names
    assert "vpn (access) reaches forgejo tcp/443" in names
    assert "egress-proxy (services) is refused postgres tcp/5432" in names
    assert "LAN is refused the PVE host tcp/8006" in names
    assert "forgejo has an encrypted backup under 26 h old" in names
    assert "restore test" in names


def test_open_host_fails_every_denial(facts):
    plan, model = setup(facts)
    results = run_verify(plan, model, "root@pve.internal", firewall(plan, model, leak=True), lambda h, p: "open", lambda n: PUBLIC)
    failed = {r.name for r in statuses(results)["FAIL"]}
    assert "step-ca (mgmt) is refused the PVE host tcp/8006" in failed
    assert "workspace (build) is refused deb.debian.org directly by IP" in failed
    assert "LAN is refused the PVE host tcp/22" in failed
    assert not any(" reaches " in name for name in failed)


def test_lan_open_before_the_owner_closes_it_is_degraded_and_checked_at_the_lan_address(facts):
    plan, model = setup(facts)
    asked = []

    def connect(host, port):
        asked.append(host)
        return "open"

    target = "root@10.42.1.254"
    results = run_verify(plan, model, target, firewall(plan, model), connect, lambda n: PUBLIC, lan_state="open", lan_host="192.0.2.10")
    by = statuses(results)
    assert not [r for r in by["FAIL"] if r.name.startswith("LAN")] and asked == []
    assert "LAN is refused the PVE host" in [r.name for r in by["DEGRADED"]]
    # Closed or closing: probed at the LAN address, not the mgmt one apply uses.
    results = run_verify(plan, model, target, firewall(plan, model), connect, lambda n: PUBLIC, lan_state="closing", lan_host="192.0.2.10")
    assert asked == ["192.0.2.10", "192.0.2.10"]
    assert "LAN is refused the PVE host tcp/22" in {r.name for r in statuses(results)["FAIL"]}


def test_refused_is_not_dropped(facts):
    plan, model = setup(facts)
    run = firewall(plan, model)

    def refusing(target, command):
        rc, out, err = run(target, command)
        return rc, out.replace("rc=124", "rc=1"), err

    results = run_verify(plan, model, "root@pve.internal", refusing, lambda h, p: "refused", lambda n: PUBLIC)
    fails = statuses(results)["FAIL"]
    assert fails and all("refused" in r.detail for r in fails)


def test_without_target_nothing_passes(facts):
    plan, model = setup(facts)
    results = run_verify(plan, model, None, None, None, None)
    assert {r.status for r in results} == {"SKIP"}


def test_firewall_down_and_unreachable_guest_fail(facts):
    plan, model = setup(facts, "small")

    def broken(target, command):
        if command == "pve-firewall status":
            return 0, "Status: disabled/stopped\n", ""
        return 255, "", "ssh: connect to host pve.internal port 22: Connection timed out"

    results = run_verify(plan, model, "root@pve.internal", broken, lambda h, p: "timeout", lambda n: PUBLIC)
    by = statuses(results)
    assert by["FAIL"][0].name == "pve-firewall is running"
    probe_names = {p.name for p in probes(plan, model, PUBLIC)[0]}
    probe_fails = [r for r in by["FAIL"][1:] if r.name in probe_names]
    assert probe_fails and all(r.detail.startswith("probe did not run") for r in probe_fails)
    # Token and service checks run in guests too, and say why they failed.
    others = [r for r in by["FAIL"][1:] if r.name not in probe_names and not r.name.startswith(("forgejo has", "restore"))]
    assert {r.name.split()[0] for r in others} >= {"agent", "dispatcher", "forgejo", "runner"}
    assert all("ssh: connect" in r.detail for r in others), [r for r in others if "ssh: connect" not in r.detail]
    assert [r.name for r in by["DEGRADED"]] == ["backups"]


@pytest.mark.parametrize("cls", ["small", "mid", "large"])
def test_every_planned_port_is_probed(facts, cls):
    plan, model = setup(facts, cls)
    names = " ".join(p.name for p in probes(plan, model, PUBLIC)[0])
    assert "forgejo tcp/443" in names and "the PVE host tcp/8006" in names


def _run_locally(command: str) -> str:
    # The same string pct exec hands to bash inside the container.
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=10).stdout


def test_probe_commands_work_in_bash():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    def answer():
        conn, _ = server.accept()
        conn.recv(1024)
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    out = _run_locally(_connect_via("127.0.0.1", port, "192.0.2.80"))
    assert out.splitlines()[0].startswith("HTTP/1.1 403") and out.splitlines()[-1] == "rc=0"
    server.close()
    assert _run_locally(_tcp("127.0.0.1", port)).strip() == "rc=1"  # closed port: refused


def test_host_only_port_is_refused_from_mgmt_guests(facts):
    plan, model = setup(facts)
    names = [p.name for p in probes(plan, model, PUBLIC)[0]]
    assert any(n.endswith("(mgmt) is refused pbs tcp/8007") for n in names)
    assert any(n.endswith("(access) reaches pbs tcp/8007") for n in names)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (OSError(errno.ENETUNREACH, "Network is unreachable"), "SKIP"),
        (socket.gaierror(socket.EAI_NONAME, "Name or service not known"), "SKIP"),
        (TimeoutError("timed out"), "PASS"),
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "FAIL"),
    ],
)
def test_the_lan_check_passes_only_on_a_drop(monkeypatch, error, status):
    """A drop times out; no route or no address never reached the firewall."""

    def connect(*args, **kwargs):
        raise error

    monkeypatch.setattr(socket, "create_connection", connect)
    assert {r.status for r in _lan("192.0.2.10", tcp_connect)} == {status}


def test_a_service_nullbill_does_not_serve_yet_is_degraded_when_nothing_answers(facts):
    plan, model = setup(facts, "large")
    run = firewall(plan, model)
    allowed = [p for p in probes(plan, model, PUBLIC)[0] if p.expect == "open"]
    quiet = (" windows-dev ", " forgejo ", " model-gateway ")
    silent = {f"pct exec {p.vmid} -- bash -c {shlex.quote(p.command)}" for p in allowed if any(q in p.name for q in quiet)}
    assert any(" windows-dev " in p.name for p in allowed)

    def nothing_listens(target, command):
        rc, out, err = run(target, command)
        return (rc, "rc=124\n", err) if command in silent else (rc, out, err)

    by = statuses(run_verify(plan, model, "root@pve.internal", nothing_listens, lambda h, p: "timeout", lambda n: PUBLIC))
    degraded = [r for r in by["DEGRADED"] if " windows-dev " in r.name]
    assert degraded and all("Remote Desktop" in r.detail for r in degraded)
    # Services NullBill installs still fail when nothing answers.
    assert any(" forgejo " in r.name for r in by["FAIL"])
    assert any(" model-gateway " in r.name for r in by["FAIL"])
    assert not any(" windows-dev " in r.name for r in by["FAIL"])


def test_results_are_told_in_order_as_they_come_with_probes_run_side_by_side(facts):
    plan, model = setup(facts)
    run = firewall(plan, model)
    running, most = [0], [0]
    lock = threading.Lock()

    def slow(target, command):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        try:
            if command.startswith("pct exec"):
                threading.Event().wait(0.01)
            return run(target, command)
        finally:
            with lock:
                running[0] -= 1

    told = []
    results = run_verify(plan, model, "root@pve.internal", slow, lambda h, p: "timeout", lambda n: PUBLIC, say=told.append)
    assert told == results
    assert 1 < most[0] <= verify.PARALLEL
