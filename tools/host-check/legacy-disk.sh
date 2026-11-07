# shellcheck shell=bash
# The kept-disk script (nb-legacy-disk) against real NTFS, exFAT and FAT
# images on loop devices, mounted by the real ntfs-3g through FUSE. The
# container has no systemd: a systemctl stand-in mounts each unit's What,
# Where, Type and Options with mount(8), so the units' own text is what
# gets mounted. Sourced by check.sh, which defines fail.

legacy_disk_checks() {
  local by_id=/tmp/legacy/by-id by_uuid=/tmp/legacy/by-uuid units=/tmp/legacy/units base=/tmp/legacy/mnt prom=/tmp/legacy/textfile/nullbill-legacy.prom
  mkdir -p "$by_id" "$by_uuid" "$units" /tmp/legacy/fake
  cat >/tmp/legacy/systemctl <<'SH'
#!/bin/bash
# systemctl for a container without systemd: start and stop mount units.
set -eu
echo "$*" >>/tmp/legacy/systemctl.log
units=${NB_UNIT_DIR:?}
field() { sed -n "s/^$1=//p" "$units/$2"; }
case $1 in
  start) mount -t "$(field Type "$2")" -o "$(field Options "$2")" "$(field What "$2")" "$(field Where "$2")" ;;
  stop) [ ! -e "$units/$2" ] || ! mountpoint -q "$(field Where "$2")" || umount "$(field Where "$2")" ;;
  daemon-reload | enable | disable) ;;
  *) exit 2 ;;
