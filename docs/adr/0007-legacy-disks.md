# ADR 0007: Keeping, sharing and later converting a disk from a previous OS

Status: accepted (owner approved ntfs-3g and Samba, 2026-11-07)

## Context

A spare PC often comes with a second disk full of the owner's files, most
often NTFS from Windows. Today such a disk is `untouched`: safe, but nobody
can reach the files. The only other choice, `use: pbs` with `wipe: true`,
erases it. The owner wants to keep the disk as it is, reach the files from
their devices over the VPN, and later convert the disk to NullBill's own
layout once the data has been dealt with.

What was checked before deciding (2026-11-07):

- PVE 9's kernel (`proxmox-headers-6.17.9-1-pve`, `.config`) builds `ntfs3`,
  `exfat`, `xfs` and `btrfs` as modules and `vfat` and `ext4` in.
- The in-kernel `ntfs3` refuses read-write only when the volume's dirty flag
  is set (without `force`) or its journal cannot be replayed
  (`fs/ntfs3/super.c`, v6.17). It never looks for a Windows hibernation
  file, so a volume left by Fast Startup or hibernation mounts read-write.
  Read-only mounts replay nothing and write nothing (`fs/ntfs3/fslog.c`).
- `ntfs-3g` 2022.10.3 (Debian 13), tested on loop images:
  `ntfs-3g.probe --readwrite` exits 14 on a hibernated volume, as its man
  page says (15 is an unclean journal). It exits 0 on a volume whose dirty
  flag (chkdsk scheduled) is set, and `ntfs-3g` then mounts it read-write;
  `ntfsinfo -m` refuses such a volume. Asked for read-write on a hibernated
  volume, `ntfs-3g` mounts it read-only and still exits 0. A read-only mount
  leaves the image byte for byte unchanged.
- PVE lets only `root@pam` itself set a bind mount point; API tokens of
  root are refused (pve-container's permission check; Proxmox forum thread
  124543). OpenTofu uses the token `root@pam!nullbill`.
- `smartmontools` is already installed on every PVE host
  (`libpve-storage-perl` depends on it).
- Debian 13 has util-linux 2.41.5, whose `mount` supports
  `X-mount.idmap=`.

## Decision

**Disk use `legacy`.** `disks.<id>: {use: legacy}` in `config.yaml` (or on
the page) keeps a disk as it is. It is never the default. plan picks each
partition with NTFS, exFAT or FAT, skips EFI, Microsoft Reserved and Windows
Recovery partitions by their GPT type GUID or MBR type code, and refuses
anything else it is asked to mount (BitLocker, LUKS, LVM, RAID, ZFS,
Storage Spaces, dynamic disks) by name. ext4, xfs and btrfs wait for a
later change (below).

**Read-only unless pinned.** `mode: rw` is allowed on NTFS only. Before a
read-write mount the host script requires `ntfs-3g.probe --readwrite` to
pass and `ntfsinfo -m` to accept the volume, and after mounting it checks
that the mount really is read-write. exFAT and FAT carry no hibernation
mark that Linux can read, so they are read-only. Nothing in NullBill runs
`fsck`, `ntfsfix`, `wipefs` or `mkfs` on a legacy disk.

**ntfs-3g as the NTFS driver.** `ntfs-3g.probe` is needed on the host in any
case, because `ntfs3` cannot tell a hibernated volume. Using `ntfs-3g` for
the mount too adds a second refusal of read-write on a hibernated volume,
and CI can test the real driver (FUSE works in CI containers; CI kernels
have no `ntfs3`). It is installed on the host only when the plan keeps an
NTFS disk.

**Mount on the host.** `nb-legacy-disk` (host baseline) mounts each kept
partition read-only or read-write at `/mnt/nb-legacy/<name>` through a
systemd mount unit by UUID, owned by host uid and gid 101000 (uid 1000 in an
unprivileged container). The empty mount point is made immutable first, so
nothing can land on the system disk while the disk is missing. The unit
orders itself before `pve-guests.service`, so the share guest starts with
the disk mounted.

**Share from a `files` guest, over SMB.** An unprivileged container in
mgmt, never agent-managed, gets each mount as a bind mount point, set by
apply over SSH as root with `pct set` after OpenTofu (OpenTofu ignores the
container's mount points). Samba serves one share per mount: SMB3 only,
encryption required, no guest access, one user whose password apply makes
and keeps in the secrets file. The firewall lets only the VPN guest reach
port 445, and the tailnet policy only the owner role. Samba's log goes to
the journal, and its auth lines go to Loki with the other auth logs, so
FailedLogins covers the share.

**Not backed up.** PBS backs up guests; PVE never backs up a bind mount. The
plan says so on every legacy disk.

**Converting later** (Phases 2 and 3, each its own change): moving a disk
away from `legacy` needs `wipe: true` and either `evacuate: <storage>`
(apply copies the data there, checks counts, sizes and a sample of
checksums, and stops on any difference) or `discard_data: true`. Until
those exist, plan refuses any other use of a disk that is mounted as
legacy.

## Alternatives

- **SFTP from OpenSSH instead of Samba.** No new package, but Windows
  Explorer and macOS Finder cannot open SFTP without an extra app, and the
  owner's devices are Windows and macOS. NFS has no per-user password and
  is not built into Windows Home.
- **ntfs3 as the driver.** Faster (no FUSE), but read-write would rest on
  NullBill's own probe alone, and CI could not mount it. The share runs
  over the network, which is slower than FUSE on a disk anyway.
- **Bind mounts through OpenTofu.** The provider authenticates with an API
  token, which PVE refuses for bind mounts; a root password in apply would
  widen what apply holds.
- **ext4, xfs and btrfs through an idmapped mount.** The idmap fixes
  ownership, but files keep their Unix owners and modes, and one Samba
  user cannot read another user's private files. Left for a later change,
  which needs either per-owner share users or a read-only view as root.
- **A SMART exporter.** smartctl is already on the host, so probe reads each
  disk's health with it, and the plan warns about a disk that does not
  report PASSED. Alerts on SMART changes can come from a small textfile
  script like `nb-backup-metrics` without a new package; that is a later
  change.

## Consequences

- New packages: `ntfs-3g` on the host (only with a kept NTFS disk) and
  `samba` in the files guest, both from Debian.
- The share follows the disk: if the disk is missing at boot, the share is
  empty and read-only, and the LegacyDiskMissing alert fires.
- The DiskAlmostFull alert leaves read-only filesystems out, so a full
  read-only disk does not page forever.
- A disk kept read-write and full of writes from the share is still not
  backed up. The page and the runbook (`docs/runbooks/legacy-disk.md`) say
  to copy anything important elsewhere.
