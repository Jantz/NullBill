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
    want = {n: g.get("vmid", g.get("template_vmid")) for n, g in plan["guests"].items()}
    assert ids == want
    forgejo = res["proxmox_virtual_environment_container"]["forgejo"]
    assert forgejo["initialization"]["ip_config"]["ipv4"] == {"address": "10.42.2.17/24", "gateway": "10.42.2.1"}
    assert forgejo["network_interface"][0]["bridge"] == "vmbr102"
    assert res["proxmox_virtual_environment_vm"]["linux-runner"]["template"] is True


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
    assert config["provider"]["proxmox"] == {"endpoint": "${var.pve_endpoint}"}
    assert [n for n, v in config["variable"].items() if "default" in v] == ["lan_bridge"]
    text = json.dumps(config)
    assert "password" not in text and "api_token" not in text


def test_windows_iso_variable_only_with_windows(facts):
    assert "windows_iso" not in rendered(facts, "small")[0]["variable"]
    assert "windows_iso" in rendered(facts, "mid")[0]["variable"]
