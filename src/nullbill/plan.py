"""Turn probe facts and the merged model into a sized, commented plan.

Every rule that can fail collects its message; plan fails once, listing all
of them, so the owner sees every problem in one run.
"""

import ipaddress
from typing import Any

from nullbill.errors import NullBillError
from nullbill.proxy import allows
from nullbill.render import Note
from nullbill.tailnet import check_tailnet

SIZE_KEYS = ("cores", "memory_mib", "disk_gib")


class _Errors(list):
    def raise_if_any(self) -> None:
        if self:
            raise NullBillError(*self)


def _planned_services(model: dict, errors: _Errors) -> dict[str, dict]:
    workloads = set(model.get("intent", {}).get("workloads", []))
    known = {s["workload"] for s in model["services"].values()} - {"core"}
    for unknown in sorted(workloads - known):
        errors.append(f"intent.workloads: unknown workload {unknown!r}; known: {', '.join(sorted(known))}")
    return {
        name: svc
        for name, svc in model["services"].items()
        if svc.get("enabled", True) and (svc["workload"] == "core" or svc["workload"] in workloads)
    }


def _check_refs(model: dict, errors: _Errors) -> None:
    kinds = {"zone": model["zones"], "service": model["services"], "role": model["roles"]}

    def check(ref: str, where: str) -> None:
        kind, _, name = ref.partition(":")
        if name != "*" and name not in kinds[kind]:
            errors.append(f"{where}: {ref} does not exist")

    for name, svc in model["services"].items():
        if svc["zone"] not in model["zones"]:
            errors.append(f"services.{name}.zone: zone {svc['zone']!r} does not exist")
        for port in svc.get("ports", []):
            for ref in port["from"]:
                check(ref, f"services.{name}.ports")
    for name, role in model["roles"].items():
        for ref in role["reach"]:
            check(ref, f"roles.{name}.reach")
    for name, person in model["people"].items():
        if person["role"] not in model["roles"]:
            errors.append(f"people.{name}.role: role {person['role']!r} does not exist")
    public = model["verify"]["public_name"]
    if not allows(model["proxy"]["allow"], public):
        errors.append(f"verify.public_name: {public} is not on proxy.allow, so verify could not prove the proxy works")
    ids: dict[int, str] = {}
    for name, svc in model["services"].items():
        if svc["id"] in ids:
            errors.append(f"services.{name}.id: {svc['id']} already used by service {ids[svc['id']]}")
        ids[svc["id"]] = name
    seen: dict[int, str] = {}
    for name, zone in model["zones"].items():
        if zone["index"] in seen:
            errors.append(f"zones.{name}.index: {zone['index']} already used by zone {seen[zone['index']]}")
        seen[zone["index"]] = name


def _gpus(model: dict, facts: dict, planned: dict, errors: _Errors) -> tuple[dict, list[str]]:
    """Return the GPU section and the host-shared GPUs that serve inference."""
    pins = model.get("gpus", {})
    discrete = [g for g in facts["gpus"] if not g["integrated"]]
    for pci in sorted(set(pins) - {g["pci"] for g in discrete}):
        errors.append(f"gpus.{pci}: no discrete GPU at this address in the probe")
    section: dict[str, Any] = {}
    inference_gpus = []
    for gpu in discrete:
        pin = pins.get(gpu["pci"], {})
        owner = pin.get("owner", "host-shared")
        vram = gpu["vram_mib"] or pin.get("vram_mib")
        entry: dict[str, Any] = {"name": gpu["name"], "owner": owner, "vram_mib": vram}
        if vram is None:
            errors.append(f"gpus.{gpu['pci']}.vram_mib: the probe could not read VRAM for {gpu['name']}; answer it in config.yaml")
        if owner.startswith("vm:"):
            vm = owner[3:]
            if planned.get(vm, {}).get("kind") != "vm":
                errors.append(f"gpus.{gpu['pci']}.owner: {vm!r} is not a planned VM")
            if "consumers" in pin:
                errors.append(f"gpus.{gpu['pci']}.consumers: a GPU passed to a VM has no other consumers")
            slot = gpu["pci"].rsplit(".", 1)[0]
            foreign = [p for p in gpu["iommu_peers"] if p.rsplit(".", 1)[0] != slot]
            if gpu["iommu_group"] is None:
                errors.append(f"gpus.{gpu['pci']}: IOMMU is off or unreported; passthrough needs it")
            elif foreign:
                errors.append(f"gpus.{gpu['pci']}: IOMMU group {gpu['iommu_group']} also holds {', '.join(foreign)}")
            section[gpu["pci"]] = Note(entry, f"Passed through whole to {vm} on request.")
            continue
        default = {"inference": vram} if "inference" in planned and vram else {}
        consumers = pin.get("consumers", default)
        for consumer in consumers:
            if not planned.get(consumer, {}).get("gpu"):
                errors.append(f"gpus.{gpu['pci']}.consumers: {consumer!r} is not a planned GPU service")
        if vram is not None and sum(consumers.values()) > vram:
            errors.append(f"gpus.{gpu['pci']}: VRAM budgets total {sum(consumers.values())} MiB, the GPU has {vram} MiB")
        entry["consumers"] = consumers
        if "inference" in consumers:
            inference_gpus.append(gpu["pci"])
        section[gpu["pci"]] = Note(entry, "Owned by the host and shared; consumers list their VRAM budget in MiB.")
    return section, inference_gpus


