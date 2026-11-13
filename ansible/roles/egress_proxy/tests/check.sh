#!/usr/bin/env bash
# Container check for the egress_proxy role, run by tools/check-roles: place
# the small class's rendered squid.conf twice, run Squid on the proxy's own
# address, and prove a guest in a proxied zone gets allowlisted names only,
# a guest in another zone gets nothing, and a broken squid.conf is refused.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

files=/ansible/roles/egress_proxy/tests/files
python3 /ansible/roles/router/tests/vars.py "$files" | python3 -c 'import json, sys; print(json.dumps({"egress_proxy_files": json.load(sys.stdin)["router_files"]}))' >/tmp/vars.json
converge egress-proxy.yml -e egress_proxy_manage_services=false -e @/tmp/vars.json
cmp -s /etc/squid/squid.conf "$files/etc/squid/squid.conf" || fail "squid.conf placed"

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends iproute2 >/dev/null
# The proxy's address, a client in the mgmt zone (proxied) and one in the
# services zone (direct egress, so not a client).
ip addr add 10.42.2.10/32 dev lo
ip addr add 10.42.1.50/32 dev lo
ip addr add 10.42.2.50/32 dev lo
squid -f /etc/squid/squid.conf -N >/tmp/squid.log 2>&1 &
for _ in $(seq 30); do (exec 3<>/dev/tcp/10.42.2.10/3128) 2>/dev/null && break; sleep 1; done
through() { curl -s -o /dev/null -w '%{http_code}' -x http://10.42.2.10:3128 --interface "$1" "$2" || true; }
code=$(through 10.42.1.50 https://deb.debian.org/debian/dists/trixie/Release)
[ "$code" = 200 ] || { tail /tmp/squid.log; fail "a mgmt guest fetches from deb.debian.org (got $code)"; }
code=$(through 10.42.1.50 https://example.com/)
[ "$code" = 403 ] || [ "$code" = 000 ] || fail "a name off the allowlist is refused (got $code)"
code=$(through 10.42.2.50 https://deb.debian.org/debian/dists/trixie/Release)
[ "$code" = 403 ] || [ "$code" = 000 ] || fail "a guest outside the proxied zones is refused (got $code)"
pass "allowlisted names for proxied zones only"

python3 - /tmp/vars.json <<'PY' >/tmp/broken.json
import json, sys
data = json.load(open(sys.argv[1]))
data["egress_proxy_files"]["/etc/squid/squid.conf"] += "http_access allow everything_that_is_not_an_acl\n"
print(json.dumps(data))
PY
if ansible-playbook -i localhost, -c local -e egress_proxy_manage_services=false -e @/tmp/broken.json /ansible/egress-proxy.yml >/tmp/broken.log 2>&1; then
  fail "a broken squid.conf was accepted"
fi
grep -q "failed to validate" /tmp/broken.log || fail "the squid.conf check failed for another reason: $(tail -5 /tmp/broken.log)"
cmp -s /etc/squid/squid.conf "$files/etc/squid/squid.conf" || fail "the working squid.conf was kept"
pass "a broken squid.conf is refused and the working one stays"
