"""The download client: what plan, apply, verify and the page need for
qBittorrent (docs/adr/0020-download-client.md).

It runs alone in the torrents zone and downloads into one folder of a kept
disk the owner made editable (legacy.py), bind-mounted at STORE: the same
disk Jellyfin plays and the files guest shares, so a finished download
shows up in both without being copied.

BitTorrent talks to whoever else holds a file, on any port, so the egress
proxy cannot carry it. The owner says which way it goes, and there is no
default: `torrents.route: vpn` sends everything the client sends through
the owner's own WireGuard VPN, and both firewall layers then let the guest
reach that VPN's server and nothing else outside; `home` sends it from the
home connection, whose public address everyone else in a download sees.
The VPN's file holds a private key, so it is a secret (VPN_SECRET); only
the server's address and port are in config.yaml, where the firewall is
made from.
"""

import ipaddress
import re
import shlex

from nullbill import direct, legacy, power
from nullbill.errors import NullBillError, Refused
from nullbill.flows import PRIVATE, TORRENTS_PORT
from nullbill.probe import Runner
from nullbill.render import Note, strip_notes
from nullbill.results import Result

QBITTORRENT = "qbittorrent"
WORKLOAD = "torrents"
ZONE = "torrents"
PORT = 8080
PLAYBOOK = "qbittorrent.yml"
# Where the client sees its folder of the kept disk, and where unfinished
# downloads wait inside it (with a .ignore file, so Jellyfin skips them).
STORE = "/downloads"
INCOMPLETE = "incomplete"
# The kept disks' files belong to uid 1000 in an unprivileged container
# (legacy.SHARE_UID); the client runs as that uid.
UID = legacy.SHARE_UID
USER = "qbt"
ADMIN_USER = "nullbill-admin"
NB_TORRENTS = "/usr/local/sbin/nb-torrents"
VPN, HOME = "vpn", "home"
# The plan's egress for the zone behind a VPN: out to the VPN's server on
# its UDP port and nowhere else; packages come through the egress proxy.
TUNNEL = "tunnel"
INTERFACE = "wg0"
# The secrets: the owner's WireGuard file, and the password of the
# client's page, which the Services page shows.
VPN_SECRET = "torrents_vpn_conf"
PASSWORD_SECRET = "torrents_password"
# The module WireGuard needs on the host: a container cannot load one.
MODULE = "wireguard"
MODULE_FILE = "nullbill-wireguard.conf"
# A tunnel whose last handshake is older than this is down: WireGuard
# makes a new one every two minutes while anything is sent, and the
# keepalive below sends.
HANDSHAKE_S = 180
KEEPALIVE_S = 25
RUNBOOK = "docs/runbooks/downloads.md"

WHAT_HOME = "Everyone else in a download sees your home's public address, and so does anyone who watches that download."
WHAT_VPN = "Everything it sends leaves through your VPN; if the VPN drops, it sends nothing."
# qBittorrent asks whoever starts it to accept this; choosing a route on
# the page, where it is shown, is the owner's answer.
NOTICE = "While something downloads, the parts you have are uploaded to others. What you download and share is your own responsibility."


def enabled(plan: dict) -> bool:
    return QBITTORRENT in plan["guests"]


def route(plan: dict) -> str | None:
    return (plan.get("torrents") or {}).get("route")


def endpoint(plan: dict) -> tuple[str, int] | None:
    """The VPN server's address and UDP port when the client goes through one."""
    found = plan.get("torrents") or {}
    if found.get("route") != VPN:
        return None
    host, _, port = found["endpoint"].rpartition(":")
    return host, int(port)


def peer_port(plan: dict) -> int | None:
    """The port others reach the client on, TCP and UDP, when it sends from
    the home connection: the router passes it on from the uplink. None
    behind a VPN, where nothing at home is open for it."""
    return (plan.get("torrents") or {}).get("port")


def port_request(plan: dict) -> dict | None:
    """What nb-portmap on the router asks the home router for besides
    Tailscale's port (its settings' "more"); None without such a port."""
    port = peer_port(plan)
    if port is None:
        return None
    return {"enabled": bool(plan["torrents"]["port_mapping"]), "port": port, "protos": ["tcp", "udp"]}


