"""Services of the owner's own (docs/adr/0015-custom-services.md).

A service config.yaml defines and the catalog does not is the owner's own:
NullBill has no role for what runs in it. Everything around it comes from
its `services` entry as for any other service: its guest, address and name,
the firewall on both layers, its proxy allow-list, updates, monitoring,
backups, running on demand, the people it is given to and verify's probes
of each flow.

What runs in it comes from a setup script kept beside config.yaml
($NULLBILL_HOME/custom/<name>/setup.sh), written by the owner or by the
page's agent with the owner's approval. apply's custom stage runs it as
root in the guest, once for each version of the script (ansible/roles/
custom_service). A service without one is an empty Debian guest the owner
fills by hand.

`define` is what the page's agent may add (web/chat.py's add_service). It
is narrower than what config.yaml allows the owner: a guest of its own in a
zone whose only way out is the proxy, never in mgmt or access, open only to
the owner, to the people the owner ticks it for and to guests named by
zone or service.
"""

import copy
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any

from nullbill import power
from nullbill.errors import NullBillError
from nullbill.model import AGENT_SSH, NODE_EXPORTER, POWER_DEFAULTS, VM_SSH, shipped
from nullbill.probe import Runner
from nullbill.render import Note, strip_notes
from nullbill.results import Result

