#!/usr/bin/env bash
# Run install.sh twice on this stand-in host, from the repo at /src (its
# committed HEAD), then check the page, the controller and its SSH to here.
set -euo pipefail
fail() {
  echo "FAIL $1"
  exit 1
}
export NULLBILL_REPO=file:///src NULLBILL_REF=$1 NULLBILL_BRIDGE=eth0
/usr/sbin/sshd
in_ct() { docker exec "$NB_CT" "$@"; }

bash /src/install.sh >/tmp/first.log 2>&1 || { cat /tmp/first.log; fail "first install"; }
code=$(sed -n 's/^Setup code: //p' /tmp/first.log)
[[ $code =~ ^[0-9A-F]{4}(-[0-9A-F]{4}){3}$ ]] || { cat /tmp/first.log; fail "install prints a setup code"; }
url=$(sed -n 's/^Open \(https:[^ ]*\) in a browser.*/\1/p' /tmp/first.log)
[ -n "$url" ] || fail "install prints the page's address"
echo "ok   install makes the container and prints $url and a setup code"

state=$(curl -fsSk "$url/api/state")
[ "$state" = '{"auth": false, "has_password": false}' ] || fail "the page answers: $state"
in_ct ssh -o BatchMode=yes "root@$(ip -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1)" true || fail "the container logs in to the host with its key and the pinned host key"
[ "$(in_ct stat -c %a /var/lib/nullbill)" = 700 ] || fail "NULLBILL_HOME is private"
in_ct /opt/nullbill/venv/bin/ansible-playbook --version >/dev/null || fail "ansible"
in_ct tofu version | grep -q v1.13.2 || fail "tofu"
in_ct sops --version 2>/dev/null | grep -q 3.13.3 || fail "sops"
in_ct age-keygen --version >/dev/null 2>&1 || fail "age"
echo "ok   the page answers; the controller has its tools, a private home, and SSH to the host"

bash /src/install.sh >/tmp/second.log 2>&1 || { cat /tmp/second.log; fail "second install"; }
grep -q "exists; keeping it" /tmp/second.log || fail "a second run keeps the container"
[ "$(grep -c nullbill@ /root/.ssh/authorized_keys)" = 1 ] || fail "a second run adds no second key"
grep -q "^Setup code: $code$" /tmp/second.log || fail "the setup code survives a re-run"
[ "$(grep -c '^create' /var/log/fake-pct.log)" = 1 ] || fail "a second run creates nothing"
echo "ok   a second run keeps the container, key and setup code"
