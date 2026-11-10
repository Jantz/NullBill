"""On demand: a workload that stays installed and runs only when wanted
(docs/adr/0013-on-demand.md).

catalog.yaml's `power` says, per workload, whether it always runs or runs
on demand, and how it stops again. plan counts what always runs as before
and asks of each on-demand workload only that it fits beside that; which
of them run at once is decided on the host, by host_baseline's nb-power,
from the rules this module renders (`policy`, written to POLICY_FILE by
apply's host stage). nb-power starts and stops their guests, sees use
(each container's nb-busy, a pool's running clones, a VM's CPU), stops
what its rule lets it stop, and exports what the monitoring role's alerts
and the page read. verify asks it what is off, so a check of a guest that
is off on purpose reports SKIP.
"""

import json
import shlex
from typing import Any

from nullbill.probe import Runner
from nullbill.render import Note, strip_notes
from nullbill.results import Result

ALWAYS, ON_DEMAND = "always", "on-demand"
AUTO, ASK, MANUAL = "auto", "ask", "manual"
COMMAND = "/usr/local/sbin/nb-power"
POLICY_FILE = "/etc/nullbill/power.json"
TIMER = "nb-power.timer"
# nb-power start's exit when the workload does not fit beside what runs;
# its answer names what would have to stop.
NO_ROOM = 3
# verify: the timer runs nb-power every minute; this long without a run
# means it has stopped.
STALE_S = 300
# apply's hold: a setup run that died is given up on after this long, and
# what it started is stopped again.
HOLD_S = 6 * 3600


def groups(model: dict, planned: dict[str, dict], errors: list[str]) -> dict[str, list[str]]:
    """The planned workloads that run on demand, each with its guests in the
    order they start. Collects what is wrong with `power` in config.yaml."""
    workloads = {svc["workload"] for svc in model["services"].values()}
    out: dict[str, list[str]] = {}
    for name, entry in model["power"].items():
        where = f"power.{name}"
        if name == "core":
            errors.append(f"{where}: core guests always run")
            continue
        if name not in workloads:
            errors.append(f"{where}: no service has the workload {name!r}")
            continue
        members = [n for n, svc in planned.items() if svc["workload"] == name]
        if entry["run"] != ON_DEMAND or not members:
            continue
        switches = [n for n in members if planned[n]["kind"] != "vm-pool"]
        if not switches:
            errors.append(f"{where}.run: nothing to start or stop: {', '.join(members)} only makes clones")
            continue
        # A container says whether it is in use, a pool's clones show it; a
        # VM by itself shows only its CPU.
        seen = any(planned[n]["kind"] != "vm" for n in members) or "cpu_pct" in entry.get("busy", {})
        if entry["stop"] == AUTO and not seen:
            errors.append(
                f"{where}.stop: auto needs a way to see that {entry['title']} is in use, and a VM shows none; "
                f"set {where}.busy.cpu_pct, or stop: ask"
            )
        out[name] = members
    return out


def section(model: dict, found: dict[str, list[str]], mib: dict[str, int], headroom_mib: int) -> Note | None:
    """The plan's `power`: what runs on demand and the memory they share.
    mib: what the plan counts for each of them while it runs. None, so the
    plan has no such key, while nothing runs on demand."""
    if not found:
        return None
    entries = {}
    for name, members in found.items():
        rule = model["power"][name]
        entry: dict[str, Any] = {
            "title": rule["title"],
            "guests": members,
            "memory_mib": mib[name],
            "stop": rule["stop"],
            "idle_minutes": rule["idle_minutes"],
        }
        if "cpu_pct" in rule.get("busy", {}):
            entry["cpu_pct"] = rule["busy"]["cpu_pct"]
        entries[name] = Note(entry, _rule_note(rule))
    total = sum(e.value["memory_mib"] for e in entries.values())
    together = total <= headroom_mib
    return Note(
        {
            "headroom_mib": Note(headroom_mib, "What the guests that always run leave; what runs on demand shares it."),
            "together": Note(
                together,
                "Everything on demand fits at once."
                if together
                else f"Not everything on demand fits at once ({total} MiB): starting one may mean stopping another; the page asks first.",
            ),
            "groups": entries,
        },
        "Installed, off until started on the page (Services); the host's nb-power starts and stops them.",
    )


