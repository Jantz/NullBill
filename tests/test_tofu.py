"""OpenTofu configuration. tools/check-tofu validates it against the provider."""

import json
import re

from conftest import fixture_config

from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.router import render_router
from nullbill.tofu import render_tofu


def rendered(facts, cls: str) -> tuple[dict, dict, dict]:
    model = build_model(fixture_config(cls))
    plan = build_plan(model, facts(cls))
    return json.loads(render_tofu(plan, model)["main.tf.json"]), strip_notes(plan), model


def nics(config: dict) -> list[dict]:
    res = config["resource"]
    out = [n for vm in res["proxmox_virtual_environment_vm"].values() for n in vm["network_device"]]
    return out + [n for ct in res["proxmox_virtual_environment_container"].values() for n in ct["network_interface"]]


def test_every_vnic_has_firewall_and_unique_mac(facts):
    for cls in ("small", "mid", "large"):
        config, _, _ = rendered(facts, cls)
        macs = [n["mac_address"] for n in nics(config)]
        assert all(n["firewall"] is True for n in nics(config)), cls
        assert len(macs) == len(set(macs)), cls
        assert all(m.startswith("02:4e:42:00:") for m in macs), cls


def test_guests_match_plan(facts):
    config, plan, _ = rendered(facts, "mid")
    res = config["resource"]
    ids = {
        n: r["vm_id"] for kind in ("proxmox_virtual_environment_vm", "proxmox_virtual_environment_container") for n, r in res[kind].items()
    }
    want = {n: g.get("vmid", g.get("template_vmid")) for n, g in plan["guests"].items() if not g.get("installed")}
    assert ids == want
    # OpenTofu must never be able to replace the container it runs in.
    assert "nullbill" in plan["guests"] and "nullbill" not in ids
    forgejo = res["proxmox_virtual_environment_container"]["forgejo"]
    assert forgejo["initialization"]["ip_config"]["ipv4"] == {"address": "10.42.2.17/24", "gateway": "10.42.2.1"}
    assert forgejo["network_interface"][0]["bridge"] == "vmbr102"
    # apply bakes the golden image and converts it; OpenTofu leaves that alone.
    runner = res["proxmox_virtual_environment_vm"]["linux-runner"]
    assert runner["template"] is False and runner["started"] is False
    assert runner["lifecycle"] == {"ignore_changes": ["template"]}


def test_router_macs_match_its_link_files(facts):
    config, plan, model = rendered(facts, "large")
    router = config["resource"]["proxmox_virtual_environment_vm"]["router"]
    links = render_router(plan, model)
    link_macs = sorted(re.search(r"MACAddress=(\S+)", t)[1] for p, t in links.items() if p.endswith(".link"))
    assert sorted(n["mac_address"] for n in router["network_device"]) == link_macs
    assert router["network_device"][0]["bridge"] == "${var.lan_bridge}"


def test_images_are_checksummed_and_nothing_identifying_is_rendered(facts):
    config, _, _ = rendered(facts, "mid")
    for download in config["resource"]["proxmox_download_file"].values():
        assert download["checksum_algorithm"] == "sha512" and len(download["checksum"]) == 128
    # The endpoint, node and keys come from apply; the token from its environment.
    assert config["provider"]["proxmox"] == {
        "endpoint": "${var.pve_endpoint}",
        "ssh": {"node": [{"name": "${var.node_name}", "address": "${var.pve_ssh_address}"}]},
    }
    assert [n for n, v in config["variable"].items() if "default" in v and not n.startswith("pbs_")] == ["lan_bridge"]
    assert all(config["variable"][n]["sensitive"] for n in ("pbs_token_secret", "pbs_encryption_key"))
    storage = config["resource"]["proxmox_storage_pbs"]["nb_pbs"]
    assert storage["password"] == "${var.pbs_token_secret}"
    assert storage["encryption_key"] == "${var.pbs_encryption_key}"
    assert "api_token" not in json.dumps(config)


