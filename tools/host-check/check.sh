#!/usr/bin/env bash
# Run the host baseline twice against this container, then check the result
# with the tools that read it. /ansible is the repo's ansible directory.
set -euo pipefail

play() {
  ansible-playbook -i localhost, -c local -e host_baseline_manage_services=false \
    -e host_baseline_ntfs=true -e host_baseline_api_name=pve.nullbill.internal -e host_baseline_api_address=10.42.1.254 /ansible/host-baseline.yml
}
fail() {
  echo "FAIL $1"
  exit 1
}
# shellcheck source=tools/host-check/legacy-disk.sh
source /usr/local/lib/legacy-disk.sh

dpkg-query -W -f '${Package} ${Version}\n' ansible-core openssh-server
ssh-keygen -A >/dev/null
mkdir -p /root/.ssh /run/sshd

# Without a root key the role must stop before touching sshd.
: >/root/.ssh/authorized_keys
if play >/tmp/nokey.log 2>&1; then fail "baseline ran although root has no key"; fi
grep -q "authorized_keys is missing or empty" /tmp/nokey.log || fail "no-key refusal message"
test ! -e /etc/ssh/sshd_config.d/10-nullbill.conf || fail "sshd changed without a root key"
echo "ok   refuses to run without a root key"

# What a fresh PVE 9 install has: a key, the enterprise repositories, and
# PVE's CA with a node certificate for the LAN name and address only.
# pvenode stands in by putting the certificate where PVE's would.
node=$(hostname)
mkdir -p /etc/pve/priv "/etc/pve/nodes/$node"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=Proxmox Virtual Environment" \
  -keyout /etc/pve/priv/pve-root-ca.key -out /etc/pve/pve-root-ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=$node.example.internal" \
  -keyout "/etc/pve/nodes/$node/pve-ssl.key" -out /tmp/node.csr 2>/dev/null
openssl x509 -req -in /tmp/node.csr -CA /etc/pve/pve-root-ca.pem -CAkey /etc/pve/priv/pve-root-ca.key -CAcreateserial \
  -CAserial /etc/pve/priv/pve-root-ca.srl -days 730 -out "/etc/pve/nodes/$node/pve-ssl.pem" \
  -extfile <(echo "subjectAltName=IP:127.0.0.1,IP:::1,DNS:localhost,IP:192.0.2.10,DNS:$node,DNS:$node.example.internal") 2>/dev/null
cat >/usr/bin/pvenode <<'SH'
#!/bin/sh
[ "$1 $2" = "cert set" ] || exit 2
cp "$3" "/etc/pve/nodes/$(hostname)/pveproxy-ssl.pem"
cp "$4" "/etc/pve/nodes/$(hostname)/pveproxy-ssl.key"
SH
chmod +x /usr/bin/pvenode
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test" >/root/.ssh/authorized_keys
printf 'Types: deb\nURIs: https://enterprise.proxmox.com/debian/%s\nSuites: trixie\nComponents: %s\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
  pve pve-enterprise >/etc/apt/sources.list.d/pve-enterprise.sources
printf 'Types: deb\nURIs: https://enterprise.proxmox.com/debian/%s\nSuites: trixie\nComponents: %s\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
  ceph-squid enterprise >/etc/apt/sources.list.d/ceph.sources