def _rule_note(rule: dict) -> str:
    minutes = rule["idle_minutes"]
    span = f"{minutes // 60} h" if minutes % 60 == 0 else f"{minutes} min"
    if rule["stop"] == AUTO:
        return f"Stops by itself after {span} without use."
    if rule["stop"] == ASK:
        return f"Never stops by itself; you are told once it has gone {span} without use."
    return "Runs until you stop it."


def on_demand(plan: dict) -> dict[str, str]:
    """guest -> its workload, for every guest the (bare) plan runs on demand."""
    found = (plan.get("power") or {}).get("groups") or {}
    return {guest: name for name, group in found.items() for guest in group["guests"]}


# Guests whose service has a step-ca certificate (apply.TLS; a test keeps
# the two equal). They last 24 h, so one runs out while its guest is off;
# nb-power has step-ca issue it anew when it starts the guest.
CERT_GUESTS = ("postgres", "monitoring", "forgejo", "runner-dispatcher", "model-gateway")


def policy(plan_with_notes: dict) -> dict:
    """nb-power's rules, from the plan."""
    plan = strip_notes(plan_with_notes)
    power = plan.get("power") or {}
    out: dict[str, Any] = {"headroom_mib": power.get("headroom_mib", 0), "groups": {}}
    ca = plan["guests"].get("step-ca")
    if ca and power.get("groups"):
        out["ca"] = {"vmid": ca["vmid"], "url": f"https://step-ca.{plan['domain']}"}
    for name, group in (power.get("groups") or {}).items():
        guests = []
        for member in group["guests"]:
            g = plan["guests"][member]
            if g["kind"] == "vm-pool":
                first, last = (int(v) for v in g["clone_vmids"].split("-"))
                guests.append({"name": member, "kind": "vm-pool", "first": first, "last": last, "memory_mib": g["memory_mib"]})
            else:
                entry = {"name": member, "kind": g["kind"], "vmid": g["vmid"], "memory_mib": g["memory_mib"]}
                if member in CERT_GUESTS:
                    entry["certs"] = [f"{member}.{plan['domain']}"]
                guests.append(entry)
        out["groups"][name] = {
            "title": group["title"],
            "memory_mib": group["memory_mib"],
            "stop": group["stop"],
            "idle_minutes": group["idle_minutes"],
            **({"cpu_pct": group["cpu_pct"]} if "cpu_pct" in group else {}),
            "guests": guests,
        }
    return out


def render_policy(plan: dict) -> str:
    return json.dumps(policy(plan), indent=2, sort_keys=True) + "\n"


def status(target: str, run: Runner) -> dict | None:
    """What nb-power says is on and off, or None when it does not answer."""
    rc, out, _ = run(target, f"{COMMAND} status --json")
    if rc != 0:
        return None
    try:
        found = json.loads(out)
    except ValueError:
        return None
    return found if isinstance(found, dict) and isinstance(found.get("groups"), list) else None


def off_guests(found: dict | None) -> dict[str, str]:
    """guest -> its workload's title, for each on-demand guest that is not
    running. A pool's clones come and go; its group is off when nothing in
    it runs."""
    out = {}
    for group in (found or {}).get("groups", []):
        for guest in group["guests"]:
            if guest["kind"] == "vm-pool":
                if group["state"] == "off":
                    out[guest["name"]] = group["title"]
            elif guest["state"] != "running":
                out[guest["name"]] = group["title"]
    return out


class Off(Exception):
    """A check reached into a guest that runs on demand and is off."""

    def __init__(self, guest: str, title: str):
        super().__init__(f"{guest} is off")
        self.guest, self.title = guest, title


