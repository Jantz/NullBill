"""Resolve the services' `ports` entries into concrete flows.

Both the pve-firewall files and the router ruleset are generated from this
one list, so they cannot disagree about what is allowed.
"""

from dataclasses import dataclass

PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")
HOST = "pve-host"
# The PVE web UI/API and SSH: verify checks the LAN is refused both.
HOST_PORTS = (8006, 22)
HOST_SET = "nb-host"
# tailscaled's UDP port on the vpn guest (ansible/roles/vpn sets it). Not
# Tailscale's default 41641: a PC at home running Tailscale holds that port on
# the home router, which then gives the vpn guest another one for every
# destination, and devices are told a port that does not answer. Two paths
# need the port by number:
# - out: from this port alone the vpn guest may reach private addresses past
#   the router, so a device on the home network talks to it directly instead
#   of through Tailscale's relays (DERP), which are slow for video;
# - in: the router passes what arrives for this port on its uplink to the vpn
#   guest, which accepts it. A device behind a mobile carrier's NAT cannot be
#   answered at the port Tailscale's servers saw it use; it has to reach the
#   server first, on a port the home router forwards (nullbill/direct.py).
TAILSCALE_PORT = 41642
# Where nb-portmap on the router hears home routers answer its UPnP search:
# the answer is a new packet from the home router, not a reply to the
# multicast search, so the router's input chain names the port.
SSDP_REPLY_PORT = 1901
# In a port's `from`: the people whose own `services` list names the port's
# service. Like a role, they arrive through the vpn guest, so the firewall
# opens the port to it and the tailnet policy decides who gets through.
SHARED = "shared"
# A zone's egress in the plan, where its guests install packages through
# the egress proxy: proxy, and tunnel (the torrents zone behind the owner's
# VPN, torrents.py), whose only other way out is that VPN's server.
PROXIED = ("proxy", "tunnel")


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


def from_people(ref: str) -> bool:
    """Whether a `from` entry names people, a role or `shared`, not guests."""
    return ref == SHARED or ref.startswith("role:")


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
    if from_people(ref):
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
    # The PVE host's own ports. Role sources arrive through the VPN, and the
    # tailnet policy limits them to the owner role.
    host_ip = plan["zones"]["mgmt"]["host_ip"]
    for port in model["host"].get("ports", []):
        for ref in port["from"]:
            resolved = _resolve(ref, plan, base) if ref != "host" else None
            if resolved:
                source_set, cidrs, sources = resolved
                flows.append(Flow(ref, source_set, cidrs, sources, HOST, (host_ip,), port["proto"], port["port"]))
    return flows


def mac(zone_index: int, host: int) -> str:
    """Deterministic, locally administered MAC for a guest leg in a zone.

    02:4e:42 marks NullBill (0x4e 0x42 is "NB"); index 0 is the router uplink.
    """
    return f"02:4e:42:00:{zone_index:02x}:{host:02x}"
