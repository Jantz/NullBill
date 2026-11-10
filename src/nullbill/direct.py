"""Direct connections from devices on any network to the vpn guest.

Tailscale connects two devices directly when one of them can be reached at
an address and port the other learns from Tailscale's servers. A phone on
mobile data cannot be: its carrier sends it out from another port for every
destination. So the server has to be the one that can, and behind a home
router it is only on a port that router forwards. Without one such devices
still connect, through Tailscale's relays (DERP): slower by tens of
milliseconds, and too slow for video.

Three things make the port, flows.TAILSCALE_PORT, reachable:
- nb-portmap on the NullBill router asks the home router for it every
  quarter of an hour, NAT-PMP first, then UPnP (tailnet.port_mapping; the
  owner may forward it by hand instead);
- the router's ruleset passes what arrives for it on to the vpn guest;
- the vpn guest's pve-firewall file accepts it.
tailscaled cannot ask for itself: the gateway it would ask is the NullBill
router, and the home router is one hop further.

apply's vpn stage and verify read what the home router answered and compare
it with the addresses Tailscale offers other devices for the vpn guest, and
record the outcome in $NULLBILL_HOME/direct.json for the page (Access,
Tailscale). The home's public address goes to neither the record nor the log.
"""

import ipaddress
import json
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path

from nullbill.flows import PRIVATE, TAILSCALE_PORT
from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

STATE = "direct.json"
LABEL = "devices on mobile data reach the vpn guest directly"
ROUTER_STATE = "/var/lib/nullbill-portmap/state.json"
# nb-portmap runs every 15 minutes and a mapping lasts 60: past three missed
# runs the mapping it reported may be gone.
STALE_S = 45 * 60
RELAYED = "devices on mobile data go through Tailscale's relays"
# nb-portmap's words: a home router that says it is not on the internet
# itself, one that refuses this device, and a port that is another device's.
NO_PUBLIC_ADDRESS = "it has no public address of its own"
REFUSED = "it does not let this device open ports"
OTHER_DEVICE = "another device"
# The page's switch for tailnet.port_mapping.
SWITCH = "Ask the home router for the port (Access page, Tailscale)"

DIRECT = "direct"  # the home router forwards the port and Tailscale offers it
CLOSED = "closed"  # the home router did not open the port
SHARED = "shared"  # no public address to open a port on: the provider's NAT, or a second router
UNSEEN = "unseen"  # the port is open, but Tailscale does not offer it
OFF = "off"  # tailnet.port_mapping is false: the owner forwards by hand, or not at all
WAITING = "waiting"  # the vpn guest is not on the tailnet
UNREAD = "unread"  # no answer to read, or an old one

_PRIVATE = tuple(ipaddress.ip_network(n) for n in (*PRIVATE, "127.0.0.0/8", "0.0.0.0/8"))


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str = ""  # why, for every kind but DIRECT
    port: int = TAILSCALE_PORT
    address: str | None = None  # the router on the home network: where a forward made by hand goes
    method: str | None = None  # NAT-PMP or UPnP, once the home router opened the port


def router_command(plan: dict) -> str:
    """What the host runs to read nb-portmap's last answer and the router's clock."""
    script = f"cat {ROUTER_STATE} && date +%s"
    return f"qm guest exec {plan['guests']['router']['vmid']} --timeout 15 -- sh -c {shlex.quote(script)}"


def vpn_command(plan: dict) -> str:
    return f"pct exec {plan['guests']['vpn']['vmid']} -- {shlex.join(['tailscale', 'status', '--json'])}"