esac
SH
  chmod +x /tmp/legacy/systemctl
  export NB_DISK_DIR=$by_id NB_UUID_DIR=$by_uuid NB_UNIT_DIR=$units NB_LEGACY_DIR=$base NB_SYSTEMCTL=/tmp/legacy/systemctl NB_TEXTFILE_DIR=/tmp/legacy/textfile
  image() { # name size mkfs...
    truncate -s "$2" "/tmp/legacy/$1.img"
    local loop
    loop=$(losetup -f --show "/tmp/legacy/$1.img")
    "${@:3}" "$loop" >/dev/null 2>&1
    ln -sf "$loop" "$by_id/ata-SYNTHETIC_$1-part2"
    ln -sf "$loop" "$by_uuid/$(blkid -p -o value -s UUID "$loop")"
    echo "$loop"
  }
  sum() { sha256sum "/tmp/legacy/$1.img" | cut -d' ' -f1; }
  legacy() { /usr/local/sbin/nb-legacy-disk "$@"; }
  keep() { # name uuid fstype mode
    legacy mount "ata-SYNTHETIC_$1" 2 "$2" "$3" "$4" old-hdd 101000 101000
  }
  local dir=$base/old-hdd unit=$units/'tmp-legacy-mnt-old\x2dhdd.mount'

  local ntfs uuid before
  ntfs=$(image NTFS 64M mkntfs -q -F -f -L Data)
  uuid=$(blkid -p -o value -s UUID "$ntfs")

  # Refusals before anything is made.
  if keep NTFS "$uuid" exfat ro 2>/tmp/legacy/err; then fail "kept a disk whose filesystem differs from the plan"; fi
  grep -q "holds ntfs, the plan says exfat" /tmp/legacy/err || fail "wrong filesystem message: $(cat /tmp/legacy/err)"
  if keep NTFS 0000000000000000 ntfs ro 2>/tmp/legacy/err; then fail "kept a disk whose UUID differs from the plan"; fi
  grep -q "has UUID $uuid, the plan says 0000000000000000" /tmp/legacy/err || fail "wrong UUID message"
  if legacy mount ata-SYNTHETIC_GONE 2 "$uuid" ntfs ro old-hdd 101000 101000 2>/tmp/legacy/err; then fail "kept a missing disk"; fi
  grep -q "is the disk connected" /tmp/legacy/err || fail "missing disk message"
  test ! -e "$dir" || fail "a refused disk left a mount point"
  echo "ok   kept disks: a missing disk, another filesystem or another UUID than planned are refused, and nothing is made"

  # Read-only: mounted by its by-id name, owned by the share user, and not one byte written.
  echo hello >/tmp/legacy/hello.txt
  mkdir -p /tmp/legacy/seed && mount -t ntfs-3g "$ntfs" /tmp/legacy/seed && cp /tmp/legacy/hello.txt /tmp/legacy/seed/ && umount /tmp/legacy/seed
  before=$(sum NTFS)
  [ "$(keep NTFS "$uuid" ntfs ro)" = mounted ] || fail "read-only NTFS not mounted"
  findmnt -rno OPTIONS --mountpoint "$dir" | grep -q '^ro,' || fail "NTFS not mounted read-only"
  [ "$(stat -c %u:%g "$dir/hello.txt")" = 101000:101000 ] || fail "files not owned by the share user (host 101000)"
  if touch "$dir/new" 2>/dev/null; then fail "wrote to a read-only kept disk"; fi
  grep -qx 'Options=ro,noatime,uid=101000,gid=101000,dmask=0077,fmask=0177' "$unit" || fail "unit options: $(grep Options "$unit")"
  grep -qx "What=$by_id/ata-SYNTHETIC_NTFS-part2" "$unit" || fail "unit mounts by the partition's by-id name"
  grep -qx 'Before=pve-guests.service' "$unit" || fail "unit is not ordered before the guests"
  [ "$(keep NTFS "$uuid" ntfs ro)" = kept ] || fail "second run did not keep the mount"
  [ "$(legacy list)" = "old-hdd $uuid ro ntfs mounted" ] || fail "list: $(legacy list)"
  grep -qx 'nb_legacy_disk_mounted{name="old-hdd",mode="ro"} 1' "$prom" || fail "metric of a mounted disk: $(cat "$prom")"
  echo "ok   a kept NTFS disk mounts read-only by its by-id name as the share user, and stays as it is on the next run"

  # The disk goes missing: the empty mount point takes no writes.
  umount "$dir"
  if touch "$dir/lost" 2>/dev/null; then fail "wrote into the mount point of a missing disk"; fi
  [ "$(legacy list)" = "old-hdd $uuid ro ntfs unmounted" ] || fail "list of a missing disk: $(legacy list)"
  legacy metrics
  grep -qx 'nb_legacy_disk_mounted{name="old-hdd",mode="ro"} 0' "$prom" || fail "metric of a missing disk: $(cat "$prom")"
  [ "$(keep NTFS "$uuid" ntfs ro)" = mounted ] || fail "the disk was not mounted again once back"
  echo "ok   while the disk is missing its mount point is immutable, so nothing lands on the system disk, and LegacyDiskMissing's metric says 0"
  [ "$(legacy unmount old-hdd)" = removed ] || fail "unmount"
  if [ -e "$dir" ] || [ -e "$unit" ]; then fail "unmount left the mount point or the unit"; fi
  test ! -e "$prom" || fail "the metric outlived the last kept disk"
  [ "$(sum NTFS)" = "$before" ] || fail "a read-only mount changed the disk"
  echo "ok   unmounting removes the unit and the mount point, and read-only use left the image byte for byte as it was"

  # Read-write on a clean volume, then Windows' hibernation file appears.
  [ "$(keep NTFS "$uuid" ntfs rw)" = mounted ] || fail "clean NTFS not mounted read-write"
  findmnt -rno OPTIONS --mountpoint "$dir" | grep -q '^rw,' || fail "NTFS not mounted read-write"
  grep -q '^Options=rw,.*,windows_names$' "$unit" || fail "read-write unit without windows_names"
  { printf 'HIBR'; head -c 4092 /dev/zero; } >"$dir/hiberfil.sys"
  legacy unmount old-hdd >/dev/null
  before=$(sum NTFS)
  if keep NTFS "$uuid" ntfs rw 2>/tmp/legacy/err; then fail "mounted a hibernated NTFS read-write"; fi
  grep -q "Windows is hibernated on it" /tmp/legacy/err || fail "hibernation message: $(cat /tmp/legacy/err)"
  test -z "$(findmnt -rno TARGET --mountpoint "$dir")" || fail "a refused read-write left a mount"
  [ "$(keep NTFS "$uuid" ntfs ro)" = mounted ] || fail "a hibernated NTFS could not be kept read-only"
  legacy unmount old-hdd >/dev/null
  [ "$(sum NTFS)" = "$before" ] || fail "checking a hibernated disk changed it"
  echo "ok   read-write is refused on an NTFS disk Windows left hibernated, which still mounts read-only, unchanged"

  # ntfs-3g mounts a hibernated volume read-only when asked for read-write
  # and exits 0; a probe that missed it must not leave a read-only mount
  # standing in for the read-write one.
  printf '#!/bin/sh\nexit 0\n' >/tmp/legacy/fake/ntfs-3g.probe
  chmod +x /tmp/legacy/fake/ntfs-3g.probe
  if PATH=/tmp/legacy/fake:$PATH keep NTFS "$uuid" ntfs rw 2>/tmp/legacy/err; then fail "kept a read-write request that mounted read-only"; fi
  grep -q "mounted ro instead of rw; nothing kept mounted" /tmp/legacy/err || fail "fallback message: $(cat /tmp/legacy/err)"
  test -z "$(findmnt -rno TARGET --mountpoint "$dir")" || fail "the read-only fallback was left mounted"
  test ! -e "$unit" || fail "the failed read-write unit was left to mount at boot"
  echo "ok   a read-write mount that came up read-only is taken down and reported"

  legacy unmount old-hdd >/dev/null
  # Unclean journal: ntfs-3g.probe --readwrite exits 15.
  # shellcheck disable=SC2016 # the fake's own $1
  printf '#!/bin/sh\n[ "$1" = --readwrite ] && exit 15\nexit 0\n' >/tmp/legacy/fake/ntfs-3g.probe
  if PATH=/tmp/legacy/fake:$PATH keep NTFS "$uuid" ntfs rw 2>/tmp/legacy/err; then fail "mounted an unclean NTFS read-write"; fi
  grep -q "Windows did not unmount it cleanly" /tmp/legacy/err || fail "unclean message: $(cat /tmp/legacy/err)"
  echo "ok   read-write is refused when ntfs-3g.probe reports an unclean volume (exit 15)"

  # A volume Windows marked for chkdsk: its dirty flag, which ntfs-3g.probe passes.
  local dirty
  dirty=$(image DIRTY 64M mkntfs -q -F -f -L Dirty)
  python3 - "$dirty" <<'PY'