def store(plan: dict) -> dict | None:
    """The kept partition the downloads go to (legacy.mounts' entry, with
    `folder` and `path`, the folder on the host). `share` is the kept
    disk's name, whether the files guest shares it or not (`shared`)."""
    found = (plan.get("torrents") or {}).get("store")
    if not found:
        return None
    m = next(m for m in legacy.mounts(plan) if m["name"] == found["share"])
    return {**m, "share": m["name"], "shared": "share" in m, "folder": found["folder"], "path": f"{m['mount']}/{found['folder']}"}


def writable(mounts: list[dict]) -> list[str]:
    """The names of the kept disks that can be written to."""
    return [m["name"] for m in mounts if m["mode"] == "rw"]


def kept(model: dict, facts: dict | None) -> list[dict]:
    """The kept disks' partitions as config.yaml pins them, for the page: it
    asks before a plan exists, and a plan that fails for another reason
    names none."""
    found = {}
    for disk in (facts or {}).get("disks", []):
        pin = model.get("disks", {}).get(disk["id"])
        if pin and pin.get("use") == "legacy" and not disk.get("system"):
            found[disk["id"]] = {"use": "legacy", "partitions": legacy.plan_disk(disk["id"], disk, pin, [])[0]}
    return legacy.mounts({"disks": found})


def _endpoint_problem(text: str, base: str) -> str | None:
    host, _, port = text.rpartition(":")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"torrents.vpn.endpoint: {host} is not an address"
    if not 1 <= int(port) <= 65535:
        return f"torrents.vpn.endpoint: {port} is not a port"
    inside = address in ipaddress.ip_network(base) or any(address in ipaddress.ip_network(net) for net in PRIVATE)
    if inside or address.is_loopback or address.is_multicast or address.is_unspecified or address.is_reserved:
        return f"torrents.vpn.endpoint: {host} is not an address on the internet, where a VPN's server is"
    return None


def plan_section(model: dict, planned: dict, mounts: list[dict], media_planned: bool, errors: list[str]) -> Note | dict:
    """Which way the client's traffic goes and where its downloads land.
    mounts: the kept partitions (legacy.mounts), shared by the files guest
    or only mounted on the host (disks.<id>.share: false)."""
    if QBITTORRENT not in planned:
        return Note({}, "Not planned.")
    chosen = model["torrents"]
    section: dict = {}
    way = chosen.get("route")
    if way is None:
        errors.append(
            "torrents.route: say which way downloads go: vpn (through a WireGuard VPN of your own) or home "
            "(from your home connection; others in a download then see its public address). "
            f"On the page: Setup, What to run, Downloads ({RUNBOOK})"
        )
    elif way == VPN:
        server = (chosen.get("vpn") or {}).get("endpoint")
        if server is None:
            errors.append("torrents.vpn: no VPN yet: paste your provider's WireGuard file on the page (Setup, What to run, Downloads)")
        else:
            problem = _endpoint_problem(server, model["network"]["base"])
            if problem:
                errors.append(problem)
            section["route"] = Note(VPN, f"{WHAT_VPN} Both firewall layers let its guest reach {server} (UDP) and nothing else outside.")
            section["endpoint"] = server
    else:
        section["route"] = Note(HOME, f"From the home connection, without a VPN. {WHAT_HOME} verify reports it as DEGRADED.")
        section["port"] = Note(
            TORRENTS_PORT,
            "Others connect to the client here, TCP and UDP: the router passes it on from the home network, "
            "once the home router forwards it.",
        )
        asks = chosen["port_mapping"]
        section["port_mapping"] = Note(
            asks,
            "The router asks the home router for that port every 15 minutes (NAT-PMP, then UPnP)."
            if asks
            else "Nobody asks the home router: forward the port to the router's address on the home network yourself, or leave it shut.",
        )
    can = writable(mounts)
    names = [m["name"] for m in mounts]
    share = chosen.get("share")
    where = "editable under Setup, Hardware, Other disks (disks.<id>.mode: rw)"
    if share and share not in names:
        errors.append(f"torrents.share: no kept disk is called {share!r}; kept are: {', '.join(names) or 'none'}")
    elif share and share not in can:
        errors.append(f"torrents.share: the kept disk {share} is read-only, so nothing can be downloaded to it; make it {where}")
    elif not share and not can:
        have = (
            f"the kept disk {names[0]} is read-only" if len(names) == 1 else "every kept disk is read-only" if names else "no disk is kept"
        )
        errors.append(f"torrents: downloads go to a kept disk that can be written to, and {have}; keep one and make it {where}")
    elif not share and len(can) > 1:
        errors.append(f"torrents.share: say which kept disk downloads go to: {', '.join(can)}")
    else:
        share = share or can[0]
        folder = chosen["folder"]
        why = f"Downloads land in the folder {folder} of the kept disk {share}; the client sees that folder and nothing else of the disk."
        if any(m["name"] == share and "share" not in m for m in mounts):
            # disks.<id>.share: false. Jellyfin's library and the file share are the shared disks.
            why += " That disk is not shared, so neither the file share nor Jellyfin shows them: they are reached on the server only."
        elif media_planned:
            why += f" Jellyfin shows them under /media/{share}/{folder}."
        why += " Not backed up, like the rest of a kept disk."
        section["store"] = Note({"share": share, "folder": folder}, why)
    section["keep_free_gib"] = Note(
        chosen["keep_free_gib"], "Downloads pause while the disk has less than this free, and go on once it has more."
    )
    return section


