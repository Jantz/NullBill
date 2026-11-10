"""Turn probe facts and the merged model into a sized, commented plan.

Every rule that can fail collects its message; plan fails once, listing all
of them, so the owner sees every problem in one run.
"""

import ipaddress
from typing import Any

from nullbill import backups, ci, custom, games, legacy, media, power, torrents
from nullbill.errors import NullBillError
from nullbill.flows import SHARED
from nullbill.proxy import allows
from nullbill.render import Note, strip_notes
from nullbill.tailnet import check_tailnet

SIZE_KEYS = ("cores", "memory_mib", "disk_gib")


class _Errors(list):
    def raise_if_any(self) -> None:
        if self:
            raise NullBillError(*self)


# Planned by itself when a disk is kept and shared, never by intent.
FILES_WORKLOAD = "files"

# Smallstep's apt repository, which every guest with a certificate installs
# from, and the name it redirects to (catalog.yaml's proxy.allow).
SMALLSTEP_NAMES = ("packages.smallstep.com", "pkgs.infra.smallstep.com")


def _planned_services(model: dict, errors: _Errors, facts: dict | None = None) -> dict[str, dict]:
    workloads = set(model.get("intent", {}).get("workloads", []))
    known = {s["workload"] for s in model["services"].values()} - {"core"}
    for unknown in sorted(workloads - known):
        errors.append(f"intent.workloads: unknown workload {unknown!r}; known: {', '.join(sorted(known))}")
    if FILES_WORKLOAD in workloads:
        errors.append(f"intent.workloads: {FILES_WORKLOAD} is planned by itself once a disk is kept and shared (disks.<id>.use: legacy)")
        workloads.discard(FILES_WORKLOAD)
    pins = model.get("disks", {})
    if any(p["use"] == "legacy" and p.get("share", True) for p in pins.values()):
        workloads.add(FILES_WORKLOAD)
    # Copies of kept disks live in the files guest: those made already, and
    # those this apply makes before it converts a kept disk.
    kept = {d["id"] for d in (facts or {}).get("disks", []) if legacy.kept_mounts(d)}
    if (facts or {}).get("copies") or any(p.get("evacuate") and i in kept for i, p in pins.items()):
        workloads.add(FILES_WORKLOAD)
    return {
        name: svc
        for name, svc in model["services"].items()
        if svc.get("enabled", True) and (svc["workload"] == "core" or svc["workload"] in workloads)
    }


def planned_services(model: dict, facts: dict | None = None) -> dict[str, dict]:
    """The services a config plans, without saying what is wrong with it."""
    return _planned_services(model, _Errors(), facts)


def counted_mib(kind: str, memory_mib: int, share_pct: int, count: int = 1) -> int:
    """What a guest takes from the host's memory: all of a VM's (each clone
    of a pool), the container share of a container's limit."""
    if kind == "lxc":
        return memory_mib * share_pct // 100
    return memory_mib * count


