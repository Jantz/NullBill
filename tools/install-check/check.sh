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

LANG=en_US.UTF-8 bash /src/install.sh >/tmp/first.log 2>&1 || { cat /tmp/first.log; fail "first install"; }
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
in_ct runuser -u nbagent -- /usr/local/bin/claude --version | grep -q "(Claude Code)" || fail "the page's agent CLI runs as nbagent"
[ "$(in_ct stat -c %a /var/lib/nullbill-agent)" = 700 ] || fail "the agent's home is private"
echo "ok   the page answers; the controller has its tools, a private home, and SSH to the host"

# What apply adds when it closes the LAN: the host under its mgmt address.
mgmt="10.42.1.254 $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
in_ct sh -c "echo '$mgmt' >>/root/.ssh/known_hosts"
LANG=en_US.UTF-8 bash /src/install.sh >/tmp/second.log 2>&1 || { cat /tmp/second.log; fail "second install"; }
in_ct grep -qxF "$mgmt" /root/.ssh/known_hosts || fail "a second run keeps the host's mgmt address in known_hosts"
[ "$(in_ct grep -c "^$(ip -4 -o addr show dev eth0 | awk '{print $4}' | cut -d/ -f1) " /root/.ssh/known_hosts)" = 1 ] ||
  fail "a second run keeps one line for the host's LAN address"
grep -q "exists; keeping it" /tmp/second.log || fail "a second run keeps the container"
[ "$(grep -c nullbill@ /root/.ssh/authorized_keys)" = 1 ] || fail "a second run adds no second key"
grep -q "^Setup code: $code$" /tmp/second.log || fail "the setup code survives a re-run"
[ "$(grep -c '^create' /var/log/fake-pct.log)" = 1 ] || fail "a second run creates nothing"
echo "ok   a second run keeps the container, key, setup code and the host keys NullBill trusts"

# Signing in and choosing a password makes the ntfy topic in the sops file.
jar=$(mktemp)
api() { curl -fsSk -b "$jar" -c "$jar" -H 'X-NullBill: 1' -H 'Content-Type: application/json' "$url$1" ${2:+-d "$2"}; }
api /api/login "{\"secret\": \"$code\"}" >/dev/null || fail "sign in with the setup code"
api /api/password '{"password": "correct horse battery"}' >/dev/null || { in_ct tail /var/log/nullbill-web.log; fail "choose a password"; }
topic=$(api /api/state | grep -o '"ntfy_url": "[^"]*"' | cut -d'"' -f4)
[[ $topic =~ ^https://ntfy\.sh/nullbill-[A-Za-z0-9_-]{24}$ ]] || fail "the page shows an ntfy topic: $topic"
in_ct grep -q '^ntfy_topic: ENC\[' /var/lib/nullbill/secrets.sops.yaml || fail "the topic is stored encrypted"
[ "$(in_ct stat -c %a /var/lib/nullbill/secrets.sops.yaml)" = 600 ] || fail "the secrets file is private"
in_ct runuser -u nbagent -- test ! -r /var/lib/nullbill/secrets.sops.yaml || fail "the agent cannot read the secrets"
echo "ok   a password makes the alert topic, kept encrypted in the secrets file"