def mount_point(found: dict) -> str:
    """The downloads folder as a bind mount point of the client's guest."""
    return f"{found['path']},mp={STORE},backup=0,ro=0"


# An owner's WireGuard file, as VPN providers hand them out for wg-quick.
KEY = re.compile(r"[A-Za-z0-9+/]{43}=")
MAX_CONF = 4096


def _fields(text: str) -> tuple[dict[str, str], list[dict[str, str]], list[str]]:
    interface: dict[str, str] = {}
    peers: list[dict[str, str]] = []
    problems: list[str] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower() == "[interface]":
            current = interface
        elif line.lower() == "[peer]":
            peers.append({})
            current = peers[-1]
        elif "=" in line and current is not None:
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip()
        else:
            problems.append(f"the line {line[:40]!r} is not part of a WireGuard file")
    return interface, peers, problems


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def read_conf(text: str, base: str, resolve=None) -> dict:
    """What NullBill takes from a WireGuard file: the keys, the tunnel's
    IPv4 address, the VPN's DNS servers and its server. Everything wrong
    with the file is raised together. resolve: turns a server given by name
    into an address (the firewall needs one); None refuses a name."""
    if len(text) > MAX_CONF:
        raise NullBillError(f"the pasted file is {len(text)} characters long; a WireGuard file is a few lines")
    interface, peers, problems = _fields(text)
    out: dict = {}
    if not KEY.fullmatch(interface.get("privatekey", "")):
        problems.append("it has no PrivateKey under [Interface]; paste the whole file your VPN provider made, not the public part")
    else:
        out["private_key"] = interface["privatekey"]
    addresses = []
    for item in _list(interface.get("address", "")):
        try:
            found = ipaddress.ip_interface(item)
        except ValueError:
            problems.append(f"Address {item} is not an address")
            continue
        if found.version == 4:
            addresses.append(found)
    if not addresses:
        problems.append("it has no IPv4 Address under [Interface]")
    elif any(a.ip in ipaddress.ip_network(base) for a in addresses):
        problems.append(f"its Address {addresses[0].ip} lies inside NullBill's own network {base}")
    else:
        out["address"] = f"{addresses[0].ip}/32"
    dns = []
    for item in _list(interface.get("dns", "")):
        try:
            if ipaddress.ip_address(item).version == 4:
                dns.append(item)
        except ValueError:
            pass  # a search domain
    if not dns:
        problems.append("it names no DNS server (a DNS line with an IPv4 address); without one, names would be looked up outside the VPN")
    elif any(ipaddress.ip_address(d) in ipaddress.ip_network(base) for d in dns):
        problems.append(f"its DNS server {dns[0]} lies inside NullBill's own network {base}")
    else:
        out["dns"] = dns
    if "mtu" in interface:
        if interface["mtu"].isdigit() and 576 <= int(interface["mtu"]) <= 1500:
            out["mtu"] = int(interface["mtu"])
        else:
            problems.append(f"MTU {interface['mtu']} is not a size from 576 to 1500")
    if len(peers) != 1:
        problems.append(f"it has {len(peers)} [Peer] sections; a VPN provider's file has one")
        peers = [{}]
    peer = peers[0]
    if not KEY.fullmatch(peer.get("publickey", "")):
        problems.append("it has no PublicKey under [Peer]")
    else:
        out["public_key"] = peer["publickey"]
    if "presharedkey" in peer:
        if KEY.fullmatch(peer["presharedkey"]):
            out["preshared_key"] = peer["presharedkey"]
        else:
            problems.append("its PresharedKey is not a key")
    if "0.0.0.0/0" not in _list(peer.get("allowedips", "")):
        problems.append("its AllowedIPs lacks 0.0.0.0/0, so only part of the traffic would go through the VPN")
    host, _, port = peer.get("endpoint", "").rpartition(":")
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        problems.append("it has no Endpoint (the VPN's server and port) under [Peer]")
    else:
        try:
            address = str(ipaddress.IPv4Address(host))
        except ValueError:
            address = None
            if host.startswith("["):
                problems.append(
                    "its Endpoint is an IPv6 address; NullBill's network is IPv4 only, so ask your provider for a file with an IPv4 server"
                )
            elif resolve is None:
                problems.append(f"its Endpoint names the server ({host}); the firewall needs its address")
            else:
                try:
                    address = resolve(host)
                except OSError as exc:
                    problems.append(
                        f"its Endpoint {host} could not be looked up from here ({exc.strerror or exc}); "
                        "put the server's address in its place"
                    )
        if address:
            out["endpoint"] = f"{address}:{int(port)}"
            problem = _endpoint_problem(out["endpoint"], base)
            if problem:
                problems.append(problem.removeprefix("torrents.vpn.endpoint: "))
    if problems:
        raise (
            Refused("This WireGuard file cannot be used", problems)
            if len(problems) > 1
            else NullBillError(f"this WireGuard file cannot be used: {problems[0]}")
        )
    return out