def _check_refs(model: dict, errors: _Errors) -> None:
    kinds = {"zone": model["zones"], "service": model["services"], "role": model["roles"]}

    def check(ref: str, where: str) -> None:
        kind, _, name = ref.partition(":")
        if kind in ("host", SHARED):
            return
        if name != "*" and name not in kinds[kind]:
            errors.append(f"{where}: {ref} does not exist")

    for name, svc in model["services"].items():
        if svc["zone"] not in model["zones"]:
            errors.append(f"services.{name}.zone: zone {svc['zone']!r} does not exist")
        elif svc.get("agent_managed") and svc["zone"] in AGENT_FREE_ZONES:
            errors.append(f"services.{name}.agent_managed: {svc['zone']} guests stay out of the agent's reach")
        elif svc.get("proxy_allow") and (model["zones"][svc["zone"]]["egress"] != "proxy" or svc["kind"] == "vm-pool" or svc.get("attach")):
            errors.append(f"services.{name}.proxy_allow: only a single-address guest in an egress: proxy zone can have its own allow-list")
        for port in svc.get("ports", []):
            for ref in port["from"]:
                check(ref, f"services.{name}.ports")
    for port in model["host"].get("ports", []):
        for ref in port["from"]:
            check(ref, "host.ports")
    for name, role in model["roles"].items():
        for ref in role["reach"]:
            if ref == "host":
                errors.append(f"roles.{name}.reach: host is only a flow source; the owner role reaches the host through the VPN")
            if ref == SHARED:
                errors.append(f"roles.{name}.reach: shared is only a flow source; give a person services under people instead")
            check(ref, f"roles.{name}.reach")
    for name, person in model["people"].items():
        if person["role"] not in model["roles"]:
            errors.append(f"people.{name}.role: role {person['role']!r} does not exist")
    public = model["verify"]["public_name"]
    if not allows(model["proxy"]["allow"], public):
        errors.append(f"verify.public_name: {public} is not on proxy.allow, so verify could not prove the proxy works")
    for name in SMALLSTEP_NAMES:
        if not allows(model["proxy"]["allow"], name):
            errors.append(f"proxy.allow: {name} is missing; guests behind the proxy install step-ca and step-cli from it")
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
        # VRAM matters only to what budgets it: inference on a host-shared GPU.
        # Without a driver on the host the probe cannot read it, and an unused
        # GPU must not stop the plan.
        if vram is None and not owner.startswith("vm:") and ("inference" in planned or "consumers" in pin):
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
            # What a PVE PCI mapping names the device by (tofu.py).
            entry |= {"pci_id": gpu["id"], "iommu_group": gpu["iommu_group"]}
            section[gpu["pci"]] = Note(entry, f"Passed through whole to {vm} on request, all its functions together.")
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
            # The host driver and the container's user space are NVIDIA's
            # (ansible/roles/nvidia_host, inference).
            if gpu["vendor"] != "nvidia":
                errors.append(f"gpus.{gpu['pci']}: local models run on NVIDIA cards only so far; {gpu['name']} is {gpu['vendor']}")
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


def _model(model: dict, gpus: dict, inference_gpus: list[str], errors: _Errors) -> Note | None:
    """The local model: pinned, or the largest from the catalog's models
    that fits the VRAM the inference GPUs budget to inference."""
    budget = sum(gpus[pci].value["consumers"].get("inference", 0) for pci in inference_gpus)
    models = model["models"]
    pinned = model.get("inference", {}).get("model")
    if pinned:
        if pinned not in models:
            errors.append(f"inference.model: {pinned!r} is not in models; known: {', '.join(sorted(models))}")
            return None
        if models[pinned]["vram_mib"] > budget:
            errors.append(f"inference.model: {pinned} needs {models[pinned]['vram_mib']} MiB of VRAM, inference has {budget} MiB")
        return Note(pinned, "Pinned in config.yaml.")
    fits = [name for name, m in models.items() if m["vram_mib"] <= budget]
    if not fits:
        errors.append(f"inference: no model in models fits the {budget} MiB of VRAM inference has")
        return None
    best = max(fits, key=lambda name: (models[name]["vram_mib"], name))
    return Note(best, f"The largest of the catalog's models that fits the {budget} MiB of VRAM inference has.")


def guest_storage(facts: dict) -> dict | None:
    """The storage the guests' disks go on: the roomiest one that holds both."""
    usable = [s for s in facts["storage"] if {"images", "rootdir"} <= set(s["content"])]
    return max(usable, key=lambda s: (s["avail_gib"], s["name"])) if usable else None


def _storage(facts: dict, errors: _Errors) -> dict | None:
    storage = guest_storage(facts)
    if storage is None:
        errors.append("probe: no active storage holds both VM images and container root disks")
    return storage


