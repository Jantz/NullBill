# Keep a copy of the backup encryption key

The PVE host encrypts every backup before it reaches PBS. Without the key,
the backups cannot be read by anyone, including you. PVE keeps it in
`/etc/pve/priv/storage/nb-pbs.enc`, so losing the host's disk loses that copy.
apply's backups stage makes the key once with PVE's own client and keeps it
as `pbs_encryption_key` in `$NULLBILL_HOME/secrets.sops.yaml`. That copy is
on the nullbill container, which lives on the same host and whose own
backups need this key, so it does not help when the host is gone. Keep a
copy offline.

Reference: <https://pbs.proxmox.com/docs/backup-client.html#encryption>

The quick copy: the page's **Backups**, **The key**, **Download the key**
gives you the key as a file (`nullbill-backup-key.json`). Put it in a
password manager or on a USB stick kept away from the server, and never on
a disk that syncs. The page shows the key's fingerprint and when you last
downloaded it. In step 1 of a restore, that file is `nb-pbs.key.json`.

The paper copy is safer, since no file exists to leak. You do this
yourself, once, after the first setup run with backups:

1. In the Proxmox web UI, select the node `pve` (the host, `pve.internal`)
   and open **Shell**. Make a paper copy and keep it offline, never in the
   repo:

   ```sh
   proxmox-backup-client key paperkey /etc/pve/priv/storage/nb-pbs.enc --output-format text
   ```

   It prints the key as text and as a QR code. Print it or write it down.
   Do not save it as a file on a synced or backed-up disk.
2. Check the copy: the `fingerprint` line on the paper must equal the one
   `proxmox-backup-client key show /etc/pve/priv/storage/nb-pbs.enc`
   prints. The server reads a file back out of a backup after each night
   (the page's **Backups**, **Restore test**), and `nullbill verify` does
   on every run, which proves the key on the host still opens the
   backups.

## Restoring after losing the host

1. Reinstall PVE. Write the key from paper into `nb-pbs.key.json` (the
   JSON between the BEGIN and END lines).
2. Add the storage with the token secret from the PBS VM's
   `/root/nullbill/pbs-token.json`, or from a new token if the PBS VM is
   lost too (re-run the pbs role on a VM with the kept datastore disk;
   it adopts the existing chunk store):

   ```sh
   pvesm add pbs nb-pbs --server <pbs ip> --datastore backups \
     --username 'nb-pve@pbs!pve' --password '<token secret>' \
     --fingerprint '<PBS certificate fingerprint>' \
     --encryption-key nb-pbs.key.json
   ```

   `pvesm` reads the key from that file
   (<https://pve.proxmox.com/pve-docs/pvesm.1.html>); delete the file
   afterwards, PVE keeps its own copy.

3. Restore guests from the PVE UI (Storage > nb-pbs > Backups) or with
   `pct restore` and `qmrestore`.
