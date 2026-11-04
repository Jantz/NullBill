"""Resolve the services' `ports` entries into concrete flows.

Both the pve-firewall files and the router ruleset are generated from this
one list, so they cannot disagree about what is allowed.
"""

from dataclasses import dataclass

PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
HOST = "pve-host"
HOST_PORTS = (8006, 22)
HOST_SET = "nb-host"


@dataclass(frozen=True)
class Flow:
    ref: str  # the `from` entry as written, e.g. zone:build
    source_set: str  # pve-firewall ipset holding the source addresses
    source_cidrs: tuple[str, ...]
    sources: tuple[str, ...]  # guests that originate the flow
    dest: str  # a guest name, or HOST
    dest_cidrs: tuple[str, ...]
    proto: str
    port: int


def svc_set(name: str) -> str:
    return f"nb-svc-{name}"


def addresses(guest: dict) -> list[str]:
    if "ips" in guest:
        return sorted(guest["ips"].values())
    return [guest.get("ip") or guest["clone_range"]]


def _resolve(ref: str, plan: dict, base: str) -> tuple[str, tuple[str, ...], tuple[str, ...]] | None:
    kind, _, name = ref.partition(":")
    guests = plan["guests"]
    if kind == "host":
        # The host sits on the mgmt bridge; no guest originates this flow.
        return HOST_SET, (plan["zones"]["mgmt"]["host_ip"],), ()
    if kind == "zone" and name == "*":
        return "nb-zones", (base,), tuple(guests)
    if kind == "zone":
        if name not in plan["zones"]:
            return None
        members = tuple(g for g, v in guests.items() if v["zone"] == name)
        return f"nb-zone-{name}", (plan["zones"][name]["subnet"],), members
    if kind == "role":
        # People arrive through the VPN subnet router, which masquerades them;
        # per-person limits live in the tailnet policy.
        name = "vpn"
    if name not in guests:
        return None
    return svc_set(name), tuple(addresses(guests[name])), (name,)


def resolve_flows(plan: dict, model: dict) -> list[Flow]:
    base = model["network"]["base"]
    flows = []
    for dest, svc in model["services"].items():
        if dest not in plan["guests"]:
            continue
        for port in svc.get("ports", []):
            for ref in port["from"]:
                resolved = _resolve(ref, plan, base)
                if resolved:
                    source_set, cidrs, sources = resolved
                    dest_cidrs = tuple(addresses(plan["guests"][dest]))
                    flows.append(Flow(ref, source_set, cidrs, sources, dest, dest_cidrs, port["proto"], port["port"]))
    if "vpn" in plan["guests"]:
        # The tailnet policy limits host access to the owner role.
        vpn = plan["guests"]["vpn"]
        host_ip = plan["zones"]["mgmt"]["host_ip"]
        for port in HOST_PORTS:
            flows.append(Flow("role:owner", svc_set("vpn"), (vpn["ip"],), ("vpn",), HOST, (host_ip,), "tcp", port))
    return flows


def mac(zone_index: int, host: int) -> str:
    """Deterministic, locally administered MAC for a guest leg in a zone.

    02:4e:42 marks NullBill (0x4e 0x42 is "NB"); index 0 is the router uplink.
    """
    return f"02:4e:42:00:{zone_index:02x}:{host:02x}"
