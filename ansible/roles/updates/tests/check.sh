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

origins=$(unattended-upgrade --dry-run --debug 2>&1 | grep "Allowed origins are:")
[[ $origins == "Allowed origins are: origin=Debian,codename=bookworm-security,label=Debian-Security, origin=Debian,codename=bookworm,label=Debian" ]] || fail "unattended origins: $origins"
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
