"""OpenTofu configuration for the bridges, images and guests in the plan.

Rendered as JSON, which OpenTofu reads like HCL, for the bpg/proxmox
provider. Every vNIC has the PVE firewall on and a MAC from flows.mac(), so
the pve-firewall ipfilter, the router's .link names and DHCP all agree on
which interface is which. The API endpoint, node name and token are
variables and environment, never part of the rendered files.
"""

import json

from nullbill.flows import mac
from nullbill.render import strip_notes

PROVIDER = {"source": "bpg/proxmox", "version": "0.118.1"}
TOFU_VERSION = "~> 1.13.0"
NODE = "${var.node_name}"
KEYS = "${var.ssh_authorized_keys}"
TAGS = ["nullbill"]


def _bridge_ref(bridge: str) -> str:
    return f"proxmox_network_linux_bridge.{bridge}"


def _bridges(plan: dict) -> dict:
    return {
        z["bridge"]: {"node_name": NODE, "name": z["bridge"], "comment": f"nullbill zone {name}", "autostart": True}
        for name, z in plan["zones"].items()
    }


def _downloads(plan: dict, model: dict) -> dict:
    images = model["images"]
    media = plan["disk"]["media"]
    lxc, cloud = images["debian-lxc"], images["debian-cloud"]
    return {
        "debian_lxc": {
            "node_name": NODE,
            "datastore_id": media,
            "content_type": "vztmpl",
            "url": lxc["url"],
            "checksum": lxc["sha512"],
            "checksum_algorithm": "sha512",
        },
        "debian_cloud": {
            "node_name": NODE,
            "datastore_id": media,
            # PVE lists disk images under iso content only with an .img name.
            "content_type": "iso",
            "file_name": cloud["url"].rsplit("/", 1)[1].removesuffix(".qcow2") + ".img",
            "url": cloud["url"],
            "checksum": cloud["sha512"],
            "checksum_algorithm": "sha512",
        },
    }


def _ipv4(plan: dict, zone: str, address: str | None) -> dict:
    if address is None:
        return {"ipv4": {"address": "dhcp"}}
    z = plan["zones"][zone]
    return {"ipv4": {"address": f"{address}/{z['subnet'].split('/')[1]}", "gateway": z["gateway"]}}


def _dns(plan: dict, zone: str) -> dict:
    return {"domain": plan["domain"], "servers": [plan["zones"][zone]["gateway"]]}


def _container(name: str, guest: dict, plan: dict, model: dict, storage: str) -> dict:
    zone = guest["zone"]
    index, host = model["zones"][zone]["index"], model["services"][name]["id"]
    return {
        "node_name": NODE,
        "vm_id": guest["vmid"],
        "description": f"nullbill {name}",
        "tags": TAGS,
        "unprivileged": True,
        "start_on_boot": True,
        "started": True,
        # Debian 12's systemd needs nesting in an unprivileged container.
        "features": {"nesting": True},
        "cpu": {"cores": guest["cores"]},
        "memory": {"dedicated": guest["memory_mib"], "swap": 0},
        "disk": {"datastore_id": storage, "size": guest["disk_gib"]},
        "operating_system": {
            "template_file_id": "${proxmox_download_file.debian_lxc.id}",
            "type": "debian",
        },
        "network_interface": [{"name": "eth0", "bridge": plan["zones"][zone]["bridge"], "mac_address": mac(index, host), "firewall": True}],
        "initialization": {
            "hostname": name,
            "ip_config": _ipv4(plan, zone, guest["ip"]),
            "dns": _dns(plan, zone),
            "user_account": {"keys": KEYS},
        },
        "depends_on": [_bridge_ref(plan["zones"][zone]["bridge"])],
    }


def _nic(bridge: str, address: str) -> dict:
    # network_device is an attribute in JSON syntax, so every field is spelled
    # out; the rest are the provider's defaults.
    return {
        "bridge": bridge,
        "mac_address": address,
        "model": "virtio",
        "firewall": True,
        "enabled": True,
        "disconnected": False,
        "mtu": 0,
        "queues": 0,
        "rate_limit": 0,
        "trunks": "",
        "vlan_id": 0,
    }


