"""verify: prove from inside each zone and from the LAN that the flows the
plan allows pass and everything else is refused.

Checks are built from the plan, so a service added by YAML is verified the
same way as the catalog's. They run over SSH as `pct exec` in one container
per zone; backups.py adds backup age and a restore test. A check that cannot
run reports SKIP, never PASS.
"""

import shlex
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass

from nullbill import agent, backups, ci, forge, lan, legacy
from nullbill.errors import NullBillError
from nullbill.flows import HOST, HOST_PORTS, resolve_flows
from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

TIMEOUT_S = 3
Connect = Callable[[str, int], str]  # -> "open", "refused" or "timeout"
Resolve = Callable[[str], str]


@dataclass(frozen=True)
class Probe:
    """One TCP connection attempt from a container, or a CONNECT through the proxy."""

    name: str
    vmid: int
    command: str
    expect: str  # "open", "closed", or for proxy checks the HTTP status class "2" or "4"
    kind: str = "tcp"
    # catalog's listener: what puts the program behind the port in place,
    # when NullBill does not; nothing answering is then DEGRADED, not FAIL.
    listener: str = ""


def _tcp(ip: str, port: int) -> str:
    return f"timeout {TIMEOUT_S} bash -c '</dev/tcp/{ip}/{port}' 2>/dev/null; echo rc=$?"


def _connect_via(proxy: str, port: int, target: str) -> str:
    request = f"CONNECT {target}:443 HTTP/1.1\\r\\nHost: {target}:443\\r\\n\\r\\n"
    return f"timeout {TIMEOUT_S} bash -c 'exec 3<>/dev/tcp/{proxy}/{port}; printf \"{request}\" >&3; head -1 <&3' 2>/dev/null; echo rc=$?"


def representatives(plan: dict) -> dict[str, str | None]:
    """The container each zone's checks run from: its lowest-VMID LXC. Not
    the nullbill container, which keeps a LAN leg until --close-lan."""
    reps: dict[str, str | None] = {}
    for zone in plan["zones"]:
        lxcs = sorted(
            (g["vmid"], n) for n, g in plan["guests"].items() if g["zone"] == zone and g["kind"] == "lxc" and not g.get("installed")
        )
        reps[zone] = lxcs[0][1] if lxcs else None
    return reps


def _targets(plan: dict, model: dict, zone: str) -> list[tuple[str, str, int, str]]:
    """(dest, proto, port, address as seen from zone) for every listening port."""
    out = []
    for dest, svc in model["services"].items():
        guest = plan["guests"].get(dest)
        if guest is None or guest["kind"] == "vm-pool":
            continue
        ip = guest["ips"][zone] if "ips" in guest else guest["ip"]
        out += [(dest, p["proto"], p["port"], ip) for p in svc.get("ports", [])]
    host_ip = plan["zones"]["mgmt"]["host_ip"]
    out += [(HOST, p["proto"], p["port"], host_ip) for p in model["host"].get("ports", [])]
    return out


def probes(plan_with_notes: dict, model: dict, public_ip: str) -> tuple[list[Probe], list[Result]]:
    """Every probe to run, and SKIPs for zones nothing can probe from."""
    plan = strip_notes(plan_with_notes)
    flows = resolve_flows(plan, model)
    out: list[Probe] = []
    skipped: list[Result] = []
    proxy = plan["guests"].get("egress-proxy")
    proxy_port = next((p["port"] for p in model["services"]["egress-proxy"].get("ports", [])), None)
    public_name = model["verify"]["public_name"]
    reps = representatives(plan)
    for zone, rep in reps.items():
        if rep is None:
            skipped.append(Result("SKIP", f"zone {zone}", "no container to probe from"))
            continue
        out += _port_probes(plan, model, flows, zone, rep)
        vmid = plan["guests"][rep]["vmid"]
        egress = plan["zones"][zone]["egress"]
        direct = egress == "direct"
        verb = "reaches" if direct else "is refused"
        out.append(Probe(f"{rep} ({zone}) {verb} {public_name} directly by IP", vmid, _tcp(public_ip, 443), "open" if direct else "closed"))
        if egress == "proxy" and proxy and proxy_port:
            via = any(rep in f.sources and f.dest == "egress-proxy" for f in flows)
            if via:
                cmd = _connect_via(proxy["ip"], proxy_port, public_name)
                out.append(Probe(f"{rep} ({zone}) reaches {public_name} through the proxy", vmid, cmd, "2", "proxy"))
                cmd = _connect_via(proxy["ip"], proxy_port, public_ip)
                out.append(Probe(f"{rep} ({zone}) is refused a bare IP by the proxy", vmid, cmd, "4", "proxy"))
        router = plan["zones"][zone]["gateway"]
        name = f"router.{plan['domain']}"
        cmd = f"timeout {TIMEOUT_S} getent ahostsv4 {name} >/dev/null; echo rc=$?"
        out.append(Probe(f"{rep} ({zone}) resolves {name} via {router}", vmid, cmd, "open", "dns"))
    # The operator and the dispatcher hold PVE tokens: prove their reach
    # separately.
    for holder in ("operator", "runner-dispatcher"):
        guest = plan["guests"].get(holder)
        if guest and guest["kind"] == "lxc" and holder not in reps.values():
            out += _port_probes(plan, model, flows, guest["zone"], holder)
    return out, skipped


