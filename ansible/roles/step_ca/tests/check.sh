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
