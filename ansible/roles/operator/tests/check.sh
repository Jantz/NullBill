#!/usr/bin/env bash
# Container check for the operator role, run by tools/check-roles. The CA,
# an agent-managed guest and the operator share this container: the agent
# user gets a certificate from the real agent provisioner and logs in to
# the guest with nb-ssh. The PVE token, PVE CA and model API key are
# throwaway values; nothing here talks to PVE or the model API.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

ansible-playbook -i localhost, -c local -e step_ca_manage_services=false /ansible/step-ca.yml >/tmp/ca.log 2>&1 ||
  { cat /tmp/ca.log; fail "step-ca role"; }
echo "127.0.0.1 step-ca.nullbill.internal" >>/etc/hosts
STEPPATH=/etc/step-ca setpriv --reuid=step --regid=step --init-groups \
  step-ca /etc/step-ca/config/ca.json --password-file /etc/step-ca/secrets/password >/tmp/step-ca.log 2>&1 &
for _ in $(seq 20); do
  step ca health --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt >/dev/null 2>&1 && break
  sleep 0.5
done
step ca health --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt >/dev/null 2>&1 ||
  { cat /tmp/step-ca.log; fail "CA health"; }
ansible-playbook -i localhost, -c local -e agent_ssh_manage_services=false \
  -e "agent_ssh_user_ca_key='$(cat /etc/step-ca/certs/ssh_user_ca_key.pub)'" /ansible/agent-ssh.yml >/tmp/guest.log 2>&1 ||
  { cat /tmp/guest.log; fail "agent_ssh role"; }
/usr/sbin/sshd -E /tmp/sshd.log

vars=$(mktemp)
python3 - "$vars" <<'PY'
import json, sys
json.dump({
    "operator_pve_token": "nb-agent@pve!operator=00000000-0000-0000-0000-000000000000",
    "operator_pve_ca": "throwaway, not a certificate",
    "operator_ca_root": open("/etc/step-ca/certs/root_ca.crt").read(),
    "operator_step_password": open("/etc/step-ca/secrets/agent-password").read(),
    "operator_anthropic_api_key": "throwaway-key",
}, open(sys.argv[1], "w"))
PY
converge operator.yml -e "@$vars"

test "$(stat -c '%a %U %G' /etc/nullbill/agent)" = "750 root agent" || fail "secrets directory mode"
for f in pve-token pve-root-ca.pem ca-root.pem step-password anthropic-api-key; do
  test "$(stat -c '%a %U %G' "/etc/nullbill/agent/$f")" = "640 root agent" || fail "$f mode"
done
if su nobody -s /bin/bash -c 'cat /etc/nullbill/agent/pve-token' >/dev/null 2>&1; then fail "other users read the token"; fi
id -nG agent | grep -qwE 'sudo|root|adm' && fail "agent is in a privileged group"
pass "secrets are root's, readable by the agent user only; the agent has no sudo"

want=$(sed -n 's/^operator_claude_code_sha512: //p' /ansible/roles/operator/defaults/main.yml)
sha512sum /opt/claude-code/*/package.tgz | awk '{print $1}' | grep -qx "$want" || fail "agent CLI checksum"
head -c 4 "$(readlink -f /usr/local/bin/claude)" | grep -q ELF || fail "agent CLI is not an executable"
python3 -c 'import json; s = json.load(open("/etc/claude-code/managed-settings.json")); assert s["env"]["DISABLE_AUTOUPDATER"] == "1", s' ||
  fail "managed settings"
test "$(su agent -c "$(python3 -c 'import json; print(json.load(open("/etc/claude-code/managed-settings.json"))["apiKeyHelper"])')")" = throwaway-key ||
  fail "the agent CLI's key helper"
if su agent -c 'test -w /etc/claude-code/managed-settings.json || test -w /etc/claude-code/CLAUDE.md'; then
  fail "agent can change its rules"
fi
pass "agent CLI pinned by checksum; its managed settings and rules are root's"

out=$(su - agent -c 'nb-ssh 127.0.0.1 id -un' 2>/tmp/nb-ssh.log) || { cat /tmp/nb-ssh.log /tmp/sshd.log; fail "nb-ssh"; }
test "$out" = agent || fail "nb-ssh logged in as '$out'"
before=$(stat -c %Y /home/agent/.ssh/nb-agent-cert.pub)
sleep 1
su - agent -c 'nb-ssh 127.0.0.1 true'
test "$(stat -c %Y /home/agent/.ssh/nb-agent-cert.pub)" = "$before" || fail "nb-ssh fetched a certificate it did not need"
# ssh's -l agent wins over a user in the destination; agent_ssh's check
# shows the certificate itself never logs in as root.
out=$(su - agent -c 'nb-ssh root@127.0.0.1 id -un' 2>/dev/null || true)
test "$out" != root || fail "nb-ssh logged in as root"
if su - agent -c 'ssh -i ~/.ssh/nb-agent -o IdentitiesOnly=yes -o BatchMode=yes root@127.0.0.1 true' 2>/dev/null; then
  fail "the agent's certificate logs in as root"
fi
pass "nb-ssh logs in to an agent-managed guest as agent with a reused certificate, never as root"
