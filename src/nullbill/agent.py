"""What the runtime agent may do on the PVE host.

The agent's API token lives in the operator container. It may read
everything (PVEAuditor on /) and change only the power state and snapshots
of guests in the agent-managed pool; the router, PBS, the CA and the
operator are never in that pool (plan.AGENT_FREE_ZONES). The token is
privilege-separated, so it holds only the ACLs given to it here, and those
never exceed its user's. apply and destroy use the owner's credential.

Reference: https://pve.proxmox.com/pve-docs/chapter-pveum.html
"""

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
