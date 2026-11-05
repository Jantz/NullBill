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
