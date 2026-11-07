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

If the old PC no longer starts, keep the disk read-only.

## Steps

1. Put the disk in the server and start it. Proxmox leaves it alone.
2. On the page, **Look at the hardware** again. Step 3 lists the disk under
   **Other disks** with its partitions, labels, sizes and health.
3. Choose **Keep its files and share them to me**. Tick **Let me change
   files on it** only if you need to, and only for an NTFS disk Windows shut
   down fully. Press **Save and plan** and check the plan: the disk shows as
   `use: legacy` with each partition it keeps, and a `files` guest appears.
4. **Run setup**. The tofu stage mounts the disk under `/mnt/nb-legacy` on
   Proxmox, read-only unless you chose otherwise, and the files stage shares
   it.
5. Under **Your kept disks**, the page shows each share's address and the
   user `nbshare`. Press **Show the password** and keep it in your password
   manager.

## Open the files

From a device signed in to your Tailscale (or on the home network until you
close it):

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

## If the disk goes missing

If the disk is unplugged or fails to start, the server still boots, the share
shows an empty folder, and the alert **LegacyDiskMissing** reaches ntfy after
10 minutes. Nothing is written in its place: the empty mount point refuses
writes. Plug it back in and run setup.

Each probe reads the disk's SMART health. If the page says the disk is
failing (the plan shows `health: FAILED`), copy what you need off it soon.
There is no alert for this between probes yet.

## Stop keeping it

Choose **Leave it alone** and run setup. The share goes away and the disk is
unmounted; its files are not touched. Then you can take it out.

Erasing a kept disk to use it for backups is refused while it is kept: first
choose **Leave it alone** and run setup, make sure you have your files
elsewhere, then choose **Erase everything on it** and type the end of its
name. Copying the files off for you before erasing is not built yet.

## By hand

In `$NULLBILL_HOME/config.yaml`:

```yaml
disks:
  ata-EXAMPLE_DISK:
    use: legacy
    mode: ro            # rw: NTFS only, after a full Windows shutdown
    partitions: [2]     # which ones; default: every NTFS, exFAT or FAT one
    share: true         # false: mount on Proxmox only, no files guest
    name: photos        # share name; default: the partition's label
```

On the host, `nb-legacy-disk list` shows what is kept and whether it is
mounted.