def guard(plan: dict, run: Runner, off: dict[str, str]) -> Runner:
    """run, except that a command into a container that is off (`pct exec
    <vmid> ...`) raises Off instead of failing there: verify then reports
    that check SKIP with the reason."""
    asleep = {str(plan["guests"][name]["vmid"]): name for name in off if name in plan["guests"]}

    def guarded(target: str, command: str) -> tuple[int, str, str]:
        words = command.split(None, 3)
        if words[:2] == ["pct", "exec"] and len(words) > 2 and words[2] in asleep:
            raise Off(asleep[words[2]], off[asleep[words[2]]])
        return run(target, command)

    return guarded


def off_reason(title: str) -> str:
    return f"{title} is off (it runs on demand); start it on the page, under Services, to check it"


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    return [Result("SKIP", name, reason) for name in _check_names(plan)] if plan.get("power") else []


def _check_names(plan: dict) -> list[str]:
    names = ["the host starts and stops what runs on demand", "the host's on-demand rules are the plan's"]
    names += [f"{guest} does not start with the server" for guest, _ in _switches(plan)]
    names.append("what runs on demand fits the memory left for it")
    return names


def _switches(plan: dict) -> list[tuple[str, dict]]:
    return [(n, plan["guests"][n]) for n in on_demand(plan) if plan["guests"][n]["kind"] != "vm-pool"]


def check(plan_with_notes: dict, target: str, run: Runner, found: dict | None) -> list[Result]:
    """verify: nb-power's timer runs and has run lately, its rules are the
    plan's, no on-demand guest starts at boot, and what runs fits."""
    plan = strip_notes(plan_with_notes)
    if not plan.get("power"):
        return []
    names = _check_names(plan)
    results = []
    rc, out, err = run(target, f"systemctl is-active {TIMER}")
    if rc != 0 or out.strip() != "active":
        results.append(Result("FAIL", names[0], f"{TIMER} is {out.strip() or err.strip()[:80] or 'not running'}; run setup"))
    elif found is None:
        results.append(Result("FAIL", names[0], f"{COMMAND} status does not answer; run setup"))
    elif found.get("now", 0) - (found.get("ticked") or 0) > STALE_S:
        results.append(Result("FAIL", names[0], f"nb-power last ran {int(found['now'] - (found.get('ticked') or 0)) // 60} min ago"))
    else:
        results.append(Result("PASS", names[0]))
    rc, out, err = run(target, f"cat {shlex.quote(POLICY_FILE)}")
    if rc != 0:
        results.append(Result("FAIL", names[1], f"cannot read {POLICY_FILE}: {(err or out).strip()[:120]}"))
    elif out != render_policy(plan_with_notes):
        results.append(Result("FAIL", names[1], f"{POLICY_FILE} differs from the plan; run setup"))
    else:
        results.append(Result("PASS", names[1]))
    for guest, g in _switches(plan):
        name = f"{guest} does not start with the server"
        tool = "pct" if g["kind"] == "lxc" else "qm"
        rc, out, err = run(target, f"{tool} config {g['vmid']}")
        if rc != 0:
            results.append(Result("FAIL", name, f"cannot read its config: {(err or out).strip()[:120]}"))
        elif "onboot: 1" in out.splitlines():
            results.append(Result("FAIL", name, "it starts at boot (onboot: 1); run setup"))
        else:
            results.append(Result("PASS", name))
    if found is None:
        results.append(Result("SKIP", names[-1], "nb-power does not answer"))
    elif found["used_mib"] > found["headroom_mib"]:
        running = ", ".join(g["title"] for g in found["groups"] if g["state"] != "off")
        over = found["used_mib"] - found["headroom_mib"]
        results.append(Result("DEGRADED", names[-1], f"{running} run at once, {over} MiB more than the plan counts; stop one on the page"))
    else:
        results.append(Result("PASS", names[-1]))
    return results