def saved_conf(conf: dict) -> str:
    """What is kept of a pasted file (read_conf's answer), as a WireGuard
    file again: the lines NullBill uses and no others, the server by its
    address. read_conf reads it back."""
    lines = ["[Interface]", f"PrivateKey = {conf['private_key']}", f"Address = {conf['address']}", f"DNS = {', '.join(conf['dns'])}"]
    if "mtu" in conf:
        lines.append(f"MTU = {conf['mtu']}")
    lines += ["", "[Peer]", f"PublicKey = {conf['public_key']}"]
    if "preshared_key" in conf:
        lines.append(f"PresharedKey = {conf['preshared_key']}")
    lines += ["AllowedIPs = 0.0.0.0/0", f"Endpoint = {conf['endpoint']}"]
    return "\n".join(lines) + "\n"


def wg_conf(conf: dict) -> str:
    """The file `wg setconf` reads in the guest: keys and peer only; the
    address, routes and DNS are nb-torrents' business."""
    lines = ["[Interface]", f"PrivateKey = {conf['private_key']}", "", "[Peer]", f"PublicKey = {conf['public_key']}"]
    if "preshared_key" in conf:
        lines.append(f"PresharedKey = {conf['preshared_key']}")
    lines += ["AllowedIPs = 0.0.0.0/0", f"Endpoint = {conf['endpoint']}", f"PersistentKeepalive = {KEEPALIVE_S}"]
    return "\n".join(lines) + "\n"


def variables(plan: dict, model: dict, password: str, vpn_conf: str | None) -> dict:
    """The qbittorrent role's variables. vpn_conf: the owner's WireGuard
    file from the secrets, when the plan routes through it."""
    found = plan["torrents"]
    out = {
        "qbittorrent_uid": UID,
        "qbittorrent_port": PORT,
        "qbittorrent_username": ADMIN_USER,
        "qbittorrent_password": password,
        "qbittorrent_store": STORE,
        "qbittorrent_keep_free_gib": found["keep_free_gib"],
        "qbittorrent_route": found["route"],
        # Fixed either way; only from home is it reachable from outside.
        "qbittorrent_listen_port": TORRENTS_PORT,
        "qbittorrent_base": model["network"]["base"],
        "qbittorrent_tunnel": {},
        "qbittorrent_wg_conf": "",
    }
    if found["route"] == VPN:
        base = model["network"]["base"]
        if not vpn_conf:
            raise NullBillError(
                "downloads go through a VPN, and no WireGuard file is saved; paste it on the page (Setup, What to run, Downloads)"
            )
        conf = read_conf(vpn_conf, base)
        if conf["endpoint"] != found["endpoint"]:
            raise NullBillError(
                f"the saved WireGuard file is for the server {conf['endpoint']}, the plan opens the firewall to {found['endpoint']}; "
                "paste the file again on the page (Setup, What to run, Downloads), which sets both"
            )
        out["qbittorrent_tunnel"] = {
            "interface": INTERFACE,
            "address": conf["address"],
            "dns": conf["dns"],
            "mtu": conf.get("mtu", 1420),
            "endpoint": conf["endpoint"],
            "handshake_s": HANDSHAKE_S,
        }
        out["qbittorrent_wg_conf"] = wg_conf(conf)
    return out


