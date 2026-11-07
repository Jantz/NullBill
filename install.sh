#!/usr/bin/env bash
# NullBill installer (docs/adr/0006-nullbill-on-the-server.md). Run it once,
# as root, in the shell of a fresh Proxmox VE 9 host (in the web UI:
# select the node, then Shell):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Jantz/NullBill/main/install.sh)"
#
# It makes the `nullbill` container, sets NullBill up inside it, and prints
# the address and one-time code of the setup page. Running it again keeps
# what exists and updates NullBill to the requested version.
set -euo pipefail

repo=${NULLBILL_REPO:-https://github.com/Jantz/NullBill}
ref=${NULLBILL_REF:-main}
vmid=${NULLBILL_VMID:-2002}
bridge=${NULLBILL_BRIDGE:-vmbr0}

say() { printf '\n== %s\n' "$*"; }
die() {
  printf 'nullbill install: %s\n' "$*" >&2
  exit 1
}
# pct exec passes this shell's locale on; Proxmox sets LANG=en_US.UTF-8,
# which the container's Debian has not generated, and Ansible then refuses
# to start. C.UTF-8 is built into every Debian.
in_ct() { pct exec "$vmid" -- env LANG=C.UTF-8 LC_ALL=C.UTF-8 LANGUAGE= "$@"; }

[ "$(id -u)" = 0 ] || die "run this as root"
{ [ -d /etc/pve ] && command -v pct >/dev/null; } || die "this is not a Proxmox VE host"
# PVE 8 reached its end of life in 2026-08 (https://pve.proxmox.com/wiki/FAQ).
pveversion | grep -Eq 'pve-manager/9\.' || die "needs Proxmox VE 9 (PVE 8 is past its end of life; upgrade first), found: $(pveversion)"
host_ip=$(ip -4 -o addr show dev "$bridge" | awk '{print $4}' | cut -d/ -f1 | head -1)
[ -n "$host_ip" ] || die "no IPv4 address on $bridge; set NULLBILL_BRIDGE to the bridge on your LAN"

if pct status "$vmid" >/dev/null 2>&1; then
  pct config "$vmid" | grep -q '^hostname: nullbill$' || die "VMID $vmid is taken by another guest; set NULLBILL_VMID"
  say "Container $vmid (nullbill) exists; keeping it"
else
  storage=$(pvesm status --content rootdir 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1; exit}')
  [ -n "$storage" ] || die "no active storage holds containers (content rootdir)"
  say "Downloading the Debian 12 container template"
  pveam update >/dev/null
  template=$(pveam available --section system | awk '$2 ~ /^debian-12-standard_.*_amd64\.tar\.zst$/ {print $2}' | sort -V | tail -1)
  [ -n "$template" ] || die "no Debian 12 template in pveam's list"
  pveam list local | grep -q "$template" || pveam download local "$template" >/dev/null
  say "Creating container $vmid (nullbill) on $storage"
  # Its LAN leg (net0) serves the setup page until VPN access works; apply
  # adds the mgmt leg (net1) behind the PVE firewall.
  pct create "$vmid" "local:vztmpl/$template" --hostname nullbill --unprivileged 1 --features nesting=1 \
    --cores 1 --memory 768 --swap 512 --rootfs "$storage:8" \
    --net0 "name=eth0,bridge=$bridge,ip=dhcp" --onboot 1 --startup order=1 \
    --tags nullbill --description "NullBill: the setup and status page. Made by install.sh." >/dev/null
fi
pct status "$vmid" | grep -q running || pct start "$vmid"

say "Waiting for the container's network"
for _ in $(seq 60); do
  in_ct getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done
in_ct getent hosts deb.debian.org >/dev/null || die "the container has no working network on $bridge"

say "Installing NullBill ($ref)"
in_ct bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates curl openssl >/dev/null'
if in_ct test -d /opt/nullbill/.git; then
  in_ct git -C /opt/nullbill fetch -q origin "$ref"
  in_ct git -C /opt/nullbill checkout -q --detach FETCH_HEAD
else
  in_ct git clone -q "$repo" /opt/nullbill
  in_ct git -C /opt/nullbill checkout -q --detach "$ref"
fi
in_ct /opt/nullbill/tools/setup-controller

say "Letting NullBill reach this host over SSH"
key=$(in_ct cat /root/.ssh/id_ed25519.pub)
grep -qxF "$key" /root/.ssh/authorized_keys 2>/dev/null || echo "$key" >>/root/.ssh/authorized_keys
# The host's own key, so the container never trusts a key on first sight.
# Other lines stay: once the LAN is closed, NullBill reaches this host at
# its mgmt address, which apply added here.
known=$(mktemp)
in_ct sh -c 'cat /root/.ssh/known_hosts 2>/dev/null || true' | awk -v h="$host_ip" '$1 != h' >"$known"
echo "$host_ip $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)" >>"$known"
pct push "$vmid" "$known" /root/.ssh/known_hosts --perms 0644
info=$(mktemp)
printf '{"host": "%s", "node": "%s", "bridge": "%s", "vmid": %s}\n' "$host_ip" "$(hostname)" "$bridge" "$vmid" >"$info"
pct push "$vmid" "$info" /var/lib/nullbill/install.json --perms 0600
rm -f "$known" "$info"

say "Starting the setup page"
in_ct install -m 0644 /opt/nullbill/tools/nullbill-web.service /etc/systemd/system/nullbill-web.service
in_ct systemctl daemon-reload
in_ct systemctl enable -q nullbill-web
in_ct systemctl restart nullbill-web
for _ in $(seq 30); do
  in_ct test -f /var/lib/nullbill/web/cert.pem && break
  sleep 1
done
ct_ip=$(in_ct ip -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1 | head -1)
fingerprint=$(in_ct openssl x509 -in /var/lib/nullbill/web/cert.pem -noout -fingerprint -sha256 | cut -d= -f2)

say "Done"
echo "Open https://$ct_ip:8443 in a browser on this network."
echo "Your browser will warn about the certificate. Its SHA-256 fingerprint is:"
echo "  $fingerprint"
if in_ct test -f /var/lib/nullbill/setup-code; then
  echo "Setup code: $(in_ct cat /var/lib/nullbill/setup-code)"
else
  echo "Sign in with the password you chose."
fi
