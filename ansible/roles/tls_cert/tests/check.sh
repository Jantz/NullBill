#!/usr/bin/env bash
# Container check for the tls_cert role, run by tools/check-roles: a real
# step-ca in the same container issues the first certificate from a
# one-time token, then renews it with the certificate alone.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

ansible-playbook -i localhost, -c local -e step_ca_manage_services=false /ansible/step-ca.yml >/tmp/ca.log 2>&1 ||
  { cat /tmp/ca.log; fail "step-ca role"; }
echo "127.0.0.1 step-ca.nullbill.internal" >>/etc/hosts
STEPPATH=/etc/step-ca setpriv --reuid=step --regid=step --init-groups \
  step-ca /etc/step-ca/config/ca.json --password-file /etc/step-ca/secrets/password >/tmp/step-ca.log 2>&1 &
ca=(--ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt)
for _ in $(seq 20); do step ca health "${ca[@]}" >/dev/null 2>&1 && break; sleep 0.5; done

name=forgejo.nullbill.internal
token() {
  STEPPATH=/etc/step-ca step ca token "$name" --san "$name" "${ca[@]}" \
    --provisioner admin --provisioner-password-file /etc/step-ca/secrets/password 2>/dev/null
}
groupadd -f forgejo
vars=$(mktemp)
python3 - "$vars" "$(token)" <<'PY'
import json, sys
json.dump({"tls_cert_name": "forgejo.nullbill.internal", "tls_cert_token": sys.argv[2], "tls_cert_group": "forgejo",
           "tls_cert_ca_root": open("/etc/step-ca/certs/root_ca.crt").read()}, open(sys.argv[1], "w"))
PY
converge tls-cert.yml -e tls_cert_manage_services=false -e "@$vars"

crt=/etc/nullbill/tls/$name.crt
key=/etc/nullbill/tls/$name.key
openssl verify -CAfile /etc/step-ca/certs/root_ca.crt -untrusted /etc/step-ca/certs/intermediate_ca.crt "$crt" | grep -q ": OK" ||
  fail "chain"
openssl x509 -in "$crt" -noout -ext subjectAltName | grep -q "DNS:$name" || fail "SAN"
test "$(stat -c '%a %U %G' "$key")" = "640 root forgejo" || fail "key mode $(stat -c '%a %U %G' "$key")"
pass "first certificate from a one-time token, chained to the root; key readable by the service's group only"

used=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["tls_cert_token"])' "$vars")
if STEPPATH=/tmp/s step ca certificate "$name" /tmp/again.crt /tmp/again.key --token "$used" "${ca[@]}" >/dev/null 2>&1; then
  fail "a used token issued a second certificate"
fi
pass "the token works once"

serial=$(openssl x509 -in "$crt" -noout -serial)
# What the renewer does, once, without waiting for two thirds of the lifetime.
STEPPATH=/etc/nullbill/tls/step step ca renew --force "${ca[@]}" --exec "/usr/local/sbin/nb-cert-renewed-$name" \
  "$crt" "$key" >/dev/null 2>&1 || fail "renew"
test "$(openssl x509 -in "$crt" -noout -serial)" != "$serial" || fail "renew kept the old certificate"
su nobody -s /bin/bash -c "cat '$crt'" >/dev/null || fail "renewed certificate unreadable"
test "$(stat -c '%a %U %G' "$key")" = "640 root forgejo" || fail "renew changed the key mode"
grep -q -- "step ca renew --daemon .*--exec /usr/local/sbin/nb-cert-renewed-$name" "/etc/systemd/system/nb-cert-renew-$name.service" ||
  fail "renewer unit"
pass "renewal with the certificate alone replaces it and keeps the key's mode"
