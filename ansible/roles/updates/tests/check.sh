#!/usr/bin/env bash
# Container check for the updates role, run by tools/check-roles: set up
# unattended upgrades twice, run the nightly upgrade the way
# apt-daily-upgrade.service does, and read the metrics monitoring alerts on.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

converge updates.yml -e updates_manage_services=false
# Nobody answers on this proxy, so run only the proxy tasks with it.
only_proxy=(ansible-playbook -i "localhost," -c local --tags updates_proxy -e updates_manage_services=false /ansible/updates.yml)
"${only_proxy[@]}" -e updates_apt_proxy=http://192.0.2.10:3128 >/tmp/proxy.log 2>&1 || { cat /tmp/proxy.log; fail "run with a proxy"; }
grep -q 'Acquire::https::Proxy "http://192.0.2.10:3128";' /etc/apt/apt.conf.d/30nullbill-proxy || fail "apt proxy set"
"${only_proxy[@]}" >/tmp/direct.log 2>&1 || { cat /tmp/direct.log; fail "run without a proxy"; }
test ! -e /etc/apt/apt.conf.d/30nullbill-proxy || fail "apt proxy removed in a direct zone"
pass "apt goes through the proxy where the zone asks for it, directly elsewhere"

dpkg -s qemu-guest-agent >/dev/null 2>&1 && fail "no guest agent in a container"
converge updates.yml -e updates_manage_services=false -e updates_guest_agent=true
dpkg -s qemu-guest-agent >/dev/null 2>&1 || fail "guest agent installed in a VM"
pass "the QEMU guest agent goes into VMs only"

# PVE derives cloud-init's instance from a VM's cloud-init settings; a new
# instance makes new host keys unless the role's drop-in says otherwise.
keep=/etc/cloud/cloud.cfg.d/90-nullbill-host-keys.cfg
converge updates.yml -e updates_manage_services=false -e updates_keep_host_keys=true
test ! -e $keep || fail "no cloud-init drop-in without cloud-init"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloud-init openssh-server >/tmp/cloud-init-install.log 2>&1 || { cat /tmp/cloud-init-install.log; fail "cloud-init installed"; }
rm -f /etc/ssh/ssh_host_*
seed=/var/lib/cloud/seed/nocloud
mkdir -p $seed
printf '#cloud-config\n{}\n' >$seed/user-data
boot() { # instance-id: one boot's cloud-init, printing the ed25519 host key's fingerprint
  rm -rf /run/cloud-init
  printf 'instance-id: %s\n' "$1" >$seed/meta-data
  cloud-init init --local >>/tmp/cloud-init.out 2>&1 || true
  cloud-init init >>/tmp/cloud-init.out 2>&1 || true
  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2
}
first=$(boot one)
[ "$(boot two)" != "$first" ] || fail "cloud-init makes new keys for a new instance by default"
converge updates.yml -e updates_manage_services=false -e updates_keep_host_keys=true
kept=$(boot three)
[ "$(boot four)" = "$kept" ] || fail "host keys kept across a new instance"
grep -qx 'ssh_deletekeys: false' $keep || fail "drop-in written"
pass "in a VM, cloud-init keeps the SSH host keys when PVE gives it a new instance"

origins=$(unattended-upgrade --dry-run --debug 2>&1 | grep "Allowed origins are:")
[[ $origins == "Allowed origins are: origin=Debian,codename=trixie-security,label=Debian-Security, origin=Debian,codename=trixie,label=Debian" ]] || fail "unattended origins: $origins"
apt-config dump Unattended-Upgrade::Automatic-Reboot | grep -qx 'Unattended-Upgrade::Automatic-Reboot "true";' || fail "guests reboot after updates"
apt-config dump Unattended-Upgrade::Automatic-Reboot-Time | grep -qx 'Unattended-Upgrade::Automatic-Reboot-Time "04:30";' || fail "reboot time"
grep -qx "\$nrconf{restart} = 'a';" /etc/needrestart/conf.d/50-nullbill.conf || fail "needrestart restarts services"
pass "Debian security updates and point releases, rebooting at 04:30 when one needs it"

