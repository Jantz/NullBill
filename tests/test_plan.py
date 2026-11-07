import copy
import json
import os
from pathlib import Path

import pytest
import yaml
from conftest import CLASSES, fixture_config

from nullbill.cli import plan_text
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

GOLDEN = Path(__file__).parent / "golden"


def plan_for(facts: dict, config: dict) -> dict:
    return build_plan(build_model(config), facts)


@pytest.mark.parametrize("cls", CLASSES)
def test_golden(cls, facts):
    """Set UPDATE_GOLDEN=1 to rewrite; review the diff before committing it."""
    text = plan_text(build_model(fixture_config(cls)), json.dumps(facts(cls), sort_keys=True).encode())
    path = GOLDEN / f"{cls}.plan.yaml"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text()


def legacy_config() -> dict:
    """The large class with its old NTFS disk kept and shared."""
    return {**fixture_config("large"), "disks": {"ata-SYNTHETIC_HDD_0002": {"use": "legacy"}}}


def test_golden_legacy(facts):
    """A variant of the large class: its old Windows data disk kept as legacy."""
    text = plan_text(build_model(legacy_config()), json.dumps(facts("large"), sort_keys=True).encode())
    path = GOLDEN / "large-legacy.plan.yaml"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text()


@pytest.mark.parametrize("cls", CLASSES)
def test_rendered_plan_parses_back(cls, facts):
    model, f = build_model(fixture_config(cls)), facts(cls)
    text = plan_text(model, json.dumps(f).encode())
    assert yaml.safe_load(text) == strip_notes(build_plan(model, f))


def test_unquoted_pci_key_is_rejected():
    with pytest.raises(NullBillError, match="gpus"):
        build_model(yaml.safe_load("gpus:\n  0000:01:00.0: {owner: host-shared}\n"))


@pytest.mark.parametrize("cls", CLASSES)
def test_class_matches_fixture(cls, facts):
    assert plan_for(facts(cls), fixture_config(cls))["class"].value == cls


def test_yaml_alone_adds_zone_and_service(facts):
    """M1: a synthetic service and zone added only in config.yaml change the plan."""
    config = fixture_config("small")
    before = plan_for(facts("small"), config)
    config["zones"] = {"lab": {"index": 20, "description": "Synthetic test zone."}}
    config["services"] = {
        "echo": {
            "id": 50,
            "kind": "lxc",
            "zone": "lab",
            "workload": "core",
            "floor": {"cores": 1, "memory_mib": 128, "disk_gib": 1},
            "ports": [{"port": 7, "proto": "tcp", "from": ["zone:build"]}],
        }
    }
    after = plan_for(facts("small"), config)
    assert "lab" not in before["zones"].value
    assert after["zones"].value["lab"] == {"bridge": "vmbr120", "subnet": "10.42.20.0/24", "gateway": "10.42.20.1", "egress": "none"}
    assert after["guests"]["echo"] == {
        "kind": "lxc",
        "zone": "lab",
        "cores": 1,
        "memory_mib": 128,
        "disk_gib": 1,
        "vmid": 2050,
        "ip": "10.42.20.50",
    }
    assert "zone:build -> echo tcp/7" in after["flows"].value


def errors_of(facts: dict, config: dict) -> list[str]:
    with pytest.raises(NullBillError) as exc:
        plan_for(facts, config)
    return exc.value.messages


def test_floors_over_capacity_fail(facts):
    config = {"intent": {"workloads": ["agents", "linux-ci", "windows-ci", "windows-dev"]}}
    messages = errors_of(facts("small"), config)
    assert messages[0] == (
        "memory: planned guests need 16384 MiB at their floors (containers at 50% of their limit); "
        "the host has 15848 MiB minus a 3072 MiB host reserve"
    )
    assert messages[1] == "disk: guests need 440 GiB, storage local-lvm has 350 GiB free"


