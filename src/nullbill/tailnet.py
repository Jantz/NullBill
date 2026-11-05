"""Tailscale policy from people and roles, checked against the free plan.

The vpn guest is a subnet router that masquerades every person to its own
address, so pve-firewall and the router cannot tell people apart. The
tailnet policy is therefore the only per-person gate, and it grants exactly
the flows whose `from` names a role: nothing a role's `reach` does not
cover, and the PVE host and mgmt to the owner role only.
"""

import json

from nullbill.flows import HOST, resolve_flows
from nullbill.render import Note, strip_notes

OWNER = "owner"
TAG = "tag:nb-vpn"


def group(role: str) -> str:
    return f"group:nb-{role}"


def _within_reach(reach: list[str], dest: str, zone: str | None) -> bool:
    return "zone:*" in reach or f"zone:{zone}" in reach or f"service:{dest}" in reach


def _role_flows(plan: dict, model: dict) -> list:
    return [f for f in resolve_flows(plan, model) if f.ref.startswith("role:")]


def check_tailnet(plan_with_notes: dict, model: dict, errors: list[str]) -> Note:
    """Append rule failures to errors; return the plan's tailnet section."""
    plan = strip_notes(plan_with_notes)
    people = model["people"]
    roles_used = sorted({p["role"] for p in people.values()})
    if OWNER not in roles_used:
        errors.append(
            "people: no person has role owner; list yourself with your Tailscale login, or nobody can reach the host once the LAN is closed"
        )
    for flow in _role_flows(plan, model):
        role = flow.ref.partition(":")[2]
        zone = "mgmt" if flow.dest == HOST else plan["guests"][flow.dest]["zone"]
        label = "the PVE host" if flow.dest == HOST else flow.dest
        if zone == "mgmt" and role != OWNER:
            errors.append(f"{flow.ref} -> {label} {flow.proto}/{flow.port}: only the owner role reaches mgmt and the PVE host")
        elif flow.dest != HOST and not _within_reach(model["roles"][role]["reach"], flow.dest, zone):
            errors.append(f"{flow.ref} -> {label} {flow.proto}/{flow.port}: outside roles.{role}.reach")
    limits = model["tailnet"]["free_limits"]
    used = {"users": len(people), "groups": len(roles_used), "tagged_devices": 1}
    for key, count in used.items():
        if count > limits[key]:
            errors.append(f"tailnet: {count} {key.replace('_', ' ')}, but Tailscale's free Personal plan allows {limits[key]}")
    return Note(
        {key: f"{count} of {limits[key]}" for key, count in used.items()},
        "Tailscale Personal: free for personal, non-commercial use. One group per role; the vpn guest is the tagged device.",
    )


def _grants(plan: dict, model: dict) -> list[dict]:
    by_dest: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for flow in _role_flows(plan, model):
        ports = by_dest.setdefault((flow.ref.partition(":")[2], flow.dest_cidrs), [])
        if f"{flow.proto}:{flow.port}" not in ports:
            ports.append(f"{flow.proto}:{flow.port}")
    return [{"src": [group(role)], "dst": list(dst), "ip": ports} for (role, dst), ports in by_dest.items()]


def _tests(plan: dict, model: dict) -> list[dict]:
    """Assertions Tailscale evaluates on every policy push; a failing one rejects the push."""
    host = plan["zones"]["mgmt"]["host_ip"]
    # A non-owner must be refused the host and the router, both on mgmt.
    denied = [f"{host}:8006", f"{host}:22", f"{plan['zones']['mgmt']['gateway']}:22"]
    tests = []
    for role in sorted({p["role"] for p in model["people"].values()}):
        login = next(p["login"] for p in model["people"].values() if p["role"] == role)
        accept = [f"{f.dest_cidrs[0]}:{f.port}" for f in _role_flows(plan, model) if f.ref == f"role:{role}" and f.proto == "tcp"]
        test = {"src": login, "proto": "tcp", "accept": sorted(set(accept))}
        if role != OWNER:
            test["deny"] = denied
        tests.append(test)
    return tests


def routes(plan_with_notes: dict, model: dict) -> list[str]:
    """Subnets the vpn guest advertises: each zone a role's flow ends in. The
    grants above still decide which person reaches which address in them."""
    plan = strip_notes(plan_with_notes)
    zones = {"mgmt" if f.dest == HOST else plan["guests"][f.dest]["zone"] for f in _role_flows(plan, model)}
    return [plan["zones"][z]["subnet"] for z in plan["zones"] if z in zones]


def render_tailnet(plan_with_notes: dict, model: dict) -> dict[str, str]:
    """Return {"policy.json": the tailnet policy file}."""
    plan = strip_notes(plan_with_notes)
    groups: dict[str, list[str]] = {}
    for person in model["people"].values():
        groups.setdefault(group(person["role"]), []).append(person["login"])
    policy = {
        "groups": {g: sorted(m) for g, m in sorted(groups.items())},
        "tagOwners": {TAG: [group(OWNER)]},
        "autoApprovers": {"routes": {model["network"]["base"]: [TAG]}},
        "grants": _grants(plan, model),
        "tests": _tests(plan, model),
    }
    return {"policy.json": json.dumps(policy, indent=2) + "\n"}