metrics() {
  /usr/share/prometheus-node-exporter-collectors/apt_info.py >/var/lib/prometheus/node-exporter/apt.prom
  /usr/local/sbin/nb-update-metrics
  cat /var/lib/prometheus/node-exporter/*.prom
}
# The role ran a moment ago: the setup time stands in until the first run.
since=$(stat -c %Y /var/lib/nullbill/updates-since)
metrics >/tmp/before.prom
grep -qx "nb_unattended_upgrade_success_timestamp_seconds $since" /tmp/before.prom || fail "setup time stands in before the first run"
grep -qx "nb_unattended_upgrade_enabled 1" /tmp/before.prom || { grep nb_ /tmp/before.prom; fail "enabled reported"; }
grep -q "^node_reboot_required 0" /tmp/before.prom || fail "reboot flag exported"
pass "metrics before the first run"

# A run whose unattended-upgrade fails leaves the last success where it was.
mv /usr/bin/unattended-upgrade /usr/bin/unattended-upgrade.real
printf '#!/bin/sh\necho "dpkg: error processing package" >&2\nexit 1\n' >/usr/bin/unattended-upgrade
chmod +x /usr/bin/unattended-upgrade
/usr/lib/apt/apt.systemd.daily install || true
mv /usr/bin/unattended-upgrade.real /usr/bin/unattended-upgrade
test ! -e /var/lib/apt/periodic/upgrade-stamp || fail "a failed run stamped success"
pass "a failed run is not counted as a success"

# The nightly run for real: afterwards nothing from the security archive is
# pending and the success time moves.
touch /run/reboot-required
/usr/lib/apt/apt.systemd.daily update
/usr/lib/apt/apt.systemd.daily install
metrics >/tmp/after.prom
test -e /var/lib/apt/periodic/upgrade-stamp || fail "the run stamped success"
grep -qx "nb_unattended_upgrade_success_timestamp_seconds $(stat -c %Y /var/lib/apt/periodic/upgrade-stamp)" /tmp/after.prom || fail "success time from the stamp"
if grep -E '^apt_upgrades_pending\{origin="[^"]*-security[^"]*"' /tmp/after.prom | grep -vq ' 0$'; then
  grep apt_upgrades_pending /tmp/after.prom
  fail "security updates still pending after the run"
fi
grep -q "^node_reboot_required 1" /tmp/after.prom || fail "reboot required exported"
pass "the nightly run installs pending security updates and moves the success time"

# nb-busy, which the host's nb-power asks before it stops a guest that runs
# on demand: sessions, the service's own answer in busy.d, and without one
# a connection from outside to the service's ports.
converge updates.yml -e updates_manage_services=false -e '{"updates_busy_ports": [7777, 7778]}'
[ "$(cat /etc/nullbill/busy-ports)" = "7777
7778" ] || fail "the service's ports written"
[ -z "$(nb-busy)" ] || { nb-busy; fail "an idle guest reports nothing"; }
bash -c 'exec -a "sshd: agent@pts/0" sleep 30' &
bash -c 'exec -a "sshd: agent@notty" sleep 30' &
bash -c 'exec -a "sshd: agent [priv]" sleep 30' &
bash -c 'exec -a "tmux: server" sleep 30' &
sleep 0.5
[ "$(nb-busy)" = "2 SSH sessions
a tmux or screen session is open" ] || { nb-busy; fail "sessions reported"; }
kill $(jobs -p)
wait 2>/dev/null || true
[ -z "$(nb-busy)" ] || fail "idle again once the sessions end"
# apt at work, from its timer or by hand; not the helper that waits for the
# guest to stop, which runs all day.
bash -c 'exec -a "/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal" sleep 30' &
sleep 0.5
[ -z "$(nb-busy)" ] || { nb-busy; fail "unattended-upgrade-shutdown is not an update"; }
bash -c 'exec -a "/usr/bin/python3 /usr/bin/unattended-upgrade" sleep 30' &
bash -c 'exec -a "/usr/bin/dpkg --status-fd 10 --configure --pending" sleep 30' &
sleep 0.5
[ "$(nb-busy)" = "updates are installing" ] || { nb-busy; fail "installing updates counts as use"; }
kill $(jobs -p)
wait 2>/dev/null || true
pass "nb-busy: an idle guest says nothing; SSH sessions, a tmux session and installing updates count as use"

address=$(hostname -i | cut -d' ' -f1)
python3 - "$address" <<'PY' &
import socket, sys, time
server = socket.create_server((sys.argv[1], 7777))
local = socket.create_server(("127.0.0.1", 7778))
held = [socket.create_connection((sys.argv[1], 7777)), socket.create_connection(("127.0.0.1", 7778))]
held += [server.accept()[0], local.accept()[0]]
open("/tmp/listening", "w").close()
time.sleep(30)
PY
listener=$!
for _ in $(seq 50); do [ -e /tmp/listening ] && break; sleep 0.1; done
[ "$(nb-busy connections 7777)" = 1 ] || { ss -tn; fail "a connection from outside is counted"; }
[ "$(nb-busy connections 7778)" = 0 ] || fail "a connection from the guest itself is not"
[ "$(nb-busy connections 7777 7778 9)" = 1 ] || fail "several ports"
[ "$(nb-busy)" = "1 connection" ] || { nb-busy; fail "without a program in busy.d, a connection to the service's ports is use"; }
# The service's own answer replaces the guess from connections.
printf '#!/bin/sh\necho "playing on 2 devices"\n' >/etc/nullbill/busy.d/service
chmod +x /etc/nullbill/busy.d/service
[ "$(nb-busy)" = "playing on 2 devices" ] || { nb-busy; fail "the service's own answer"; }
printf '#!/bin/sh\n' >/etc/nullbill/busy.d/service
[ -z "$(nb-busy)" ] || fail "the service says it is idle, connections or not"
kill "$listener"
pass "nb-busy: connections from outside count until the service's role says what use is"

printf '#!/bin/sh\necho "no API key" >&2\nexit 3\n' >/etc/nullbill/busy.d/service
[ "$(nb-busy)" = "cannot tell: service failed (no API key)" ] || { nb-busy; fail "a failing program counts as use"; }
printf '#!/bin/sh\nsleep 30\n' >/etc/nullbill/busy.d/service
[ "$(nb-busy)" = "cannot tell: service gave no answer" ] || { nb-busy; fail "a hanging program counts as use"; }
rm /etc/nullbill/busy.d/service
if nb-busy nonsense 2>/dev/null; then fail "unknown arguments refused"; fi
pass "nb-busy: a program that fails or hangs counts as use, so the guest stays on"
