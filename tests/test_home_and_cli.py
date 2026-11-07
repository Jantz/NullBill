import json
from pathlib import Path

import pytest
import yaml
from conftest import fixture_config, write_raw

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
    assert out[0] == "DEGRADED backups: no separate disk for PBS"
