"""Server names such as forgejo.nullbill.internal on the owner's devices.

apply's tailnet stage sets Tailscale's split DNS: every tailnet device sends
lookups under the plan's domain to the router's address in the vpn guest's
zone (tailnet.nameserver), whose dnsmasq already answers them for the
guests. It records that here, so the page shows names first. verify reads
the DNS configuration Tailscale hands the vpn guest, which every device on
the tailnet gets alike, and then asks that address for a name from the vpn
guest: a device's lookup arrives there through the vpn guest too, so this is
the stretch of its way that lies on the server. Addresses keep working
without any of this.

The Tailscale app's own warning "DNS unavailable" is not this check's
business: the app raises it when any lookup it passed on got no answer in
time, from the network's own DNS servers as much as from the router, and
drops it at the next answer (net/dns/resolver/forwarder.go).
"""

import json
import re
import shlex
from pathlib import Path

from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result
from nullbill.tailnet import nameserver

STATE = "names.json"
LABEL = "tailnet devices look up server names at the router"
ANSWERS = "the router answers lookups of server names from the vpn guest"
ADVICE = "make a Tailscale OAuth client with DNS write as well as Policy file, paste it on Access and run setup"
# `tailscale dns status`: "  - nullbill.internal.             -> 10.42.6.1"
ROUTE = re.compile(r"^\s*- (\S+?)\.?\s+-> (\S+)")


def record(home: Path, domain: str, server: str) -> None:
    (home / STATE).write_text(json.dumps({"domain": domain, "nameserver": server}) + "\n")


def forget(home: Path) -> None:
    (home / STATE).unlink(missing_ok=True)


def active(home: Path) -> bool:
    """Whether the last setup run left the tailnet resolving the plan's names."""
    return (home / STATE).is_file()


def routes(status: str) -> dict[str, list[str]]:
    """Split DNS routes from `tailscale dns status`, nameservers without a port."""
    out: dict[str, list[str]] = {}
    section = False
    for line in status.splitlines():
        if line.startswith("Split DNS Routes:"):
            section = True
        elif section and not line.strip():
            break
        elif section and (match := ROUTE.match(line)):
            out.setdefault(match[1], []).append(match[2].removesuffix(":53"))
    return out


def command(plan: dict) -> str:
    """What verify runs on the host: the DNS settings the vpn guest got."""
    return f"pct exec {plan['guests']['vpn']['vmid']} -- {shlex.join(['tailscale', 'dns', 'status'])}"


def lookup(plan: dict) -> tuple[str, str, str]:
    """(what the host runs, the name asked for, the address it has): the
    vpn guest's resolver is the router's leg in its zone, the address the
    tailnet's lookups go to, and every plan has the router's own name."""
    name = f"router.{plan['domain']}"
    return f"pct exec {plan['guests']['vpn']['vmid']} -- {shlex.join(['getent', 'ahostsv4', name])}", name, plan["zones"]["mgmt"]["gateway"]


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    return [Result("SKIP", name, reason) for name in (LABEL, ANSWERS)] if nameserver(plan_with_notes) else []


def answers(plan: dict, server: str, target: str, run: Runner) -> Result:
    command, name, address = lookup(plan)
    rc, out, err = run(target, command)
    got = sorted({line.split()[0] for line in out.splitlines() if line.split()})
    if rc == 0 and got == [address]:
        return Result("PASS", ANSWERS)
    said = f"it answered {', '.join(got)}, not {address}" if rc == 0 and got else (err.strip()[:120] or "no answer")
    return Result("FAIL", ANSWERS, f"asked {server} for {name}: {said}")


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    server = nameserver(plan)
    if not server:
        return []
    rc, out, err = run(target, command(plan))
    if rc != 0:
        return [Result("FAIL", LABEL, (err or out).strip()[:120] or f"exit {rc}"), Result("SKIP", ANSWERS, "the vpn guest does not answer")]
    asked = answers(plan, server, target, run)
    if "No tailnet information available" in out:
        return [Result("SKIP", LABEL, "the vpn guest is not on the tailnet yet"), asked]
    found = routes(out).get(plan["domain"], [])
    if found == [server]:
        return [Result("PASS", LABEL), asked]
    now = ", ".join(found) or "nowhere"
    return [Result("DEGRADED", LABEL, f"Tailscale sends {plan['domain']} to {now}, not {server}; {ADVICE}"), asked]