def _port_probes(plan: dict, model: dict, flows: list, zone: str, rep: str) -> list[Probe]:
    out = []
    vmid = plan["guests"][rep]["vmid"]
    for dest, proto, port, ip in _targets(plan, model, zone):
        if dest == rep or proto == "udp":
            # A UDP connect proves nothing; DNS is checked by resolving.
            continue
        allowed = any(rep in f.sources and f.dest == dest and f.proto == proto and f.port == port for f in flows)
        label = "the PVE host" if dest == HOST else dest
        verb = "reaches" if allowed else "is refused"
        listener = model["services"].get(dest, {}).get("listener", "") if allowed else ""
        out.append(
            Probe(f"{rep} ({zone}) {verb} {label} tcp/{port}", vmid, _tcp(ip, port), "open" if allowed else "closed", listener=listener)
        )
    return out


def _judge(probe: Probe, rc: int, out: str, err: str) -> Result:
    marker = [line for line in out.splitlines() if line.startswith("rc=")]
    if rc != 0 or not marker:
        return Result("FAIL", probe.name, f"probe did not run: {err.strip()[:120] or f'ssh exited {rc}'}")
    code = int(marker[-1][3:])
    if probe.kind == "proxy":
        status = next((line.split()[1][:1] for line in out.splitlines() if line.startswith("HTTP/")), None)
        if status == probe.expect:
            return Result("PASS", probe.name)
        return Result("FAIL", probe.name, f"proxy answered {status + 'xx' if status else 'nothing'}")
    if probe.kind == "dns":
        return Result("PASS", probe.name) if code == 0 else Result("FAIL", probe.name, "name did not resolve")
    state = {0: "open", 124: "timeout"}.get(code, "refused")
    if probe.expect == "open":
        if state == "open":
            return Result("PASS", probe.name)
        why = "nothing listening; the firewall let it through" if state == "refused" else "timed out"
        if probe.listener:
            return Result("DEGRADED", probe.name, f"nothing answers ({why}): {probe.listener}")
        return Result("FAIL", probe.name, why)
    if state == "timeout":
        return Result("PASS", probe.name)
    # A refusal came from the destination, so the packet got past every filter.
    return Result("FAIL", probe.name, "connected" if state == "open" else "reached the destination (refused, not dropped)")


def _lan(host: str, connect: Connect) -> list[Result]:
    results = []
    for port in HOST_PORTS:
        name = f"LAN is refused the PVE host tcp/{port}"
        state = connect(host, port)
        if state == "timeout":
            results.append(Result("PASS", name))
        elif state.startswith("error"):
            # No route, no address: nothing reached the firewall, so nothing was shown.
            results.append(Result("SKIP", name, f"could not test from here: {state.removeprefix('error: ')}"))
        else:
            results.append(Result("FAIL", name, "connected" if state == "open" else "refused, not dropped"))
    return results


def tcp_connect(host: str, port: int) -> str:
    """open, refused, timeout (a drop), or "error: ..." when the attempt
    never got as far as the host's firewall."""
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT_S):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "timeout"
    except OSError as err:
        return f"error: {err.strerror or err}"


def resolve_ipv4(name: str) -> str:
    return socket.getaddrinfo(name, 443, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]


def run_verify(
    plan: dict,
    model: dict,
    target: str | None,
    run: Runner,
    connect: Connect = tcp_connect,
    resolve: Resolve = resolve_ipv4,
    now: backups.Clock = time.time,
    lan_state: str = lan.CLOSED,
    lan_host: str | None = None,
) -> list[Result]:
    results: list[Result] = []
    bare = strip_notes(plan)
    if bare["backups"]["status"] == "DEGRADED":
        results.append(Result("DEGRADED", "backups", "no separate disk for PBS"))
    elif bare["backups"]["status"] == "disabled":
        results.append(Result("DEGRADED", "backups", "switched off; nothing is backed up"))
    if target is None:
        planned, skipped = probes(plan, model, "unresolved")
        reason = "no target.ssh in config.yaml"
        return (
            results
            + skipped
            + [Result("SKIP", p.name, reason) for p in planned]
            + [Result("SKIP", f"LAN is refused the PVE host tcp/{port}", reason) for port in HOST_PORTS]
            + backups.skipped(plan, reason)
            + agent.skipped(plan, reason)
            + forge.skipped(plan, reason)
            + ci.skipped(plan, reason)
            + legacy.skipped(plan, reason)
        )
    name = model["verify"]["public_name"]
    try:
        public_ip = resolve(name)
    except OSError as exc:
        raise NullBillError(f"verify: cannot resolve {name} on this controller: {exc}") from exc
    planned, skipped = probes(plan, model, public_ip)
    rc, out, err = run(target, "pve-firewall status")
    if rc != 0 or "running" not in out:
        results.append(Result("FAIL", "pve-firewall is running", (out or err).strip()[:120]))
    else:
        results.append(Result("PASS", "pve-firewall is running"))
    for probe in planned:
        command = f"pct exec {probe.vmid} -- bash -c {shlex.quote(probe.command)}"
        results.append(_judge(probe, *run(target, command)))
    results += skipped
    if lan_state == lan.OPEN:
        results.append(Result("DEGRADED", "LAN is refused the PVE host", "open until you close it over Tailscale (docs/runbooks/vpn.md)"))
    else:
        results += _lan(lan_host or target.rpartition("@")[2], connect)
    results += backups.check(plan, target, run, now)
    results += agent.check(plan, target, run)
    results += forge.check(plan, target, run)
    results += ci.check(plan, target, run)
    results += legacy.check(plan, target, run)
    return results