HOME_LABEL = "downloads leave through a VPN"
TUNNEL_LABEL = "the download client's tunnel to the VPN is up"
THROUGH_LABEL = "the download client sends through the tunnel, and its guest reaches nothing outside by itself"
DROP_LABEL = "a dropped tunnel stops the download client's traffic"
SPACE_LABEL = "downloads are not paused for lack of room"
PATHS_LABEL = "every download is saved inside the downloads folder"
INSIDE = "inside"
# qBittorrent's word once a peer has connected to it from outside.
REACHED = "connected"
PORT_LABEL = "the home router sends the download client's port to it"


def _labels(plan: dict) -> list[str]:
    found = store(plan)
    labels = ["the download client answers and asks for a sign-in"]
    if found:
        labels.append(f"the download client writes to {found['folder']} on the kept disk {found['share']}")
        labels.append(SPACE_LABEL)
        labels.append(PATHS_LABEL)
    labels += [HOME_LABEL, PORT_LABEL] if route(plan) == HOME else [TUNNEL_LABEL, THROUGH_LABEL, DROP_LABEL]
    return labels


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    return [Result("SKIP", label, reason) for label in _labels(plan)] if enabled(plan) else []


def paths_result(rc: int, out: str, err: str) -> Result:
    """nb-torrents paths: a download whose folder lies outside STORE cannot
    be written (the client's user owns nothing else), which qBittorrent
    shows only in its log; DEGRADED, since the other downloads work."""
    said = [line.strip() for line in out.splitlines() if line.strip()]
    if rc != 0 or not said:
        return Result("FAIL", PATHS_LABEL, (err.strip() or f"exit {rc}")[:160])
    if said == [INSIDE]:
        return Result("PASS", PATHS_LABEL)
    more = f" (and {len(said) - 1} more: nb-torrents paths in the guest lists them)" if len(said) > 1 else ""
    return Result(
        "DEGRADED",
        PATHS_LABEL,
        f"{said[0][:300]}{more}. The client can write under {STORE} only, so such a download stays unfinished: "
        f"in qBittorrent, set its location to a folder under {STORE}",
    )


def outside_test(port: int) -> str:
    """How the owner finds out for themselves: the server sees its home
    router's answers, never the port from the internet's side."""
    return (
        f"To find out: start a download with many peers and run the checks again, which pass once anyone has connected from outside; "
        f"or ask a port checking website for TCP {port} from a device at home"
    )


def port_result(plan: dict, rc: int, out: str, err: str, reached: str | None = None) -> Result:
    """Whether others can connect to the client. Measured when it can be:
    qBittorrent says `connected` once a peer got in from outside, and that
    is PASS whatever the home router said. Without it there is only what
    the home router answered nb-portmap, which is evidence and no proof (a
    forward made by hand, or on a second router, cannot be seen from in
    here, and some home routers answer wrongly about their own address): a
    port it did not open is DEGRADED with "may", a port nobody asks for is
    SKIP, and both say how to test from outside."""
    port = peer_port(plan)
    what = f"TCP and UDP {port}"
    if reached == REACHED:
        return Result("PASS", PORT_LABEL, "others have connected to it from outside")
    if not plan["torrents"]["port_mapping"]:
        why = (
            f"the server does not ask, and nobody has connected from outside yet; a forward of {what} made by hand cannot be seen from here"
        )
        return Result("SKIP", PORT_LABEL, f"{why}. {outside_test(port)}")
    state, _, why = direct.router_state(rc, out, err)
    if state is None:
        return Result("SKIP", PORT_LABEL, why)
    entry = (state.get("more") or {}).get(WORKLOAD)
    if not isinstance(entry, dict) or entry.get("port") != port or not entry.get("enabled"):
        return Result("SKIP", PORT_LABEL, "the router has not asked the home router yet; it does within a quarter of an hour of setup")
    shut = [proto for proto in ("tcp", "udp") if not (entry.get("open") or {}).get(proto)]
    if not shut:
        return Result("PASS", PORT_LABEL)
    said = (entry.get("why") or {}).get(shut[0]) or {}
    heard = "; ".join(f"{method}: {reason}" for method, reason in said.items()) or "no answer"
    to = state.get("address") or "the NullBill router"
    return Result(
        "DEGRADED",
        PORT_LABEL,
        f"it did not open {' and '.join(p.upper() for p in shut)} {port} when asked ({heard}), and nobody has connected from outside yet, "
        f"so the port may be shut. That answer is no proof: a forward made by hand may work all the same. {outside_test(port)}. "
        f"Downloads work either way; with the port shut, others cannot connect to you. "
        f"To open it: on the home router turn on UPnP or NAT-PMP, or forward {what} to {to} yourself",
    )


