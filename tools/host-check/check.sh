#!/usr/bin/env bash
# Run the host baseline twice against this container, then check the result
# with the tools that read it. /ansible is the repo's ansible directory.
set -euo pipefail

play() {
  ansible-playbook -i localhost, -c local -e host_baseline_manage_services=false \
    -e host_baseline_api_name=pve.nullbill.internal -e host_baseline_api_address=10.42.1.254 /ansible/host-baseline.yml
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

# What a fresh PVE 8 install has: a key, the enterprise repositories, and
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

api=/etc/pve/nodes/$node/pveproxy-ssl.pem
for check in "-verify_hostname pve.nullbill.internal" "-verify_ip 10.42.1.254" "-verify_ip 192.0.2.10" "-verify_hostname $node"; do
  # shellcheck disable=SC2086
  openssl verify -CAfile /etc/pve/pve-root-ca.pem $check "$api" | grep -q ": OK" || fail "API certificate: $check"
done
grep -q "^nb_pve_api_cert_expiry_timestamp_seconds [0-9]" /var/lib/prometheus/node-exporter/nullbill-pve-api.prom || fail "API certificate metric"
echo "ok   the PVE API certificate names the mgmt name and address besides PVE's own, from PVE's CA"

test -s /etc/audit/rules.d/50-nullbill.rules || fail "audit rules"
echo "ok   audit rules installed (loading them needs a kernel; not checked here)"