def test_overrides_are_checked(facts):
    config = copy.deepcopy(fixture_config("small"))
    config["overrides"] = {
        "forgejo": {"memory_mib": 512},
        "windows-dev": {"cores": 4},
        "postgres": {"count": 2},
        "workspace": {"cores": 8},
    }
    assert sorted(errors_of(facts("small"), config)) == [
        "overrides.forgejo.memory_mib: 512 is below the floor of 1024",
        "overrides.postgres.count: only vm-pool services have a count",
        "overrides.windows-dev: not a planned service",
        "workspace: needs 8 cores, the host has 4 threads",
    ]


def test_pinned_pool_count(facts):
    config = {**fixture_config("mid"), "overrides": {"linux-runner": {"count": 2}}}
    guest = plan_for(facts("mid"), config)["guests"]["linux-runner"]
    assert guest["count"].value == 2 and guest["count"].why == "Clones, pinned in config.yaml."


def test_disk_with_signatures_needs_wipe_pin(facts):
    config = {**fixture_config("large"), "disks": {"ata-SYNTHETIC_HDD_0002": {"use": "pbs"}}}
    (message,) = errors_of(facts("large"), config)
    assert message == "disks.ata-SYNTHETIC_HDD_0002: carries gpt, ntfs; set wipe: true to let apply format it"

    config["disks"]["ata-SYNTHETIC_HDD_0002"]["wipe"] = True
    plan = plan_for(facts("large"), config)
    assert plan["disks"]["ata-SYNTHETIC_HDD_0002"].value["use"] == "pbs"
    assert plan["disks"]["ata-SYNTHETIC_HDD_0003"].value["use"] == "unused"


def test_unpinned_disk_with_signatures_is_untouched(facts):
    plan = plan_for(facts("large"), fixture_config("large"))
    assert plan["disks"]["ata-SYNTHETIC_HDD_0002"].value["use"] == "untouched"
    assert plan["backups"].value == {"target": "pbs datastore on ata-SYNTHETIC_HDD_0003", "status": "ok"}


def test_the_pbs_disk_apply_made_stays_the_pbs_disk_after_a_new_probe(facts):
    """nb-backup-disk leaves LVM's signature on the disk it picked; a probe
    after that must not read it as someone else's disk, or the render drops
    the PBS datastore disk and the backup job."""
    from nullbill.tofu import render_tofu

    config = fixture_config("mid")
    model = build_model(config)
    renders = []
    for signatures, vgs in (([], []), (["LVM2_member"], ["nb-pbs-disk"])):
        f = facts("mid")
        disk = next(d for d in f["disks"] if d["id"] == "ata-SYNTHETIC_HDD_0002")
        disk["signatures"], disk["vgs"] = signatures, vgs
        plan = strip_notes(build_plan(model, f))
        assert plan["disks"]["ata-SYNTHETIC_HDD_0002"]["use"] == "pbs"
        assert plan["backups"]["status"] == "ok"
        renders.append(json.loads(render_tofu(plan, model)["main.tf.json"])["resource"])
    before, after = renders
    assert after["proxmox_virtual_environment_vm"]["pbs"]["disk"] == before["proxmox_virtual_environment_vm"]["pbs"]["disk"]
    assert after.get("proxmox_backup_job") == before.get("proxmox_backup_job") is not None
    # The same pinned for PBS needs no wipe; any other LVM disk stays untouched.
    pinned = {**config, "disks": {"ata-SYNTHETIC_HDD_0002": {"use": "pbs"}}}
    assert strip_notes(plan_for(f, pinned))["disks"]["ata-SYNTHETIC_HDD_0002"]["use"] == "pbs"
    disk["vgs"] = ["someone-elses"]
    assert strip_notes(plan_for(f, config))["disks"]["ata-SYNTHETIC_HDD_0002"]["use"] == "untouched"


def test_a_storage_disk_apply_made_needs_no_wipe_after_a_new_probe(facts):
    f = facts("mid")
    disk = next(d for d in f["disks"] if d["id"] == "ata-SYNTHETIC_HDD_0002")
    disk["signatures"], disk["vgs"] = ["LVM2_member"], ["nb-data"]
    config = {**fixture_config("mid"), "disks": {"ata-SYNTHETIC_HDD_0002": {"use": "storage"}}}
    assert strip_notes(plan_for(f, config))["disks"]["ata-SYNTHETIC_HDD_0002"]["storage"] == "nb-data"
    disk["vgs"] = ["nb-other"]
    (message,) = errors_of(f, config)
    assert message == "disks.ata-SYNTHETIC_HDD_0002: carries LVM2_member; set wipe: true to let apply format it"


