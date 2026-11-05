#!/usr/bin/env bash
# Container check for the agent_ssh role, run by tools/check-roles. A local
# CA made with ssh-keygen stands in for step-ca here; the step_ca check
# logs in with a certificate from the real agent provisioner.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

work=$(mktemp -d)
cd "$work"
ssh-keygen -q -t ed25519 -N '' -C test-ca -f ca
ssh-keygen -q -t ed25519 -N '' -C other-ca -f other
ssh-keygen -q -t ed25519 -N '' -C agent -f key

converge agent-ssh.yml -e agent_ssh_manage_services=false -e "agent_ssh_user_ca_key='$(cat ca.pub)'"

mkdir -p /run/sshd
/usr/sbin/sshd -E /tmp/sshd.log

# sign CA PRINCIPAL VALIDITY: a fresh certificate for key.pub.
sign() {
  rm -f key-cert.pub
  ssh-keygen -q -s "$1" -I test -n "$2" -V "$3" key.pub
}

# login USER: run id as USER with key and its certificate; prints the uid.
login() {
  ssh -n -i key -o CertificateFile=key-cert.pub -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    "$1@127.0.0.1" id -u
}

sign ca agent +30m
uid=$(login agent) || { cat /tmp/sshd.log; fail "certificate login as agent"; }
test "$uid" != 0 || fail "agent is root"
pass "a certificate for principal agent logs in as the agent user"

if login root >/dev/null 2>&1; then fail "agent certificate logs in as root"; fi
sign ca root +30m
if login root >/dev/null 2>&1; then fail "root certificate logs in as root"; fi
pass "no certificate logs in as root, whatever its principal"

sign ca agent -2h:-1h
if login agent >/dev/null 2>&1; then fail "expired certificate accepted"; fi
pass "an expired certificate is refused"

sign other agent +30m
if login agent >/dev/null 2>&1; then fail "certificate from another CA accepted"; fi
pass "a certificate from another CA is refused"

# With a valid certificate, the agent plants its own key: it must not work.
sign ca agent +30m
login agent >/dev/null
ssh -n -i key -o CertificateFile=key-cert.pub -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
  agent@127.0.0.1 "mkdir -p ~/.ssh && cat >~/.ssh/authorized_keys" <key.pub
rm key-cert.pub
if ssh -n -i key -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR agent@127.0.0.1 true 2>/dev/null; then
  fail "a key the agent added logs in"
fi
pass "a key the agent adds for itself is ignored"

id -nG agent | grep -qwE 'sudo|root|adm' && fail "agent is in a privileged group"
grep -q '^agent:!' /etc/shadow || fail "agent has a usable password"
settings=$(/usr/sbin/sshd -T -C user=agent,host=client,addr=127.0.0.1)
grep -qx 'disableforwarding yes' <<<"$settings" || fail "agent may forward"
root_settings=$(/usr/sbin/sshd -T -C user=root,host=client,addr=127.0.0.1)
grep -qx 'authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2' <<<"$root_settings" || fail "agent Match block leaks to other users"
pass "agent has no password, no sudo and no forwarding; other users are unchanged"
