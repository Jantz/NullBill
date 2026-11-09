"""People besides the owner (docs/adr/0011-people.md), for the page's
Access, People tab.

A person is an entry in config.yaml's `people`: the login they sign in to
Tailscale with, and the services the owner picked for them. Only the ports
that list `shared` in the catalog can be picked, and none in mgmt. The
tailnet policy (tailnet.py) is what holds each person to their picks, so a
change here is one policy push: nothing on the server changes, and it takes
effect within seconds for devices already connected.

Joining is Tailscale's own invitation, which only a signed-in person can
send (tailscale.py), so the owner sends it from Tailscale's admin console.
Access belongs to the login, not to the invitation: whoever else opens an
invite link joins the tailnet and reaches nothing.
"""

import re
from datetime import datetime

from nullbill.errors import NullBillError
from nullbill.flows import HOST, SHARED
from nullbill.render import strip_notes
from nullbill.tailnet import OWNER, person_flows, shared_ports

FRIEND = "friend"
NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
LOGIN = re.compile(r"[^@\s]+@[^@\s]+")
USERS_PAGE = "https://console.tailscale.com/admin/users"
DOWNLOAD = "https://tailscale.com/download"
ROOT_FILE = "nullbill-root.crt"
# How a person's device comes to trust the browser CA (browserca.py), as
# the page's Access, Trust this server tab tells the owner at more length.
TRUST = [
    "Windows: open the file, Install Certificate, Current User, then place it in Trusted Root Certification Authorities.",
    "Mac: open the file so Keychain Access adds it, open it there and set Trust to Always Trust.",
    "iPhone, iPad: open the file, then Settings, Profile Downloaded, Install; "
    "then Settings, General, About, Certificate Trust Settings, and turn it on.",
    "Android: Settings, search for CA certificate, Install anyway, and pick the file.",
]


def _guests(plan: dict | None) -> dict:
    return (strip_notes(plan) if plan else {}).get("guests") or {}


def shareable(model: dict, plan: dict | None) -> list[dict]:
    """Every service a person can be given, planned or not, as the owner's
    tick list shows it."""
    guests = _guests(plan)
    out = []
    for name, svc in model["services"].items():
        ports = shared_ports(model, name)
        if not ports or not svc.get("enabled", True):
            continue
        share = svc.get("share", {})
        out.append(
            {
                "name": name,
                "title": share.get("title", name),
                "what": share.get("what", svc.get("description", "")),
                "ports": [f"{p['proto']}/{p['port']}" for p in ports],
                "planned": name in guests,
            }
        )
    return out


def reach(model: dict, plan: dict | None, name: str, shared: bool | None = None) -> dict[str, list[str]]:
    """What a person reaches in the plan, as {guest: ["tcp/443", ...]}:
    everything, or only what was picked for them (shared), or only what
    their role reaches by itself."""
    if not plan:
        return {}
    out: dict[str, list[str]] = {}
    for flow in person_flows(strip_notes(plan), model, name):
        if flow.dest != HOST and shared in (None, flow.ref == SHARED):
            out.setdefault(flow.dest, []).append(f"{flow.proto}/{flow.port}")
    return out


def owner_login(config: dict) -> str:
    return next((p["login"] for p in config.get("people", {}).values() if p.get("role") == OWNER), "")


def save(config: dict, model: dict, name: str, login: str, services: list[str]) -> dict:
    """The config with one person added, or their login and services changed.
    The owner is set under Setup, not here."""
    name, login = name.strip().lower(), login.strip()
    if not NAME.fullmatch(name):
        raise NullBillError("name the person with lowercase letters, digits and dashes, such as alex")
    if not LOGIN.fullmatch(login):
        raise NullBillError("their Tailscale login is the email address they sign in to Tailscale with, such as alex@example.com")
    people = dict(config.get("people") or {})
    if people.get(name, {}).get("role") == OWNER:
        raise NullBillError(f"{name} is you; your own login is under Setup, What to run")
    for other, person in people.items():
        if other != name and person["login"].lower() == login.lower():
            who = "you" if person.get("role") == OWNER else other
            raise NullBillError(f"{login} is the login of {who} already")
    allowed = {s["name"] for s in shareable(model, None)}
    unknown = [s for s in services if s not in allowed]
    if unknown:
        raise NullBillError(f"{', '.join(map(str, unknown))} cannot be given to a person")
    # Whatever else the entry holds stays (the owner's ssh_keys are owner.py's).
    kept = {key: value for key, value in people.get(name, {}).items() if key != "services"}
    person = {"role": FRIEND, **kept, "login": login}
    if services:
        # In the tick list's order, whatever order they were ticked in.
        person["services"] = [s["name"] for s in shareable(model, None) if s["name"] in services]
    return {**config, "people": {**people, name: person}}


def remove(config: dict, name: str) -> dict:
    people = dict(config.get("people") or {})
    if name not in people:
        raise NullBillError(f"there is no person named {name}")
    if people[name].get("role") == OWNER:
        raise NullBillError("the owner stays; without one nobody reaches the server")
    del people[name]
    return {**config, "people": people}


