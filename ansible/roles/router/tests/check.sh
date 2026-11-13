#!/usr/bin/env bash
# Container check for the router role, run by tools/check-roles: place the
# rendered small-class router files twice, prove a broken ruleset or dnsmasq
# file is refused and the working one stays in place, and run nb-portmap
# where no home router answers. tools/check-guests has one that does.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

mkdir -p /etc/cloud/cloud.cfg.d /etc/network/interfaces.d /etc/netplan
echo "auto eth0" >/etc/network/interfaces.d/50-cloud-init
echo "network: {version: 2}" >/etc/netplan/50-cloud-init.yaml
python3 /ansible/roles/router/tests/vars.py /ansible/roles/router/tests/files >/tmp/vars.json
converge router.yml -e router_manage_services=false -e @/tmp/vars.json

cmp -s /etc/nftables.conf /ansible/roles/router/tests/files/etc/nftables.conf || fail "nftables.conf placed"
test ! -e /etc/network/interfaces.d/50-cloud-init || fail "cloud-init's interface file removed"
# Debian 12's cloud image names the legs through netplan, whose .link files win.
test ! -e /etc/netplan/50-cloud-init.yaml || fail "cloud-init's netplan file removed"
grep -qx "network: {config: disabled}" /etc/cloud/cloud.cfg.d/99-nullbill-network.cfg || fail "cloud-init network off"
test -f /etc/systemd/network/11-nb-mgmt.link || fail ".link files placed"
pass "rendered files placed; cloud-init no longer configures the network"

# The port mapping: the script, the clients it runs and the units that run it.
test -x /usr/local/sbin/nb-portmap || fail "nb-portmap installed"
command -v upnpc >/dev/null || fail "Debian's UPnP client installed"
units=(/etc/systemd/system/nullbill-portmap.service /etc/systemd/system/nullbill-portmap.timer)
verify=$(systemd-analyze verify "${units[@]}" 2>&1) || fail "systemd refuses the port mapping units: $verify"
grep -q "nullbill-portmap" <<<"$verify" && fail "systemd warns about the port mapping units: $verify"
# This container has no nb-uplink: nothing is asked, and that is no error.
STATE_DIRECTORY=/tmp/portmap
export STATE_DIRECTORY
mkdir -p "$STATE_DIRECTORY"
said=$(/usr/local/sbin/nb-portmap /etc/nullbill/portmap.json) || fail "nb-portmap exits 0 whatever the home router answers"
grep -q "did not open UDP 41642: NAT-PMP: nb-uplink has no address or no default route" <<<"$said" || fail "nb-portmap without an uplink: $said"
python3 - <<'PY' || fail "nb-portmap's state for verify and the page"
import json
state = json.load(open("/tmp/portmap/state.json"))
assert state["enabled"] is True and state["port"] == 41642 and state["method"] is None and state["at"] > 0, state
PY
pass "nb-portmap, its clients and its timer are in place; without a home router to ask it reports that and exits 0"

for broken in /etc/nftables.conf /etc/nullbill/dnsmasq-internal.conf; do
  python3 - /tmp/vars.json "$broken" <<'PY' >/tmp/broken.json
import json, sys
data = json.load(open(sys.argv[1]))
data["router_files"][sys.argv[2]] += "this is not valid\n"
print(json.dumps(data))
PY
  if ansible-playbook -i localhost, -c local -e router_manage_services=false -e @/tmp/broken.json /ansible/router.yml >/tmp/broken.log 2>&1; then
    fail "a broken $broken was accepted"
  fi
  grep -q "failed to validate" /tmp/broken.log || fail "the $broken check failed for another reason: $(tail -5 /tmp/broken.log)"
  cmp -s "$broken" "/ansible/roles/router/tests/files$broken" || fail "the working $broken was kept"
done
pass "a broken ruleset or dnsmasq file is refused and the working one stays"
