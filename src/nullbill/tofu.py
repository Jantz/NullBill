"""OpenTofu configuration for the bridges, images and guests in the plan.

Rendered as JSON, which OpenTofu reads like HCL, for the bpg/proxmox
provider. Every vNIC has the PVE firewall on and a MAC from flows.mac(), so
the pve-firewall ipfilter, the router's .link names and DHCP all agree on
which interface is which. The API endpoint, node name and token are
variables and environment, never part of the rendered files.
"""

import json

from nullbill import agent, backups, ci, legacy, media, power, torrents
from nullbill.flows import mac
from nullbill.render import strip_notes

PROVIDER = {"source": "bpg/proxmox", "version": "0.118.1"}
TOFU_VERSION = "~> 1.13.0"
NODE = "${var.node_name}"
KEYS = "${var.ssh_authorized_keys}"
TAGS = ["nullbill"]
# State and saved plans hold secrets (the PBS token, generated tokens), so
# OpenTofu encrypts both and refuses to write either in plaintext. The
# passphrase comes from apply's environment as TF_VAR_state_passphrase.
# Reference: https://opentofu.org/docs/language/state/encryption/
ENCRYPTION = {
    "key_provider": {"pbkdf2": {"nullbill": {"passphrase": "${var.state_passphrase}"}}},
    "method": {"aes_gcm": {"nullbill": {"keys": "key_provider.pbkdf2.nullbill"}}},
    "state": {"method": "method.aes_gcm.nullbill", "enforced": True},
    "plan": {"method": "method.aes_gcm.nullbill", "enforced": True},
}


def _bridge_ref(bridge: str) -> str:
    return f"proxmox_network_linux_bridge.{bridge}"


def _bridges(plan: dict) -> dict:
    bridges = {
        z["bridge"]: {"node_name": NODE, "name": z["bridge"], "comment": f"nullbill zone {name}", "autostart": True}
        for name, z in plan["zones"].items()
    }
    # The host's own address on mgmt (.254); apply routes the other zones
    # through the router from there.
    mgmt = plan["zones"]["mgmt"]
    bridges[mgmt["bridge"]]["address"] = f"{mgmt['host_ip']}/{mgmt['subnet'].split('/')[1]}"
    return bridges


# After a power cut, guests start in this order; the rest follow.
BOOT_ORDER = {"router": 1, "step-ca": 2, "postgres": 3}
LATER = 10


def _startup(name: str) -> dict:
    return {"order": BOOT_ORDER.get(name, LATER)}


def _ignore(resource: dict, *attributes: str) -> dict:
    """Leave these attributes of a guest that exists as they are."""
    lifecycle = resource.setdefault("lifecycle", {})
    lifecycle["ignore_changes"] = [*lifecycle.get("ignore_changes", []), *attributes]
    return resource


def _downloads(plan: dict, model: dict) -> dict:
    images = model["images"]
    media = plan["disk"]["media"]
    lxc = images["debian-lxc"]
    downloads = {
        "debian_lxc": {
            "node_name": NODE,
            "datastore_id": media,
            "content_type": "vztmpl",
            "url": lxc["url"],
            "checksum": lxc["sha512"],
            "checksum_algorithm": "sha512",
        },
    }
    cloud = images["debian-cloud"]
    downloads["debian_cloud"] = {
        "node_name": NODE,
        "datastore_id": media,
        # PVE lists disk images under iso content only with an .img name.
        "content_type": "iso",
        "file_name": cloud["url"].rsplit("/", 1)[1].removesuffix(".qcow2") + ".img",
        "url": cloud["url"],
        "checksum": cloud["sha512"],
        "checksum_algorithm": "sha512",
    }
    return downloads


def _ipv4(plan: dict, zone: str, address: str | None) -> dict:
    if address is None:
        return {"ipv4": {"address": "dhcp"}}
    z = plan["zones"][zone]
    return {"ipv4": {"address": f"{address}/{z['subnet'].split('/')[1]}", "gateway": z["gateway"]}}