import struct, sys
# Set VOLUME_IS_DIRTY in $Volume's VOLUME_INFORMATION, in $MFT and $MFTMirr.
with open(sys.argv[1], "r+b") as f:
    boot = f.read(512)
    cluster = struct.unpack_from("<H", boot, 11)[0] * boot[13]
    c = struct.unpack_from("<b", boot, 64)[0]
    size = c * cluster if c > 0 else 2 ** -c
    starts = [struct.unpack_from("<q", boot, o)[0] * cluster + 3 * size for o in (48, 56)]
    f.seek(starts[0])
    rec = f.read(size)
    a = struct.unpack_from("<H", rec, 20)[0]
    while struct.unpack_from("<I", rec, a)[0] != 0x70:
        a += struct.unpack_from("<I", rec, a + 4)[0]
    flags = a + struct.unpack_from("<H", rec, a + 20)[0] + 10
    for start in starts:
        f.seek(start + flags)
        f.write(struct.pack("<H", struct.unpack_from("<H", rec, flags)[0] | 1))
PY
  ntfs-3g.probe --readwrite "$dirty" >/dev/null 2>&1 || fail "the dirty test image is not what ntfs-3g.probe passes"
  before=$(sum DIRTY)
  if legacy mount ata-SYNTHETIC_DIRTY 2 "$(blkid -p -o value -s UUID "$dirty")" ntfs rw dirty 101000 101000 2>/tmp/legacy/err; then
    fail "mounted an NTFS marked for chkdsk read-write"
  fi
  grep -q "Windows marked it for a disk check" /tmp/legacy/err || fail "chkdsk message: $(cat /tmp/legacy/err)"
  [ "$(sum DIRTY)" = "$before" ] || fail "checking a dirty disk changed it"
  echo "ok   read-write is refused on an NTFS volume marked for chkdsk, which ntfs-3g.probe alone passes"

  # A cloned disk carries the same UUID: the partition named is the one mounted.
  # The volume serial sits at byte 0x48 of the boot sector, little-endian.
  python3 -c 'import sys; f = open(sys.argv[1], "r+b"); f.seek(0x48); f.write(int(sys.argv[2], 16).to_bytes(8, "little"))' "$dirty" "$uuid"
  [ "$(blkid -p -o value -s UUID "$dirty")" = "$uuid" ] || fail "the twin's UUID"
  ln -sf "$dirty" "$by_uuid/$uuid"
  [ "$(keep NTFS "$uuid" ntfs ro)" = mounted ] || fail "kept disk beside its twin"
  [ "$(findmnt -rno SOURCE --mountpoint "$dir")" = "$ntfs" ] || fail "mounted the twin, not the disk named"
  legacy unmount old-hdd >/dev/null
  echo "ok   of two disks with the same UUID (a clone), the one the plan names is mounted"

  # A refused change puts back what was mounted.
  [ "$(keep NTFS "$uuid" ntfs ro)" = mounted ] || fail "read-only mount before a refused change"
  if PATH=/tmp/legacy/fake:$PATH keep NTFS "$uuid" ntfs rw 2>/dev/null; then fail "unclean NTFS switched to read-write"; fi
  findmnt -rno OPTIONS --mountpoint "$dir" | grep -q '^ro,' || fail "the read-only mount was not put back after a refused change"
  legacy unmount old-hdd >/dev/null
  echo "ok   a refused switch to read-write puts the read-only mount back"

  # Nothing may already be in the mount point or have the disk mounted.
  mkdir -p "$dir" && touch "$dir/stray"
  if keep NTFS "$uuid" ntfs ro 2>/tmp/legacy/err; then fail "mounted over a non-empty mount point"; fi
  grep -q "is not empty" /tmp/legacy/err || fail "non-empty message"
  rm -rf "$dir"
  mount -t ntfs-3g -o ro "$ntfs" /tmp/legacy/seed
  if keep NTFS "$uuid" ntfs ro 2>/tmp/legacy/err; then fail "mounted a disk that is mounted elsewhere"; fi
  grep -q "is mounted at /tmp/legacy/seed" /tmp/legacy/err || fail "mounted-elsewhere message: $(cat /tmp/legacy/err)"
  umount /tmp/legacy/seed
  echo "ok   a mount point with files in it, or a disk mounted elsewhere, is refused"

  legacy_copy_checks "$ntfs" "$uuid"

  # exFAT and FAT stay read-only: their hibernation cannot be told.
  local exfat vfat
  exfat=$(image EXFAT 64M mkfs.exfat -L Old)
  vfat=$(image VFAT 64M mkfs.vfat -n OLD)
  for fs in exfat vfat; do
    local dev=$exfat
    [ "$fs" = vfat ] && dev=$vfat
    local upper=${fs^^}
    if keep "$upper" "$(blkid -p -o value -s UUID "$dev")" "$fs" rw 2>/tmp/legacy/err; then fail "$fs kept read-write"; fi
    grep -q "$fs stays read-only" /tmp/legacy/err || fail "$fs read-write message"
    if grep -qw "$fs" /proc/filesystems || modprobe -q "$fs" 2>/dev/null; then
      [ "$(keep "$upper" "$(blkid -p -o value -s UUID "$dev")" "$fs" ro)" = mounted ] || fail "$fs not mounted read-only"
      findmnt -rno OPTIONS --mountpoint "$dir" | grep -q '^ro,' || fail "$fs not read-only"
      legacy unmount old-hdd >/dev/null
      echo "ok   $fs mounts read-only"
    else
      echo "SKIP $fs mount: this kernel has no $fs driver (PVE's has)"
    fi
  done
  echo "ok   exFAT and FAT are refused read-write"

  legacy_disk_cleanup
}