def _vm(name: str, guest: dict, plan: dict, model: dict, storage: str) -> dict:
    zone = guest["zone"]
    svc = model["services"][name]
    index = model["zones"][zone]["index"]
    windows = zone == "windows"
    pool = guest["kind"] == "vm-pool"
    vm = {
        "node_name": NODE,
        "vm_id": guest["template_vmid"] if pool else guest["vmid"],
        "name": name,
        "description": f"nullbill {name}" + (" golden image; clones are made per job" if pool else ""),
        "tags": TAGS,
        "on_boot": not pool,
        "started": not pool,
        "machine": "q35",
        "cpu": {"cores": guest["cores"], "type": "x86-64-v2-AES"},
        "memory": {"dedicated": guest["memory_mib"]},
        "scsi_hardware": "virtio-scsi-single",
        # The guest agent is not in the base images, so tofu must not wait for it.
        "agent": {"enabled": False},
    }
    if name == "router":
        legs = [_nic("${var.lan_bridge}", mac(0, svc["id"]))]
        legs += [_nic(z["bridge"], mac(model["zones"][zn]["index"], svc["id"])) for zn, z in plan["zones"].items()]
        vm["network_device"] = legs
        # Uplink by DHCP from the LAN; each leg is its zone's gateway.
        ip_config = [{"ipv4": {"address": "dhcp"}}]
        ip_config += [{"ipv4": {"address": f"{z['gateway']}/{z['subnet'].split('/')[1]}"}} for z in plan["zones"].values()]
        bridges = [z["bridge"] for z in plan["zones"].values()]
    else:
        vm["network_device"] = [_nic(plan["zones"][zone]["bridge"], mac(index, svc["id"]))]
        ip_config = [_ipv4(plan, zone, None if pool else guest["ip"])]
        bridges = [plan["zones"][zone]["bridge"]]
    if windows:
        # Windows installs from the ISO the owner supplies; the plan lists it.
        vm |= {
            "bios": "ovmf",
            "operating_system": {"type": "win11"},
            "efi_disk": {"datastore_id": storage, "type": "4m", "pre_enrolled_keys": True},
            "tpm_state": {"datastore_id": storage, "version": "v2.0"},
            "cdrom": {"file_id": "${var.windows_iso}", "interface": "ide2"},
            "disk": [{"datastore_id": storage, "interface": "scsi0", "size": guest["disk_gib"], "discard": "on", "iothread": True}],
        }
    else:
        vm |= {
            "template": pool,
            "operating_system": {"type": "l26"},
            # Debian cloud images log to a serial console.
            "serial_device": [{"device": "socket"}],
            "disk": [
                {
                    "datastore_id": storage,
                    "interface": "scsi0",
                    "size": guest["disk_gib"],
                    "file_id": "${proxmox_download_file.debian_cloud.id}",
                    "discard": "on",
                    "iothread": True,
                }
            ],
            "initialization": {
                "datastore_id": storage,
                "ip_config": ip_config,
                "dns": _dns(plan, zone),
                "user_account": {"username": "nullbill", "keys": KEYS},
            },
        }
    vm["depends_on"] = [_bridge_ref(b) for b in bridges]
    return vm


def _variables(plan: dict) -> dict:
    variables = {
        "pve_endpoint": {"type": "string", "description": "PVE API URL, e.g. https://pve.example.internal:8006/"},
        "node_name": {"type": "string", "description": "PVE node name"},
        "lan_bridge": {"type": "string", "default": "vmbr0", "description": "Bridge on the LAN, the router's uplink"},
        "ssh_authorized_keys": {"type": "list(string)", "description": "Keys for the nullbill user on Linux guests"},
    }
    if any(g["zone"] == "windows" for g in plan["guests"].values()):
        variables["windows_iso"] = {"type": "string", "description": "Volume id of the Windows ISO, e.g. local:iso/windows.iso"}
    return variables


def render_tofu(plan_with_notes: dict, model: dict) -> dict[str, str]:
    """Return {"main.tf.json": the OpenTofu configuration}."""
    plan = strip_notes(plan_with_notes)
    storage = plan["disk"]["storage"]
    containers = {n: _container(n, g, plan, model, storage) for n, g in plan["guests"].items() if g["kind"] == "lxc"}
    vms = {n: _vm(n, g, plan, model, storage) for n, g in plan["guests"].items() if g["kind"] in ("vm", "vm-pool")}
    config = {
        "//": "Generated by nullbill. Do not edit; change config.yaml and re-run apply.",
        "terraform": {"required_version": TOFU_VERSION, "required_providers": {"proxmox": PROVIDER}},
        # The API token comes from PROXMOX_VE_API_TOKEN in apply's environment.
        "provider": {"proxmox": {"endpoint": "${var.pve_endpoint}"}},
        "variable": _variables(plan),
        "resource": {
            "proxmox_network_linux_bridge": _bridges(plan),
            "proxmox_download_file": _downloads(plan, model),
            "proxmox_virtual_environment_container": containers,
            "proxmox_virtual_environment_vm": vms,
        },
    }
    return {"main.tf.json": json.dumps(config, indent=2) + "\n"}