def test_single_disk_backups_degraded(facts):
    assert plan_for(facts("small"), fixture_config("small"))["backups"].value["status"] == "DEGRADED"


def test_gpu_over_budget_fails(facts):
    config = {**fixture_config("mid"), "gpus": {"0000:01:00.0": {"consumers": {"inference": 30000}}}}
    (message,) = errors_of(facts("mid"), config)
    assert message == "gpus.0000:01:00.0: VRAM budgets total 30000 MiB, the GPU has 24564 MiB"


def test_gpu_passthrough_takes_gpu_from_inference(facts):
    config = {**fixture_config("large"), "gpus": {"0000:65:00.0": {"owner": "vm:windows-dev"}}}
    plan = plan_for(facts("large"), config)
    assert plan["gpus"]["0000:65:00.0"].value["owner"] == "vm:windows-dev"
    assert plan["inference"]["engine"].value == "llama.cpp"


def test_gpu_passthrough_refuses_shared_iommu_group(facts):
    f = facts("large")
    f["gpus"][1]["iommu_peers"].append("0000:66:00.0")
    config = {**fixture_config("large"), "gpus": {"0000:65:00.0": {"owner": "vm:windows-dev"}}}
    (message,) = errors_of(f, config)
    assert message == "gpus.0000:65:00.0: IOMMU group 80 also holds 0000:66:00.0"


def test_gpu_without_vram_needs_answer(facts):
    f = facts("mid")
    f["gpus"][0]["vram_mib"] = None
    assert any("vram_mib" in m for m in errors_of(f, fixture_config("mid")))
    config = {**fixture_config("mid"), "gpus": {"0000:01:00.0": {"vram_mib": 16384}}}
    assert plan_for(f, config)["gpus"]["0000:01:00.0"].value["vram_mib"] == 16384


def test_an_unused_gpu_without_vram_does_not_stop_the_plan(facts):
    """A host without the GPU's driver reports no VRAM; without inference
    nothing budgets it, so the plan goes on."""
    f = facts("mid")
    f["gpus"][0]["vram_mib"] = None
    config = {**fixture_config("mid"), "intent": {"workloads": ["agents", "windows-dev"]}}
    assert plan_for(f, config)["gpus"]["0000:01:00.0"].value["vram_mib"] is None


def test_inference_without_gpu_fails(facts):
    messages = errors_of(facts("small"), {"intent": {"workloads": ["inference"]}})
    assert messages[0] == "intent asks for inference, but no host-shared GPU budgets VRAM to it"


def test_unknown_workload_and_dangling_refs_fail(facts):
    assert errors_of(facts("small"), {"intent": {"workloads": ["mining"]}})[0].startswith("intent.workloads: unknown workload")
    config = {"roles": {"default": {"reach": ["service:nope"]}}, "people": {"dev-b": {"role": "ghost", "login": "dev-b@example.internal"}}}
    assert sorted(errors_of(facts("small"), config)) == [
        "people.dev-b.role: role 'ghost' does not exist",
        "roles.default.reach: service:nope does not exist",
    ]


def test_host_is_not_a_reach_target(facts):
    config = {"roles": {"default": {"reach": ["host"]}}}
    assert errors_of(facts("small"), config) == [
        "roles.default.reach: host is only a flow source; the owner role reaches the host through the VPN"
    ]


def test_duplicate_service_id_fails(facts):
    config = {
        "services": {
            "echo": {
                "id": 13,
                "kind": "lxc",
                "zone": "services",
                "workload": "core",
                "floor": {"cores": 1, "memory_mib": 128, "disk_gib": 1},
            }
        }
    }
    assert errors_of(facts("small"), config) == ["services.echo.id: 13 already used by service postgres"]