# nb-legacy-copy: the kept NTFS disk copied into a volume. pvesm and pvesh
# stand-ins make each volume a loop device on a sparse file.
legacy_copy_checks() {
  local ntfs=$1 uuid=$2 dir=/tmp/legacy/mnt/old-hdd vols=/tmp/legacy/vols
  mkdir -p "$vols" /tmp/legacy/copy-fake
  cat >/tmp/legacy/pvesm <<'SH'
#!/bin/bash
# pvesm for the copy check: volumes are loop devices on files in $vols.
set -eu
vols=/tmp/legacy/vols
echo "$*" >>/tmp/legacy/pvesm.log
case $1 in
  list) for f in "$vols"/*.img; do [ -e "$f" ] || continue; n=$(basename "$f" .img); echo "$2:$n raw images 1 ${n#vm-}"; done ;;
  alloc) truncate -s "$5" "$vols/$4.img"; losetup -f --show "$vols/$4.img" >"$vols/$4.loop"; echo "successfully created '$2:$4'" ;;
  path) cat "$vols/${2#*:}.loop" ;;
  free) n=${2#*:}; losetup -d "$(cat "$vols/$n.loop")"; rm -f "$vols/$n.img" "$vols/$n.loop" ;;
  *) exit 2 ;;
esac
SH
  cat >/tmp/legacy/pvesh <<'SH'
#!/bin/sh
echo "{\"type\": \"${NB_FAKE_TYPE:-lvmthin}\", \"total\": 107374182400, \"avail\": ${NB_FAKE_AVAIL:-53687091200}}"
SH
  chmod +x /tmp/legacy/pvesm /tmp/legacy/pvesh
  export NB_PVESM=/tmp/legacy/pvesm NB_PVESH=/tmp/legacy/pvesh NB_COPIES=/tmp/legacy/kept-copies NB_COPY_DIR=/tmp/legacy/copying
  copy() { /usr/local/sbin/nb-legacy-copy copy old-hdd local-lvm 2023 1 ata-SYNTHETIC_NTFS; }
  volumes() { find "$vols" -name '*.img' | wc -l; }

  # Files worth copying: many small ones, a big one, odd names, a subdirectory.
  mkdir -p /tmp/legacy/seed
  # The hibernation check left hiberfil.sys behind; this test image may lose it.
  mount -t ntfs-3g -o remove_hiberfile "$ntfs" /tmp/legacy/seed
  mkdir -p "/tmp/legacy/seed/Fotos 2019/Urlaub"
  for i in $(seq 1 450); do echo "photo $i" >"/tmp/legacy/seed/Fotos 2019/Urlaub/IMG_$i.jpg"; done
  head -c 20000000 /dev/urandom >"/tmp/legacy/seed/Fotos 2019/video.mp4"
  printf 'x' >"/tmp/legacy/seed/name with spaces & ümlauts.txt"
  umount /tmp/legacy/seed
  # ntfs-3g writes back after umount returns; wait until it has exited.
  while grep -qsa 'legacy/[s]eed' /proc/[0-9]*/cmdline; do sleep 0.2; done
  [ "$(keep NTFS "$uuid" ntfs ro)" = mounted ] || fail "kept disk for the copy"
  local before
  before=$(sum NTFS)

  # Not enough room, another kind of storage, a missing disk: refused, nothing made.
  if NB_FAKE_AVAIL=1073741824 copy 2>/tmp/legacy/err; then fail "copied onto a storage without room"; fi
  grep -q "the copy needs 1 GiB and 5 % must stay free" /tmp/legacy/err || fail "room message: $(cat /tmp/legacy/err)"
  if NB_FAKE_TYPE=zfspool copy 2>/tmp/legacy/err; then fail "copied onto ZFS"; fi
  grep -q "is zfspool; a copy goes on LVM or LVM-thin storage" /tmp/legacy/err || fail "storage type message"
  if /usr/local/sbin/nb-legacy-copy copy gone local-lvm 2023 1 X 2>/tmp/legacy/err; then fail "copied a disk that is not mounted"; fi
  grep -q "is not mounted; is the disk connected" /tmp/legacy/err || fail "not mounted message"
  [ "$(volumes)" = 0 ] || fail "a refused copy left a volume"
  echo "ok   a copy is refused without room on LVM storage, or without the kept disk mounted, and makes nothing"

  # A copy that does not match is freed and not recorded.
  fake_rsync() { # what to do to the copy after the real rsync
    # shellcheck disable=SC2016 # the fake's own variables
    printf '#!/bin/sh\n/usr/bin/rsync "$@" || exit $?\nfor d in "$@"; do :; done\ncd "$d"\n%s\n' "$1" >/tmp/legacy/copy-fake/rsync
  }
  fake_rsync "rm -f 'Fotos 2019/Urlaub/IMG_7.jpg'"
  chmod +x /tmp/legacy/copy-fake/rsync
  if PATH=/tmp/legacy/copy-fake:$PATH copy 2>/tmp/legacy/err; then
    fail "recorded a copy with a file missing: $(cat /tmp/legacy/err)"
  fi
  grep -q "the copy differs: missing \['Fotos 2019/Urlaub/IMG_7.jpg'\]" /tmp/legacy/err || fail "missing file message: $(cat /tmp/legacy/err)"
  [ "$(volumes)" = 0 ] || fail "a failed copy left its volume"
  test ! -e /tmp/legacy/kept-copies || fail "a failed copy was recorded"
  # Same sizes, other bytes: only the hashes can tell.
  fake_rsync "sed -i s/photo/PHOTO/ 'Fotos 2019/Urlaub/'*.jpg"
  if PATH=/tmp/legacy/copy-fake:$PATH copy 2>/tmp/legacy/err; then fail "recorded a copy with changed files"; fi
  grep -q "the copy differs: 'Fotos 2019/Urlaub/IMG_.*' has other contents" /tmp/legacy/err || fail "changed file message: $(cat /tmp/legacy/err)"
  [ "$(volumes)" = 0 ] || fail "a failed copy left its volume"
  echo "ok   a copy missing a file or holding a changed one is freed and never recorded"

  # A copy cut short (apply's connection dropped) is freed by the next one.
  /tmp/legacy/pvesm alloc local-lvm 2023 vm-2023-disk-0 1G >/dev/null
  echo "old-hdd local-lvm:vm-2023-disk-0" >/tmp/legacy/kept-copies.pending
  local out
  out=$(copy 2>/tmp/legacy/err) || fail "copy: $(cat /tmp/legacy/err)"
  grep -q "freed local-lvm:vm-2023-disk-0, left over from an unfinished copy" /tmp/legacy/err || fail "leftover not freed"
  [ "$out" = "copied local-lvm:vm-2023-disk-0" ] || fail "copy printed $out"
  [ "$(volumes)" = 1 ] || fail "volumes after the copy: $(volumes)"
  test ! -e /tmp/legacy/kept-copies.pending || fail "the pending record outlived the copy"
  grep -Eq '^old-hdd local-lvm:vm-2023-disk-0 1 453 2000[0-9]{4} ata-SYNTHETIC_NTFS$' /tmp/legacy/kept-copies || fail "record: $(cat /tmp/legacy/kept-copies)"
  grep -q "453 files, .* bytes; 200 compared by SHA-256" /tmp/legacy/err || fail "sample size: $(tail -1 /tmp/legacy/err)"
  [ "$(copy)" = "kept local-lvm:vm-2023-disk-0" ] || fail "second run did not keep the copy"
  mkdir -p /tmp/legacy/check
  mount -o ro "$(cat "$vols/vm-2023-disk-0.loop")" /tmp/legacy/check
  diff -r "$dir" /tmp/legacy/check >/dev/null || fail "the copy differs from the kept disk"
  [ "$(stat -c %u:%g "/tmp/legacy/check/Fotos 2019/video.mp4")" = 101000:101000 ] || fail "the copy is not owned by the share user"
  umount /tmp/legacy/check
  legacy unmount old-hdd >/dev/null
  [ "$(sum NTFS)" = "$before" ] || fail "copying changed the kept disk"
  /tmp/legacy/pvesm free local-lvm:vm-2023-disk-0
  echo "ok   a verified copy is recorded once, equals the kept disk, keeps its owner, and the disk is unchanged"
}

# Loop devices belong to the kernel, not the container: detach them even
# when a check fails, or each failed run leaks some. image runs in a
# subshell, so the images, not a list, say which loops are ours.
legacy_disk_cleanup() {
  findmnt -rno TARGET | grep '^/tmp/legacy/' | sort -r | xargs -r umount -l 2>/dev/null || true
  for img in /tmp/legacy/*.img /tmp/legacy/vols/*.img; do
    [ -e "$img" ] || continue
    losetup -j "$img" | cut -d: -f1 | xargs -r -n1 losetup -d 2>/dev/null || true
  done
}
