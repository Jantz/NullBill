#!/usr/bin/env bash
# Container check for the vpn role, run by tools/check-roles: install
# Tailscale, run tailscaled as its unit would, converge twice logged out,
# then try a login with a key Tailscale refuses.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

routes='["10.42.1.0/24", "10.42.2.0/24"]'
play() { ansible-playbook -i localhost, -c local -e vpn_manage_services=false -e "{\"vpn_routes\": $routes}" "$@" /ansible/vpn.yml; }

# The packages first, then tailscaled the way its unit starts it.
play --skip-tags vpn_login >/tmp/install.log 2>&1 || { cat /tmp/install.log; fail "install"; }
dpkg-query -W -f '${Version}' tailscale | grep -qx "$(sed -n 's/^vpn_tailscale_version: //p' /ansible/roles/vpn/defaults/main.yml)" ||
  fail "tailscale at the pinned version"
grep -qx 'FLAGS="--tun=userspace-networking"' /etc/default/tailscaled || fail "userspace networking"
mkdir -p /var/lib/tailscale
tailscaled --tun=userspace-networking --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock \
  >/tmp/tailscaled.log 2>&1 &
for _ in $(seq 30); do tailscale status --json >/dev/null 2>&1 && break; sleep 1; done
pass "Tailscale from its signed repository at the pinned version, in userspace networking (no tun device)"

converge vpn.yml -e vpn_manage_services=false -e "{\"vpn_routes\": $routes}"
state=$(tailscale status --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["BackendState"])')
test "$state" = NeedsLogin || fail "logged out without a key: $state"
pass "without an auth key the guest stays logged out and the role still converges"

if play -e vpn_auth_key=tskey-auth-kNOTAREALKEY-0000 >/tmp/login.log 2>&1; then fail "a refused key logged in"; fi
grep -q "Log in as the tagged subnet router" /tmp/login.log || { cat /tmp/login.log; fail "login was not tried"; }
grep -q -- "--advertise-routes=10.42.1.0/24,10.42.2.0/24" /tmp/login.log || fail "routes advertised at login"
grep -q "NOTAREALKEY" /tmp/login.log && fail "the auth key reached the log"
test ! -e /run/nullbill-tailscale-key || fail "the key file was left behind"
pass "a login uses the key from a root-only file, removes it either way, and never logs it"
