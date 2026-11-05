"""What the runtime agent may do on the PVE host.

The agent's API token lives in the operator container. It may read
everything (PVEAuditor on /) and change only the power state and snapshots
of guests in the agent-managed pool; the router, PBS, the CA and the
operator are never in that pool (plan.AGENT_FREE_ZONES). The token is
privilege-separated, so it holds only the ACLs given to it here, and those
never exceed its user's. apply and destroy use the owner's credential.

Reference: https://pve.proxmox.com/pve-docs/chapter-pveum.html
"""

import json
import secrets
import shlex

from nullbill import ci
from nullbill.probe import Runner
from nullbill.pveapi import audit, caller
from nullbill.render import strip_notes
from nullbill.results import Result

POOL = "agent-managed"
ROLE = "NullBillAgent"
# VM.Snapshot covers creating and deleting snapshots; rollback is the
# separate VM.Snapshot.Rollback and is not granted.
PRIVILEGES = ["VM.PowerMgmt", "VM.Snapshot"]
USER = "nb-agent@pve"
TOKEN = "operator"
TOKEN_ID = f"{USER}!{TOKEN}"


def managed(plan: dict) -> dict[str, dict]:
    return {n: g for n, g in plan["guests"].items() if g.get("agent_managed")}


# Where the operator role puts the token ("user@realm!name=secret") and the
# PVE root CA. Only root in the operator container reads them.
TOKEN_FILE = "/etc/nullbill/agent/pve-token"
CA_FILE = "/etc/nullbill/agent/pve-root-ca.pem"
DENIED_SNAPSHOT = "nb-verify-denied"


def expectations(plan: dict) -> list[tuple[str, str, set[str]]]:
    """(label, ACL path, the privileges beyond Audit the agent must hold)."""
    out = []
    for name, guest in plan["guests"].items():
        managed = bool(guest.get("agent_managed"))
        if guest["kind"] == "vm-pool":
            # The template is in no pool; its clones are in the CI pool.
            out.append((f"agent token cannot change the {name} template", f"/vms/{guest['template_vmid']}", set()))
            if managed:
                out.append((f"agent token may power-manage and snapshot {name} clones, nothing else", f"/pool/{ci.POOL}", set(PRIVILEGES)))
            continue
        label = f"agent token may power-manage and snapshot {name}, nothing else" if managed else f"agent token cannot change {name}"
        out.append((label, f"/vms/{guest['vmid']}", set(PRIVILEGES) if managed else set()))
    out.append(("agent token cannot change the host", "/", set()))
    return out


# Loki's own port on the monitoring guest, behind the gate (the monitoring
# role's monitoring_loki_internal_port); verify asks it from inside.
LOKI = "http://127.0.0.1:3101"
# Polls Loki for the command verify ran as the agent and prints the uid
# labels of the streams that hold it; the shipper pushes within seconds.
LOKI_QUERY = """
import json, sys, time, urllib.parse, urllib.request
query = '{job="commands",host="%s"} |= "%s"'
url = "%s/loki/api/v1/query_range?" + urllib.parse.urlencode({"query": query, "since": "15m", "limit": "20"})
for _ in range(30):
    found = json.load(urllib.request.urlopen(url, timeout=5))["data"]["result"]
    if found:
        print(" ".join(sorted({s["stream"].get("uid", "") for s in found})))
        sys.exit(0)
    time.sleep(2)
sys.exit(1)
"""


def ssh_guest(plan: dict) -> str | None:
    """The agent-managed guest verify reaches over agent SSH."""
    return next((n for n, g in plan["guests"].items() if g.get("agent_managed") and g["kind"] != "vm-pool"), None)


def _labels(plan: dict) -> list[str]:
    names = [label for label, _, _ in expectations(plan)] + ["agent token is refused a snapshot of router"]
    guest = ssh_guest(plan)
    if guest:
        names += [f"agent reaches {guest} over SSH as agent", "agent SSH to router is refused"]
        if "monitoring" in plan["guests"]:
            names.append(f"the agent's command on {guest} is in Loki under its uid")
    return names


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if "operator" not in plan["guests"]:
        return []
    return [Result("SKIP", n, reason) for n in _labels(plan)]