def _dns(plan: dict, zone: str) -> dict:
    return {"domain": plan["domain"], "servers": [plan["zones"][zone]["gateway"]]}


def _on_demand(resource: dict, guest: dict, on_boot: str) -> dict:
    """A guest that runs on demand (power.py) is made stopped and does not
    start with the server; the host's nb-power starts and stops it, so
    OpenTofu leaves whether it runs alone."""
    if guest.get("run") != power.ON_DEMAND:
        return resource
    resource |= {on_boot: False, "started": False}
    return _ignore(resource, "started")


def _container(name: str, guest: dict, plan: dict, model: dict, storage: str) -> dict:
    zone = guest["zone"]
    index, host = model["zones"][zone]["index"], model["services"][name]["id"]
    container = {
        "node_name": NODE,
        "vm_id": guest["vmid"],
        "description": f"nullbill {name}",
        "tags": TAGS,
        "unprivileged": True,
        "start_on_boot": True,
        "startup": _startup(name),
        "started": True,
        # Debian's systemd needs nesting in an unprivileged container.
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
    # The template is what a container is made from. A newer pin must not
    # make OpenTofu replace a container that exists, with all it holds; the
    # release stage upgrades its Debian in place (release.py).
    _ignore(container, "operating_system")
    if model["services"][name].get("gpu"):
        # apply gives it the GPU's device nodes as root over SSH (PVE allows
        # device passthrough to root@pam alone; ansible/roles/inference).
        _ignore(container, "device_passthrough")
    if name == "files" and (legacy.shares(plan) or legacy.copies(plan)):
        # apply sets the kept disks' bind mount points as root over SSH: PVE
        # refuses bind mounts to API tokens (docs/adr/0007-legacy-disks.md).
        # It attaches the copies of converted disks the same way.
        _ignore(container, "mount_point")
        if legacy.copies(plan):
            # A copy is a volume of this container: destroying it would
            # destroy the owner's only copy of those files.
            container["lifecycle"]["prevent_destroy"] = True
    if name == media.JELLYFIN:
        # apply sets the kept disks' bind mounts and the integrated GPU's
        # render node as root, as for the files guest and inference.
        _ignore(container, "mount_point", "device_passthrough")
    if name == torrents.QBITTORRENT:
        # apply sets its downloads folder, a bind mount, as root likewise.
        _ignore(container, "mount_point")
    return _in_agent_pool(_on_demand(container, guest, "start_on_boot"), guest)


def _nic(bridge: str, address: str, model: str = "virtio") -> dict:
    # network_device is an attribute in JSON syntax, so every field is spelled
    # out; the rest are the provider's defaults.
    return {
        "bridge": bridge,
        "mac_address": address,
        "model": model,
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
        "description": f"nullbill {name}" + (" golden image; apply bakes it, the dispatcher clones it per job" if pool else ""),
        "tags": TAGS,
        "on_boot": not pool,
        "started": not pool,
        "machine": "q35",
        "cpu": {"cores": guest["cores"], "type": "x86-64-v2-AES"},
        "memory": {"dedicated": guest["memory_mib"]},
        "scsi_hardware": "virtio-scsi-single",
        # apply's updates stage installs qemu-guest-agent in every Linux VM
        # (and the CI bake in golden images), so PVE can freeze file systems
        # for backups and shut down cleanly. tofu does not wait for it to
        # report addresses: the addresses are in the plan. Windows's agent
        # comes with the VirtIO drivers, which its VMs do without.
        "agent": {"enabled": not windows, "trim": not windows, "wait_for_ip": {"disabled": True}},
    }
    if not pool:
        vm["startup"] = _startup(name)
    if name == "router":
        legs = [_nic("${var.lan_bridge}", mac(0, svc["id"]))]
        legs += [_nic(z["bridge"], mac(model["zones"][zn]["index"], svc["id"])) for zn, z in plan["zones"].items()]
        vm["network_device"] = legs
        # Uplink by DHCP from the LAN; each leg is its zone's gateway.
        ip_config = [{"ipv4": {"address": "dhcp"}}]
        ip_config += [{"ipv4": {"address": f"{z['gateway']}/{z['subnet'].split('/')[1]}"}} for z in plan["zones"].values()]
        bridges = [z["bridge"] for z in plan["zones"].values()]
    else:
        # Windows has no VirtIO drivers of its own; Intel's e1000e it has.
        vm["network_device"] = [_nic(plan["zones"][zone]["bridge"], mac(index, svc["id"]), "e1000e" if windows else "virtio")]
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
            # SATA and e1000e, which Windows drives with its own drivers: the
            # provider allows one CD-ROM, so the VirtIO driver ISO cannot sit
            # beside the installer.
            "disk": [{"datastore_id": storage, "interface": "sata0", "size": guest["disk_gib"], "discard": "on", "ssd": True}],
        }
    else:
        vm |= {
            # apply bakes a pool's golden image, then converts it (ci.py).
            "template": False,
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
                # On SCSI, not the default ide2: on q35, PVE puts IDE drives
                # on the ICH9 AHCI controller, which Debian's cloud kernel
                # has no driver for. Cloud-init then finds no NoCloud drive
                # and leaves the guest with no addresses, user or host keys.
                "interface": "scsi1",
                "ip_config": ip_config,
                # The router resolves through its uplink's DHCP until its own
                # dnsmasq runs; every other guest asks the router.
                **({} if name == "router" else {"dns": _dns(plan, zone)}),
                "user_account": {"username": "nullbill", "keys": KEYS},
            },
        }
    if guest["zone"] != "windows":
        # As for a container's template: the image a VM that exists was
        # made from stays; a pool's golden image is replaced on purpose.
        _ignore(vm, "disk[0].file_id")
    vm["depends_on"] = [_bridge_ref(b) for b in bridges]
    gpus = _passed_gpus(plan, name)
    if gpus:
        # A mapping, not a raw PCI address: the API token may attach mapped
        # devices, raw ones only root@pam's password can.
        vm["hostpci"] = [{"device": f"hostpci{n}", "mapping": _mapping_name(pci), "pcie": True} for n, pci in enumerate(gpus)]
        vm["depends_on"] += [f"{MAPPING}.{_mapping_name(pci).replace('-', '_')}" for pci in gpus]
    if name == "pbs" and backups.enabled(plan):
        vm["disk"].append(
            {
                # A disk of PBS's own, or the storage the guests are on. apply
                # moves and grows the volume itself before OpenTofu looks
                # (apply.place_datastore), so this only ever makes it.
                "datastore_id": backups.store(plan)[0],
                "interface": "virtio1",
                "size": backups.store(plan)[1],
                "serial": backups.DISK_SERIAL,
                "file_format": "raw",
                "discard": "on",
                "iothread": True,
                # The datastore: never hand it to a backup of its own.
                "backup": False,
            }
        )
        # Destroying the VM would destroy the datastore disk with it, and
        # every backup on it; a change that needs a new VM stops tofu instead.
        vm.setdefault("lifecycle", {})["prevent_destroy"] = True
    if pool:
        _ignore(vm, "template")
        # In no pool: the dispatcher may clone it, nobody may start or change it.
        return vm
    return _in_agent_pool(_on_demand(vm, guest, "on_boot"), guest)