def _seen(stamp: str) -> float | None:
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def devices(listed: list[dict]) -> dict[str, list[dict]]:
    """Each login's own devices on the tailnet, newest contact first. A
    tagged device is nobody's, and one shared in belongs to another tailnet."""
    out: dict[str, list[dict]] = {}
    for device in listed:
        if device.get("tags") or device.get("isExternal") or not device.get("user"):
            continue
        out.setdefault(device["user"].lower(), []).append(
            {
                "name": device.get("hostname") or str(device.get("name", "")).partition(".")[0],
                "os": device.get("os", ""),
                "seen": _seen(device.get("lastSeen", "")),
                "connected": bool(device.get("connectedToControl")),
            }
        )
    for mine in out.values():
        mine.sort(key=lambda d: (not d["connected"], -(d["seen"] or 0)))
    return out


def opens(model: dict, plan: dict, service: str, ports: list[str], names: bool) -> dict:
    """How one planned service reads to a person who reaches these ports of
    it: what it is, where it opens, and by address too when it leads with
    the name."""
    plan = strip_notes(plan)
    share = model["services"][service].get("share", {})
    ip = plan["guests"][service]["ip"]
    name = f"{service}.{plan['domain']}"

    # A game server's players need its version of the game (games.py).
    version = model["games"].get(service, {}).get("version", "")

    def fill(text: str, host: str) -> str:
        text = text.replace("{host}", host).replace("{ip}", ip)
        return text.replace("{port}", ports[0].partition("/")[2]).replace("{version}", version)

    listed = [f"port {p.partition('/')[2]}{' (UDP)' if p.startswith('udp/') else ''}" for p in ports]
    where = share.get("open", "{host}, " + " and ".join(listed))
    out = {
        "name": service,
        "title": share.get("title", service),
        "what": share.get("what", ""),
        "where": fill(where, name if names else ip),
    }
    if names and fill(where, ip) != out["where"]:
        out["by_address"] = fill(where, ip)
    if share.get("note"):
        out["note"] = fill(share["note"], name if names else ip)
    return out


def wants_root(model: dict, plan: dict | None, name: str) -> bool:
    """One of the person's services opens over HTTPS, with a certificate
    their browser knows only once the device trusts the browser CA's root."""
    return any(model["services"][service].get("share", {}).get("open", "").startswith("https://") for service in reach(model, plan, name))


def instructions(model: dict, plan: dict | None, name: str, names: bool, root: bool = False) -> str:
    """What the owner sends a person with their invitation: how to join,
    and what then opens. In the owner's own voice, ready to paste. `root`:
    the browser CA's root exists, so the owner can send it along."""
    person = model["people"][name]
    lines = [
        "Here is how you get onto my server. It takes about five minutes.",
        "",
        f"1. Install Tailscale on the device you want to use: {DOWNLOAD}",
        f"2. Open the invitation from Tailscale that I send you and sign in as {person['login']}. "
        "It has to be this account: what you may open belongs to it.",
        "3. Open the Tailscale app, sign in with the same account and pick my network if it asks which one.",
        "   On Linux also run: sudo tailscale set --accept-routes --accept-dns",
        "",
    ]
    given = [opens(model, plan, service, ports, names) for service, ports in reach(model, plan, name).items()]
    if not given:
        return "\n".join([*lines, "Nothing is open for you yet. I will tell you when it is."]) + "\n"
    lines.append("While Tailscale is connected, this opens for you:")
    for item in given:
        lines += ["", f"- {item['title']}{': ' + item['what'] if item['what'] else ''}", f"  {item['where']}"]
        if "by_address" in item:
            lines.append(f"  If the name does not open: {item['by_address']}")
        if "note" in item:
            lines.append(f"  {item['note']}")
    if root and wants_root(model, plan, name):
        lines += [
            "",
            f"Your browser warns about my server's certificate until your device trusts it. I send you the file {ROOT_FILE} "
            "for that; it can vouch for my server and for nothing else.",
            *(f"- {step}" for step in TRUST),
        ]
    lines += ["", "That is all you can reach on my server, and my network lets nothing reach your device."]
    return "\n".join(lines) + "\n"


def rows(model: dict, plan: dict | None, on_tailnet: dict[str, list[dict]] | None) -> list[dict]:
    """One row per person, the owner first. `on_tailnet` is devices() of
    the tailnet, or None while Tailscale cannot be asked."""
    out = []
    for name, person in model["people"].items():
        mine = None if on_tailnet is None else on_tailnet.get(person["login"].lower(), [])
        out.append(
            {
                "name": name,
                "login": person["login"],
                "role": person["role"],
                "owner": person["role"] == OWNER,
                "services": person.get("services", []),
                "by_role": [] if person["role"] == OWNER else list(reach(model, plan, name, shared=False)),
                "devices": mine,
            }
        )
    return sorted(out, key=lambda r: not r["owner"])


def strangers(model: dict, on_tailnet: dict[str, list[dict]]) -> list[str]:
    """Logins with a device on the tailnet that no person here has. The
    policy gives them nothing; the owner may add them or remove them."""
    known = {p["login"].lower() for p in model["people"].values()}
    return sorted(login for login in on_tailnet if login not in known)


def find_user(users: list[dict], login: str) -> dict | None:
    """The tailnet's user with this login, unless that is its owner."""
    for user in users:
        if str(user.get("loginName", "")).lower() == login.lower() and user.get("role") != OWNER:
            return user
    return None
