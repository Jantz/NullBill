#!/usr/bin/env bash
# Container check for the custom_service role, run by tools/check-roles: a
# setup script runs as root with what the role gives it, once for each
# version of it; one that fails or hangs fails the run with the end of its
# output and runs again next time; a second run of the same script changes
# nothing.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

vars=/tmp/custom-vars.json
# The variables apply passes (nullbill/custom.py's variables), for a script.
with_script() { # file [name=value...]
  python3 - "$@" <<'PY' >"$vars"
import hashlib, json, sys
text = open(sys.argv[1]).read()
out = {
    "custom_service_name": "wiki",
    "custom_service_script": text,
    "custom_service_sha256": hashlib.sha256(text.encode()).hexdigest(),
    "custom_service_proxy": "http://10.42.2.10:3128",
    "custom_service_domain": "nullbill.internal",
    "custom_service_address": "10.42.9.64",
    "custom_service_ports": ["tcp/8080", "udp/9000"],
}
for pair in sys.argv[2:]:
    key, _, value = pair.partition("=")
    out[key] = json.loads(value)
print(json.dumps(out))
PY
}
play() { ansible-playbook -i localhost, -c local -e "@$vars" /ansible/custom-service.yml; }
mark=/var/lib/nb-custom/setup.sha256

# apt works from a script: the frontend is set, and nothing waits for input.
cat >/tmp/setup.sh <<'SH'
#!/bin/bash
set -euo pipefail
echo run >>/srv/runs
env | grep -E '^(NB_|DEBIAN_FRONTEND|https?_proxy|no_proxy)' | sort >/srv/env.txt
id -u >/srv/uid
pwd >/srv/cwd
if read -r -t 1 line; then echo "read $line" >/srv/stdin; else echo closed >/srv/stdin; fi
# The check's container reaches Debian's mirrors itself; a guest's apt is
# sent through the proxy by the updates role.
http_proxy='' https_proxy='' apt-get install -y -qq --no-install-recommends tree >/dev/null
SH
# No proxy in the check: curl and apt in the role's own tasks go direct.
with_script /tmp/setup.sh
converge custom-service.yml -e "@$vars"
[ "$(cat /srv/runs)" = run ] || { cat /srv/runs; fail "the script ran once over two runs of the playbook"; }
[ "$(cat /srv/uid)" = 0 ] || fail "the script runs as root"
[ "$(cat /srv/cwd)" = /root ] || fail "the script starts in /root"
[ "$(cat /srv/stdin)" = closed ] || fail "the script has nothing to read"
command -v tree >/dev/null || fail "the script installed a package"
for want in 'NB_SERVICE=wiki' 'NB_DOMAIN=nullbill.internal' 'NB_ADDRESS=10.42.9.64' 'NB_PORTS=tcp/8080 udp/9000' \
  'DEBIAN_FRONTEND=noninteractive' 'https_proxy=http://10.42.2.10:3128' 'no_proxy=localhost,127.0.0.1,.nullbill.internal'; do
  grep -qxF "$want" /srv/env.txt || { cat /srv/env.txt; fail "the script's environment has $want"; }
done
[ "$(cat $mark)" = "$(sha256sum /tmp/setup.sh | cut -d' ' -f1)" ] || fail "the script's SHA-256 is kept once it ran to its end"
[ "$(stat -c %a /usr/local/lib/nb-custom/setup.sh)" = 700 ] || fail "the script is root's alone"
pass "the script ran once, as root, with the service's name, address, ports and proxy, and installed a package"

# Another version of the script runs; the same one does not run again.
echo 'echo second >>/srv/runs' >>/tmp/setup.sh
with_script /tmp/setup.sh
play >/tmp/changed.log 2>&1 || { cat /tmp/changed.log; fail "a changed script"; }
[ "$(tr '\n' ' ' </srv/runs)" = "run run second " ] || { cat /srv/runs; fail "a changed script runs again, whole"; }
play >/tmp/same.log 2>&1 || { cat /tmp/same.log; fail "the same script again"; }
[ "$(wc -l </srv/runs)" = 3 ] || fail "the same script does not run twice"
[ "$(/usr/local/sbin/nb-custom-setup)" = unchanged ] || fail "nb-custom-setup says the script is unchanged"
[ "$(/usr/local/sbin/nb-custom-setup again)" = ran ] || fail "nb-custom-setup again says it ran"
[ "$(wc -l </srv/runs)" = 5 ] || fail "nb-custom-setup again runs it regardless"
pass "a changed script runs again, the same one does not, and nb-custom-setup again does"

# A script that fails: the run fails with the end of its output, nothing
# says it ran, and it runs again next time, changed or not.
cat >/tmp/broken.sh <<'SH'
#!/bin/bash
set -e
echo "before it broke"
if [ ! -e /srv/fixed ]; then echo "no such package: wikiserver" >&2; exit 3; fi
echo mended >/srv/mended
SH
with_script /tmp/broken.sh
if play >/tmp/broken.log 2>&1; then fail "a failing script passed"; fi
grep -q "the setup script failed (exit 3)" /tmp/broken.log || { tail -20 /tmp/broken.log; fail "the run says the script failed, with its exit status"; }
grep -q "no such package: wikiserver" /tmp/broken.log || fail "the run shows the end of the script's output"
[ ! -e $mark ] || fail "a script that failed is not marked as run"
grep -q "before it broke" /var/log/nb-custom-setup.log || fail "the script's output is kept in the guest"
touch /srv/fixed
play >/tmp/mended.log 2>&1 || { cat /tmp/mended.log; fail "the same script, once what it needed was there"; }
[ -e /srv/mended ] || fail "a script that failed runs again though it did not change"
pass "a failing script fails the run with its output, and runs again next time"

# A script that never ends is ended.
printf '#!/bin/sh\nsleep 600\n' >/tmp/hangs.sh
with_script /tmp/hangs.sh custom_service_timeout_s=3
started=$SECONDS
if play >/tmp/hangs.log 2>&1; then fail "a script that hangs passed"; fi
[ $((SECONDS - started)) -lt 120 ] || fail "a script that hangs was not ended"
grep -q "still running after 3 s" /tmp/hangs.log || { tail -20 /tmp/hangs.log; fail "the run says the script was ended"; }
pass "a script still running after its time is ended, and the run says so"

# Not a script: refused before anything is written.
echo 'just words' >/tmp/words.sh
with_script /tmp/words.sh
if play >/tmp/words.log 2>&1; then fail "text without #! was run"; fi
with_script /tmp/setup.sh 'custom_service_sha256="0000"'
if play >/tmp/sum.log 2>&1; then fail "a script that is not the one apply named was run"; fi
pass "text that is no script, and a script with another SHA-256 than apply named, are refused"
