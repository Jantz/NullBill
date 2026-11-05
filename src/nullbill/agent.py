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


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if "operator" not in plan["guests"]:
        return []
    names = [label for label, _, _ in expectations(plan)]
    return [Result("SKIP", n, reason) for n in [*names, "agent token is refused a snapshot of router"]]


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    operator = plan["guests"].get("operator")
    if operator is None:
        return []
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
    return results