MAPPING = "proxmox_hardware_mapping_pci"


def _passed_gpus(plan: dict, name: str) -> list[str]:
    return sorted(pci for pci, gpu in plan["gpus"].items() if gpu["owner"] == f"vm:{name}")


def _mapping_name(pci: str) -> str:
    """nb-gpu-0000-01-00 for 0000:01:00.0."""
    return "nb-gpu-" + pci.rsplit(".", 1)[0].replace(":", "-")


def _mappings(plan: dict) -> dict:
    """A PVE PCI mapping per GPU passed to a VM. Its path is the slot
    without the function, as PVE's "All Functions": a GPU's audio function
    goes with it (the plan refuses other devices in its IOMMU group)."""
    mappings = {}
    for pci, gpu in sorted(plan["gpus"].items()):
        if not gpu["owner"].startswith("vm:"):
            continue
        name = _mapping_name(pci)
        entry = {"id": gpu["pci_id"], "node": NODE, "path": pci.rsplit(".", 1)[0], "iommu_group": gpu["iommu_group"]}
        mappings[name.replace("-", "_")] = {"name": name, "comment": f"nullbill: {gpu['name']} for {gpu['owner'][3:]}", "map": [entry]}
    return mappings


def _in_agent_pool(resource: dict, guest: dict) -> dict:
    if guest.get("agent_managed"):
        resource["pool_id"] = agent.POOL
        resource["depends_on"] = [*resource["depends_on"], "proxmox_virtual_environment_pool.agent_managed"]
    return resource


