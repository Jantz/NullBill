#!/usr/bin/env bash
# Container check for the console role, run by tools/check-roles: set up the
# on-site screen twice, then drive the same Chromium headless with the same
# policy and flags: it opens a page whose key it was given, refuses one it
# was not, and is blocked from any other address. Switching it off removes
# the unit. No display here, so cage itself is not started.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

page() { # page DIR PORT: serve a NullBill page over HTTPS with a fresh key
  mkdir -p "$1"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 -subj /CN=nullbill \
    -keyout "$1/key.pem" -out "$1/cert.pem" 2>/dev/null
  echo '<!doctype html><title>NullBill</title><p id="ok">the setup page</p>' >"$1/index.html"
  python3 - "$1" "$2" <<'PY' &
import functools, http.server, ssl, sys
d, port = sys.argv[1], int(sys.argv[2])
server = http.server.ThreadingHTTPServer(("127.0.0.1", port), functools.partial(http.server.SimpleHTTPRequestHandler, directory=d))
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(f"{d}/cert.pem", f"{d}/key.pem")
server.socket = tls.wrap_socket(server.socket, server_side=True)
server.serve_forever()
PY
}
spki() { openssl x509 -in "$1" -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64; }
page /tmp/page 18443
page /tmp/other 18444
key=$(spki /tmp/page/cert.pem)

on=(-e console_enabled=true -e console_manage_services=false -e console_url=https://127.0.0.1:18443/ -e "console_page_spki=$key")
converge console.yml "${on[@]}"
unit=/etc/systemd/system/nullbill-console.service
grep -q -- "--ignore-certificate-errors-spki-list=$key https://127.0.0.1:18443/" $unit || fail "unit pins the page's key"
grep -q '^ExecStart=/usr/bin/cage -d -s -- /usr/bin/chromium --kiosk' $unit || fail "cage runs chromium as a kiosk, console switching allowed"
[ "$(getent passwd nb-console | cut -d: -f7)" = /usr/sbin/nologin ] || fail "the screen user has no shell"
systemd-analyze verify $unit 2>&1 | grep -v -e 'Failed to prepare' -e 'getty@' | grep . && fail "systemd-analyze finds problems in the unit"
pass "the screen's unit, policy and user"

# As the unit would, minus the compositor: headless, and no sandbox, which
# needs namespaces the container does not give. Headless mode works through
# DevTools, which the policy switches off, so that one setting is checked
# here and left out while browsing.
policy=/etc/chromium/policies/managed/nullbill.json
python3 - "$policy" <<'PY' || fail "the policy switches off DevTools, downloads and printing"
import json, sys
p = json.load(open(sys.argv[1]))
assert p["DeveloperToolsAvailability"] == 2 and p["DownloadRestrictions"] == 3 and p["PrintingEnabled"] is False, p
assert p["URLBlocklist"] == ["*"] and p["URLAllowlist"] == ["https://127.0.0.1:18443/", "about:blank"], p
del p["DeveloperToolsAvailability"]
json.dump(p, open(sys.argv[1], "w"))
PY
browse() { # browse URL SPKI: the DOM a fresh Chromium profile gets, as the screen user
  local profile
  profile=$(mktemp -d)
  chown nb-console "$profile"
  setpriv --reuid=nb-console --regid=nb-console --init-groups env HOME=/var/lib/nb-console \
    timeout 60 chromium --headless --no-sandbox --disable-gpu --incognito --dump-dom --user-data-dir="$profile" \
    --ignore-certificate-errors-spki-list="$2" "$1" 2>/dev/null || true
}
browse https://127.0.0.1:18443/ "$key" | grep -q 'the setup page' || fail "the screen opens the page whose key it pins"
browse https://127.0.0.1:18443/ "$(spki /tmp/other/cert.pem)" | grep -q 'the setup page' && fail "the screen opened a page with another key"
browse https://127.0.0.1:18444/ "$(spki /tmp/other/cert.pem)" | grep -q 'the setup page' && fail "the policy let the screen open another address"
pass "the screen opens the NullBill page with its pinned key and nothing else"

converge console.yml -e console_manage_services=false
for f in $unit /etc/chromium/policies/managed/nullbill.json /etc/pam.d/nullbill-console; do
  test ! -e "$f" || fail "$f is left after switching the screen off"
done
pass "switching the screen off removes it"