def _as_agent(vmid: int, command: str) -> str:
    return f"pct exec {vmid} -- runuser -l agent -c {shlex.quote(command)}"


def ssh_check(plan: dict, target: str, run: Runner, marker: str) -> list[Result]:
    """nb-ssh from the operator as the agent: it reaches an agent-managed
    guest as `agent`, not the router, and the command lands in Loki under
    the uid that ran it."""
    guest = ssh_guest(plan)
    if guest is None:
        return []
    operator = plan["guests"]["operator"]["vmid"]
    domain = plan["domain"]
    nb_ssh = "nb-ssh -o ConnectTimeout=10"
    results = []
    label = f"agent reaches {guest} over SSH as agent"
    rc, out, err = run(target, _as_agent(operator, f"{nb_ssh} {guest}.{domain} 'echo {marker} && id -un && id -u'"))
    words = out.split()
    uid = words[2] if rc == 0 and words[:2] == [marker, "agent"] and len(words) == 3 else None
    results.append(Result("PASS", label) if uid else Result("FAIL", label, (err or out).strip()[:120] or f"exit {rc}"))
    label = "agent SSH to router is refused"
    rc, out, err = run(target, _as_agent(operator, f"{nb_ssh} router.{domain} 'echo {marker}'"))
    results.append(Result("PASS", label) if rc != 0 and marker not in out else Result("FAIL", label, "router ran the agent's command"))
    if "monitoring" not in plan["guests"]:
        return results
    label = f"the agent's command on {guest} is in Loki under its uid"
    if uid is None:
        results.append(Result("SKIP", label, "the agent's SSH command did not run"))
        return results
    script = LOKI_QUERY % (guest, marker, LOKI)
    rc, out, err = run(target, shlex.join(["pct", "exec", str(plan["guests"]["monitoring"]["vmid"]), "--", "python3", "-c", script]))
    if rc != 0:
        results.append(Result("FAIL", label, (err.strip() or "not found in 60 s")[:120]))
    elif out.split() != [uid]:
        results.append(Result("FAIL", label, f"uid labels {out.strip() or 'none'}, agent is {uid}"))
    else:
        results.append(Result("PASS", label, f"uid {uid}"))
    return results


def check(plan_with_notes: dict, target: str, run: Runner, marker: str | None = None) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    operator = plan["guests"].get("operator")
    if operator is None:
        return []
    # apply sets the operator up only once the owner gave the agent CLI a
    # credential; until then there is no token to check.
    rc, out, _ = run(target, f"pct exec {operator['vmid']} -- sh -c 'test -s {TOKEN_FILE} || echo unset'")
    if rc == 0 and out.strip() == "unset":
        return skipped(plan, "the operator is not set up yet; paste the agent's key on the page and run setup")
    api = caller(plan, target, run, operator["vmid"], TOKEN_FILE, CA_FILE)
    results = [audit(api, label, path, want) for label, path, want in expectations(plan)]
    # One real attempt outside the pool. PVE checks the ACL before it acts,
    # so a correct host changes nothing.
    status, body = api("GET", "/nodes", "")
    nodes = [n["node"] for n in json.loads(body or "{}").get("data", [])] if status == "200" else []
    router = plan["guests"].get("router")
    label = "agent token is refused a snapshot of router"
    if not nodes or router is None:
        results.append(Result("FAIL", label, f"cannot list nodes: {status} {body[:80]}"))
        return results
    status, body = api("POST", f"/nodes/{nodes[0]}/qemu/{router['vmid']}/snapshot", f"snapname={DENIED_SNAPSHOT}")
    if status == "403":
        results.append(Result("PASS", label))
    elif status.startswith("2"):
        results.append(Result("FAIL", label, f"PVE accepted it: delete snapshot {DENIED_SNAPSHOT} on {router['vmid']}"))
    else:
        results.append(Result("FAIL", label, f"{status} {body[:80]}"))
    return results + ssh_check(plan, target, run, marker or f"nb-verify-{secrets.token_hex(4)}")
