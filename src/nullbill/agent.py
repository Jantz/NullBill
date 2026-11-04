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
import shlex

from nullbill.probe import Runner
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


def _api(host_ip: str, method: str, path: str, data: str = "") -> str:
    """Shell for one API call as the agent; prints the status, then the body."""
    url = shlex.quote(f"https://{host_ip}:8006/api2/json{path}")
    body = f"--data {shlex.quote(data)} " if data else ""
    return (
        "set -e; f=$(mktemp); trap 'rm -f \"$f\"' EXIT; "
        f"curl -sS -m 10 -o \"$f\" -w '%{{http_code}}\\n' --cacert {CA_FILE} "
        f'-H "Authorization: PVEAPIToken=$(cat {TOKEN_FILE})" -X {method} {body}{url}; cat "$f"'
    )


def _privileges(body: str, path: str) -> set[str]:
    return set(json.loads(body or "{}").get("data", {}).get(path, {}))


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if "operator" not in plan["guests"]:
        return []
    names = [_label(n, g) for n, g in plan["guests"].items()]
    return [
        Result("SKIP", n, reason) for n in [*names, "agent token cannot change the host", "agent token is refused a snapshot of router"]
    ]


def _label(name: str, guest: dict) -> str:
    if guest.get("agent_managed"):
        return f"agent token may power-manage and snapshot {name}, nothing else"
    return f"agent token cannot change {name}"


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    operator = plan["guests"].get("operator")
    if operator is None:
        return []
    host_ip = plan["zones"]["mgmt"]["host_ip"]

    def api(method: str, path: str, data: str = "") -> tuple[str, str]:
        command = f"pct exec {operator['vmid']} -- bash -c {shlex.quote(_api(host_ip, method, path, data))}"
        rc, out, err = run(target, command)
        if rc != 0:
            return "error", (err or out).strip()[:120]
        status, _, body = out.partition("\n")
        return status.strip(), body

    results = []
    for name, guest in plan["guests"].items():
        vmid = guest.get("vmid") or guest["template_vmid"]
        path = f"/vms/{vmid}"
        status, body = api("GET", f"/access/permissions?path={path}")
        label = _label(name, guest)
        if status != "200":
            results.append(Result("FAIL", label, f"permissions query: {status} {body[:80]}"))
            continue
        changes = {p for p in _privileges(body, path) if not p.endswith(".Audit")}
        want = set(PRIVILEGES) if guest.get("agent_managed") else set()
        results.append(
            Result("PASS", label) if changes == want else Result("FAIL", label, f"holds {', '.join(sorted(changes)) or 'nothing'}")
        )
    status, body = api("GET", "/access/permissions?path=/")
    label = "agent token cannot change the host"
    if status != "200":
        results.append(Result("FAIL", label, f"permissions query: {status} {body[:80]}"))
    else:
        changes = {p for p in _privileges(body, "/") if not p.endswith(".Audit")}
        results.append(Result("FAIL", label, ", ".join(sorted(changes))) if changes else Result("PASS", label))
    # One real attempt outside the pool. PVE checks the ACL before it acts,
    # so a correct host changes nothing.
    status, body = api("GET", "/nodes")
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
