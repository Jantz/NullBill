#!/usr/bin/env bash
# Container check for the step_ca role, run by tools/check-roles: apply it
# twice, start the CA, issue a certificate and verify it against the root.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

converge step-ca.yml -e step_ca_manage_services=false

export STEPPATH=/etc/step-ca
echo "127.0.0.1 step-ca.nullbill.internal" >>/etc/hosts
# No init in the container: start the CA the way the unit does.
setpriv --reuid=step --regid=step --init-groups \
  step-ca /etc/step-ca/config/ca.json --password-file /etc/step-ca/secrets/password >/tmp/ca.log 2>&1 &
for _ in $(seq 20); do step ca health --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt >/dev/null 2>&1 && break; sleep 0.5; done
step ca health --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt | grep -qx ok || { cat /tmp/ca.log; fail "CA health"; }
pass "CA answers on 443 as the step user"

cd /tmp
step ca certificate forgejo.nullbill.internal leaf.crt leaf.key \
  --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt \
  --provisioner admin --provisioner-password-file /etc/step-ca/secrets/password >/dev/null 2>&1 || fail "issue certificate"
openssl verify -CAfile /etc/step-ca/certs/root_ca.crt -untrusted /etc/step-ca/certs/intermediate_ca.crt leaf.crt | grep -q ": OK" || fail "chain"
openssl x509 -in leaf.crt -noout -ext subjectAltName | grep -q "DNS:forgejo.nullbill.internal" || fail "SAN"
pass "issues a certificate that chains to the root"

curl -fsS --cacert /etc/step-ca/certs/root_ca.crt https://step-ca.nullbill.internal/acme/acme/directory | grep -q newOrder || fail "ACME directory"
pass "ACME provisioner serves its directory"

test "$(stat -c '%a %U' /etc/step-ca/secrets/password)" = "600 step" || fail "password file mode"
pass "key password is 0600 and owned by step"

# The agent provisioner: SSH user certificates for principal agent only.
agent_cert() {
  rm -f agent agent.pub agent-cert.pub
  step ssh certificate "$@" --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt \
    --provisioner agent --provisioner-password-file /etc/step-ca/secrets/agent-password \
    --no-password --insecure --no-agent --force >/tmp/agent-cert.log 2>&1
}
agent_cert root agent --principal root || { cat /tmp/agent-cert.log; fail "agent certificate"; }
principals=$(ssh-keygen -L -f agent-cert.pub | sed -n '/Principals:/,/Critical Options:/p' | sed '1d;$d' | tr -d ' ')
test "$principals" = agent || fail "principals are '$principals'"
ssh-keygen -L -f agent-cert.pub | grep -q 'Type: .* user certificate' || fail "not a user certificate"
pass "agent provisioner signs user certificates for principal agent, whatever was asked"

if agent_cert agent agent --not-after 2h; then fail "a 2 h agent certificate was issued"; fi
agent_cert agent agent --not-after 1h || { cat /tmp/agent-cert.log; fail "a 1 h agent certificate"; }
pass "agent certificates last at most 1 h"

if agent_cert --host role-check agent; then fail "agent provisioner signed a host certificate"; fi
if step ca certificate forgejo.nullbill.internal x.crt x.key --ca-url https://step-ca.nullbill.internal \
  --root /etc/step-ca/certs/root_ca.crt --provisioner agent \
  --provisioner-password-file /etc/step-ca/secrets/agent-password --force >/dev/null 2>&1; then
  fail "agent provisioner signed an X.509 certificate"
fi
pass "agent provisioner signs no host or X.509 certificate"

# End to end: a guest set up by agent_ssh with this CA's key.
ansible-playbook -i localhost, -c local -e agent_ssh_manage_services=false \
  -e "agent_ssh_user_ca_key='$(cat /etc/step-ca/certs/ssh_user_ca_key.pub)'" /ansible/agent-ssh.yml >/tmp/agent-ssh.log 2>&1 ||
  { cat /tmp/agent-ssh.log; fail "agent_ssh role"; }
start_journald
/usr/sbin/sshd -E /tmp/sshd.log
agent_cert agent agent || fail "agent certificate"
ssh_as() {
  ssh -n -i agent -o CertificateFile=agent-cert.pub -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR "$1@127.0.0.1" id -un
}
test "$(ssh_as agent)" = agent || { cat /tmp/sshd.log; fail "step-ca certificate login as agent"; }
if ssh_as root >/dev/null 2>&1; then fail "step-ca agent certificate logs in as root"; fi
pass "a certificate from the agent provisioner logs in as agent on a guest, not as root"
