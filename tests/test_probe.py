import json

import pytest
from conftest import FIXTURES, write_raw

from nullbill.errors import NullBillError
from nullbill.probe import COMMANDS, parse, run_probe


def test_small_facts(facts):
    f = facts("small")
    assert f["pve_version"] == "8.2.4"
    assert f["cpu"] == {"model": "Synthetic 4-thread CPU", "threads": 4, "sockets": 1}
    assert f["memory_mib"] == 16229000 // 1024
    (disk,) = f["disks"]
    assert disk["id"] == "ata-SYNTHETIC_SSD_0001"
    assert disk["system"] is True
    assert [g["integrated"] for g in f["gpus"]] == [True]
    assert [s["name"] for s in f["storage"]] == ["local", "local-lvm"]


def test_mid_gpu_and_blank_disk(facts):
    f = facts("mid")
    (gpu,) = f["gpus"]
    assert gpu == {
        "pci": "0000:01:00.0",
        "vendor": "nvidia",
        "id": "10de:0001",
        "name": "NVIDIA Corporation Synthetic GPU 24G",
        "integrated": False,
        "vram_mib": 24564,
        "iommu_group": 14,
        "iommu_peers": ["0000:01:00.1"],
    }
    hdd = next(d for d in f["disks"] if d["id"] == "ata-SYNTHETIC_HDD_0002")
    assert hdd["system"] is False and hdd["signatures"] == [] and hdd["rotational"] is True
    assert hdd["vgs"] == []
    # pvs names the volume group of each physical volume, on a partition too.
    system = next(d for d in f["disks"] if d["id"] == "nvme-SYNTHETIC_NVME_0001")
    assert system["vgs"] == ["pve"]


def test_large_signatures(facts):
    f = facts("large")
    old = next(d for d in f["disks"] if d["id"] == "ata-SYNTHETIC_HDD_0002")
    assert old["signatures"] == ["gpt", "ntfs"]
    assert len([g for g in f["gpus"] if not g["integrated"]]) == 2


def test_optional_command_failure_is_tolerated(tmp_path):
    raw = write_raw(tmp_path / "raw", "small")
    assert parse(raw)["gpus"][0]["vram_mib"] is None


def test_required_command_failure_is_reported(tmp_path):
    raw = write_raw(tmp_path / "raw", "small")
    (raw / "lsblk.rc").write_text("1\n")
    with pytest.raises(NullBillError, match="lsblk"):
        parse(raw)


def test_the_home_network_is_what_the_default_route_leaves_by(tmp_path):
    raw = write_raw(tmp_path / "raw", "small")
    assert parse(raw)["lan"] == ["192.0.2.0/24"]
    # NullBill's own bridges and a second uplink without the default route are not the LAN.
    routes = [
        {"dst": "default", "gateway": "192.0.2.1", "dev": "vmbr0", "protocol": "static"},
        {"dst": "192.0.2.0/24", "dev": "vmbr0", "protocol": "kernel", "scope": "link", "prefsrc": "192.0.2.10"},
        {"dst": "198.51.100.0/24", "dev": "eno2", "protocol": "kernel", "scope": "link"},
        {"dst": "10.42.0.0/24", "dev": "nb-uplink", "protocol": "kernel", "scope": "link"},
        {"dst": "203.0.113.0/24", "gateway": "192.0.2.1", "dev": "vmbr0", "protocol": "static"},
    ]
    (raw / "routes.out").write_text(json.dumps(routes))
    assert parse(raw)["lan"] == ["192.0.2.0/24"]


@pytest.mark.parametrize("state", ["failed", "garbage", "absent"])
def test_routes_that_cannot_be_read_leave_the_lan_unknown(tmp_path, state):
    raw = write_raw(tmp_path / "raw", "small")
    if state == "failed":
        (raw / "routes.rc").write_text("1\n")
    elif state == "garbage":
        (raw / "routes.out").write_text("not json")
    else:
        # A probe kept from before the command was added.
        for suffix in ("out", "err", "rc"):
            (raw / f"routes.{suffix}").unlink()
    assert parse(raw)["lan"] is None


def test_the_routes_command_only_reads():
    assert COMMANDS["routes"] == ("ip -4 -j route show", False)


def fake_runner(fixture):
    import yaml

    spec = yaml.safe_load((FIXTURES / f"{fixture}.yaml").read_text())
    by_command = {cmd: spec.get(name, {}) for name, (cmd, _) in COMMANDS.items()}

    def run(target, command):
        assert target == "root@pve.internal"
        entry = by_command[command]
        return entry.get("rc", 0), entry.get("out", ""), entry.get("err", "")

    return run


def test_run_probe_writes_facts_and_latest(tmp_path):
    run_dir = run_probe("root@pve.internal", tmp_path, fake_runner("mid"))
    latest = tmp_path / "probe" / "latest"
    assert latest.resolve() == run_dir.resolve()
    assert json.loads((latest / "facts.json").read_text())["cpu"]["threads"] == 16


def test_run_probe_fails_on_required_command(tmp_path):
    def run(target, command):
        return (255, "", "ssh: connect refused") if command.startswith("lsblk") else fake_runner("mid")(target, command)

    with pytest.raises(NullBillError, match="lsblk"):
        run_probe("root@pve.internal", tmp_path, run)


def test_a_command_past_the_ssh_timeout_is_a_failed_command(monkeypatch):
    # verify's gateway check outlived it and the traceback ended the run
    # with "It ended without saying why".
    import subprocess

    from nullbill import probe

    def hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial")

    monkeypatch.setattr(probe.subprocess, "run", hang)
    assert probe.ssh_run("root@192.0.2.1", "true") == (124, "partial", f"no answer within {probe.SSH_TIMEOUT_S} s")
