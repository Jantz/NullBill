"""The Services page's On demand: what the plan runs on demand, what the
host's nb-power says of each right now, and the owner's switches
(docs/adr/0013-on-demand.md).

The plan says what should run on demand; the host's rules are those of the
last setup run. Where they differ the page shows the host's, since those
are what Start and Stop act on, and says what the next setup run changes.
"""

import re
import shlex

from nullbill import power
from nullbill.errors import NullBillError

NAME = re.compile(r"[a-z][a-z0-9-]{0,40}")
ACTIONS = ("start", "stop", "keep")
# nb-power's last events shown under the switches.
EVENTS = 8


def rules(plan: dict) -> dict | None:
    """The plan's on-demand workloads, or None when it has none."""
    found = plan.get("power") or {}
    if not found.get("groups"):
        return None
    return {
        "headroom_mib": found["headroom_mib"],
        "together": found["together"],
        "groups": [
            {
                "name": name,
                "title": g["title"],
                "stop": g["stop"],
                "idle_minutes": g["idle_minutes"],
                "memory_mib": g["memory_mib"],
                "guests": list(g["guests"]),
            }
            for name, g in found["groups"].items()
        ],
    }


def view(planned: dict | None, live: dict | None, acting: dict[str, str], why: str | None = None) -> dict:
    """planned: `rules` of the saved plan. live: nb-power's status, or None
    when it was not asked or does not answer (`why` says which). acting:
    workload -> starting or stopping, for a switch that is being thrown."""
    wanted = {g["name"]: g for g in (planned or {}).get("groups", [])}
    groups = []
    if live is None:
        for g in wanted.values():
            groups.append({**g, "guests": [{"name": n} for n in g["guests"]], "state": "unknown"})
    else:
        for g in live["groups"]:
            entry = {**g, "guests": [{"name": m["name"], "state": m.get("state")} for m in g["guests"]]}
            if g["name"] not in wanted:
                entry["next"] = "always"
            elif any(wanted[g["name"]][k] != g[k] for k in ("stop", "idle_minutes")):
                entry["next"] = "rules"
            groups.append(entry)
        have = {g["name"] for g in live["groups"]}
        for name, g in wanted.items():
            if name not in have:
                groups.append({**g, "guests": [{"name": n} for n in g["guests"]], "state": "pending", "next": "on-demand"})
    for g in groups:
        if g["name"] in acting:
            g["acting"] = acting[g["name"]]
    titles = {g["name"]: g["title"] for g in groups}
    out = {
        "live": live is not None,
        "why": why,
        "headroom_mib": (live or planned or {}).get("headroom_mib", 0),
        "used_mib": (live or {}).get("used_mib"),
        "setup": bool((live or {}).get("setup")),
        "groups": groups,
        "events": [
            {"at": e["at"], "title": titles.get(e["group"], e["group"]), "what": e["what"], "why": e.get("why", "")}
            for e in reversed(((live or {}).get("events") or [])[-EVENTS:])
        ],
    }
    return out


def command(body: dict, known: set[str], by: str = "the page") -> tuple[str, str, str]:
    """(action, workload, nb-power's command line) for what the page asks."""
    action, name = body.get("action"), str(body.get("name", ""))
    if action not in ACTIONS:
        raise NullBillError(f"send action: {', '.join(ACTIONS)}")
    if not NAME.fullmatch(name) or name not in known:
        raise NullBillError(f"{name or 'that'} does not run on demand on the server; run setup if you just changed it")
    argv = [power.COMMAND, action, name]
    if action == "keep":
        if not isinstance(body.get("on"), bool):
            raise NullBillError("send on: true or false")
        argv.append("on" if body["on"] else "off")
    else:
        argv += ["--by", by]
    if action == "start":
        agreed = body.get("stop") or []
        if not isinstance(agreed, list) or any(not NAME.fullmatch(str(n)) or str(n) not in known for n in agreed):
            raise NullBillError("stop: the workloads to stop first, as shown")
        if agreed:
            argv += ["--stop", ",".join(str(n) for n in agreed)]
        if body.get("keep") is True:
            argv.append("--keep")
    return str(action), name, shlex.join([*argv, "--json"])
