"""Tailscale policy from people, roles and shared services, checked against
the free plan.

The vpn guest is a subnet router that masquerades every person to its own
address, so pve-firewall and the router cannot tell people apart. The
tailnet policy is therefore the only per-person gate. A person gets the
flows whose `from` names their role, and the `shared` flows of each service
in their own `services` list: nothing a role's `reach` does not cover, and
the PVE host and mgmt to the owner role only.

Names: Tailscale's split DNS sends the plan's domain to the router's address
in the access zone (apply's tailnet stage sets it), so every person's devices
may ask the router on port 53 and the vpn guest advertises that one address.
"""

import json

from nullbill.flows import HOST, SHARED, Flow, from_people, resolve_flows
from nullbill.render import Note, strip_notes

OWNER = "owner"
TAG = "tag:nb-vpn"
# A device the owner tags for a remote agent (docs/runbooks/agent-access.md)
# reaches the page's agent API and nothing else; the page still wants the
# agent's own token. An agent on the owner's own device goes as the owner.
AGENT_TAG = "tag:nb-agent"
PAGE_PORT = 8443


def group(role: str) -> str:
    return f"group:nb-{role}"


def _within_reach(reach: list[str], dest: str, zone: str | None) -> bool:
    return "zone:*" in reach or f"zone:{zone}" in reach or f"service:{dest}" in reach


def _people_flows(plan: dict, model: dict) -> list[Flow]:
    """Flows people originate: a role's, and those a service shares."""
    return [f for f in resolve_flows(plan, model) if from_people(f.ref)]


def _role_flows(plan: dict, model: dict) -> list[Flow]:
    return [f for f in _people_flows(plan, model) if f.ref != SHARED]


def _grouped(plan: dict, model: dict) -> list[str]:
    """Roles that get a group: held by someone and named by a flow. Tailscale
    refuses a policy that names a group it does not define, and the free plan
    counts groups, so a role without flows (friend) has none."""
    held = {p["role"] for p in model["people"].values()}
    named = {f.ref.partition(":")[2] for f in _role_flows(plan, model)}
    return sorted((held & named) | ({OWNER} & held))


def shared_ports(model: dict, service: str) -> list[dict]:
    """The ports of a service the owner may give to a person."""
    return [p for p in model["services"].get(service, {}).get("ports", []) if SHARED in p["from"]]


def person_flows(plan: dict, model: dict, name: str) -> list[Flow]:
    """What one person reaches, each port once: their role's flows first,
    then the shared flows of the services picked for them."""
    person = model["people"][name]
    flows = _people_flows(plan, model)
    mine = [f for f in flows if f.ref == f"role:{person['role']}"]
    mine += [f for f in flows if f.ref == SHARED and f.dest in person.get("services", [])]
    seen: set[tuple[str, str, int]] = set()
    out = []
    for flow in mine:
        key = (flow.dest, flow.proto, flow.port)
        if key not in seen:
            seen.add(key)
            out.append(flow)
    return out


def _check_people(model: dict, errors: list[str]) -> None:
    people = model["people"]
    if OWNER not in {p["role"] for p in people.values()}:
        errors.append(
            "people: no person has role owner; list yourself with your Tailscale login, or nobody can reach the host once the LAN is closed"
        )
    by_login: dict[str, str] = {}
    for name, person in people.items():
        # Tailscale compares logins without regard to case.
        first = by_login.setdefault(person["login"].lower(), name)
        if first != name:
            errors.append(f"people.{name}.login: {first} has the same Tailscale login; one person, one entry")
        for service in person.get("services", []):
            if service not in model["services"]:
                errors.append(f"people.{name}.services: there is no service {service}")
            elif not shared_ports(model, service):
                errors.append(
                    f"people.{name}.services: {service} has no port that lists shared in its from, so it cannot be given to a person"
                )


def check_tailnet(plan_with_notes: dict, model: dict, errors: list[str]) -> Note:
    """Append rule failures to errors; return the plan's tailnet section."""
    plan = strip_notes(plan_with_notes)
    people = model["people"]
    _check_people(model, errors)
    for flow in _people_flows(plan, model):
        role = flow.ref.partition(":")[2]
        zone = "mgmt" if flow.dest == HOST else plan["guests"][flow.dest]["zone"]
        label = "the PVE host" if flow.dest == HOST else flow.dest
        if zone == "mgmt" and role != OWNER:
            errors.append(f"{flow.ref} -> {label} {flow.proto}/{flow.port}: only the owner role reaches mgmt and the PVE host")
        elif flow.ref != SHARED and flow.dest != HOST and not _within_reach(model["roles"][role]["reach"], flow.dest, zone):
            errors.append(f"{flow.ref} -> {label} {flow.proto}/{flow.port}: outside roles.{role}.reach")
    limits = model["tailnet"]["free_limits"]
    used = {"users": len(people), "groups": len(_grouped(plan, model)), "tagged_devices": 1}
    for key, count in used.items():
        if count > limits[key]:
            errors.append(f"tailnet: {count} {key.replace('_', ' ')}, but Tailscale's free Personal plan allows {limits[key]}")
    return Note(
        {key: f"{count} of {limits[key]}" for key, count in used.items()},
        "Tailscale Personal: free for personal, non-commercial use. One group per role that reaches something; "
        "the vpn guest is the tagged device.",
    )


