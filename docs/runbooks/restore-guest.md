# Restore one guest

Two ways back, newest first: a snapshot on the host (seconds, same disk),
or the nightly backup on PBS (minutes; it survives a lost disk only when
the backups have a disk of their own, see [backups.md](backups.md)). Find the
guest's VMID in `$NULLBILL_HOME/plan.yaml` (VMID = 2000 + its id).

References: <https://pve.proxmox.com/pve-docs/chapter-vzdump.html#vzdump_restore>,
<https://pve.proxmox.com/pve-docs/pct.1.html>, <https://pve.proxmox.com/pve-docs/qm.1.html>

## From a snapshot

On the host:

```sh
pct listsnapshot <vmid>          # containers; qm listsnapshot for VMs
pct rollback <vmid> <snapshot>   # qm rollback <vmid> <snapshot>
```

The agent may take snapshots of agent-managed guests but not roll back, so
a rollback is always yours.

## From PBS

1. List the backups: `pvesm list nb-pbs --vmid <vmid>`. Each volid looks like
   `nb-pbs:backup/ct/<vmid>/<time>`.
2. Stop the guest: `pct stop <vmid>` or `qm stop <vmid>`.
3. Restore over it:

   ```sh
   pct restore <vmid> <volid> --force --storage <storage>   # containers
   qmrestore <volid> <vmid> --force --storage <storage>     # VMs
   ```

   `--force` overwrites the guest's current disks. Leave it out and use a
   free VMID to restore beside the original instead, then compare.
4. Start it and run `nullbill verify`. It checks the guest's flows, and that
   every guest has a fresh encrypted backup.

If the PVE host itself is lost, start with
[pbs-encryption-key.md](pbs-encryption-key.md): the backups cannot be read
without the key.
