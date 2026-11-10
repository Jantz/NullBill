# Keep a disk from your old PC

A disk you used in a previous PC, say the Windows disk with your photos,
can go into the server as it is. NullBill mounts it on Proxmox and shares
its files to you over Tailscale. It never formats, repairs or wipes such a
disk. The design and its limits are in
[ADR 0007](../adr/0007-legacy-disks.md).

What it can keep: partitions with NTFS, exFAT or FAT (FAT32). Windows' own
partitions (EFI, Microsoft reserved, recovery) are left alone. BitLocker,
Linux file systems (ext4, LVM, ZFS, RAID members) and Storage Spaces are not
kept; the page names them and leaves them alone.

**NullBill does not back a kept disk up.** The nightly backups cover the
guests, not the files on this disk. Keep a copy of anything you cannot lose
somewhere else, as you did before.

## Before you move the disk

On the old PC, if it still starts, shut Windows down fully:

```
shutdown /s /t 0
```

A normal shutdown with Fast Startup on (the Windows default) leaves the disk
half asleep. NullBill then keeps it read-only and refuses to let you change
files on it, so that neither side overwrites the other's changes. If you only
want to read the files, this does not matter.

If the old PC no longer starts, or the disk is in the server already, see
[Change files on it](#change-files-on-it) below: the page tells how Windows
left the disk and what can be done.

## Steps

1. Put the disk in the server and start it. Proxmox leaves it alone.
2. On the page's **Setup** page, press **Read the hardware** again. The
   **What to run** card lists the disk under **Other disks** with its
   partitions, labels, sizes and health.
3. Choose **Keep its files and share them to me**. Tick **Let me change
   files on it** only if you need to (see [Change files on
   it](#change-files-on-it)). Press **Save and plan** and check the **Plan** card: the disk
   shows as `use: legacy` with each partition it keeps, and a `files` guest
   appears.
4. **Run setup**. The tofu stage mounts the disk under `/mnt/nb-legacy` on
   Proxmox, read-only unless you chose otherwise, and the files stage shares
   it.
5. On the page's **Services** page, the **Kept disk files** card shows
   each share's address and the user `nbshare`. Press **Show password** (or
   **Copy**) and keep it in your password manager.

## Open the files

From a device signed in to your Tailscale. The `files` guest sits in
NullBill's own network, which the home network has no route to, so a
device at home needs Tailscale too:

- **Windows**: in File Explorer's address bar, type `\\10.42.1.23\data`
  (the address the page shows). Sign in as `nbshare` with the password.
  To keep it, right-click **This PC**, **Map network drive**.
- **macOS**: Finder, **Go**, **Connect to Server**, `smb://10.42.1.23/data`.
- **Linux**: the file manager's **Other Locations**,
  `smb://10.42.1.23/data`.
- **iPhone, iPad**: Files, the **...** menu, **Connect to Server**.
- **Android**: a file manager with SMB, such as the one built into many
  phones, or any SMB client app.

Only SMB 3 with encryption gets in; very old clients (Windows 7, old NAS
apps) cannot connect. No guest or anonymous access; wrong passwords show up
in the failed-login alert like a wrong SSH key does.

## Change files on it

The share is read-only until you tick **Let me change files on it** on the
**Setup** page, under **What to run**, and run setup. Only NTFS can be
changed, and only once Windows has shut the disk down fully. NullBill
checks that each time it reads the hardware, without writing to the disk,
and the disk's card says what it found:

- **Checked: Windows shut this disk down fully.** Tick the box, press
  **Save and plan**, run setup. Setup stops the share and Jellyfin for a
  moment, mounts the disk read-write and starts them again. No file is
  touched.
- **Not checked yet.** The server reads this only once it keeps an NTFS
  disk. Run setup once with the disk kept read-only, press **Read the
  hardware**, and look again. If you tick the box anyway, setup checks
  before it changes anything and stops there if the disk is not fit; the
  share stays up, read-only.
- **Windows did not shut this disk down fully.** This is what a normal
  **Shut down** with Fast Startup leaves (the Windows default), and also
  hibernation or pulling the disk while Windows runs. The disk may hold
  changes Windows has not finished writing. Two ways out:

  1. **Lose nothing.** Connect the disk to any Windows PC (a USB dock will
     do), wait until it shows in File Explorer, then shut that PC down with
     `shutdown /s /t 0`. Put the disk back, press **Read the hardware**; the
     card then says it was shut down fully.
  2. **That Windows is gone, or the disk cannot be moved.** Tick **Discard
     what Windows left unfinished** and type the end of the disk's name.
     Setup then empties the journal Windows left on the disk (`ntfsfix -d`)
     and mounts it read-write. Every file you can open today stays as it
     is. What is lost are changes Windows had not finished writing; after a
     normal Shut down there are usually none, after a crash or a pulled
     cable there may be some. Do not use this while a hibernated Windows
     that had this disk open will start again with it: it would write its
     old view over your changes. The confirmation counts for the disk as it
     is now. If Windows uses the disk again, you are asked again.
- **A partition holds a hibernated Windows**, or **Windows marked this disk
  for a disk check**. NullBill does not clear these. Start that Windows and
  shut it down fully, or run `chkdsk /f` on the disk from a Windows PC, then
  read the hardware again.

Until one of these is done the disk stays as it is, read-only, and setup
does not fail because of it unless the box is ticked.

Jellyfin always sees the disk read-only. Files you add over the share show
up in Jellyfin after its next library scan.

## If the disk goes missing

If the disk is unplugged or fails to start, the server still boots, the share
shows an empty folder, and the alert **LegacyDiskMissing** reaches ntfy after
10 minutes. Nothing is written in its place: the empty mount point refuses
writes. Plug it back in and run setup.

How full the disk is with your own files raises no alert. Only a share you
can write to with under 1 GiB left does (**KeptDiskFull**, a warning):
nothing more can be saved to it until you delete or move files.

Each probe reads the disk's SMART health. If the page says the disk is
failing (the plan shows `health: FAILED`), copy what you need off it soon.
There is no alert for this between probes yet.

## Stop keeping it

Choose **Leave it alone** and run setup. The share goes away and the disk is
unmounted; its files are not touched. Then you can take it out.

## Reuse it for backups or as storage

To erase a kept disk, **What to run** on the **Setup** page offers two
choices for it, and for each, whether the disk then holds the nightly
backups or becomes extra storage in Proxmox (an LVM-thin storage called
`nb-data`, for volumes you put there).
Both ask you to type the end of its name.

- **Copy its files onto the server, check the copy, then erase the disk.**
  Setup copies each kept partition to a new volume on the server's storage
  (the page shows about how much room it takes), compares the copy with the
  disk, and only then erases the disk. A copy of a full disk takes hours;
  the run prints a line a minute, and the files guest is stopped meanwhile.
  If anything differs, setup stops and the disk is left as it was. The copy
  is shared under the same name as before, read-write, and is backed up
  every night from then on. If the disk becomes storage, the copy then moves
  onto it, checked again, and the first copy is deleted; setup takes about
  twice as long.
- **Erase everything on it without a copy.** For when you have its files
  elsewhere. The plan says how many GiB are lost.

The copy needs room: the files plus 5 %, and the plan shrinks the CI runners'
space to make it. If it does not fit, the plan says so and nothing happens.
Files Windows stored compressed (NTFS compression) take their full size in
the copy, more than the disk shows as used; if the copy runs out of room it
stops, nothing is recorded and the disk is not erased.
Copying works when the server's storage is LVM, the Proxmox default; on a ZFS
install, copy the files off yourself over the share, then choose the erase.

What is not copied: Windows' alternate data streams (such as the "downloaded
from the internet" mark) and NTFS permissions. Files, folders, links, sizes
and dates are.

## By hand

In `$NULLBILL_HOME/config.yaml`:

```yaml
disks:
  ata-EXAMPLE_DISK:
    use: legacy
    mode: ro            # rw: NTFS only, after a full Windows shutdown
    # discard_journal: [jb95e30db0b42]  # with rw: the journal Windows left
    #                     # open, by the fingerprint plan's error names
    partitions: [2]     # which ones; default: every NTFS, exFAT or FAT one
    share: true         # false: mount on Proxmox only, no files guest
    name: photos        # share name; default: the partition's label
```

To reuse it for backups, replace that pin with:

```yaml
  ata-EXAMPLE_DISK:
    use: pbs              # or storage: LVM-thin storage of its own
    wipe: true
    evacuate: local-lvm   # copy first, to this storage; or:
    # discard_data: true  # erase without a copy
    # storage: nb-photos  # use: storage only; default nb-data
```

A copy is a volume of the files guest. To put it somewhere else, use
**Move Storage** on its mount point in Proxmox (the files guest,
**Resources**); setup leaves it where you put it.

On the host, `nb-legacy-disk list` shows what is kept and whether it is
mounted, `/var/lib/nullbill/discarded-journals` each journal setup discarded
(time, partition, fingerprint), and `nb-legacy-copy list` the verified copies
(name, volume, GiB, files, bytes, disk). `journalctl -u 'nb-legacy-copy-*'`
shows a copy's progress.
