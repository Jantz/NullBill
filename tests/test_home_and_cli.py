import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from conftest import fixture_config, write_raw

from nullbill import apply, cli
from nullbill.cli import main
from nullbill.errors import NullBillError
from nullbill.home import REPO_ROOT, nullbill_home
from nullbill.probe import COMMANDS, parse, run_probe


def test_home_must_be_set(monkeypatch):
    monkeypatch.delenv("NULLBILL_HOME", raising=False)
    with pytest.raises(NullBillError, match="not set"):
        nullbill_home()


def test_home_inside_repo_is_refused(monkeypatch):
    monkeypatch.setenv("NULLBILL_HOME", str(REPO_ROOT / "tests"))
    with pytest.raises(NullBillError, match="inside the repository"):
        nullbill_home()


@pytest.mark.skipif(Path("/etc/pve").exists(), reason="nullbill refuses to run on a PVE host")
def test_plan_command_writes_plan(tmp_path, monkeypatch, capsys):
    run = tmp_path / "probe" / "run1"
    facts = parse(write_raw(run / "raw", "mid"))
    (run / "facts.json").write_text(json.dumps(facts))
    (tmp_path / "probe" / "latest").symlink_to("run1")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(fixture_config("mid")))
    monkeypatch.setenv("NULLBILL_HOME", str(tmp_path))
    assert main(["plan"]) == 0
    assert (tmp_path / "plan.yaml").read_text() == capsys.readouterr().out
    assert "class: mid" in (tmp_path / "plan.yaml").read_text()


@pytest.mark.skipif(Path("/etc/pve").exists(), reason="nullbill refuses to run on a PVE host")
def test_the_first_probe_needs_only_what_install_sh_wrote(tmp_path, monkeypatch, capsys):
    """The page's order: probe, then choices (which write config.yaml)."""
    import nullbill.cli as cli
    from nullbill import lan

    (tmp_path / "install.json").write_text(json.dumps({"host": "192.0.2.10", "node": "pve", "bridge": "vmbr0", "vmid": 2002}))
    monkeypatch.setenv("NULLBILL_HOME", str(tmp_path))
    spec = yaml.safe_load((Path(__file__).parent / "fixtures" / "mid.yaml").read_text())
    by_command = {command: spec.get(name, {"rc": 1}) for name, (command, _) in COMMANDS.items()}
    targets = []

    def run(target, command):
        targets.append(target)
        entry = by_command[command]
        return entry.get("rc", 0), entry.get("out", ""), entry.get("err", "")

    monkeypatch.setattr(cli, "run_probe", lambda target, home: run_probe(target, home, run))
    assert main(["probe"]) == 0
    assert set(targets) == {"root@192.0.2.10"}
    assert (tmp_path / "probe" / "latest" / "facts.json").is_file()
    # Once the LAN is closed, the probe goes to the mgmt address, like apply.
    run1 = tmp_path / "probe" / "run1"
    (run1 / "facts.json").parent.mkdir(parents=True, exist_ok=True)
    (run1 / "facts.json").write_text(json.dumps(parse(write_raw(run1 / "raw", "mid"))))
    (tmp_path / "probe" / "latest").unlink(missing_ok=True)
    (tmp_path / "probe" / "latest").symlink_to("run1")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(fixture_config("mid") | {"target": {"ssh": "root@192.0.2.10"}}))
    lan.write(tmp_path, lan.CLOSED)
    assert cli.probe_target(tmp_path) == "root@10.42.1.254"


def test_plan_command_reports_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NULLBILL_HOME", str(tmp_path))
    assert main(["plan"]) == 1
    assert "run `nullbill probe` first" in capsys.readouterr().err


@pytest.mark.skipif(Path("/etc/pve").exists(), reason="nullbill refuses to run on a PVE host")
def test_apply_dry_run_renders_build(tmp_path, monkeypatch, capsys):
    run = tmp_path / "probe" / "run1"
    (run / "facts.json").parent.mkdir(parents=True)
    (run / "facts.json").write_text(json.dumps(parse(write_raw(run / "raw", "small"))))
    (tmp_path / "probe" / "latest").symlink_to("run1")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(fixture_config("small")))
    monkeypatch.setenv("NULLBILL_HOME", str(tmp_path))
    assert main(["apply", "--dry-run"]) == 0
    assert (tmp_path / "build" / "firewall" / "cluster.fw").is_file()
    assert "firewall/cluster.fw" in capsys.readouterr().out