# PVE checks the PBS connection when the storage is added, so the storage and
# the job wait until apply's backups stage has configured PBS and set these.
WHEN_PBS_READY = '${var.pbs_fingerprint == "" ? 0 : 1}'


def _backup_resources(plan: dict) -> dict:
    pbs_ip = plan["guests"]["pbs"]["ip"]
    storage = {
        "count": WHEN_PBS_READY,
        "id": backups.STORAGE,
        "server": pbs_ip,
        "datastore": backups.DATASTORE,
        "username": backups.TOKEN_ID,
        "password": "${var.pbs_token_secret}",
        "fingerprint": "${var.pbs_fingerprint}",
        # Encrypted on the host before upload. apply keeps a copy of the key
        # in the controller's secrets, so it survives the host.
        "encryption_key": "${var.pbs_encryption_key}",
        "content": ["backup"],
        # The token cannot delete; retention runs on PBS, so PVE must not try.
        "backups": {"keep_all": True},
    }
    job = {
        "count": WHEN_PBS_READY,
        "id": backups.JOB,
        "schedule": backups.SCHEDULE,
        "storage": f"${{proxmox_storage_pbs.{_tf_name(backups.STORAGE)}[0].id}}",
        "vmid": [str(g["vmid"]) for g in backups.guests(plan).values()],
        "mode": "snapshot",
        "enabled": True,
        "repeat_missed": True,
        "notes_template": "{{guestname}}",
        "comment": "nullbill nightly backup; retention runs on PBS",
    }
    return {
        "proxmox_storage_pbs": {_tf_name(backups.STORAGE): storage},
        "proxmox_backup_job": {_tf_name(backups.JOB): job},
    }


def _acls(prefix: str, user: str, token_id: str, grants: dict[str, tuple[str, str, list[str]]]) -> dict:
    """The same ACLs for a user and its privilege-separated token, whose
    rights are the overlap of the two."""
    acls = {}
    for holder, key, value, needs in (
        ("user", "user_id", user, [f"proxmox_virtual_environment_user.{prefix}"]),
        ("token", "token_id", token_id, [f"proxmox_user_token.{prefix}"]),
    ):
        for grant, (path, role_id, deps) in grants.items():
            acls[f"{prefix}_{holder}_{grant}"] = {
                "path": path,
                "role_id": role_id,
                key: value,
                "propagate": True,
                "depends_on": deps + needs,
            }
    return acls


def _identity(prefix: str, user: str, token: str, comment: str) -> dict:
    return {
        "proxmox_virtual_environment_user": {prefix: {"user_id": user, "enabled": True, "comment": comment}},
        "proxmox_user_token": {
            prefix: {
                "user_id": f"${{proxmox_virtual_environment_user.{prefix}.user_id}}",
                "token_name": token,
                "privileges_separation": True,
                "comment": f"nullbill {token}",
            }
        },
    }


