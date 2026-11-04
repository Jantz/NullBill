#!/usr/bin/env bash
# Container check for the postgres role, run by tools/check-roles: apply it
# twice, then prove the client gets in over TLS and nothing else does.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

cat >/tmp/vars.yml <<'YAML'
postgres_manage_services: false
postgres_databases:
  - {name: forgejo, client: 127.0.0.1, password: "it's a test"}
YAML
converge postgres.yml -e @/tmp/vars.yml

export PGPASSWORD="it's a test"
psql "host=127.0.0.1 dbname=forgejo user=forgejo sslmode=require" -tAc "SELECT current_user" | grep -qx forgejo \
  || fail "client login over TLS"
pass "forgejo logs in to its database over TLS"

psql "host=127.0.0.1 dbname=forgejo user=forgejo sslmode=disable" -tAc "SELECT 1" >/dev/null 2>&1 \
  && fail "plaintext login accepted"
pass "plaintext connections are rejected"

psql "host=127.0.0.1 dbname=postgres user=forgejo sslmode=require" -tAc "SELECT 1" >/dev/null 2>&1 \
  && fail "forgejo reached another database"
pass "a client reaches only its own database"

PGPASSWORD=wrong psql "host=127.0.0.1 dbname=forgejo user=forgejo sslmode=require" -tAc "SELECT 1" >/dev/null 2>&1 \
  && fail "wrong password accepted"
pass "a wrong password is rejected"

# Move the client to another address: the same role from here must now be
# turned away by pg_hba, not by the password.
sed -i 's/client: 127.0.0.1/client: 192.0.2.17/' /tmp/vars.yml
ansible-playbook -i localhost, -c local -e @/tmp/vars.yml /ansible/postgres.yml >/tmp/move.log 2>&1 || { cat /tmp/move.log; fail "re-run with a new client address"; }
out=$(psql "host=127.0.0.1 dbname=forgejo user=forgejo sslmode=require" -tAc "SELECT 1" 2>&1) \
  && fail "login from an address that is not the client"
grep -q 'no pg_hba.conf entry for host "127.0.0.1"' <<<"$out" || fail "unexpected refusal: $out"
pass "the role is refused from any address but its client's"