def _public(address: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    return not any(ip in net for net in _PRIVATE)


def _endpoint(text: object) -> tuple[str, int] | None:
    host, _, port = str(text).rpartition(":")
    return (host, int(port)) if port.isdigit() else None


def offered(status: dict) -> list[tuple[str, int]]:
    """The public IPv4 addresses and ports Tailscale gives other devices for
    the vpn guest: what its servers saw its packets come from."""
    found = [_endpoint(a) for a in (status.get("Self") or {}).get("Addrs") or []]
    return [e for e in found if e and _public(e[0])]


def router_state(rc: int, out: str, err: str) -> tuple[dict | None, int | None, str]:
    """(nb-portmap's state, the router's clock, "") from what `qm guest exec`
    printed, or (None, None, why not). qm prints the guest agent's answer as
    JSON whether the command in the guest failed or not."""
    try:
        answer = json.loads(out)
        text = answer.get("out-data", "")
    except (ValueError, AttributeError):
        return None, None, f"cannot ask the router: {(err or out).strip()[:120] or f'exit {rc}'}"
    if not answer.get("exited"):
        return None, None, "the router did not answer in time"
    if answer.get("exitcode") != 0 or not text.strip():
        return None, None, f"nothing to read on the router: {(answer.get('err-data') or 'nb-portmap has not run yet').strip()[:120]}"
    body, _, clock = text.strip().rpartition("\n")
    try:
        state = json.loads(body)
        return (state, int(clock), "") if isinstance(state, dict) else (None, None, "the router's answer is not nb-portmap's")
    except ValueError:
        return None, None, "the router's answer is not nb-portmap's"


def forward(port: int, address: str | None, why: dict) -> str:
    """What the owner can do on the home router, by what it answered."""
    said = " ".join(str(w) for w in why.values())
    to = address or "the NullBill router"
    if OTHER_DEVICE in said:
        return f"on the home router, remove the forwarding of UDP {port} to that device: the server asks again within a quarter of an hour"
    # A forward made by hand cannot be seen from in here: the owner says so with the switch.
    by_hand = f"or forward UDP {port} to {to} there yourself and switch off {SWITCH}"
    if REFUSED in said:
        return f"on the home router, allow the device at {to} to open ports by itself; {by_hand}"
    return f"on the home router, turn on UPnP or NAT-PMP; {by_hand}"


def judge(asked: bool, state: dict, clock: int, seen: list[tuple[str, int]]) -> Finding:
    """What nb-portmap's state and Tailscale's own endpoints say together."""
    port = state.get("port") or TAILSCALE_PORT
    address = state.get("address")

    def finding(kind: str, detail: str = "", method: str | None = None) -> Finding:
        return Finding(kind, detail, port, address, method)

    if not asked or not state.get("enabled"):
        by_hand = f"forward UDP {port} to {address or 'the NullBill router'} on the home router yourself"
        unknown = f"the home router is not asked for UDP {port}, so the server cannot tell whether the port is open"
        return finding(OFF, f"{unknown}; {by_hand}, or {RELAYED}")
    age = clock - int(state.get("at") or 0)
    if age > STALE_S:
        return finding(UNREAD, f"nb-portmap on the router last ran {age // 60} minutes ago; nullbill-portmap.timer runs it every 15")
    method, external = state.get("method"), _endpoint(state.get("external") or "")

    def behind(opened: bool) -> str:
        """The home router's own outside address is not a public one."""
        inner = "" if opened else f", and on the home router to {address or 'the NullBill router'}"
        return (
            "the home router is behind another router (its own outside address is not a public one), so a port it opens "
            f"cannot be reached from the internet: if that router is your provider's, {RELAYED}; "
            f"if it is a second box at home, forward UDP {port} on it to the home router{inner}"
        )

    if not method or not external:
        why = state.get("why") or {}
        if NO_PUBLIC_ADDRESS in why.values():
            return finding(SHARED, behind(opened=False))
        said = ", ".join(f"{m}: {w}" for m, w in why.items()) or "no answer"
        return finding(CLOSED, f"the home router did not open UDP {port} ({said}), so {RELAYED}; {forward(port, address, why)}")
    if not _public(external[0]):
        return finding(SHARED, behind(opened=True), method)
    if external in seen:
        return finding(DIRECT, method=method)
    if any(ip == external[0] for ip, _ in seen):
        ports = ", ".join(str(p) for ip, p in seen if ip == external[0])
        leaves = f"the server's packets leave it from port {ports}, which is the one Tailscale tells devices"
        return finding(UNSEEN, f"the home router opened UDP {port}, but {leaves}; {RELAYED}", method)
    if seen:
        other = "Tailscale's servers see the server at another address than the home router's own"
        return finding(SHARED, f"{other}: your provider puts a router of its own in between, so {RELAYED}", method)
    return finding(UNSEEN, f"the home router opened UDP {port}, but Tailscale on the vpn guest has found no public address yet", method)


def find(plan_with_notes: dict, model: dict, target: str, run: Runner) -> Finding | None:
    """Read both ends through the host. None when no vpn guest is planned."""
    plan = strip_notes(plan_with_notes)
    if "vpn" not in plan["guests"]:
        return None
    rc, out, err = run(target, vpn_command(plan))
    try:
        status = json.loads(out) if rc == 0 else None
    except ValueError:
        status = None
    if not isinstance(status, dict):
        return Finding(UNREAD, f"tailscale status on the vpn guest: {(err or out).strip()[:120] or f'exit {rc}'}")
    if status.get("BackendState") != "Running":
        return Finding(WAITING, "the vpn guest is not on the tailnet yet")
    state, clock, why = router_state(*run(target, router_command(plan)))
    if state is None or clock is None:
        return Finding(UNREAD, why)
    return judge(bool(model["tailnet"]["port_mapping"]), state, clock, offered(status))


def line(found: Finding) -> str:
    """One line for the run's log."""
    if found.kind == DIRECT:
        return f"devices on any network reach the server directly: the home router sends UDP {found.port} here ({found.method})"
    return found.detail


def record(home: Path, found: Finding | None) -> None:
    """Keep the outcome for the page."""
    if found is None:
        (home / STATE).unlink(missing_ok=True)
    else:
        (home / STATE).write_text(json.dumps(asdict(found)) + "\n")


def recorded(home: Path) -> dict | None:
    try:
        found = json.loads((home / STATE).read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def check(plan_with_notes: dict, model: dict, target: str, run: Runner, home: Path | None = None) -> list[Result]:
    """verify's check; with home, the page hears the outcome too."""
    found = find(plan_with_notes, model, target, run)
    if home is not None:
        record(home, found)
    return result(found)


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    return [Result("SKIP", LABEL, reason)] if "vpn" in strip_notes(plan_with_notes)["guests"] else []


def result(found: Finding | None) -> list[Result]:
    """verify's line: a port forwarded by hand cannot be told from a closed
    one from in here, so with the asking switched off there is nothing to
    check; relays still carry everything, so a closed port is no failure."""
    if found is None:
        return []
    if found.kind == DIRECT:
        return [Result("PASS", LABEL)]
    status = {OFF: "SKIP", WAITING: "SKIP", UNREAD: "FAIL"}.get(found.kind, "DEGRADED")
    return [Result(status, LABEL, found.detail)]