def _media(facts: dict, errors: _Errors) -> str | None:
    """Storage for downloaded container templates and disk images."""
    usable = [s for s in facts["storage"] if {"iso", "vztmpl"} <= set(s["content"])]
    if not usable:
        errors.append("probe: no active storage holds both ISO images and container templates")
        return None
    return max(usable, key=lambda s: (s["avail_gib"], s["name"]))["name"]


def _size(
    model: dict, planned: dict, facts: dict, errors: _Errors, on_demand: dict[str, list[str]] | None = None
) -> tuple[dict, dict[str, Note], dict]:
    """on_demand: the workloads that run on demand, each with its guests
    (power.groups). They share what the guests that always run leave, so
    each has to fit there by itself, not all of them at once."""
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

    reserve_mib, reserve_why = reserve(model, facts)
    available = facts["memory_mib"] - reserve_mib
    pools = [n for n in planned if planned[n]["kind"] == "vm-pool"]
    counts = {n: pinned_counts.get(n, 1) for n in pools}
    container_pct = model["host"].get("container_share_pct", 100)

    def counted(name: str, size: dict) -> int:
        return counted_mib(planned[name]["kind"], size["memory_mib"], container_pct, counts.get(name, 1))

    on_demand = on_demand or {}
    parked = {n for members in on_demand.values() for n in members}
    floor_total = sum(counted(n, s) for n, s in sizes.items())
    always_floor = sum(counted(n, s) for n, s in sizes.items() if n not in parked)
    has = f"the host has {facts['memory_mib']} MiB minus a {reserve_mib} MiB host reserve"
    if always_floor > available:
        which = "guests that always run" if parked else "planned guests"
        errors.append(f"memory: {which} need {always_floor} MiB at their floors (containers at {container_pct}% of their limit); {has}")
    else:
        for workload, members in on_demand.items():
            needs = sum(counted(n, sizes[n]) for n in members)
            if needs > available - always_floor:
                errors.append(
                    f"memory: {model['power'][workload]['title']} needs {needs} MiB while it runs, and with everything else "
                    f"on demand off {available - always_floor} MiB is left beside the guests that always run ({has})"
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
    # Copies of kept disks this apply makes stay on their storage for good.
    reserved = legacy.reserved_gib(model, facts).get(storage["name"], 0) if storage else 0
    room = storage["avail_gib"] - reserved if storage else 0
    # Without a disk of its own, PBS keeps the backups in a volume beside the
    # guests. It comes before a pool's further clones, after every floor.
    datastore = None
    if storage and "pbs" in planned and not backups.separate_disk(model, facts):
        floors = sum(s["disk_gib"] * (2 if n in counts else 1) for n, s in sizes.items())
        datastore = backups.same_disk_gib(model, room - floors)
        room -= datastore or 0
    while storage and disk_needed() > room:
        shrinkable = [n for n in growing if counts[n] > 1]
        if not shrinkable:
            break
        name = max(shrinkable, key=lambda n: (sizes[n]["disk_gib"] * counts[n], n))
        counts[name] -= 1
        limits[name] = "limited by disk"
    for name in pools:
        if counts[name] > ci.MAX_CLONES:
            errors.append(f"{name}: {counts[name]} clones; its zone's DHCP range has {ci.MAX_CLONES} leases")
    disk = disk_needed()
    if storage and disk > room:
        copies = f" besides {reserved} GiB for copies of kept disks" if reserved else ""
        store = f" besides {datastore} GiB for the backups (backups.size_gib)" if datastore else ""
        errors.append(f"disk: guests need {disk} GiB, storage {storage['name']} has {room} GiB free{copies}{store}")
    allocated = sum(counted(n, s) for n, s in sizes.items() if n not in parked)
    limits_total = sum(s["memory_mib"] * counts.get(n, 1) for n, s in sizes.items())
    group_mib = {w: sum(counted(n, sizes[n]) for n in members) for w, members in on_demand.items()}

    counting = f"VMs in full, containers at {container_pct}% of their limit."
    memory = {
        "host_mib": facts["memory_mib"],
        "reserve_mib": Note(reserve_mib, reserve_why),
        "guests_mib": Note(allocated, counting + (" Only the guests that always run." if parked else "")),
        "limits_mib": Note(limits_total, "Every guest's memory limit added up; containers take theirs only as they use it."),
        "unallocated_mib": Note(
            facts["memory_mib"] - reserve_mib - allocated,
            "Shared by what runs on demand (power)." if parked else "Not assigned to any guest.",
        ),
    }
    disk_section = {
        "storage": storage["name"] if storage else None,
        "needed_gib": Note(disk, "Includes one golden template per pool."),
        "free_gib": storage["avail_gib"] if storage else None,
        **({"copies_gib": Note(reserved, "Taken by the copies of kept disks apply makes before converting them.")} if reserved else {}),
        **(
            {"backups_gib": Note(datastore, "The backups, in a volume beside the guests: PBS has no disk of its own.")} if datastore else {}
        ),
        "media": Note(_media(facts, errors), "Holds the downloaded container template and cloud image."),
    }
    pool_counts = {n: Note(counts[n], f"Clones, {limits[n]}.") for n in pools}
    return sizes, pool_counts, {"memory": memory, "disk": disk_section, "on_demand_mib": group_mib}


def reserve(model: dict, facts: dict) -> tuple[int, str]:
    """Memory left to PVE: less on small hosts, plus ZFS's ARC where ZFS is in use."""
    host = model["host"]
    small = host.get("reserve_small")
    if small and facts["memory_mib"] < small["below_mib"]:
        mib = small["memory_mib"]
        why = f"Left to PVE itself on a host under {small['below_mib'] // 1024} GiB; pin with host.reserve_small."
    else:
        mib = host["reserve"]["memory_mib"]
        why = "Left to PVE itself; pin with host.reserve.memory_mib."
    if any(s["type"] == "zfspool" for s in facts["storage"]):
        # PVE's installer caps the ARC at 10% of memory.
        mib += facts["memory_mib"] // 10
        why += " Includes ZFS's cache, 10% of memory."
    if host["console"]["enabled"]:
        mib += host["console"]["memory_mib"]
        why += " Includes the on-site screen."
    return mib, why


def _console(model: dict, facts: dict, gpus: dict, errors: _Errors) -> Any:
    """The on-site screen: a kiosk on the host's display, if one is left to it."""
    if not model["host"]["console"]["enabled"]:
        return Note({"enabled": False}, "Off. host.console.enabled shows the NullBill page on a monitor plugged into the server.")
    integrated = [g["name"] for g in facts["gpus"] if g["integrated"]]
    shared = [entry.value["name"] for entry in gpus.values() if entry.value["owner"] == "host-shared"]
    if not integrated and not shared:
        errors.append("host.console: every GPU is passed through to a VM, so nothing is left to drive a screen")
        return None
    screen = integrated[0] if integrated else shared[0]
    return Note(
        {"enabled": True, "screen": screen},
        "Plug the monitor, keyboard and mouse into the server; this GPU drives the screen. Ctrl+Alt+F2 opens the PVE text console.",
    )


def _disks(model: dict, facts: dict, errors: _Errors, want_pbs: bool, capacity: dict | None = None) -> tuple[dict, str | None]:
    pins = model.get("disks", {})
    for disk_id in sorted(set(pins) - {d["id"] for d in facts["disks"]}):
        errors.append(f"disks.{disk_id}: no such disk in the probe")
    section: dict[str, Any] = {}
    pbs = None
    # Share names: kept partitions and copies of converted ones share one
    # namespace in the files guest.
    names: dict[str, str] = {c["name"]: f"the copy {c['volume']}" for c in facts.get("copies", [])}
    storages: dict[str, str] = {}
    # A disk apply made the PBS disk (nb-backup-disk) holds its volume group;
    # LVM's signature on it is apply's own, so it stays the PBS disk.
    made_pbs = next((d["id"] for d in facts["disks"] if backups.DISK_STORAGE in d.get("vgs", [])), None)
    for disk in facts["disks"]:
        pin = pins.get(disk["id"])
        # LVM's signature on a disk apply made PBS or storage needs no wipe.
        if pin is None or pin["use"] == "pbs":
            own_vg = disk["id"] == made_pbs
        else:
            own_vg = pin["use"] == "storage" and legacy.storage_name(pin) in disk.get("vgs", [])
        base = {"size_gib": disk["size_gib"], "signatures": disk["signatures"]}
        if disk["system"]:
            if pin:
                errors.append(f"disks.{disk['id']}: the PVE system disk cannot be pinned")
            section[disk["id"]] = Note({**base, "use": "system"}, "Holds PVE; never touched.")
            continue
        base["health"] = disk["smart"]["health"]
        health = _health_note(disk["smart"])
        if not disk["stable_id"]:
            errors.append(f"disks.{disk['id']}: no /dev/disk/by-id name; refusing to plan an unstable disk name")
        if pin and pin["use"] != "legacy":
            for key in legacy.LEGACY_KEYS:
                if key in pin:
                    errors.append(f"disks.{disk['id']}.{key}: only for use: legacy")
        if pin and pin["use"] != "storage":
            for key in legacy.STORAGE_KEYS:
                if key in pin:
                    errors.append(f"disks.{disk['id']}.{key}: only for use: storage")
        if pin and pin["use"] not in legacy.CONVERT_TO:
            for key in legacy.CONVERT_KEYS:
                if key in pin:
                    errors.append(f"disks.{disk['id']}.{key}: only for converting a kept disk to {' or '.join(legacy.CONVERT_TO)}")
        kept = legacy.kept_mounts(disk)
        converts = None
        if kept and pin and pin["use"] not in ("legacy", "ignore"):
            converts = legacy.plan_convert(disk, pin, facts, capacity or {}, errors)
            for c in converts.get("copies", []):
                if c["name"] in names:
                    errors.append(f"disks.{disk['id']}: the copy {c['name']!r} would take a share name {names[c['name']]} uses")
                names[c["name"]] = disk["id"]
        if pin and pin["use"] == "legacy":
            mounts, left = legacy.plan_disk(disk["id"], disk, pin, errors)
            for m in mounts:
                name = m["mount"].rsplit("/", 1)[1]
                if name in names:
                    errors.append(f"disks.{disk['id']}: share name {name!r} is also used on {names[name]}; set disks.<id>.name")
                names[name] = disk["id"]
            section[disk["id"]] = Note({**base, "use": "legacy", "partitions": mounts}, legacy.note(mounts, left) + health)
        elif pin and pin["use"] == "ignore":
            section[disk["id"]] = Note({**base, "use": "ignore"}, "Pinned: left alone." + health)
        elif pin and pin["use"] == "pbs" and not want_pbs:
            errors.append(f"disks.{disk['id']}: pinned for PBS, but PBS is switched off")
        elif pin and pin["use"] == "pbs":
            if disk["signatures"] and not pin.get("wipe") and not own_vg:
                errors.append(f"disks.{disk['id']}: carries {', '.join(disk['signatures'])}; set wipe: true to let apply format it")
            if pbs:
                errors.append(f"disks.{disk['id']}: a second PBS disk; {pbs} is already pinned")
            if made_pbs and made_pbs != disk["id"]:
                errors.append(f"disks.{disk['id']}: pinned for PBS, but {made_pbs} holds the PBS volume group {backups.DISK_STORAGE}")
            pbs = disk["id"]
            if own_vg:
                why = f"Pinned for PBS; it holds the PBS volume group {backups.DISK_STORAGE} already."
            else:
                why = "Pinned for PBS; apply will wipe its signatures." if disk["signatures"] else "Pinned for PBS."
            entry = {**base, "use": "pbs"}
            if converts:
                entry["converts"] = converts
                why = legacy.convert_note(converts) + " " + why
            section[disk["id"]] = Note(entry, why + health)
        elif pin and pin["use"] == "storage":
            name = legacy.storage_name(pin)
            if disk["signatures"] and not pin.get("wipe") and not own_vg:
                errors.append(f"disks.{disk['id']}: carries {', '.join(disk['signatures'])}; set wipe: true to let apply format it")
            if name in storages:
                errors.append(f"disks.{disk['id']}: storage {name} is also pinned for {storages[name]}; set disks.<id>.storage")
            storages[name] = disk["id"]
            other = next((st for st in facts["storage"] if st["name"] == name and st["type"] != "lvmthin"), None)
            if name == backups.DISK_STORAGE or other:
                errors.append(f"disks.{disk['id']}.storage: {name} is taken by another PVE storage; choose another name")
            why = f"Pinned as the LVM-thin PVE storage {name}, {legacy.storage_room_gib(disk['size_gib'])} GiB for volumes."
            why += " Guests stay on the system storage; move a volume there in PVE when you want."
            if own_vg:
                why += f" It holds the volume group {name} already."
            elif disk["signatures"]:
                why += " apply will wipe its signatures."
            entry = {**base, "use": "storage", "storage": name}
            if converts:
                entry["converts"] = converts
                why = legacy.convert_note(converts) + " " + why
            section[disk["id"]] = Note(entry, why + health)
        elif own_vg and want_pbs and pbs is None and "pbs" not in {p["use"] for p in pins.values()}:
            pbs = disk["id"]
            why = f"Holds the PBS volume group {backups.DISK_STORAGE} apply made, so it stays the PBS datastore."
            section[disk["id"]] = Note({**base, "use": "pbs"}, why + health)
        elif disk["signatures"]:
            why = "Carries signatures; only an explicit pin with wipe: true changes that."
            section[disk["id"]] = Note({**base, "use": "untouched"}, why + health)
        elif want_pbs and pbs is None and made_pbs is None and "pbs" not in {p["use"] for p in pins.values()}:
            pbs = disk["id"]
            section[disk["id"]] = Note({**base, "use": "pbs"}, "Blank, so it becomes the PBS datastore." + health)
        else:
            section[disk["id"]] = Note({**base, "use": "unused"}, "Blank and not needed." + health)
    return section, pbs


def _copies(facts: dict) -> dict[str, Note]:
    """Copies of converted kept disks; no key at all until there is one."""
    if not facts.get("copies"):
        return {}
    copies = [{k: c[k] for k in ("name", "volume", "size_gib", "files", "bytes", "disk")} for c in facts["copies"]]
    return {"copies": Note(copies, "Copies of converted kept disks, shared from the files guest and backed up.")}


def _health_note(smart: dict) -> str:
    if smart["health"] == "FAILED":
        return " SMART says this disk is failing: copy anything on it elsewhere now."
    if smart["health"] == "unknown":
        return " SMART health could not be read."
    if smart.get("reallocated_sectors"):
        return f" SMART: {smart['reallocated_sectors']} reallocated sectors, an early sign of wear."
    return ""


def _backups(model: dict, planned: dict, pbs: str | None, disks: dict, disk: dict) -> Note:
    """Where the backups go. On a disk of PBS's own the datastore volume
    takes the disk, less a GiB for LVM's metadata; else it is the volume
    _size made room for beside the guests, and the plan says what that
    does not protect from."""
    if "pbs" not in planned:
        off = {"target": None, "status": backups.DISABLED}
        return Note(off, "PBS is switched off (services.pbs.enabled: false); nothing is backed up.")
    left_out = [n for n, svc in planned.items() if svc.get("backup") is False and n != "pbs" and svc["kind"] != "vm-pool"]
    rest = {"left_out": left_out} if left_out else {}
    why = f" Not backed up: {', '.join(left_out)}, which setup builds again." if left_out else ""
    pinned = model.get("backups", {}).get("size_gib")
    if pbs:
        size = strip_notes(disks[pbs])["size_gib"] - 1
        entry = {"target": f"pbs datastore on {pbs}", "status": backups.OK, "storage": backups.DISK_STORAGE, "size_gib": size}
        unused = " backups.size_gib is not used while PBS has a disk." if pinned else ""
        return Note(entry | rest, "PBS on its own disk." + unused + why)
    size = strip_notes(disk).get("backups_gib")
    if size is None:
        none = {"target": None, "status": backups.NO_ROOM}
        return Note(none, f"No disk for PBS, and under {backups.MIN_GIB} GiB left beside the guests; add a blank disk or pin one.")
    storage = disk["storage"]
    entry = {"target": f"pbs datastore on {storage}, beside the guests", "status": backups.SAME_DISK, "storage": storage, "size_gib": size}
    how = "Pinned (backups.size_gib)." if pinned else "Set backups.size_gib for another size; it can grow, not shrink."
    return Note(
        entry | rest,
        f"PBS has no disk of its own: {backups.SAME_DISK_WHY}. A blank disk plugged in later takes them over, history included. {how}{why}",
    )


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
    for port in model["host"].get("ports", []):
        for ref in port["from"]:
            if ref.startswith("service:") and ref.partition(":")[2] not in planned:
                continue
            flows.append(f"{ref} -> the PVE host {port['proto']}/{port['port']}")
    return flows


POOL_RANGE = "128/26"
# The router, PBS, the CA, the operator and the VPN live here.
AGENT_FREE_ZONES = ("mgmt", "access")
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


def _updates(model: dict, guests: dict) -> dict:
    section: dict[str, Any] = {
        "security": Note("nightly", "Debian security updates install by themselves on the host and every Linux guest."),
        "reboot_at": Note(model["updates"]["reboot_time"], "When a guest whose update needs a reboot reboots."),
        "owner_reboots": Note(
            ["host", *(name for name, g in guests.items() if g.get("agent_managed") and g["kind"] != "vm-pool")],
            "These wait for the owner: the host takes every guest down, agents run long jobs. Monitoring reminds.",
        ),
    }
    windows = [name for name, g in guests.items() if g["zone"] == "windows"]
    if windows:
        section["not_managed"] = Note(windows, "Windows Update is not managed by NullBill yet.")
    return section


def build_plan(model: dict, facts: dict) -> dict:
    errors = _Errors()
    _check_refs(model, errors)
    errors.raise_if_any()

    planned = _planned_services(model, errors, facts)
    gpu_section, inference_gpus = _gpus(model, facts, planned, errors)
    if "inference" in planned and not inference_gpus:
        errors.append("intent asks for inference, but no host-shared GPU budgets VRAM to it")
    engine = _engine(model, inference_gpus) if "inference" in planned else None
    llm = _model(model, gpu_section, inference_gpus, errors) if "inference" in planned and inference_gpus else None
    on_demand = power.groups(model, planned, errors)
    errors.extend(custom.power_problems(model, planned))
    sizes, counts, capacity = _size(model, planned, facts, errors, on_demand)
    disk_section, pbs = _disks(model, facts, errors, want_pbs="pbs" in planned, capacity=capacity["disk"])
    errors.raise_if_any()

    console = _console(model, facts, gpu_section, errors)
    kept = legacy.shares(strip_notes({"disks": disk_section}))
    shares = [m["share"] for m in kept]
    media_section = media.plan_section(model, facts, planned, shares, errors)
    torrents_section = torrents.plan_section(model, planned, kept, media.JELLYFIN in planned, errors)
    games_section = games.plan_section(model, planned, sizes, errors)
    custom_section = custom.plan_section(model, planned)
    errors.raise_if_any()

    zones = _zones(model, planned)
    if strip_notes(torrents_section).get("route") == torrents.VPN:
        # Out to the VPN's server alone, not to the internet (torrents.py).
        zones[torrents.ZONE]["egress"] = torrents.TUNNEL
    guests = {}
    vmid_base = model["network"]["vmid_base"]
    for name, svc in planned.items():
        entry = {"kind": svc["kind"], "zone": svc["zone"], **sizes[name]}
        if svc["kind"] == "vm-pool":
            entry["count"] = counts[name]
            entry["template_vmid"] = vmid_base + svc["id"]
            entry["clone_range"] = Note(_host(zones[svc["zone"]], POOL_RANGE), "Clones take DHCP leases from this range.")
            entry["template_ip"] = Note(
                _host(zones[svc["zone"]], ci.TEMPLATE_HOST), "The golden image's reserved lease while apply bakes it."
            )
            entry["clone_vmids"] = Note(
                ci.clone_vmids(vmid_base, svc["id"], counts[name].value), "The dispatcher clones one per queued job into these."
            )
        else:
            entry["vmid"] = vmid_base + svc["id"]
            entry["ip"] = _host(zones[svc["zone"]], svc["id"])
        if svc.get("attach") == "all-zones":
            entry["attach"] = "all-zones"
            entry["ips"] = Note({z: _host(zones[z], svc["id"]) for z in zones}, "One leg in every planned zone.")
            del entry["ip"]
        if svc.get("agent_managed"):
            entry["agent_managed"] = True
        if svc["workload"] in on_demand:
            entry["run"] = Note(power.ON_DEMAND, "Off until started on the page; it does not start with the server (power).")
        if svc.get("installed"):
            entry["installed"] = Note(True, "Made by install.sh; apply adds its mgmt leg (docs/adr/0006-nullbill-on-the-server.md).")
        guests[name] = entry

    demand = power.section(model, on_demand, capacity["on_demand_mib"], capacity["memory"]["unallocated_mib"].value)
    n_gpus = len([g for g in facts["gpus"] if not g["integrated"]])
    host_class = "small" if n_gpus == 0 else "mid" if n_gpus == 1 else "large"
    plan: dict[str, Any] = {
        "class": Note(host_class, "Follows the discrete GPU count: none, one, several."),
        "host": {"threads": facts["cpu"]["threads"], **capacity["memory"], "console": console},
        "disk": capacity["disk"],
        "domain": model["network"]["domain"],
        "zones": Note(zones, "Uplink-less bridges; the router guest routes between them."),
        "guests": guests,
        "gpus": gpu_section or Note({}, "No discrete GPU."),
        "inference": {
            "engine": engine,
            "model": llm,
            "model_mib": Note(model["models"][llm.value]["size_mib"], "The model file; verify expects most of it in VRAM."),
            "gpus": Note(inference_gpus, "Host-shared GPUs that budget VRAM to inference."),
        }
        if engine and llm
        else Note({}, "Not planned."),
        "media": media_section,
        "games": games_section,
        "torrents": torrents_section,
        **({"custom": custom_section} if custom_section else {}),
        **({"power": demand} if demand else {}),
        "disks": disk_section,
        **_copies(facts),
        "backups": _backups(model, planned, pbs, disk_section, capacity["disk"]),
        "flows": Note(_flows(model, planned, set(zones)), "Allowed flows; everything else is denied."),
        "people": {
            name: {"role": p["role"], "services": p["services"]} if p.get("services") else p["role"] for name, p in model["people"].items()
        },
        "updates": _updates(model, guests),
    }
    plan["tailnet"] = check_tailnet(plan, model, errors)
    errors.raise_if_any()
    if any(svc["zone"] == "windows" for svc in planned.values()):
        plan["owner_supplies"] = ["Windows ISO", "Windows licence"]
    return plan