play >/tmp/first.log 2>&1 || { cat /tmp/first.log; fail "first run"; }
echo "ok   first run"
play >/tmp/second.log 2>&1 || { cat /tmp/second.log; fail "second run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/second.log || { tail -5 /tmp/second.log; fail "second run changed something"; }
echo "ok   second run changes nothing"

# The agent's root key runs every command through it (src/nullbill/hostroot.py).
test -x /usr/local/sbin/nb-agent-command || fail "nb-agent-command is not installed"
cmp -s /usr/local/sbin/nb-agent-command /ansible/roles/agent_ssh/files/nb-agent-command || fail "nb-agent-command differs from agent_ssh's"
echo "ok   the agent command recorder is in place"

effective=$(/usr/sbin/sshd -T)
for want in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin without-password"; do
  grep -qx "$want" <<<"$effective" || fail "sshd: $want"
done
echo "ok   sshd is key-only"

! grep -rl enterprise.proxmox.com /etc/apt/sources.list.d/ || fail "enterprise repository left"
apt-get update >/tmp/apt.log 2>&1 || { cat /tmp/apt.log; fail "apt update"; }
grep -q "download.proxmox.com/debian/pve trixie InRelease" /tmp/apt.log || fail "no-subscription repository not fetched"
! grep -E '^(W|E):' /tmp/apt.log || fail "apt update warnings"
echo "ok   no-subscription repository, no enterprise one"

origins=$(unattended-upgrade --dry-run --debug 2>&1 | grep "Allowed origins are:")
[[ $origins == "Allowed origins are: origin=Debian,codename=trixie-security,label=Debian-Security" ]] || fail "unattended origins: $origins"
apt-config dump Unattended-Upgrade::Automatic-Reboot | grep -qx 'Unattended-Upgrade::Automatic-Reboot "false";' || fail "the host must not reboot by itself"
test ! -e /etc/needrestart/conf.d/50-nullbill.conf || fail "needrestart left to PVE on the host"
test -x /usr/local/sbin/nb-update-metrics || fail "update metrics script"
echo "ok   unattended upgrades take Debian security updates only, never reboot the host"

api=/etc/pve/nodes/$node/pveproxy-ssl.pem
for check in "-verify_hostname pve.nullbill.internal" "-verify_ip 10.42.1.254" "-verify_ip 192.0.2.10" "-verify_hostname $node"; do
  # shellcheck disable=SC2086
  openssl verify -CAfile /etc/pve/pve-root-ca.pem $check "$api" | grep -q ": OK" || fail "API certificate: $check"
done
grep -q "^nb_pve_api_cert_expiry_timestamp_seconds [0-9]" /var/lib/prometheus/node-exporter/nullbill-pve-api.prom || fail "API certificate metric"
echo "ok   the PVE API certificate names the mgmt name and address besides PVE's own, from PVE's CA"

# The backup disk: a loop device with a filesystem on it, real LVM without
# udev, and a pvesm that records what it is asked to add.
cat >/etc/lvm/lvmlocal.conf <<'CONF'
activation { udev_sync = 0 udev_rules = 0 }
devices { obtain_device_list_from_udev = 0 }
CONF
truncate -s 256M /tmp/backup.img
loop=$(losetup -f --show /tmp/backup.img)
cleanup() {
  vgremove -qq -f nb-pbs-disk nb-data >/dev/null 2>&1 || true
  losetup -d "$loop"
  [ -z "${sloop:-}" ] || losetup -d "$sloop" 2>/dev/null || true
  legacy_disk_cleanup
}
trap cleanup EXIT
mkdir -p /tmp/by-id
ln -s "$loop" /tmp/by-id/ata-SYNTHETIC_HDD_0002
mkfs.ext4 -q "$loop"
cat >/usr/sbin/pvesm <<'SH'
#!/bin/sh
case "$1" in
  status) test -e "/tmp/pvesm-$3" ;;
  add) echo "$*" >>/tmp/pvesm.log && touch "/tmp/pvesm-$3" ;;
  *) exit 2 ;;