DIR = "custom"
SCRIPT = "setup.sh"
# Where the page sets a removed service's script aside; no service can have this name.
REMOVED = ".removed"
MAX_SCRIPT = 32_000
PLAYBOOK = "custom-service.yml"
# In the guest: the SHA-256 of the script whose last run ended well.
MARK = "/var/lib/nb-custom/setup.sha256"
LOG = "/var/log/nb-custom-setup.log"
# Ids for services config.yaml adds; the catalog keeps to those below.
OWN_IDS = (*range(64, 128), *range(192, 254))
ZONE = "apps"
# Zones the catalog has that the agent may put a service in. Any zone
# config.yaml defines is allowed too, if its way out is not direct.
OPEN_ZONES = (ZONE, "games")
CLOSED_ZONES = ("mgmt", "access")
# Names something else answers to.
RESERVED = ("core", "host", "pve", "shared")
NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
# NullBill's own ports on a guest: apply's and the operator's SSH, and
# node_exporter.
WANT = ("title", "what", "kind", "zone", "cores", "memory_mib", "disk_gib", "ports", "proxy_allow", "share")
WANT += ("run", "stop", "idle_minutes", "on")
KEPT_PORTS = (22, NODE_EXPORTER["port"])
SIZE_LIMITS = {"cores": (1, 64), "memory_mib": (256, 262144), "disk_gib": (2, 4096)}
MAX_PORTS = 16
MAX_ALLOW = 40
DOMAIN = re.compile(r"\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def names(model: dict) -> list[str]:
    """The services config.yaml added to the catalog's, in its order."""
    known = shipped("services")
    return [name for name in model["services"] if name not in known]


def workloads(model: dict) -> list[str]:
    """The workloads only such services have: each is a choice of its own
    under Setup, What to run. One that joins core or a catalog workload
    follows that instead."""
    taken = {svc["workload"] for name, svc in model["services"].items() if name in shipped("services")} | {"core"}
    return list(dict.fromkeys(w for w in (model["services"][n]["workload"] for n in names(model)) if w not in taken))


def planned(plan: dict, model: dict) -> list[str]:
    """Those the plan has a guest for; a pool has no guest to set up."""
    return [n for n in names(model) if n in plan["guests"] and plan["guests"][n]["kind"] != "vm-pool"]


def own_ports(svc: dict) -> list[dict]:
    """A service's ports as config.yaml gives them, without those the model adds."""
    return [p for p in svc.get("ports", []) if p not in (NODE_EXPORTER, AGENT_SSH, VM_SSH)]


def shows_use(svc: dict) -> bool:
    """Whether a container of the owner's own shows that someone is using
    it: its nb-busy counts SSH sessions, apt at work and TCP connections to
    its ports, so one that listens on no TCP port looks unused while it
    works. (NullBill's own UDP services bring a busy.d program instead.)"""
    return svc["kind"] != "lxc" or any(p["proto"] == "tcp" for p in own_ports(svc))


NO_AUTO = "it listens on no TCP port, so nothing shows that it is in use and it would be stopped under its users"


def power_problems(model: dict, planned_services: dict) -> list[str]:
    """What plan refuses: a workload of the owner's own that stops by itself
    although none of its guests shows use."""
    out = []
    own = set(names(model))
    for workload in workloads(model):
        rule = {**POWER_DEFAULTS, **model["power"].get(workload, {})}
        members = [n for n, svc in planned_services.items() if svc["workload"] == workload]
        if members and rule["run"] == "on-demand" and rule["stop"] == "auto":
            if all(n in own and not shows_use(model["services"][n]) for n in members):
                out.append(f"power.{workload}.stop: auto: {NO_AUTO}; use stop: ask")
    return out


def title(model: dict, name: str) -> str:
    """What the page calls a service: as it is shared, else its workload's
    title when it is that workload's one service, else its name."""
    svc = model["services"][name]
    alone = [n for n, s in model["services"].items() if s["workload"] == svc["workload"]] == [name]
    rule = model.get("power", {}).get(svc["workload"]) or {}
    return svc.get("share", {}).get("title") or (rule.get("title") if alone else None) or name


def plan_section(model: dict, planned_services: dict) -> Note | None:
    """The plan's `custom`: each planned service of the owner's own, as the
    Services page shows it. None, so the plan has no such key, without any."""
    out: dict[str, Any] = {}
    for name in names(model):
        svc = planned_services.get(name)
        if svc is None or svc["kind"] == "vm-pool":
            continue
        entry: dict[str, Any] = {"title": title(model, name)}
        if svc.get("description"):
            entry["what"] = svc["description"]
        entry["ports"] = [f"{p['proto']}/{p['port']}" for p in own_ports(svc)]
        if svc.get("share", {}).get("open"):
            entry["open"] = svc["share"]["open"]
        if any("shared" in p["from"] for p in own_ports(svc)):
            entry["shared"] = True
        out[name] = entry
    if not out:
        return None
    return Note(
        out,
        "Your own: NullBill makes each guest and what is around it; what runs inside is its setup script's "
        f"({DIR}/<name>/{SCRIPT} beside config.yaml), or yours by hand.",
    )


# The setup script.
def script(home: Path, name: str) -> Path:
    return home / DIR / name / SCRIPT


def read_script(home: Path | None, name: str) -> str | None:
    """A service's setup script, or None when it has none."""
    if home is None or not NAME.fullmatch(name):
        return None
    path = script(home, name)
    return path.read_text() if path.is_file() else None


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def check_script(text: str) -> str:
    """A setup script as it is kept: Unix line ends, one trailing newline."""
    text = text.replace("\r\n", "\n").rstrip() + "\n"
    if "\0" in text or "\r" in text:
        raise NullBillError("the setup script is text, with Unix line ends")
    if not re.match(r"#!/(usr/)?bin/(env )?(ba)?sh\b", text):
        raise NullBillError("the setup script starts with #!/bin/bash or #!/bin/sh; apply runs it as root in the guest")
    if len(text) > MAX_SCRIPT:
        raise NullBillError(f"the setup script is longer than {MAX_SCRIPT} characters; fetch what is large from inside it")
    return text


def variables(plan: dict, model: dict, name: str, text: str, proxy: str) -> dict:
    """The custom_service role's variables for one service. proxy: the
    egress proxy's URL where the guest's zone goes out through it, else ""."""
    return {
        "custom_service_name": name,
        "custom_service_script": text,
        "custom_service_sha256": digest(text),
        "custom_service_proxy": proxy,
        "custom_service_domain": plan["domain"],
        "custom_service_address": plan["guests"][name]["ip"],
        "custom_service_ports": [f"{p['proto']}/{p['port']}" for p in own_ports(model["services"][name])],
    }


# verify.
def _in_guest(guest: dict, command: str) -> str:
    if guest["kind"] == "lxc":
        return f"pct exec {guest['vmid']} -- bash -c {shlex.quote(command)}"
    return f"qm guest exec {guest['vmid']} --timeout 20 -- bash -c {shlex.quote(command)}"


def _answer(guest: dict, rc: int, out: str, err: str) -> tuple[int, str]:
    """A command's exit status and output; a VM's guest agent wraps both."""
    if guest["kind"] == "lxc" or rc != 0:
        return rc, out.strip() or err.strip()
    try:
        data = json.loads(out)
        return int(data.get("exitcode", 1)), str(data.get("out-data", "")).strip() or str(data.get("err-data", "")).strip()
    except (ValueError, AttributeError):
        return 1, out.strip()


RAN = "its setup script ran as it is now"
UNITS = "no unit has failed"
FAILED_UNITS = "systemctl list-units --failed --no-legend --plain | awk '{print $1}' | tr '\\n' ' '"


def _labels(plan: dict, model: dict, home: Path | None) -> list[tuple[str, str]]:
    """(service, check) for every check, in order."""
    out = []
    for name in planned(plan, model):
        if read_script(home, name) is not None:
            out.append((name, RAN))
        out.append((name, UNITS))
    return out


def skipped(plan_with_notes: dict, model: dict, home: Path | None, reason: str) -> list[Result]:
    return [Result("SKIP", f"{name}: {what}", reason) for name, what in _labels(strip_notes(plan_with_notes), model, home)]


def check(plan_with_notes: dict, model: dict, target: str, run: Runner, home: Path | None, off: dict[str, str]) -> list[Result]:
    """verify, for each planned service of the owner's own: the script
    beside config.yaml is the one that last ran to its end in the guest,
    and no unit there has failed. Whether its ports answer is the flow
    probes' to say. off: guests that run on demand and are off, each with
    its workload's title."""
    plan = strip_notes(plan_with_notes)
    results = []
    for name, what in _labels(plan, model, home):
        label = f"{name}: {what}"
        guest = plan["guests"][name]
        if name in off:
            results.append(Result("SKIP", label, power.off_reason(off[name])))
            continue
        if what == RAN:
            want = digest(read_script(home, name) or "")
            rc, got = _answer(guest, *run(target, _in_guest(guest, f"cat {MARK} 2>/dev/null")))
            if got == want:
                results.append(Result("PASS", label))
            elif rc != 0 and not got:
                results.append(
                    Result(
                        "FAIL",
                        label,
                        "it failed or has not run yet; the last setup run's log shows its output under Set up your own services",
                    )
                )
            else:
                results.append(Result("FAIL", label, "the script changed since its last good run; run setup"))
            continue
        rc, got = _answer(guest, *run(target, _in_guest(guest, FAILED_UNITS)))
        if rc != 0:
            results.append(Result("FAIL", label, (got or f"exit {rc}")[:120]))
        elif got:
            results.append(Result("DEGRADED", label, f"failed: {got[:100]}"))
        else:
            results.append(Result("PASS", label))
    return results


# What the page's agent may add.
def free_id(model: dict) -> int | None:
    used = {svc["id"] for svc in model["services"].values()}
    return next((i for i in OWN_IDS if i not in used), None)


def _host_name(text: str) -> bool:
    """A name, not an address: the proxy passes no address whatever the list says."""
    return bool(DOMAIN.fullmatch(text)) and not text.rsplit(".", 1)[-1].isdigit()


def _one_line(value: object, what: str, limit: int, problems: list[str], needed: bool = False) -> str:
    text = str(value if value is not None else "").strip()
    if "\n" in text or "\r" in text or len(text) > limit or (needed and not text):
        problems.append(f"{what} is one line of at most {limit} characters" + (", and is needed" if needed else ""))
    return text


def _ports(want: object, model: dict, name: str, problems: list[str]) -> list[dict]:
    out: list[dict] = []
    if not isinstance(want, list) or len(want) > MAX_PORTS:
        problems.append(f"ports is a list of at most {MAX_PORTS} entries, each {{port, proto, from}}")
        return out
    for entry in want:
        if not isinstance(entry, dict) or set(entry) - {"port", "proto", "from"}:
            problems.append("each port is {port, proto, from}")
            continue
        number, proto, sources = entry.get("port"), entry.get("proto", "tcp"), entry.get("from") or ["role:owner"]
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= 65535 or proto not in ("tcp", "udp"):
            problems.append(f"port {number!r}/{proto!r}: a port is a number from 1 to 65535, tcp or udp")
            continue
        if number in KEPT_PORTS and proto == "tcp":
            problems.append(f"port {number} is NullBill's own on every guest; the service listens elsewhere")
            continue
        if not isinstance(sources, list) or not sources:
            problems.append(f"port {number}: from is a list")
            continue
        for ref in sources:
            kind, _, target = str(ref).partition(":")
            if ref in ("role:owner", "shared"):
                continue
            if kind == "zone" and target in model["zones"] and target not in CLOSED_ZONES:
                continue
            if kind == "service" and target in model["services"] and target != name:
                continue
            problems.append(
                f"port {number}: from {ref!r} is not possible; use role:owner (you), shared (the people you tick it for), "
                "zone:<name> or service:<name> of another guest"
            )
        out.append({"port": number, "proto": proto, "from": [str(ref) for ref in dict.fromkeys(sources)]})
    if len({(p["port"], p["proto"]) for p in out}) != len(out):
        problems.append("a port is listed twice; give each port once, with everyone it is open to in from")
    return out


def define(config: dict, model: dict, name: str, want: dict) -> dict:
    """config.yaml with the service `name` added, or changed where it is one
    of the owner's own already. model: config's, before the change. want:
    title, what, kind, zone, cores, memory_mib, disk_gib, ports, proxy_allow,
    share ({what, open, note}), run, stop, idle_minutes, on. Every problem
    with it is raised together; nothing here looks at the hardware, which
    is the plan's to say."""
    problems: list[str] = []
    name = str(name)
    if not NAME.fullmatch(name) or name in RESERVED:
        raise NullBillError(
            f"name {name!r}: lowercase letters, digits and dashes, starting with a letter, at most 31; not {', '.join(RESERVED)}"
        )
    if name in shipped("services"):
        raise NullBillError(f"{name} is one of NullBill's own services; turn it on with set_choices, or pick another name")
    taken = {svc["workload"] for n, svc in model["services"].items() if n != name} | set(model["zones"])
    if name in taken:
        raise NullBillError(f"{name} is taken already (a workload or a zone has that name); pick another")
    unknown = set(want) - set(WANT)
    if unknown:
        problems.append(f"unknown: {', '.join(sorted(unknown))}")
    before = (config.get("services") or {}).get(name) or {}
    if before and before.get("workload", name) != name:
        raise NullBillError(f"{name} shares the workload {before['workload']} in config.yaml; that one is the owner's to change by hand")

    old_rule = (config.get("power") or {}).get(name) or {}
    svc_title = _one_line(want.get("title") or old_rule.get("title") or name, "title", 40, problems, needed=True)
    what = _one_line(want.get("what", before.get("description")), "what", 200, problems)
    kind = want.get("kind", before.get("kind", "lxc"))
    if kind not in ("lxc", "vm"):
        problems.append("kind is lxc (a container, the usual choice) or vm (when the program needs a kernel of its own, such as Docker)")
    zone = str(want.get("zone", before.get("zone", ZONE)))
    own_zones = set(config.get("zones") or {}) - shipped("zones")
    if zone not in model["zones"] or zone in CLOSED_ZONES or (zone not in OPEN_ZONES and zone not in own_zones):
        problems.append(f"zone {zone!r}: a service of the owner's own goes in {' or '.join(OPEN_ZONES)}, or in a zone config.yaml defines")
    elif model["zones"][zone].get("egress", "none") == "direct":
        problems.append(f"zone {zone}: its guests reach the internet directly; a service of the owner's own goes out through the proxy")

    floor = dict(before.get("floor") or {"cores": 1, "memory_mib": 1024, "disk_gib": 8})
    for key, (least, most) in SIZE_LIMITS.items():
        value = want.get(key, floor.get(key))
        if not isinstance(value, int) or isinstance(value, bool) or not least <= value <= most:
            problems.append(f"{key} is a whole number from {least} to {most}")
        else:
            floor[key] = value

    ports = _ports(want.get("ports", own_ports(before)), model, name, problems)
    allow = want.get("proxy_allow", before.get("proxy_allow", []))
    if not isinstance(allow, list) or len(allow) > MAX_ALLOW or any(not isinstance(d, str) or not _host_name(d) for d in allow):
        problems.append(f"proxy_allow is a list of at most {MAX_ALLOW} host names; a leading dot also matches subdomains")
        allow = []
    elif allow and zone in model["zones"] and model["zones"][zone].get("egress", "none") != "proxy":
        problems.append(f"proxy_allow: zone {zone} has no way out, so there is nothing to allow")

    share = None
    asked = want.get("share", {k: v for k, v in before.get("share", {}).items() if k != "title"})
    if not isinstance(asked, dict) or set(asked) - {"what", "open", "note"}:
        problems.append("share is {what, open, note}: how it reads to a person it is given to")
    elif any("shared" in p["from"] for p in ports):
        share = {"title": svc_title}
        for key, limit in (("what", 120), ("open", 120), ("note", 200)):
            text = _one_line(asked.get(key), f"share.{key}", limit, problems)
            if text:
                share[key] = text
    elif asked and "share" in want:
        problems.append("share: no port lists shared in from, so nobody can be given this service")

    rule: dict[str, Any] = {"title": svc_title}
    for key, allowed in (("run", ("always", "on-demand")), ("stop", ("auto", "ask", "manual"))):
        value = want.get(key, old_rule.get(key, POWER_DEFAULTS[key]))
        if value not in allowed:
            problems.append(f"{key} is one of {', '.join(allowed)}")
        elif value != POWER_DEFAULTS[key]:
            rule[key] = value
    idle = want.get("idle_minutes", old_rule.get("idle_minutes", POWER_DEFAULTS["idle_minutes"]))
    if not isinstance(idle, int) or isinstance(idle, bool) or not 5 <= idle <= 10080:
        problems.append("idle_minutes is a number of minutes from 5 to 10080 (a week)")
    elif idle != POWER_DEFAULTS["idle_minutes"]:
        rule["idle_minutes"] = idle
    if "busy" in old_rule:
        rule["busy"] = old_rule["busy"]
    if rule.get("stop") == "auto" and kind == "vm" and "busy" not in rule:
        problems.append("stop auto: a VM does not show whether it is in use, so it cannot stop by itself; use stop ask")
    if rule.get("stop") == "auto" and kind == "lxc" and not any(p["proto"] == "tcp" for p in ports):
        problems.append(f"stop auto: {NO_AUTO}; use stop ask")

    if "id" in before:
        service_id = before["id"]
    else:
        service_id = free_id(model)
        if service_id is None:
            problems.append(f"every id for services of the owner's own is taken ({len(OWN_IDS)} of them); remove one first")
    if problems:
        raise NullBillError(*problems)

    # What the tool does not set stays as the owner wrote it.
    entry = {k: v for k, v in before.items() if k in ("enabled", "agent_managed", "listener", "image")}
    entry |= {"id": service_id, "kind": kind, "zone": zone, "workload": name}
    if what:
        entry["description"] = what
    entry["floor"] = floor
    if allow:
        entry["proxy_allow"] = list(dict.fromkeys(allow))
    if share:
        entry["share"] = share
    if ports:
        entry["ports"] = ports
    out = copy.deepcopy(config)
    out.setdefault("services", {})[name] = entry
    out.setdefault("power", {})[name] = rule
    # A new one is turned on; one that exists keeps its tick unless told.
    ticked = (config.get("intent") or {}).get("workloads", [])
    turn_on = want["on"] if "on" in want else (not before or name in ticked)
    out.setdefault("intent", {})["workloads"] = [w for w in ticked if w != name] + ([name] if turn_on else [])
    return out


def forget(config: dict, model: dict, name: str) -> dict:
    """config.yaml without a service of the owner's own: its entry, its
    workload's rule and tick once no other service has that workload, its
    size pin, and its tick for each person."""
    if name not in names(model):
        raise NullBillError(f"{name} is not a service of the owner's own; NullBill's own are turned off under Setup, What to run")
    out = copy.deepcopy(config)
    workload = model["services"][name]["workload"]

    def drop(section: str, key: str) -> None:
        if key in (out.get(section) or {}):
            del out[section][key]
            if not out[section]:
                del out[section]

    drop("services", name)
    drop("overrides", name)
    last = not any(svc.get("workload") == workload for svc in (out.get("services") or {}).values())
    if last and workload in workloads(model):
        drop("power", workload)
        if "workloads" in (out.get("intent") or {}):
            out["intent"]["workloads"] = [w for w in out["intent"]["workloads"] if w != workload]
    for person in (out.get("people") or {}).values():
        if name in person.get("services", []):
            person["services"] = [s for s in person["services"] if s != name]
            if not person["services"]:
                del person["services"]
    return out


def describe(model: dict, name: str, text: str | None) -> list[str]:
    """One of the owner's own services, line by line, as the owner reads it
    on the agent's request card and the agent in its list."""
    svc = model["services"][name]
    floor = svc["floor"]
    kind = "VM" if svc["kind"] == "vm" else "container"
    egress = model["zones"][svc["zone"]].get("egress", "none")
    cores = f"{floor['cores']} core{'' if floor['cores'] == 1 else 's'}"
    lines = [
        f"{title(model, name)} ({name}): a {kind} of its own in zone {svc['zone']}, {cores}, "
        f"{floor['memory_mib']} MiB of memory, {floor['disk_gib']} GiB of disk",
    ]
    if svc.get("description"):
        lines.append(svc["description"])
    who = {"role:owner": "you", "shared": "the people you tick it for"}
    for port in own_ports(svc):
        sources = [who.get(ref) or ref.replace("zone:", "every guest in zone ").replace("service:", "the guest ") for ref in port["from"]]
        lines.append(f"Opens {port['proto']}/{port['port']} to {' and '.join(sources)}")
    if not own_ports(svc):
        lines.append("Opens no port")
    if egress == "proxy":
        more = ", ".join(svc.get("proxy_allow", []))
        usual = "Internet: only through the proxy, to the server's usual list (Debian, GitHub, PyPI, npm and the like)"
        lines.append(f"{usual} and to {more}" if more else usual)
    else:
        lines.append("Internet: none")
    rule = {**POWER_DEFAULTS, **(model.get("power", {}).get(svc["workload"]) or {})}
    unused = f"after {rule['idle_minutes']} min unused"
    stops = {"auto": f"stops by itself {unused}", "ask": f"you are reminded {unused}", "manual": "runs until you stop it"}
    lines.append("Runs: always" if rule["run"] == "always" else f"Runs: on demand; {stops[rule['stop']]}")
    if text is None:
        lines.append("No setup script: an empty Debian guest, filled by hand")
    else:
        lines.append(f"Setup script: {len(text.splitlines())} lines, run as root in the guest (sha256 {digest(text)[:12]})")
    return lines