def test_pbs_boots_debian_13_and_other_vms_debian_12(facts):
    # PBS 4 is built for Debian 13; PBS 3 on Debian 12 is past its end of life.
    config, _, _ = rendered(facts, "small")
    vms = config["resource"]["proxmox_virtual_environment_vm"]
    images = {name: vm["disk"][0]["file_id"] for name, vm in vms.items()}
    assert images.pop("pbs") == "${proxmox_download_file.debian_13_cloud.id}"
    assert set(images.values()) == {"${proxmox_download_file.debian_cloud.id}"}
    downloads = config["resource"]["proxmox_download_file"]
    assert "/trixie/" in downloads["debian_13_cloud"]["url"] and "/bookworm/" in downloads["debian_cloud"]["url"]


def test_windows_iso_variable_only_with_windows(facts):
    assert "windows_iso" not in rendered(facts, "small")[0]["variable"]
    assert "windows_iso" in rendered(facts, "mid")[0]["variable"]


def test_windows_vms_use_inbox_drivers_and_linux_vms_the_guest_agent(facts):
    config, plan, _ = rendered(facts, "mid")
    vms = config["resource"]["proxmox_virtual_environment_vm"]
    for name, vm in vms.items():
        if plan["guests"][name]["zone"] == "windows":
            assert vm["cdrom"] == {"file_id": "${var.windows_iso}", "interface": "ide2"}
            assert [d["interface"] for d in vm["disk"]] == ["sata0"]
            assert [n["model"] for n in vm["network_device"]] == ["e1000e"]
            assert vm["agent"]["enabled"] is False
        else:
            assert vm["agent"] == {"enabled": True, "trim": True, "wait_for_ip": {"disabled": True}}


def test_linux_vms_get_their_cloud_init_drive_on_scsi(facts):
    # Debian's cloud kernel cannot see q35's AHCI controller, where PVE puts ide2.
    for size in ("small", "mid", "large"):
        config, plan, _ = rendered(facts, size)
        for name, vm in config["resource"]["proxmox_virtual_environment_vm"].items():
            if plan["guests"][name]["zone"] != "windows":
                assert vm["machine"] == "q35" and vm["scsi_hardware"] == "virtio-scsi-single"
                assert vm["initialization"]["interface"] == "scsi1"
                assert "scsi1" not in [d["interface"] for d in vm["disk"]]


def test_a_gpu_passed_to_a_vm_goes_through_a_pci_mapping(facts):
    model = build_model({**fixture_config("large"), "gpus": {"0000:65:00.0": {"owner": "vm:windows-dev"}}})
    plan = build_plan(model, facts("large"))
    config = json.loads(render_tofu(plan, model)["main.tf.json"])
    gpu = strip_notes(plan)["gpus"]["0000:65:00.0"]
    (mapping,) = config["resource"]["proxmox_hardware_mapping_pci"].values()
    assert mapping["name"] == "nb-gpu-0000-65-00"
    assert mapping["map"] == [{"id": gpu["pci_id"], "node": "${var.node_name}", "path": "0000:65:00", "iommu_group": gpu["iommu_group"]}]
    vm = config["resource"]["proxmox_virtual_environment_vm"]["windows-dev"]
    assert vm["hostpci"] == [{"device": "hostpci0", "mapping": "nb-gpu-0000-65-00", "pcie": True}]
    assert "proxmox_hardware_mapping_pci.nb_gpu_0000_65_00" in vm["depends_on"]
    # Host-shared GPUs get no mapping, and no other VM a device.
    assert [n for n, v in config["resource"]["proxmox_virtual_environment_vm"].items() if "hostpci" in v] == ["windows-dev"]


def test_state_and_plans_are_encrypted(facts):
    config, _, _ = rendered(facts, "small")
    encryption = config["terraform"]["encryption"]
    assert encryption["state"] == encryption["plan"] == {"method": "method.aes_gcm.nullbill", "enforced": True}
    passphrase = config["variable"]["state_passphrase"]
    assert passphrase["sensitive"] and "default" not in passphrase
