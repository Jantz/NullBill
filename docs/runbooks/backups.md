# Backups

Every night at 02:00 the server backs up each guest that lives on, encrypted
before it leaves Proxmox, to a backup server (Proxmox Backup Server) that
runs on it as a small VM. It keeps 7 daily, 4 weekly and 6 monthly backups
of each guest. Why it is built this way:
[ADR 0017](../adr/0017-backups-on-one-disk.md).

| What | Where |
|---|---|
| Turn backups on, choose where they go, see whether they work | the page's **Backups** |
| Turn them off (deletes them) | the page's **Setup**, **What to run**, **Nightly backups** |
| Get one guest back | [restore-guest.md](restore-guest.md) |
| Keep the key, and what to do after losing the server | [pbs-encryption-key.md](pbs-encryption-key.md) |

## Turn them on

**You need:** the hardware read and the choices saved (Setup). No second
disk.

1. Open **Backups**. Under **Where they go**, pick a place:
   - **The server's own disk**: works at once on any server. 64 GiB unless
     you type another size (at least 16). These backups bring back a broken
     service, a bad update or something deleted by mistake. They do not
     survive that disk: the page and `nullbill verify` say so for as long
     as they live there.
   - **A blank disk**: listed when the server has one. Backups on a disk
     of their own survive the server's disk.
   - **A disk that holds something**: NullBill erases it only when you type
     the last six characters of its id there.
   - A disk whose files NullBill keeps and shares to you is never offered.
     To reuse one, copy its files off first:
     [legacy-disk.md](legacy-disk.md), Reuse it for backups or as storage.
2. Press **Turn on backups**, then **Review and run setup** and start the
   run. It makes the backup server and takes a first backup of every
   guest; that first backup is the longest part.
3. Back on **Backups**, download the key under **The key** and keep the
   file somewhere that is not this server: a password manager, or a USB
   stick in a drawer. Without it nobody can read the backups, you
   included. The page records that you downloaded it, not where you put
   it. A paper copy is safer still:
   [pbs-encryption-key.md](pbs-encryption-key.md).

## Give them a disk of their own later

1. Plug a disk into the server. A USB disk works; leave it plugged in.
2. On **Backups**, press **Look for a new disk**. It appears under
   **Where they go**. A blank one is taken by itself; one that holds
   something asks for its six characters.
3. **Review and run setup**. The run moves the backups onto the disk with
   their history while the backup server keeps running, and gives them
   the whole disk. A USB hard disk makes that the longest part of the
   run.

The room for the backups can grow and never shrinks. The backups cannot go
back from a disk onto the server's; turning them off and on again starts
over with none.

## Read the page

**Now** shows what the server measured, and when (it measures every hour
and at the end of a setup run):

- **Newest backups**: how many guests have one from the last 26 hours, and
  which do not.
- **Restore test**: after each night the server reads one file back out of
  one container's newest backup, a different container each night. This
  is the proof that the backups and the key work together.
- **Room**: how full the backups' space is. Old backups are cleared out
  daily.

Your phone hears about a guest without a fresh backup, a restore test that
fails for two hours, and room over 90% full.

## What is and is not backed up

The page lists both under **What is backed up**. Not backed up:

- **Local AI models**: setup builds that guest again, and its model is a
  download.
- **CI runner images**: built again each week.
- **The backup server itself**.
- **Disks whose files you keep**: they hold your only copy. NullBill does
  not back them up. Copies NullBill made of one onto the server are backed
  up.
- **Proxmox itself and its settings**: a reinstall and a setup run make
  them again from `config.yaml`.

To leave one more service out, set `services.<name>.backup: false` in
`config.yaml`.

## What this does not cover

Fire, theft, or both disks failing. That needs a copy somewhere else, which
NullBill does not make yet (issue #105).
