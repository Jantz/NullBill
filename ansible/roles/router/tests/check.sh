#!/usr/bin/env bash
# Container check for the router role, run by tools/check-roles: place the
# rendered small-class router files twice, and prove a broken ruleset or
# dnsmasq file is refused and the working one stays in place.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

mkdir -p /etc/cloud/cloud.cfg.d /etc/network/interfaces.d
echo "auto eth0" >/etc/network/interfaces.d/50-cloud-init
python3 /ansible/roles/router/tests/vars.py /ansible/roles/router/tests/files >/tmp/vars.json
converge router.yml -e router_manage_services=false -e @/tmp/vars.json

cmp -s /etc/nftables.conf /ansible/roles/router/tests/files/etc/nftables.conf || fail "nftables.conf placed"
test ! -e /etc/network/interfaces.d/50-cloud-init || fail "cloud-init's interface file removed"
grep -qx "network: {config: disabled}" /etc/cloud/cloud.cfg.d/99-nullbill-network.cfg || fail "cloud-init network off"
test -f /etc/systemd/network/11-nb-mgmt.link || fail ".link files placed"
pass "rendered files placed; cloud-init no longer configures the network"

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