def check(plan_with_notes: dict, target: str, run: Runner, public_ip: str) -> list[Result]:
    """The client as its page's visitors and its peers meet it. Behind a
    VPN: the tunnel has a fresh handshake, what the client's user sends
    takes the tunnel while root in the same guest reaches nothing outside,
    and with the tunnel taken down for a moment the client's user reaches
    nothing either (nb-torrents drop puts it back up)."""
    plan = strip_notes(plan_with_notes)
    if not enabled(plan):
        return []
    vmid = plan["guests"][QBITTORRENT]["vmid"]
    labels = _labels(plan)
    results: list[Result] = []

    def ask(command: str) -> tuple[int, str, str]:
        return run(target, f"pct exec {vmid} -- bash -c {shlex.quote(command)}")

    def judge(label: str, command: str, want: str) -> bool:
        rc, out, err = ask(command)
        got = out.strip()
        ok = got == want
        results.append(Result("PASS", label) if ok else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:160]))
        return ok

    try:
        code = (
            f"curl -sS --max-time 10 -o /dev/null -w '%{{http_code}}' http://{plan['guests'][QBITTORRENT]['ip']}:{PORT}/api/v2/app/version"
        )
        judge(labels[0], f'code=$({code}); case $code in 403) echo sign-in ;; *) echo "answered $code" ;; esac', "sign-in")
        found = store(plan)
        if found:
            test = f"findmnt -rno TARGET --mountpoint {STORE} >/dev/null && test -w {STORE} && echo writable"
            judge(labels[1], f"runuser -u {USER} -- sh -c {shlex.quote(test)}", "writable")
            rc, out, err = ask(f"{NB_TORRENTS} space")
            if rc != 0:
                results.append(Result("FAIL", SPACE_LABEL, (err.strip() or f"exit {rc}")[:160]))
            elif out.strip() == "room":
                results.append(Result("PASS", SPACE_LABEL))
            else:
                results.append(Result("DEGRADED", SPACE_LABEL, out.strip()[:160]))
            results.append(paths_result(*ask(f"{NB_TORRENTS} paths")))
        if route(plan) == HOME:
            results.append(Result("DEGRADED", HOME_LABEL, f"they leave from the home connection, as chosen. {WHAT_HOME}"))
            rc, out, _ = ask(f"{NB_TORRENTS} reached")
            results.append(port_result(plan, *run(target, direct.router_command(plan)), out.strip() if rc == 0 else None))
            return results
        up = judge(TUNNEL_LABEL, f"{NB_TORRENTS} tunnel status", "up")
        if not up:
            why = "the tunnel is down, so nothing can be sent through it"
            results += [Result("SKIP", THROUGH_LABEL, why), Result("SKIP", DROP_LABEL, why)]
            return results
        judge(THROUGH_LABEL, f"{NB_TORRENTS} tunnel through {public_ip}", "through the tunnel only")
        judge(DROP_LABEL, f"{NB_TORRENTS} tunnel drop {public_ip}", "stopped")
    except power.Off as asleep:
        done = {r.name for r in results}
        results += [Result("SKIP", label, power.off_reason(asleep.title)) for label in labels if label not in done]
    return results
