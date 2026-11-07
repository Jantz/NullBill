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
start_journald
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

sign ca agent +30m
login agent >/dev/null
uid=$(id -u agent)
journalctl -t nb-cmd -o json --no-pager | python3 -c '
import json, sys
uid = sys.argv[1]
hits = [e for e in map(json.loads, sys.stdin) if e["MESSAGE"] == "ssh: id -u" and e["_UID"] == uid]
sys.exit(0 if hits else 1)' "$uid" || fail "command not in the journal under the agent's uid"
if ssh -i key -o CertificateFile=key-cert.pub -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR agent@127.0.0.1 </dev/null >/dev/null 2>&1; then
  fail "a session without a command was allowed"
fi
pass "each agent command is in the journal under the agent's uid before it runs; no bare sessions"

# The agent owns its home, so it can write shell startup files there. One
# that runs the command itself would skip the recorder; none may run.
agent_run() {
  ssh -n -i key -o CertificateFile=key-cert.pub -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR agent@127.0.0.1 "$1"
}
# shellcheck disable=SC2016 # expanded on the guest, by the planted file
agent_run 'for f in .bashrc .profile .bash_profile .bash_login; do
  printf "%s\n" "echo startup-file-ran; exec /bin/bash --norc -c \"\$SSH_ORIGINAL_COMMAND\"" >~/$f
done'
out=$(agent_run "echo after-startup-files") || fail "a command after the startup files were planted"
test "$out" = after-startup-files || fail "a startup file ran: $out"
journalctl -t nb-cmd -o cat --no-pager >/tmp/nb-cmd.log
grep -qx "ssh: echo after-startup-files" /tmp/nb-cmd.log || fail "a command after a planted .bashrc was not recorded"
pass "startup files the agent writes in its home neither run nor skip the recorder"

stop_journald
if login agent >/dev/null 2>&1; then fail "a command ran without being recorded"; fi
start_journald
pass "a command that cannot be recorded does not run, even with startup files planted"

id -nG agent | grep -qwE 'sudo|root|adm' && fail "agent is in a privileged group"
grep -q '^agent:!' /etc/shadow || fail "agent has a usable password"
settings=$(/usr/sbin/sshd -T -C user=agent,host=client,addr=127.0.0.1)
grep -qx 'disableforwarding yes' <<<"$settings" || fail "agent may forward"
root_settings=$(/usr/sbin/sshd -T -C user=root,host=client,addr=127.0.0.1)
grep -qx 'authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2' <<<"$root_settings" || fail "agent Match block leaks to other users"
pass "agent has no password, no sudo and no forwarding; other users are unchanged"