def test_duplicate_zone_index_fails(facts):
    config = {"zones": {"lab": {"index": 3}}}
    assert errors_of(facts("small"), config) == ["zones.lab.index: 3 already used by zone build"]


def test_schema_rejects_unknown_keys():
    with pytest.raises(NullBillError, match="Additional properties"):
        build_model({"overrides": {"forgejo": {"ram": 1}}})


def test_small_hosts_keep_less_for_pve_and_zfs_adds_its_cache(facts):
    small = facts("small")
    plan = strip_notes(plan_for(small, fixture_config("small")))
    assert plan["host"]["reserve_mib"] == 3072
    zfs = copy.deepcopy(small)
    zfs["storage"].append({"name": "local-zfs", "type": "zfspool", "content": ["images"], "avail_gib": 1})
    plan = strip_notes(plan_for(zfs, fixture_config("small")))
    assert plan["host"]["reserve_mib"] == 3072 + small["memory_mib"] // 10
    mid = strip_notes(plan_for(facts("mid"), fixture_config("mid")))
    assert mid["host"]["reserve_mib"] == 4096


def test_on_site_screen_costs_host_memory_and_needs_a_gpu_left_to_the_host(facts):
    small = facts("small")
    config = {**fixture_config("small"), "host": {"console": {"enabled": True}}}
    plan = plan_for(small, config)
    assert plan["host"]["reserve_mib"].value == 3072 + 512
    integrated = next(g["name"] for g in small["gpus"] if g["integrated"])
    assert plan["host"]["console"].value == {"enabled": True, "screen": integrated}
    assert strip_notes(plan_for(small, fixture_config("small")))["host"]["console"] == {"enabled": False}
    # Every GPU passed through to a VM leaves nothing to drive the screen.
    large = facts("large")
    large["gpus"] = [g for g in large["gpus"] if not g["integrated"]]
    base = fixture_config("large")
    workloads = [w for w in base["intent"]["workloads"] if w != "inference"]
    gpus = {g["pci"]: {"owner": "vm:windows-dev"} for g in large["gpus"]}
    config = {**base, "intent": {"workloads": workloads}, "gpus": gpus, "host": {"console": {"enabled": True}}}
    assert "host.console: every GPU is passed through to a VM, so nothing is left to drive a screen" in errors_of(large, config)


def test_containers_count_at_their_share_so_16_gib_holds_every_workload_but_windows_ci(facts):
    """The owner's 16 GiB server: agents, Linux CI, local models on the GPU
    and the Windows desktop, with the on-site screen and room for a kept
    disk's files guest (512 MiB, counted at half). VMs
    count in full, containers at host.container_share_pct of their limit."""
    f = facts("mid")
    f["memory_mib"] = 15645
    f["gpus"][0]["vram_mib"] = None
    config = {
        **fixture_config("mid"),
        "intent": {"workloads": ["agents", "linux-ci", "inference", "windows-dev"]},
        "services": {"pbs": {"enabled": False}},
        "host": {"console": {"enabled": True}},
        "gpus": {"0000:01:00.0": {"vram_mib": 12282}},
    }
    plan = strip_notes(plan_for(f, config))
    guests = plan["guests"]
    vms = sum(g["memory_mib"] * g.get("count", 1) for g in guests.values() if g["kind"] != "lxc")
    containers = sum(g["memory_mib"] for g in guests.values() if g["kind"] == "lxc")
    assert plan["host"]["guests_mib"] == vms + containers // 2
    assert plan["host"]["guests_mib"] + 256 + plan["host"]["reserve_mib"] <= 15645
    assert guests["windows-dev"]["memory_mib"] == 4096 and guests["inference"]["memory_mib"] == 2048
    assert plan["gpus"]["0000:01:00.0"]["consumers"] == {"inference": 12282}
    # Counted in full, the same guests would not fit.
    full = {**config, "host": {**config["host"], "container_share_pct": 100}}
    assert errors_of(f, full)[0].startswith("memory: planned guests need")