CI_POOL = "proxmox_virtual_environment_pool.ci"


def _agent_resources(plan: dict) -> dict:
    """The agent's pool, role, user, token and ACLs. Agent-managed pool
    services are cloned into the CI pool, so the agent's role covers it too."""
    pool = "proxmox_virtual_environment_pool.agent_managed"
    role = "proxmox_virtual_environment_role.agent"
    grants = {"auditor": ("/", "PVEAuditor", []), "pool": (f"/pool/{agent.POOL}", agent.ROLE, [pool, role])}
    if any(g.get("agent_managed") for g in ci.pools(plan).values()):
        grants["ci_pool"] = (f"/pool/{ci.POOL}", agent.ROLE, [CI_POOL, role])
    resources = _identity("agent", agent.USER, agent.TOKEN, "Runtime agent; token only, in the operator container")
    resources["proxmox_user_token"]["agent"]["comment"] = "nullbill operator"
    return resources | {
        "proxmox_virtual_environment_pool": {
            "agent_managed": {"pool_id": agent.POOL, "comment": "Guests the agent may power-manage and snapshot"}
        },
        "proxmox_virtual_environment_role": {"agent": {"role_id": agent.ROLE, "privileges": agent.PRIVILEGES}},
        "proxmox_acl": _acls("agent", agent.USER, agent.TOKEN_ID, grants),
    }


def _ci_resources(plan: dict) -> dict:
    """The CI pool, and the dispatcher's roles, user, token and ACLs."""
    role, source = "proxmox_virtual_environment_role.dispatcher", "proxmox_virtual_environment_role.clone_source"
    grants = {
        "pool": (f"/pool/{ci.POOL}", ci.ROLE, [CI_POOL, role]),
        "storage": (f"/storage/{plan['disk']['storage']}", ci.STORAGE_ROLE, []),
    }
    for name, guest in ci.pools(plan).items():
        bridge = plan["zones"][guest["zone"]]["bridge"]
        grants[f"template_{_tf_name(name)}"] = (
            f"/vms/{guest['template_vmid']}",
            ci.SOURCE_ROLE,
            [source, f"proxmox_virtual_environment_vm.{name}"],
        )
        grants[f"bridge_{bridge}"] = (f"/sdn/zones/localnetwork/{bridge}", ci.BRIDGE_ROLE, [_bridge_ref(bridge)])
    acls = _acls("dispatcher", ci.USER, ci.TOKEN_ID, grants)
    for name in ci.pools(plan):
        # PVE deletes a VM's ACLs with the VM, and apply replaces a golden
        # image to bake it again (apply.retire): its ACLs must be made anew
        # with it, or state still lists them and the dispatcher cannot clone.
        for holder in ("user", "token"):
            acls[f"dispatcher_{holder}_template_{_tf_name(name)}"]["lifecycle"] = {
                "replace_triggered_by": [f"proxmox_virtual_environment_vm.{name}.id"]
            }
    return _identity("dispatcher", ci.USER, ci.TOKEN, "Runner dispatcher; token only, in its container") | {
        "proxmox_virtual_environment_role": {
            "dispatcher": {"role_id": ci.ROLE, "privileges": ci.PRIVILEGES},
            "clone_source": {"role_id": ci.SOURCE_ROLE, "privileges": ci.SOURCE_PRIVILEGES},
        },
        "proxmox_acl": acls,
    }


def _tf_name(name: str) -> str:
    return name.replace("-", "_")


