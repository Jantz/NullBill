# ADR 0017: Backups without a second disk, and a page for them

Status: accepted (the owner asked for "the most needed, wanted things",
2026-11-10; on his server backups were off and `verify` said so on every
run; the design is Claude's and he has not reviewed it)

## Context

Backups needed a disk of their own (`disks.<id>.use: pbs`, or a blank
disk). A server with one free disk had none: plan said DEGRADED, the page
said "tick only with a backup disk", and nothing was backed up. The
owner's server is that case. Its second disk is a 4 TB NTFS disk from his
old PC, over 90% full of his own files, which NullBill keeps and never
formats (ADR 0007).

Most of what an owner loses on such a server is not lost to a dead disk.
It is lost to a setup run that deletes a guest, an update that breaks a
database, a game world a friend griefed, a repository force-pushed. A
backup on the same disk gets all of that back. It does not survive the
disk, and has to say so.

What was there already and stays: Proxmox Backup Server in a VM, a nightly
job from the host, encrypted on the host before it leaves
(<https://pbs.proxmox.com/docs/backup-client.html#encryption>), a token
that may write and not delete, pruning on PBS, and `verify`'s restore of
one file.

What was checked before deciding (2026-11-10):

- The datastore is one disk of the pbs VM, found by its serial
  (`nb-backups`). OpenTofu places it on any PVE storage; the role formats
  it once and adopts it after (`ansible/roles/pbs`, its check rebuilds
  over a kept datastore).
- PVE moves a VM's disk to another storage while the VM runs and deletes
  the source after (`qm disk move`, `--delete`,
  <https://pve.proxmox.com/pve-docs/qm.1.html>), and grows one in place
  (`qm disk resize`). ext4 grows mounted (`resize2fs`).
- ntfs-3g is a FUSE driver. A VM disk image, or a second copy of every
  chunk, on the kept disk would put PBS's many small synchronous writes
  through it, need the disk read-write, and leave the NTFS journal open
  when the power goes, after which NullBill refuses to mount it for
  writing until Windows or `discard_journal` has seen it
  (docs/runbooks/legacy-disk.md).
- In the sandbox: plan, render and apply's commands against a recording
  host (`tests/test_plan.py`, `tests/test_backups.py`,
  `tests/test_apply.py`); the host's restore test against a stand-in
  client (`tests/test_backup_metrics.py`); the alert rules with promtool;
  the pbs role in Debian 13, where growing the filesystem offline works
  and `resize2fs` says "Nothing to do!" when there is nothing to grow;
  the page in Chromium against stand-ins.

Not checked, because nothing here can: `qm disk move` and `qm disk resize`
on Proxmox VE; `resize2fs` on the mounted datastore (the sandbox's kernel
refuses an online resize); a USB disk as an LVM storage across restarts;
the host's hourly restore test against a real PBS.

## Decision

- **Backups work on one disk.** Without a disk of their own the datastore
  is a volume beside the guests, on the storage they use. 64 GiB unless
  `backups.size_gib` says otherwise (16 to 4096); less when the disk has
  less left after every guest's floor, and none under 16 GiB, which is the
  old DEGRADED with nothing backed up. Plan reserves it before it sizes
  CI pools. Plan's status for it is `same-disk`.
- **It says what it is.** `verify` reports DEGRADED "the backups are on
  the disk the server runs from, so a failing disk takes both; plug in a
  disk for them" on every run, and still runs every backup-age check and
  the restore. The Backups page says the same under **Where**. This never
  reads as healthy.
- **A disk of their own is taken up when it appears.** Any blank disk
  (inside the case or on USB), a disk the owner tells the page to erase
  (typed word, as before), or a pinned one becomes `nb-pbs-disk` as
  before. apply then moves the datastore volume there with its history
  while the backup server runs, grows it to the disk, and only then lets
  OpenTofu compare. The volume never shrinks; a plan that asks for less
  than exists stops the run. The page's **Look for a new disk** is a
  probe.
- **One disk holds the backups, never a shared one.** Not the kept disk
  (see Context: its driver, its journal, and it holds the only copy of the
  owner's files), not a storage disk, not a folder next to anything.
  Converting a kept disk (ADR 0007: copy its files onto the server, then
  erase) stays the way to reuse one.
- **What is backed up.** Every guest that lives on, except the backup
  server itself, CI images and clones, and any service with
  `backup: false`. The catalog sets that for `inference`: setup builds it
  again from pins, and its model is a download of several GiB that would
  eat a small datastore. Kept disks are not backed up, as before; the
  copies made of one are. The Backups page lists both sides.
- **A restore test every night, on the server.** The host's hourly
  `nb-backup-metrics` restores one file from one container's newest
  backup once per night's backups, a different container each time, and
  tries again the next hour when it fails. It exports what it found and
  when, plus the datastore's size and use. Two alerts: `BackupRestoreFailing`
  (page) and `BackupStoreAlmostFull` (warn, over 90%). `verify` keeps its
  own restore. Before, a backup was read back only when someone ran
  `verify`.
- **A Backups page.** Where they go, the newest backup per guest, the
  last restore test, the room, each with when the server measured it;
  turning them on and choosing the place; what is and is not backed up;
  how to get something back. Overview shows a Backups cell and, while
  they are off, a row saying nothing is backed up. Turning them off
  deletes them and stays under Setup, What to run, with its question.
- **The key is the owner's to take from the page.** Backups are encrypted
  with a key the host makes. The page offers it as a file to a signed-in
  owner and records when that key was last downloaded
  (`web/backup-key.json`, with its fingerprint), which is a fact the page
  can check; whether the file is somewhere safe is not, and the page says
  so. The page agent's `backups` tool reads everything on the page except
  the key.

## Not built

- **A copy off the server** (a second PBS, or S3-compatible storage the
  owner supplies; PBS 4 has remotes and sync jobs,
  <https://pbs.proxmox.com/docs/managing-remotes.html>). It is the only
  thing that covers fire and theft, and it needs an account or a second
  machine of the owner's. Issue #105 holds it.
- **Rotating USB disks** (one at home, one elsewhere). Needs removable
  datastores and a way to say which disk is in. Same issue.
- **The backup server on demand** stays as OPEN_QUESTIONS.md has it. It
  takes 2 GiB of a 16 GiB server all day.
- **Shrinking the datastore**, or moving it back from a disk onto the
  server's. The page refuses the first and offers no way to the second;
  turning backups off and on again starts over.

## Consequences

- A server with one free disk now plans a pbs VM (2 GiB, counted in full)
  and 64 GiB that it did not before. Where that does not fit, plan says
  which and the owner unticks something or turns backups off.
- The small fixture and every plan without a pinned or blank disk change:
  their goldens now hold the backup server.
- `inference` is no longer in the nightly job on servers that have it.
- An owner who downloads the key has it as a file. The paper copy
  (docs/runbooks/pbs-encryption-key.md) remains the safer way and the
  guide says so.