def _by_dest(flows: list[Flow]) -> dict[tuple[str, ...], list[str]]:
    out: dict[tuple[str, ...], list[str]] = {}
    for flow in flows:
        ports = out.setdefault(flow.dest_cidrs, [])
        if f"{flow.proto}:{flow.port}" not in ports:
            ports.append(f"{flow.proto}:{flow.port}")
    return out


def _grants(plan: dict, model: dict) -> list[dict]:
    grouped = _grouped(plan, model)
    by_dest: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for flow in _role_flows(plan, model):
        role = flow.ref.partition(":")[2]
        if role not in grouped:
            continue
        ports = by_dest.setdefault((role, flow.dest_cidrs), [])
        if f"{flow.proto}:{flow.port}" not in ports:
            ports.append(f"{flow.proto}:{flow.port}")
    grants = [{"src": [group(role)], "dst": list(dst), "ip": ports} for (role, dst), ports in by_dest.items()]
    # What the owner picked for one person, beyond that person's role: a
    # grant of their own, by login.
    for name, person in model["people"].items():
        if person["role"] == OWNER:
            continue
        own = [f for f in person_flows(plan, model, name) if f.ref == SHARED]
        grants += [{"src": [person["login"]], "dst": list(dst), "ip": ports} for dst, ports in _by_dest(own).items()]
    return grants


def nameserver(plan_with_notes: dict) -> str | None:
    """Where tailnet devices ask for the plan's names: the router's leg in
    the vpn guest's zone. Its dnsmasq answers there already (router.py)."""
    plan = strip_notes(plan_with_notes)
    if "vpn" not in plan["guests"]:
        return None
    return plan["zones"][plan["guests"]["vpn"]["zone"]]["gateway"]


def _page(plan: dict) -> str | None:
    """The page's address, when remote agents can reach it over the tailnet."""
    guests = plan["guests"]
    return guests["nullbill"]["ip"] if "nullbill" in guests and "vpn" in guests else None


def _tests(plan: dict, model: dict) -> list[dict]:
    """Assertions Tailscale evaluates on every policy push; a failing one
    rejects the push. One per person: what they reach, and for everyone but
    the owner that the host, the router and every port people can be given
    but this person was not are refused."""
    host = plan["zones"]["mgmt"]["host_ip"]
    # A non-owner must be refused the host and the router, both on mgmt.
    fixed = [f"{host}:8006", f"{host}:22", f"{plan['zones']['mgmt']['gateway']}:22"]
    page = _page(plan)
    every = {proto: _targets(_people_flows(plan, model), proto) for proto in ("tcp", "udp")}
    tests = []
    for name, person in model["people"].items():
        mine = person_flows(plan, model, name)
        for proto in ("tcp", "udp"):
            accept = _targets(mine, proto)
            # Only TCP is asserted where no person-facing port is UDP.
            if proto == "udp" and not every["udp"]:
                continue
            if nameserver(plan):
                accept.append(f"{nameserver(plan)}:53")
            test = {"src": person["login"], "proto": proto, "accept": sorted(set(accept))}
            if person["role"] != OWNER:
                first = fixed if proto == "tcp" else []
                test["deny"] = first + sorted(set(every[proto]) - set(accept) - set(first))
                if not test["deny"]:
                    del test["deny"]
            tests.append(test)
    if page:
        tests.append({"src": AGENT_TAG, "proto": "tcp", "accept": [f"{page}:{PAGE_PORT}"], "deny": fixed})
    return tests


def _targets(flows: list[Flow], proto: str) -> list[str]:
    return [f"{f.dest_cidrs[0]}:{f.port}" for f in flows if f.proto == proto]


def routes(plan_with_notes: dict, model: dict) -> list[str]:
    """Subnets the vpn guest advertises: each zone a flow from people ends
    in, whoever holds it, so giving a person a service changes the policy
    and nothing on the server. The grants decide who reaches which address."""
    plan = strip_notes(plan_with_notes)
    zones = {"mgmt" if f.dest == HOST else plan["guests"][f.dest]["zone"] for f in _people_flows(plan, model)}
    out = [plan["zones"][z]["subnet"] for z in plan["zones"] if z in zones]
    # The nameserver alone, not its zone: nothing else there is for people.
    if nameserver(plan) and plan["guests"]["vpn"]["zone"] not in zones:
        out.append(f"{nameserver(plan)}/32")
    return out


def render_tailnet(plan_with_notes: dict, model: dict) -> dict[str, str]:
    """Return {"policy.json": the tailnet policy file}."""
    plan = strip_notes(plan_with_notes)
    grouped = _grouped(plan, model)
    groups: dict[str, list[str]] = {}
    alone = []
    for person in model["people"].values():
        if person["role"] in grouped:
            groups.setdefault(group(person["role"]), []).append(person["login"])
        else:
            alone.append(person["login"])
    page = _page(plan)
    grants = _grants(plan, model)
    if nameserver(plan):
        grants.append({"src": sorted(groups) + sorted(alone), "dst": [nameserver(plan)], "ip": ["udp:53", "tcp:53"]})
    tag_owners = {TAG: [group(OWNER)]}
    if page:
        tag_owners[AGENT_TAG] = [group(OWNER)]
        grants.append({"src": [AGENT_TAG], "dst": [page], "ip": [f"tcp:{PAGE_PORT}"]})
    policy = {
        "groups": {g: sorted(m) for g, m in sorted(groups.items())},
        "tagOwners": tag_owners,
        "autoApprovers": {"routes": {model["network"]["base"]: [TAG]}},
        "grants": grants,
        "tests": _tests(plan, model),
    }
    return {"policy.json": json.dumps(policy, indent=2) + "\n"}