def _engine(model: dict, gpus: list[str]) -> Note | None:
    pinned = model.get("inference", {}).get("engine")
    if pinned:
        return Note(pinned, "Pinned in config.yaml.")
    for rule in model["engines"]:
        if rule["min_gpus"] <= len(gpus) <= rule.get("max_gpus", len(gpus)):
            return Note(rule["engine"], f"Chosen for {len(gpus)} inference GPU(s) by the catalog engine table.")
    return None


def _storage(facts: dict, errors: _Errors) -> dict | None:
    usable = [s for s in facts["storage"] if {"images", "rootdir"} <= set(s["content"])]
    if not usable:
        errors.append("probe: no active storage holds both VM images and container root disks")
        return None
    return max(usable, key=lambda s: (s["avail_gib"], s["name"]))


def _media(facts: dict, errors: _Errors) -> str | None:
    """Storage for downloaded container templates and disk images."""
    usable = [s for s in facts["storage"] if {"iso", "vztmpl"} <= set(s["content"])]
    if not usable:
        errors.append("probe: no active storage holds both ISO images and container templates")
        return None
    return max(usable, key=lambda s: (s["avail_gib"], s["name"]))["name"]


def _size(model: dict, planned: dict, facts: dict, errors: _Errors) -> tuple[dict, dict[str, Note], dict]:
    overrides = model.get("overrides", {})
    for name in sorted(set(overrides) - set(planned)):
        errors.append(f"overrides.{name}: not a planned service")

    sizes: dict[str, dict] = {}
    pinned_counts: dict[str, int] = {}
    for name, svc in planned.items():
        size = dict(svc["floor"])
        for key in SIZE_KEYS:
            if key in overrides.get(name, {}):
                value = overrides[name][key]
                if value < svc["floor"][key]:
                    errors.append(f"overrides.{name}.{key}: {value} is below the floor of {svc['floor'][key]}")
                size[key] = value
        if "count" in overrides.get(name, {}):
            if svc["kind"] != "vm-pool":
                errors.append(f"overrides.{name}.count: only vm-pool services have a count")
            else:
                pinned_counts[name] = overrides[name]["count"]
        sizes[name] = size

    threads = facts["cpu"]["threads"]
    for name, size in sizes.items():
        if size["cores"] > threads:
            errors.append(f"{name}: needs {size['cores']} cores, the host has {threads} threads")

    reserve = model["host"]["reserve"]
    available = facts["memory_mib"] - reserve["memory_mib"]
    pools = [n for n in planned if planned[n]["kind"] == "vm-pool"]
    counts = {n: pinned_counts.get(n, 1) for n in pools}
    floor_total = sum(s["memory_mib"] * counts.get(n, 1) for n, s in sizes.items())
    if floor_total > available:
        errors.append(
            f"memory: planned guests need {floor_total} MiB at their floors; "
            f"the host has {facts['memory_mib']} MiB minus a {reserve['memory_mib']} MiB host reserve"
        )

    # Memory left after floors grows the pools whose count is not pinned, by
    # their grow weight, capped at one clone per `cores` host threads. Then
    # clones are taken back from the largest disk consumer until disk fits.
    leftover = max(available - floor_total, 0)
    growing = [n for n in pools if n not in pinned_counts]
    weight = sum(planned[n].get("grow", 1) for n in growing)
    limits = {n: "pinned in config.yaml" for n in pinned_counts}
    for name in growing:
        share = leftover * planned[name].get("grow", 1) // weight
        by_memory = 1 + share // sizes[name]["memory_mib"]
        by_cores = max(1, threads // sizes[name]["cores"])
        counts[name] = min(by_memory, by_cores)
        limits[name] = "limited by memory" if by_memory <= by_cores else "limited by host threads"

    def disk_needed() -> int:
        # Pools need a golden template on disk plus one disk per clone.
        return sum(s["disk_gib"] * (counts[n] + 1 if n in counts else 1) for n, s in sizes.items())

    storage = _storage(facts, errors)
    while storage and disk_needed() > storage["avail_gib"]:
        shrinkable = [n for n in growing if counts[n] > 1]
        if not shrinkable:
            break
        name = max(shrinkable, key=lambda n: (sizes[n]["disk_gib"] * counts[n], n))
        counts[name] -= 1
        limits[name] = "limited by disk"
    disk = disk_needed()
    if storage and disk > storage["avail_gib"]:
        errors.append(f"disk: guests need {disk} GiB, storage {storage['name']} has {storage['avail_gib']} GiB free")
    allocated = sum(s["memory_mib"] * counts.get(n, 1) for n, s in sizes.items())

    memory = {
        "host_mib": facts["memory_mib"],
        "reserve_mib": Note(reserve["memory_mib"], "Left to PVE itself; pin with host.reserve.memory_mib."),
        "guests_mib": allocated,
        "unallocated_mib": Note(facts["memory_mib"] - reserve["memory_mib"] - allocated, "Not assigned to any guest."),
    }
    disk_section = {
        "storage": storage["name"] if storage else None,
        "needed_gib": Note(disk, "Includes one golden template per pool."),
        "free_gib": storage["avail_gib"] if storage else None,
        "media": Note(_media(facts, errors), "Holds the downloaded container template and cloud image."),
    }
    pool_counts = {n: Note(counts[n], f"Clones, {limits[n]}.") for n in pools}
    return sizes, pool_counts, {"memory": memory, "disk": disk_section}


def _disks(model: dict, facts: dict, errors: _Errors) -> tuple[dict, str | None]:
    pins = model.get("disks", {})
    for disk_id in sorted(set(pins) - {d["id"] for d in facts["disks"]}):
        errors.append(f"disks.{disk_id}: no such disk in the probe")
    section: dict[str, Any] = {}
    pbs = None
    for disk in facts["disks"]:
        pin = pins.get(disk["id"])
        base = {"size_gib": disk["size_gib"], "signatures": disk["signatures"]}
        if disk["system"]:
            if pin:
                errors.append(f"disks.{disk['id']}: the PVE system disk cannot be pinned")
            section[disk["id"]] = Note({**base, "use": "system"}, "Holds PVE; never touched.")
            continue
        if not disk["stable_id"]:
            errors.append(f"disks.{disk['id']}: no /dev/disk/by-id name; refusing to plan an unstable disk name")
        if pin and pin["use"] == "ignore":
            section[disk["id"]] = Note({**base, "use": "ignore"}, "Pinned: left alone.")
        elif pin and pin["use"] == "pbs":
            if disk["signatures"] and not pin.get("wipe"):
                errors.append(f"disks.{disk['id']}: carries {', '.join(disk['signatures'])}; set wipe: true to let apply format it")
            if pbs:
                errors.append(f"disks.{disk['id']}: a second PBS disk; {pbs} is already pinned")
            pbs = disk["id"]
            why = "Pinned for PBS; apply will wipe its signatures." if disk["signatures"] else "Pinned for PBS."
            section[disk["id"]] = Note({**base, "use": "pbs"}, why)
        elif disk["signatures"]:
            why = "Carries signatures; only an explicit pin with wipe: true changes that."
            section[disk["id"]] = Note({**base, "use": "untouched"}, why)
        elif pbs is None and "pbs" not in {p["use"] for p in pins.values()}:
            pbs = disk["id"]
            section[disk["id"]] = Note({**base, "use": "pbs"}, "Blank, so it becomes the PBS datastore.")
        else:
            section[disk["id"]] = Note({**base, "use": "unused"}, "Blank and not needed.")
    return section, pbs


def _flows(model: dict, planned: dict, zones: set[str]) -> list[str]:
    flows = []
    for name, svc in planned.items():
        for port in svc.get("ports", []):
            for ref in port["from"]:
                kind, _, target = ref.partition(":")
                if kind == "zone" and target != "*" and target not in zones:
                    continue
                if kind == "service" and target not in planned:
                    continue
                flows.append(f"{ref} -> {name} {port['proto']}/{port['port']}")
    return flows


POOL_RANGE = "128/26"
HOST_ID = 254


def _host(zone: dict, host: int | str) -> str:
    """Address `host` (an id, or a 'start/prefix' range) inside a zone subnet."""
    net = ipaddress.ip_network(zone["subnet"])
    if isinstance(host, str):
        start, prefix = host.split("/")
        return str(ipaddress.ip_network((int(net.network_address) + int(start), int(prefix))))
    return str(net.network_address + host)


def _zones(model: dict, planned: dict) -> dict:
    base = ipaddress.ip_network(model["network"]["base"])
    names = [z for z in model["zones"] if z in ("access", "mgmt") or any(s["zone"] == z for s in planned.values())]
    zones = {}
    for name in names:
        index = model["zones"][name]["index"]
        subnet = ipaddress.ip_network((int(base.network_address) + (index << 8), 24))
        zones[name] = {
            "bridge": f"vmbr{100 + index}",
            "subnet": str(subnet),
            "gateway": str(subnet.network_address + 1),
            "egress": model["zones"][name].get("egress", "none"),
        }
    zones["mgmt"]["host_ip"] = str(ipaddress.ip_network(zones["mgmt"]["subnet"]).network_address + HOST_ID)
    return zones


def build_plan(model: dict, facts: dict) -> dict:
    errors = _Errors()
    _check_refs(model, errors)
    errors.raise_if_any()

    planned = _planned_services(model, errors)
    gpu_section, inference_gpus = _gpus(model, facts, planned, errors)
    if "inference" in planned and not inference_gpus:
        errors.append("intent asks for inference, but no host-shared GPU budgets VRAM to it")
    engine = _engine(model, inference_gpus) if "inference" in planned else None
    sizes, counts, capacity = _size(model, planned, facts, errors)
    disk_section, pbs = _disks(model, facts, errors)
    errors.raise_if_any()

    zones = _zones(model, planned)
    guests = {}
    vmid_base = model["network"]["vmid_base"]
    for name, svc in planned.items():
        entry = {"kind": svc["kind"], "zone": svc["zone"], **sizes[name]}
        if svc["kind"] == "vm-pool":
            entry["count"] = counts[name]
            entry["template_vmid"] = vmid_base + svc["id"]
            entry["clone_range"] = Note(_host(zones[svc["zone"]], POOL_RANGE), "Clones take DHCP leases from this range.")
        else:
            entry["vmid"] = vmid_base + svc["id"]
            entry["ip"] = _host(zones[svc["zone"]], svc["id"])
        if svc.get("attach") == "all-zones":
            entry["attach"] = "all-zones"
            entry["ips"] = Note({z: _host(zones[z], svc["id"]) for z in zones}, "One leg in every planned zone.")
            del entry["ip"]
        guests[name] = entry

    n_gpus = len([g for g in facts["gpus"] if not g["integrated"]])
    host_class = "small" if n_gpus == 0 else "mid" if n_gpus == 1 else "large"
    plan: dict[str, Any] = {
        "class": Note(host_class, "Follows the discrete GPU count: none, one, several."),
        "host": {"threads": facts["cpu"]["threads"], **capacity["memory"]},
        "disk": capacity["disk"],
        "domain": model["network"]["domain"],
        "zones": Note(zones, "Uplink-less bridges; the router guest routes between them."),
        "guests": guests,
        "gpus": gpu_section or Note({}, "No discrete GPU."),
        "inference": {"engine": engine} if engine else Note({}, "Not planned."),
        "disks": disk_section,
        "backups": (
            Note({"target": f"pbs datastore on {pbs}", "status": "ok"}, "PBS on its own disk.")
            if pbs
            else Note({"target": None, "status": "DEGRADED"}, "No separate disk for PBS; add a blank disk or pin one.")
        ),
        "flows": Note(_flows(model, planned, set(zones)), "Allowed flows; everything else is denied."),
        "people": {name: p["role"] for name, p in model["people"].items()},
    }
    plan["tailnet"] = check_tailnet(plan, model, errors)
    errors.raise_if_any()
    if any(svc["zone"] == "windows" for svc in planned.values()):
        plan["owner_supplies"] = ["Windows ISO", "Windows licence"]
    return plan