esac
SH
chmod +x /usr/sbin/pvesm
disk() { NB_DISK_DIR=/tmp/by-id /usr/local/sbin/nb-backup-disk ata-SYNTHETIC_HDD_0002 "$1"; }
if disk 0 >/tmp/disk.log 2>&1; then fail "backup disk with a filesystem taken without wipe"; fi
grep -q "carries ext4; set wipe: true" /tmp/disk.log || { cat /tmp/disk.log; fail "refusal names the signature"; }
blkid -p "$loop" | grep -q ext4 || fail "a refused disk was changed"
echo "ok   a backup disk with a filesystem is refused without wipe and left as it was"
[ "$(disk 1)" = made ] || fail "backup disk not made"
vgs --noheadings -o pv_name nb-pbs-disk | grep -q "$loop" || fail "volume group not on the disk"
grep -qx "add lvm nb-pbs-disk --vgname nb-pbs-disk --content images" /tmp/pvesm.log || fail "storage not added"
[ "$(disk 0)" = kept ] || fail "second run did not keep the disk"
[ "$(wc -l </tmp/pvesm.log)" = 1 ] || fail "storage added twice"
echo "ok   with wipe it becomes the nb-pbs-disk volume group and PVE storage, kept on the next run"

# A storage disk (phase 3 of ADR 0007): the same refusals, then an LVM-thin
# pool. Thin pools need the kernel's dm-thin target; without it, SKIP.
truncate -s 256M /tmp/storage.img
sloop=$(losetup -f --show /tmp/storage.img)
ln -s "$sloop" /tmp/by-id/ata-SYNTHETIC_HDD_0003
mkfs.ext4 -q "$sloop"
storage() { NB_DISK_DIR=/tmp/by-id /usr/local/sbin/nb-storage-disk ata-SYNTHETIC_HDD_0003 "$1" "${2:-nb-data}"; }
if storage 0 >/tmp/disk.log 2>&1; then fail "storage disk with a filesystem taken without wipe"; fi
grep -q "carries ext4; set wipe: true" /tmp/disk.log || { cat /tmp/disk.log; fail "storage refusal names the signature"; }
blkid -p "$sloop" | grep -q ext4 || fail "a refused storage disk was changed"
if storage 1 nb-pbs-disk >/tmp/disk.log 2>&1; then fail "a storage disk took the backup disk's group"; fi
grep -q "volume group nb-pbs-disk exists, but not on" /tmp/disk.log || { cat /tmp/disk.log; fail "storage name clash message"; }
if storage 1 local >/tmp/disk.log 2>&1; then fail "a storage disk took a name outside nb-"; fi
echo "ok   a storage disk with a filesystem is refused without wipe, and a name in use or outside nb- is refused"
if dmsetup targets 2>/dev/null | grep -q '^thin-pool'; then
  [ "$(storage 1)" = made ] || fail "storage disk not made"
  lvs --noheadings -o lv_attr nb-data/data | grep -q '^ *t' || fail "no thin pool on the storage disk"
  grep -qx "add lvmthin nb-data --vgname nb-data --thinpool data --content images,rootdir" /tmp/pvesm.log || fail "storage not added"
  [ "$(storage 0)" = kept ] || fail "second run did not keep the storage disk"
  [ "$(grep -c nb-data /tmp/pvesm.log)" = 1 ] || fail "storage added twice"
  echo "ok   with wipe it becomes the nb-data LVM-thin pool and PVE storage, kept on the next run"
else
  echo "SKIP the storage disk's thin pool: this kernel has no dm-thin target"
fi

# Backup metrics: Debian's python3 runs it against a pvesh with no job yet.
printf '#!/bin/sh\necho "[]"\n' >/usr/bin/pvesh
chmod +x /usr/bin/pvesh
[ "$(/usr/local/sbin/nb-backup-metrics)" = "no nightly job" ] || fail "backup metrics without a job"
test ! -e /var/lib/prometheus/node-exporter/nullbill-backups.prom || fail "backup metrics written without a job"
grep -qx "OnUnitActiveSec=1h" /etc/systemd/system/nb-backup-metrics.timer || fail "backup metrics timer"
echo "ok   backup metrics script and hourly timer, silent until the nightly job exists"

legacy_disk_checks

test -s /etc/audit/rules.d/50-nullbill.rules || fail "audit rules"
echo "ok   audit rules installed (loading them needs a kernel; not checked here)"
