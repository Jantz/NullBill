"""Server names such as forgejo.nullbill.internal on the owner's devices.

apply's tailnet stage sets Tailscale's split DNS: every tailnet device sends
lookups under the plan's domain to the router's address in the vpn guest's
zone (tailnet.nameserver), whose dnsmasq already answers them for the
guests. It records that here, so the page shows names first. verify reads
the DNS configuration Tailscale hands the vpn guest, which every device on
the tailnet gets alike. Addresses keep working without any of this.
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


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    return [Result("SKIP", LABEL, reason)] if nameserver(plan_with_notes) else []


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    server = nameserver(plan)
    if not server:
        return []
    rc, out, err = run(target, command(plan))
    if rc != 0:
        return [Result("FAIL", LABEL, (err or out).strip()[:120] or f"exit {rc}")]
    if "No tailnet information available" in out:
        return skipped(plan, "the vpn guest is not on the tailnet yet")
    found = routes(out).get(plan["domain"], [])
    if found == [server]:
        return [Result("PASS", LABEL)]
    now = ", ".join(found) or "nowhere"
    return [Result("DEGRADED", LABEL, f"Tailscale sends {plan['domain']} to {now}, not {server}; {ADVICE}")]