@pytest.mark.skipif(Path("/etc/pve").exists(), reason="nullbill refuses to run on a PVE host")
def test_apply_needs_a_target(tmp_path, monkeypatch, capsys):
    run = tmp_path / "probe" / "run1"
    (run / "facts.json").parent.mkdir(parents=True)
    (run / "facts.json").write_text(json.dumps(parse(write_raw(run / "raw", "small"))))
    (tmp_path / "probe" / "latest").symlink_to("run1")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(fixture_config("small")))
    monkeypatch.setenv("NULLBILL_HOME", str(tmp_path))
    assert main(["apply"]) == 1
    assert "target.ssh is required for apply" in capsys.readouterr().err


@pytest.mark.skipif(Path("/etc/pve").exists(), reason="nullbill refuses to run on a PVE host")
def test_verify_without_target_skips_and_exits_zero(tmp_path, monkeypatch, capsys):
    run = tmp_path / "probe" / "run1"
    (run / "facts.json").parent.mkdir(parents=True)
    (run / "facts.json").write_text(json.dumps(parse(write_raw(run / "raw", "small"))))
    (tmp_path / "probe" / "latest").symlink_to("run1")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(fixture_config("small")))
    monkeypatch.setenv("NULLBILL_HOME", str(tmp_path))
    assert main(["verify"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("0 PASS, 0 FAIL, ") and out[-1].endswith(" SKIP, 1 DEGRADED")
    assert out[0].startswith("DEGRADED backups: the backups are on the disk the server runs from")


STOPPED_RUN = """
import pathlib, sys
from nullbill import apply, cli
with cli.exit_on_sigterm():
    apply.SshReach("root@192.0.2.10", pathlib.Path(sys.argv[1])).playbook("router.yml", {}, {"files_share_password": "s3cret"})
"""


def test_a_run_stopped_from_the_page_removes_its_playbooks_secrets(tmp_path):
    # Stop this run sends SIGTERM to every process of the job. Python's own
    # SIGTERM skipped the finally that removes the playbook's vars file.
    bin_dir, tmp = tmp_path / "bin", tmp_path / "tmp"
    bin_dir.mkdir()
    tmp.mkdir()
    fake = bin_dir / "ansible-playbook"
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(0o755)
    script = tmp_path / "run.py"
    script.write_text(STOPPED_RUN)
    src = str(Path(__file__).parent.parent / "src")
    env = os.environ | {"TMPDIR": str(tmp), "PATH": f"{bin_dir}:{os.environ['PATH']}", "PYTHONPATH": src}
    run = subprocess.Popen([sys.executable, str(script), str(tmp_path)], env=env)
    deadline = time.monotonic() + 30
    while not list(tmp.glob("nullbill-apply-*/vars.json")) and time.monotonic() < deadline:
        time.sleep(0.05)
    [work] = list(tmp.glob("nullbill-apply-*"))
    assert work.name.startswith(f"nullbill-apply-{run.pid}-") and "s3cret" in (work / "vars.json").read_text()
    time.sleep(0.2)
    run.send_signal(signal.SIGTERM)
    assert run.wait(timeout=30) == 128 + signal.SIGTERM
    assert not work.exists()


def test_apply_removes_what_ended_runs_left_and_nothing_in_use(tmp_path, monkeypatch):
    ended = subprocess.Popen(["true"])
    ended.wait()
    names = {
        "ended": f"nullbill-apply-{ended.pid}-abc",
        "mine": f"nullbill-apply-{os.getpid()}-abc",
        "running": f"nullbill-apply-{os.getppid()}-abc",
        "older": "nullbill-apply-k2j3h4",
        "newer": "nullbill-apply-x9y8z7",
    }
    for name in names.values():
        (tmp_path / name).mkdir()
        (tmp_path / name / "vars.json").write_text("{}")
    past = time.time() - 3600
    os.utime(tmp_path / names["older"], (past, past))
    monkeypatch.setattr(apply, "_STARTED", time.time() - 60)
    (tmp_path / "nullbill-apply-file").write_text("not a folder")
    gone = apply.clear_work(tmp_path)
    assert sorted(p.name for p in gone) == sorted([names["ended"], names["older"]])
    kept = [names["mine"], names["running"], names["newer"], "nullbill-apply-file"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(kept)


def test_the_commands_a_job_runs_clean_up_on_sigterm_and_the_page_does_not(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "refuse_on_pve_host", lambda: None)
    monkeypatch.setattr(cli, "cmd_verify", lambda _args: seen.setdefault("verify", signal.getsignal(signal.SIGTERM)) and 0)
    monkeypatch.setattr(cli, "cmd_web", lambda _args: seen.setdefault("web", signal.getsignal(signal.SIGTERM)) and 0)
    before = signal.getsignal(signal.SIGTERM)
    assert main(["verify"]) == 0 and main(["web"]) == 0
    assert seen["verify"] is cli._terminated and seen["web"] is before
    assert signal.getsignal(signal.SIGTERM) is before
