#!/usr/bin/env bash
# Run the host baseline twice against this container, then check the result
# with the tools that read it. /ansible is the repo's ansible directory.
set -euo pipefail

play() {
  ansible-playbook -i localhost, -c local -e host_baseline_manage_services=false /ansible/host-baseline.yml
}
fail() {
  echo "FAIL $1"
  exit 1
}

dpkg-query -W -f '${Package} ${Version}\n' ansible-core openssh-server
ssh-keygen -A >/dev/null
mkdir -p /root/.ssh /run/sshd

# Without a root key the role must stop before touching sshd.
: >/root/.ssh/authorized_keys
if play >/tmp/nokey.log 2>&1; then fail "baseline ran although root has no key"; fi
grep -q "authorized_keys is missing or empty" /tmp/nokey.log || fail "no-key refusal message"
test ! -e /etc/ssh/sshd_config.d/10-nullbill.conf || fail "sshd changed without a root key"
echo "ok   refuses to run without a root key"

# What a fresh PVE 8 install has: a key and the enterprise repositories.
echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample test" >/root/.ssh/authorized_keys
echo "deb https://enterprise.proxmox.com/debian/pve bookworm pve-enterprise" >/etc/apt/sources.list.d/pve-enterprise.list
echo "deb https://enterprise.proxmox.com/debian/ceph-quincy bookworm enterprise" >/etc/apt/sources.list.d/ceph.list

play >/tmp/first.log 2>&1 || { cat /tmp/first.log; fail "first run"; }
echo "ok   first run"
play >/tmp/second.log 2>&1 || { cat /tmp/second.log; fail "second run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/second.log || { tail -5 /tmp/second.log; fail "second run changed something"; }
echo "ok   second run changes nothing"

effective=$(/usr/sbin/sshd -T)
for want in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin without-password"; do
  grep -qx "$want" <<<"$effective" || fail "sshd: $want"
done
echo "ok   sshd is key-only"

! grep -rl enterprise.proxmox.com /etc/apt/sources.list.d/ || fail "enterprise repository left"
apt-get update >/tmp/apt.log 2>&1 || { cat /tmp/apt.log; fail "apt update"; }
grep -q "download.proxmox.com/debian/pve bookworm InRelease" /tmp/apt.log || fail "no-subscription repository not fetched"
! grep -E '^(W|E):' /tmp/apt.log || fail "apt update warnings"
echo "ok   no-subscription repository, no enterprise one"

origins=$(unattended-upgrade --dry-run --debug 2>&1 | grep "Allowed origins are:")
[[ $origins == "Allowed origins are: origin=Debian,codename=bookworm-security,label=Debian-Security" ]] || fail "unattended origins: $origins"
apt-config dump Unattended-Upgrade::Automatic-Reboot | grep -qx 'Unattended-Upgrade::Automatic-Reboot "false";' || fail "the host must not reboot by itself"
test ! -e /etc/needrestart/conf.d/50-nullbill.conf || fail "needrestart left to PVE on the host"
test -x /usr/local/sbin/nb-update-metrics || fail "update metrics script"
echo "ok   unattended upgrades take Debian security updates only, never reboot the host"

test -s /etc/audit/rules.d/50-nullbill.rules || fail "audit rules"
echo "ok   audit rules installed (loading them needs a kernel; not checked here)"
