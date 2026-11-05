"""What the runner dispatcher may do on the PVE host.

The dispatcher turns each queued Forgejo Actions job into one linked clone
of a pool service's golden template (kind vm-pool), and destroys the clone
once the job is done. Its API token lives in the dispatcher container and
reaches only what that takes:

- VM.Clone and VM.Audit on each golden template, which stays out of every
  pool, so the token can copy it but never start, change or destroy it;
- create, start, stop, set the SMBIOS serial of, and destroy VMs in the CI
  pool, which holds the clones and nothing else;
- allocate disk on the guest storage and attach to the pool zones' bridges.

The template is baked by apply (boot, configure, shut down, convert), so
OpenTofu creates it as a plain stopped VM and ignores the conversion.

Reference: https://pve.proxmox.com/pve-docs/chapter-pveum.html
"""

import shlex

from nullbill.probe import Runner
from nullbill.pveapi import audit, caller
from nullbill.render import strip_notes
from nullbill.results import Result

POOL = "nb-ci"
ROLE = "NullBillDispatcher"
# VM.Allocate covers creating (cloning into the pool) and destroying;
# smbios1, which hands a clone its job, is a VM.Config.HWType option.
PRIVILEGES = ["VM.Allocate", "VM.Audit", "VM.PowerMgmt", "VM.Config.HWType"]
SOURCE_ROLE = "NullBillCloneSource"
SOURCE_PRIVILEGES = ["VM.Audit", "VM.Clone"]
USER = "nb-dispatcher@pve"
TOKEN = "dispatcher"
TOKEN_ID = f"{USER}!{TOKEN}"
# Built-in roles: Datastore.AllocateSpace and SDN.Use, each with its Audit.
STORAGE_ROLE = "PVEDatastoreUser"
BRIDGE_ROLE = "PVESDNUser"

# Clone VMIDs: vmid_base + CLONE_OFFSET + 100 * service id + n. A pool can
# hold no more clones than its zone's DHCP range (.129-.190) has leases.
CLONE_OFFSET = 1000
MAX_CLONES = 62


def pools(plan: dict) -> dict[str, dict]:
    return {n: g for n, g in plan["guests"].items() if g["kind"] == "vm-pool"}


def clone_vmids(vmid_base: int, service_id: int, count: int) -> str:
    first = vmid_base + CLONE_OFFSET + 100 * service_id
    return f"{first}-{first + count - 1}"


# Where the runner_dispatcher role keeps the token and PVE's root CA.
TOKEN_FILE = "/etc/nullbill/dispatcher/pve-token"
CA_FILE = "/etc/nullbill/dispatcher/pve-root-ca.pem"
HANDOFF_PORT = 8443


def expectations(plan: dict) -> list[tuple[str, str, set[str]]]:
    """(label, ACL path, the privileges beyond Audit the dispatcher must hold)."""
    storage = plan["disk"]["storage"]
    out = [(f"dispatcher token may create, run and destroy VMs in {POOL} only", f"/pool/{POOL}", set(PRIVILEGES) - {"VM.Audit"})]
    for name, guest in plan["guests"].items():
        if guest["kind"] == "vm-pool":
            out.append((f"dispatcher token may clone the {name} template, nothing else", f"/vms/{guest['template_vmid']}", {"VM.Clone"}))
        else:
            out.append((f"dispatcher token cannot change {name}", f"/vms/{guest['vmid']}", set()))
    out.append((f"dispatcher token may only allocate disk on {storage}", f"/storage/{storage}", {"Datastore.AllocateSpace"}))
    out.append(("dispatcher token cannot change the host", "/", set()))
    return out


def _service_checks(plan: dict) -> list[tuple[str, str, str]]:
    """(label, command run in the dispatcher container, expected output)."""
    name = f"runner-dispatcher.{plan['domain']}"
    handoff = (
        f"curl -sS -o /dev/null -w '%{{http_code}}' --cacert /etc/nullbill/tls/root_ca.crt "
        f"--resolve {name}:{HANDOFF_PORT}:127.0.0.1 https://{name}:{HANDOFF_PORT}/handoff/0"
    )
    return [
        ("runner dispatcher is running", "systemctl is-active nb-dispatcher", "active"),
        # An unknown nonce gets nothing, over TLS with its step-ca certificate.
        ("runner dispatcher refuses an unknown nonce over TLS", handoff, "404"),
    ]


def enabled(plan: dict) -> bool:
    return bool(pools(plan)) and "runner-dispatcher" in plan["guests"]


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if not enabled(plan):
        return []
    labels = [label for label, _, _ in _service_checks(plan)] + [label for label, _, _ in expectations(plan)]
    return [Result("SKIP", label, reason) for label in labels]


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if not enabled(plan):
        return []
    vmid = plan["guests"]["runner-dispatcher"]["vmid"]
    results = []
    for label, command, want in _service_checks(plan):
        rc, out, err = run(target, f"pct exec {vmid} -- bash -c {shlex.quote(command)}")
        got = out.strip()
        results.append(Result("PASS", label) if got == want else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:120]))
    api = caller(plan, target, run, vmid, TOKEN_FILE, CA_FILE)
    return results + [audit(api, label, path, want) for label, path, want in expectations(plan)]