def _variables(plan: dict) -> dict:
    variables = {
        "pve_endpoint": {"type": "string", "description": "PVE API URL, e.g. https://pve.internal:8006/"},
        "node_name": {"type": "string", "description": "PVE node name"},
        "pve_ssh_address": {"type": "string", "description": "Address the provider reaches the PVE host at over SSH"},
        "lan_bridge": {"type": "string", "default": "vmbr0", "description": "Bridge on the LAN, the router's uplink"},
        "ssh_authorized_keys": {"type": "list(string)", "description": "Keys for the nullbill user on Linux guests"},
        "state_passphrase": {"type": "string", "sensitive": True, "description": "Encrypts state and plans; 16 characters or more"},
    }
    if backups.enabled(plan):
        secret = {"type": "string", "default": "", "sensitive": True}
        variables |= {
            "pbs_fingerprint": {"type": "string", "default": "", "description": "PBS certificate SHA-256; empty until PBS is set up"},
            "pbs_token_secret": secret | {"description": "Secret of the PVE host's PBS token"},
            "pbs_encryption_key": secret | {"description": "Backup encryption key (JSON), kept on the controller"},
        }
    if any(g["zone"] == "windows" for g in plan["guests"].values()):
        variables["windows_iso"] = {"type": "string", "description": "Volume id of the Windows ISO, e.g. local:iso/windows.iso"}
    return variables


def render_tofu(plan_with_notes: dict, model: dict) -> dict[str, str]:
    """Return {"main.tf.json": the OpenTofu configuration}."""
    plan = strip_notes(plan_with_notes)
    storage = plan["disk"]["storage"]
    # install.sh made the nullbill container, which runs OpenTofu; OpenTofu
    # must never be able to replace it.
    containers = {
        n: _container(n, g, plan, model, storage) for n, g in plan["guests"].items() if g["kind"] == "lxc" and not g.get("installed")
    }
    vms = {n: _vm(n, g, plan, model, storage) for n, g in plan["guests"].items() if g["kind"] in ("vm", "vm-pool")}
    config = {
        "//": "Generated by nullbill. Do not edit; change config.yaml and re-run apply.",
        "terraform": {"required_version": TOFU_VERSION, "required_providers": {"proxmox": PROVIDER}, "encryption": ENCRYPTION},
        # The API token comes from PROXMOX_VE_API_TOKEN in apply's environment.
        # Disk imports go over SSH to the address apply reaches the host at
        # (the LAN, or mgmt once the LAN is closed), whose key the
        # controller's known_hosts holds; the provider would otherwise pick
        # an address from the host's interfaces itself.
        "provider": {
            "proxmox": {"endpoint": "${var.pve_endpoint}", "ssh": {"node": [{"name": NODE, "address": "${var.pve_ssh_address}"}]}},
        },
        "variable": _variables(plan),
        "resource": {
            "proxmox_network_linux_bridge": _bridges(plan),
            "proxmox_download_file": _downloads(plan, model),
            "proxmox_virtual_environment_container": containers,
            "proxmox_virtual_environment_vm": vms,
        },
    }
    if backups.enabled(plan):
        config["resource"] |= _backup_resources(plan)
    if _mappings(plan):
        config["resource"][MAPPING] = _mappings(plan)
    # apply installs each token in its container; neither reaches a file on
    # the controller.
    config["output"] = {"agent_token": {"value": "${proxmox_user_token.agent.value}", "sensitive": True}}
    resource = config["resource"]
    for kind, items in _agent_resources(plan).items():
        resource[kind] = resource.get(kind, {}) | items
    if ci.pools(plan):
        resource["proxmox_virtual_environment_pool"]["ci"] = {"pool_id": ci.POOL, "comment": "Runner clones, one per CI job"}
    if ci.pools(plan) and "runner-dispatcher" in plan["guests"]:
        for kind, items in _ci_resources(plan).items():
            resource[kind] = resource.get(kind, {}) | items
        config["output"]["dispatcher_token"] = {"value": "${proxmox_user_token.dispatcher.value}", "sensitive": True}
    return {"main.tf.json": json.dumps(config, indent=2) + "\n"}
