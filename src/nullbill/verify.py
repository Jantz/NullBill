"""verify: prove from inside each zone and from the LAN that the flows the
plan allows pass and everything else is refused.

Checks are built from the plan, so a service added by YAML is verified the
same way as the catalog's. They run over SSH as `pct exec` in one container
per zone; backups.py adds backup age and a restore test, llm.py the local
model through the model gateway, media.py Jellyfin. A check that cannot run reports SKIP,
never PASS.
"""

import shlex
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from nullbill import agent, backups, browserca, ci, forge, hostroot, lan, legacy, llm, media, names
from nullbill.errors import NullBillError
from nullbill.flows import HOST, HOST_PORTS, resolve_flows
from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

TIMEOUT_S = 3
# A container's probes run side by side in one `pct exec` (batch), and this
# many containers at a time, each its own ssh to the host. One probe per
# ssh, six at a time, took 3 min for some 300 on the owner's server, most
# of it drops waiting out TIMEOUT_S. sshd starts dropping new connections
# past MaxStartups' 10 unauthenticated ones, so stay well below.
PARALLEL = 6
BATCH_MARK = "--- nb-probe "
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


def batch(commands: list[str]) -> str:
    """One bash script that runs every command at once, each in its own
    subshell, then prints each one's output after a BATCH_MARK line."""
    d = "/run/nb-probes.$$"
    lines = [f"mkdir -m 0700 {d} || exit 1"]
    lines += [f"( {c} ) >{d}/{i} 2>&1 &" for i, c in enumerate(commands)]
    lines += ["wait", f"for i in $(seq 0 {len(commands) - 1}); do echo '{BATCH_MARK}'$i; cat {d}/$i; done", f"rm -rf {d}"]
    return "\n".join(lines)


def unbatch(out: str, count: int) -> list[str]:
    """Each command's output from a batch's, "" for one that printed none."""
    parts = [""] * count
    current = None
    for line in out.splitlines(keepends=True):
        if line.startswith(BATCH_MARK) and line[len(BATCH_MARK) :].strip().isdigit():
            current = int(line[len(BATCH_MARK) :])
        elif current is not None and current < count:
            parts[current] += line
    return parts


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


def _agent_root(target: str, run: Runner) -> Result:
    """The owner may give the runtime agent root on the host (hostroot.py);
    while that lasts, every check of what the agent cannot do is moot."""
    name = "the agent has no root on the PVE host"
    rc, out, err = run(target, f"cat {hostroot.KEYS}")
    if rc != 0:
        return Result("FAIL", name, f"cannot read {hostroot.KEYS}: {(err or out).strip()[:120]}")
    if hostroot.granted(out):
        return Result("DEGRADED", name, "it has: the switch Root on Proxmox on the page's Agent page (Settings) is on")
    return Result("PASS", name)


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
    say: Callable[[Result], None] | None = None,
    browser_root: Path | None = None,
) -> list[Result]:
    """Every check's result, in a fixed order; `say` hears each one as soon as
    it and those before it are in, so a long run shows its progress."""
    results: list[Result] = []

    def add(*found: Result) -> None:
        for result in found:
            results.append(result)
            if say:
                say(result)

    bare = strip_notes(plan)
    if bare["backups"]["status"] == "DEGRADED":
        add(Result("DEGRADED", "backups", "no separate disk for PBS"))
    elif bare["backups"]["status"] == "disabled":
        add(Result("DEGRADED", "backups", "switched off; nothing is backed up"))
    if target is None:
        planned, skipped = probes(plan, model, "unresolved")
        reason = "no target.ssh in config.yaml"
        add(
            *skipped,
            *[Result("SKIP", p.name, reason) for p in planned],
            *[Result("SKIP", f"LAN is refused the PVE host tcp/{port}", reason) for port in HOST_PORTS],
            *backups.skipped(plan, reason),
            *agent.skipped(plan, reason),
            *forge.skipped(plan, reason),
            *ci.skipped(plan, reason),
            *llm.skipped(plan, reason),
            *legacy.skipped(plan, reason),
            *media.skipped(plan, reason),
            *names.skipped(plan, reason),
            *(browserca.skipped(plan, reason) if browser_root is not None else []),
        )
        return results
    name = model["verify"]["public_name"]
    try:
        public_ip = resolve(name)
    except OSError as exc:
        raise NullBillError(f"verify: cannot resolve {name} on this controller: {exc}") from exc
    planned, skipped = probes(plan, model, public_ip)
    rc, out, err = run(target, "pve-firewall status")
    if rc != 0 or "running" not in out:
        add(Result("FAIL", "pve-firewall is running", (out or err).strip()[:120]))
    else:
        add(Result("PASS", "pve-firewall is running"))

    # In the plan's order, each probe from its container's batch.
    by_vmid: dict[int, list[Probe]] = {}
    for probe in planned:
        by_vmid.setdefault(probe.vmid, []).append(probe)

    def attempt(vmid: int) -> list[Result]:
        group = by_vmid[vmid]
        rc, out, err = run(target, f"pct exec {vmid} -- bash -c {shlex.quote(batch([p.command for p in group]))}")
        return [_judge(p, rc, part, err) for p, part in zip(group, unbatch(out, len(group)), strict=True)]

    with ThreadPoolExecutor(PARALLEL) as pool:
        done = {vmid: pool.submit(attempt, vmid) for vmid in by_vmid}
        told = dict.fromkeys(by_vmid, 0)
        for probe in planned:
            add(done[probe.vmid].result()[told[probe.vmid]])
            told[probe.vmid] += 1
    add(*skipped)
    if lan_state == lan.OPEN:
        add(Result("DEGRADED", "LAN is refused the PVE host", "open until you close it over Tailscale (docs/runbooks/vpn.md)"))
    else:
        add(*_lan(lan_host or target.rpartition("@")[2], connect))
    add(*backups.check(plan, target, run, now))
    if "operator" in bare["guests"]:
        add(_agent_root(target, run))
    add(*agent.check(plan, target, run))
    add(*forge.check(plan, target, run))
    add(*ci.check(plan, target, run))
    add(*llm.check(plan, target, run))
    add(*legacy.check(plan, target, run))
    add(*media.check(plan, target, run))
    add(*names.check(plan, target, run))
    if browser_root is not None:
        add(*browserca.check(plan, browser_root, target, run, lan_closed=lan_state == lan.CLOSED))
    return results
