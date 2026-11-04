# Keep a copy of the backup encryption key

The PVE host encrypts every backup before it reaches PBS. Without the key,
the backups cannot be read by anyone, including you. PVE keeps it in
`/etc/pve/priv/storage/nb-pbs.enc`, so losing the host's disk loses that copy.
apply is to create the key on the controller and keep it in `$NULLBILL_HOME`
(issue #16), which makes a second copy. Keep a third offline.

Reference: <https://pbs.proxmox.com/docs/backup-client.html#encryption>

You do this yourself, once, after the first nightly backup:

1. Print a paper copy on the host and keep it offline, never in the repo:

   ```sh
   ssh root@pve.<your domain> proxmox-backup-client key paperkey /etc/pve/priv/storage/nb-pbs.enc --output-format text
   ```

   It prints the key as text and as a QR code. Print it or write it down.
   Do not save it as a file on a synced or backed-up disk.
2. Check the copy: the `fingerprint` line on the paper must equal the one
   `ssh root@pve.<your domain> proxmox-backup-client key show /etc/pve/priv/storage/nb-pbs.enc`
   prints. `nullbill verify` restores a file every run, which proves the
   key on the host still opens the backups.

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
     --encryption-key "$(cat nb-pbs.key.json)"
   ```

3. Restore guests from the PVE UI (Storage > nb-pbs > Backups) or with
   `pct restore` and `qmrestore`.
