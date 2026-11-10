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

Two things are read, and kept apart. What the home router answered,
compared with the addresses Tailscale offers other devices for the vpn guest,
says whether the port is open: evidence, and only as far as the server sees.
A router behind another one, a port forwarded by hand and a phone whose
carrier is easy on Tailscale are all out of its sight. What settles it is the
path itself: tailscaled on the vpn guest knows, for each device it has talked
to in the last minute, whether the packets go straight or through a relay,
and from which address. A device that is on the tailnet but idle has no path
to read, so the vpn guest pings it (`tailscale ping`, which also has both ends
look for a direct path at once) and takes the path its answer came by; one
that does not answer is left out, never called relayed. A device connected
directly from an address that is not on a home network is one away from home
that reaches the server directly, whatever the home router said.

apply's vpn stage, verify and the page (Access, Tailscale, read again while
it is open) record the outcome in $NULLBILL_HOME/direct.json with the time it
was read. The home's public address and the devices' addresses go to neither
the record nor the log.
"""

import ipaddress
import json
import re
import shlex
import time
from collections.abc import Callable
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
# A connection starts on a relay and moves to the direct path within a second
# or two. `tailscale ping` stops at the first answer that came directly; a
# device whose answers all came through a relay after this many, a second
# apart, is on a relay. At most MOST devices are asked in one reading, all at
# once, so a reading takes PINGS * PING_TIMEOUT_S seconds at worst.
PINGS = 3
PING_TIMEOUT_S = 2
MOST = 8
# "100.64.0.7 pong from phone (100.64.0.7) via 198.51.100.7:41641 in 21ms":
# the address asked, put in front by ping_command, then tailscale's own line
# (cmd/tailscale/cli/ping.go). via is an address and port, DERP(<region>) or
# peer-relay(<address>).
PONG = re.compile(r"^(\S+) pong from .* via (\S+) in \S+$")
# nb-portmap's words: a home router that says it is not on the internet
# itself, one that refuses this device, and a port that is another device's.
NO_PUBLIC_ADDRESS = "it has no public address of its own"
REFUSED = "it does not let this device open ports"
OTHER_DEVICE = "another device"
# The page's switch for tailnet.port_mapping.
SWITCH = "Ask the home router for the port (Access page, Tailscale)"

# The port, as far as the server can see it.
DIRECT = "direct"  # the home router forwards the port and Tailscale offers it
CLOSED = "closed"  # the home router did not open the port
SHARED = "shared"  # no public address to open a port on: the provider's NAT, or a second router
UNSEEN = "unseen"  # the port is open, but Tailscale does not offer it
OFF = "off"  # tailnet.port_mapping is false: the owner forwards by hand, or not at all
WAITING = "waiting"  # the vpn guest is not on the tailnet
UNREAD = "unread"  # no answer to read, or an old one

# A device's path, as tailscaled on the vpn guest has it right now.
RELAYED = "relayed"
# Where a directly connected device is: its address is a home network's, or not.
HOME, AWAY = "home", "away"

_PRIVATE = tuple(ipaddress.ip_network(n) for n in (*PRIVATE, "127.0.0.0/8", "0.0.0.0/8"))


@dataclass(frozen=True)
class Device:
    """A device whose path the vpn guest has: one it sends packets to
    directly, or one that answered its ping."""

    name: str
    path: str  # DIRECT or RELAYED
    where: str = ""  # HOME or AWAY for a direct path to an IPv4 address; a relay hides it


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str = ""  # what was read about the port, for every kind but DIRECT
    port: int = TAILSCALE_PORT
    address: str | None = None  # the router on the home network: where a forward made by hand goes
    method: str | None = None  # NAT-PMP or UPnP, once the home router opened the port
    devices: tuple[Device, ...] = ()  # the paths measured when this was read


def router_command(plan: dict) -> str:
    """What the host runs to read nb-portmap's last answer and the router's clock."""
    script = f"cat {ROUTER_STATE} && date +%s"
    return f"qm guest exec {plan['guests']['router']['vmid']} --timeout 15 -- sh -c {shlex.quote(script)}"


def vpn_command(plan: dict) -> str:
    return f"pct exec {plan['guests']['vpn']['vmid']} -- {shlex.join(['tailscale', 'status', '--json'])}"


def ping_command(plan: dict, addresses: list[str]) -> str:
    """What the host runs to have the vpn guest ping these tailnet addresses,
    all at once, each line of an answer led by the address it is about."""
    one = f'tailscale ping --c {PINGS} --timeout {PING_TIMEOUT_S}s "$a" 2>&1 | sed "s/^/$a /"'
    script = f"for a in {' '.join(addresses)}; do ({one}) & done; wait"
    return f"pct exec {plan['guests']['vpn']['vmid']} -- sh -c {shlex.quote(script)}"


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


def _name(peer: dict) -> str:
    return str(peer.get("HostName") or str(peer.get("DNSName") or "").partition(".")[0] or "a device")


def _direct(peer: dict) -> Device | None:
    """The peer as a device on a direct path, if tailscaled uses one for it
    now: CurAddr is set only then (PeerRelay for a relay on another device,
    neither for one of Tailscale's own). Only the kind of address is kept."""
    at = _endpoint(peer.get("CurAddr") or "")
    if at is None or peer.get("PeerRelay") or not peer.get("Active"):
        return None
    return Device(_name(peer), DIRECT, _where(at[0]))


def _where(address: str) -> str:
    try:
        ipaddress.IPv4Address(address)
    except ValueError:
        return ""
    return AWAY if _public(address) else HOME


def _peers(status: dict) -> list[dict]:
    peers = status.get("Peer")
    return [p for p in (peers.values() if isinstance(peers, dict) else []) if isinstance(p, dict)]


def idle(status: dict) -> dict[str, dict]:
    """The devices to ping, by tailnet address: on the tailnet now (Online)
    and without a direct path in use. An address reaches a shell, so only
    what parses as one is taken."""
    found: dict[str, dict] = {}
    for peer in sorted(_peers(status), key=_name):
        if not peer.get("Online") or _direct(peer):
            continue
        for text in peer.get("TailscaleIPs") or []:
            try:
                address = ipaddress.IPv4Address(str(text))
            except ValueError:
                continue
            found[str(address)] = peer
            break
    return dict(list(found.items())[:MOST])


def pongs(out: str) -> dict[str, str]:
    """The way each pinged address's last answer came (PONG's via). An
    address that did not answer, or whose lines read otherwise, is not in it."""
    found = {}
    for text in out.splitlines():
        if match := PONG.match(text.strip()):
            found[match[1]] = match[2]
    return found


def devices(status: dict, asked: dict[str, dict] | None = None, answers: str = "") -> tuple[Device, ...]:
    """Each device whose path is known: those tailscaled sends to directly
    now, and of those pinged (`asked`, from idle) the ones that answered, by
    the way their last answer came. A device that sent no answer is left out:
    nothing was measured for it."""
    found = [device for peer in _peers(status) if (device := _direct(peer))]
    came = pongs(answers)
    for address, peer in (asked or {}).items():
        via = came.get(address)
        if via is None or _direct(peer):
            continue
        at = _endpoint(via)
        if via.startswith(("DERP(", "peer-relay(")):
            found.append(Device(_name(peer), RELAYED))
        elif at is not None:
            found.append(Device(_name(peer), DIRECT, _where(at[0].strip("[]"))))
    return tuple(sorted(found, key=lambda d: d.name))


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
        return finding(OFF, f"{unknown}; to have it open, {by_hand}")
    age = clock - int(state.get("at") or 0)
    if age > STALE_S:
        return finding(UNREAD, f"nb-portmap on the router last ran {age // 60} minutes ago; nullbill-portmap.timer runs it every 15")
    method, external = state.get("method"), _endpoint(state.get("external") or "")

    def behind(opened: bool) -> str:
        """The home router's own outside address is not a public one. What
        the router before it does with the port cannot be seen from here."""
        inner = "" if opened else f", and on the home router to {address or 'the NullBill router'}"
        return (
            "the home router's own outside address is not a public one, so another router stands between it and the internet, "
            "and the server can neither open a port on that one nor see whether one is open; "
            f"if that router is a second box at home, forward UDP {port} on it to the home router{inner}; "
            "if it is your provider's, only the provider can"
        )

    if not method or not external:
        why = state.get("why") or {}
        if NO_PUBLIC_ADDRESS in why.values():
            return finding(SHARED, behind(opened=False))
        said = ", ".join(f"{m}: {w}" for m, w in why.items()) or "no answer"
        return finding(CLOSED, f"the home router did not open UDP {port} ({said}); {forward(port, address, why)}")
    if not _public(external[0]):
        return finding(SHARED, behind(opened=True), method)
    if external in seen:
        return finding(DIRECT, method=method)
    if any(ip == external[0] for ip, _ in seen):
        ports = ", ".join(str(p) for ip, p in seen if ip == external[0])
        leaves = f"the server's packets leave it from port {ports}, which is the one Tailscale tells devices"
        return finding(UNSEEN, f"the home router opened UDP {port}, but {leaves}", method)
    if seen:
        other = "Tailscale's servers see the server at another address than the home router's own"
        return finding(
            SHARED, f"{other}: your provider puts a router of its own in between, which the server cannot open a port on", method
        )
    return finding(UNSEEN, f"the home router opened UDP {port}, but Tailscale on the vpn guest has found no public address yet", method)


def find(plan_with_notes: dict, model: dict, target: str, run: Runner) -> Finding | None:
    """Read both ends through the host, and have the vpn guest ping the
    devices that are on the tailnet without a direct path. None when no vpn
    guest is planned."""
    plan = strip_notes(plan_with_notes)
    if "vpn" not in plan["guests"]:
        return None

    def status() -> tuple[dict | None, str]:
        rc, out, err = run(target, vpn_command(plan))
        try:
            found = json.loads(out) if rc == 0 else None
        except ValueError:
            found = None
        return (found, "") if isinstance(found, dict) else (None, (err or out).strip()[:120] or f"exit {rc}")

    said, why = status()
    if said is None:
        return Finding(UNREAD, f"tailscale status on the vpn guest: {why}")
    if said.get("BackendState") != "Running":
        return Finding(WAITING, "the vpn guest is not on the tailnet yet")
    asked = idle(said)
    answers = run(target, ping_command(plan, list(asked)))[1] if asked else ""
    measured = devices(said, asked, answers)
    state, clock, why = router_state(*run(target, router_command(plan)))
    if state is None or clock is None:
        return Finding(UNREAD, why, devices=measured)
    found = judge(bool(model["tailnet"]["port_mapping"]), state, clock, offered(said))
    return Finding(found.kind, found.detail, found.port, found.address, found.method, measured)


def _names(found: list[Device]) -> str:
    names = [d.name for d in found]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


# What the reading comes to. MEASURED and ON_RELAY are paths seen in use;
# OPEN is the port alone; UNMEASURED is neither: nothing the server read
# settles it.
MEASURED = "measured"  # a device away from home is connected directly
OPEN = "open"  # the home router forwards the port and Tailscale offers it; no device away answered directly
ON_RELAY = "relayed"  # a device answers through a relay only, and none away from home is direct
UNMEASURED = "unmeasured"


# The same, for a reader who was not there (the page's agent).
MEANS = {
    MEASURED: "a device away from home is connected directly, so direct connections work; nothing to do",
    OPEN: "the home router forwards the port and Tailscale offers it; no device away from home answered just now",
    ON_RELAY: "a device answers only through a Tailscale relay, and no device away from home is connected directly",
    UNMEASURED: "no device away from home answered, so the path from mobile data was not measured; that is no fault by itself",
    WAITING: "the vpn guest is not on the tailnet yet",
    UNREAD: "the server could not read it",
}
# What tailnet.port_mapping is and is not, for the same reader.
SWITCH_MEANS = (
    "the switch only decides who opens the port on the home router: the server (UPnP or NAT-PMP) or the owner by hand; "
    "switching it off never makes a connection direct and does not change what Path measures"
)


def verdict(found: Finding) -> str:
    if found.kind in (WAITING, UNREAD):
        return found.kind
    if any(d.path == DIRECT and d.where == AWAY for d in found.devices):
        return MEASURED
    if found.kind == DIRECT:
        return OPEN
    return ON_RELAY if any(d.path == RELAYED for d in found.devices) else UNMEASURED


def measured(found: Finding) -> str:
    """What the paths in use say, in a clause."""
    away = [d for d in found.devices if d.path == DIRECT and d.where == AWAY]
    relayed = [d for d in found.devices if d.path == RELAYED]
    if away:
        return f"{_names(away)} {'is' if len(away) == 1 else 'are'} connected directly from outside the home network"
    if relayed:
        return f"{_names(relayed)} {'answers' if len(relayed) == 1 else 'answer'} only through a Tailscale relay right now"
    return "no device away from home answered, so the path from mobile data was not measured"


def line(found: Finding) -> str:
    """One line for the run's log."""
    if found.kind == DIRECT:
        return f"devices on any network reach the server directly: the home router sends UDP {found.port} here ({found.method})"
    if found.kind in (WAITING, UNREAD):
        return found.detail
    return f"{measured(found)}; {found.detail}"


def record(home: Path, found: Finding | None, now: Callable[[], float] = time.time) -> None:
    """Keep the outcome for the page, with when it was read and when a
    device away from home was last seen connected directly."""
    if found is None:
        (home / STATE).unlink(missing_ok=True)
        return
    at = int(now())
    away = [d for d in found.devices if d.path == DIRECT and d.where == AWAY]
    seen = {"names": [d.name for d in away], "at": at} if away else (recorded(home) or {}).get("seen")
    kept = asdict(found) | {"verdict": verdict(found), "at": at, "seen": seen}
    tmp = home / (STATE + ".tmp")
    tmp.write_text(json.dumps(kept) + "\n")
    tmp.replace(home / STATE)


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
    """verify's line. It passes on a port the home router opened and
    Tailscale offers, or on a device away from home seen connected directly;
    it is degraded only for a device that answers through a relay. With neither, the
    server has nothing that settles it (a port forwarded by hand or on a
    second router cannot be seen from in here, and a closed one does not
    stop every phone), so it says what it read and skips."""
    if found is None:
        return []
    said = verdict(found)
    if said == OPEN:
        return [Result("PASS", LABEL)]
    if said == MEASURED:
        return [Result("PASS", LABEL, measured(found))]
    if said in (WAITING, UNREAD):
        return [Result("SKIP" if said == WAITING else "FAIL", LABEL, found.detail)]
    return [Result("DEGRADED" if said == ON_RELAY else "SKIP", LABEL, f"{measured(found)}; {found.detail}")]
