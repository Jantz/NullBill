import json
from pathlib import Path

import pytest
import yaml
from conftest import fixture_config, write_raw

from nullbill.cli import main
from nullbill.errors import NullBillError
from nullbill.home import REPO_ROOT, nullbill_home
from nullbill.probe import parse


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
