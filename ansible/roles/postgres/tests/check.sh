#!/usr/bin/env bash
# Container check for the postgres role, run by tools/check-roles: apply it
# twice, then prove the client gets in over TLS and nothing else does.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

# What tls_cert leaves behind, from a throwaway CA instead of step-ca.
test_pki /tmp/pki postgres.nullbill.internal
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-common >/dev/null 2>&1
getent group postgres >/dev/null || groupadd -r postgres
install -d -m 0755 /etc/nullbill/tls
install -m 0644 /tmp/pki/loki.crt /etc/nullbill/tls/postgres.nullbill.internal.crt
install -m 0640 -g postgres /tmp/pki/loki.key /etc/nullbill/tls/postgres.nullbill.internal.key
install -m 0644 /tmp/pki/ca.crt /etc/nullbill/tls/root_ca.crt
echo "127.0.0.1 postgres.nullbill.internal" >>/etc/hosts

cat >/tmp/vars.yml <<'YAML'
postgres_manage_services: false
postgres_databases:
  - {name: forgejo, client: 127.0.0.1, password: "it's a test"}
YAML
# A cluster of the PostgreSQL before, not moved over: the role stops
# rather than start an empty one beside it.
install -d /var/lib/postgresql/15/main
echo 15 >/var/lib/postgresql/15/main/PG_VERSION
if ansible-playbook -i localhost, -c local -e @/tmp/vars.yml /ansible/postgres.yml >/tmp/older.log 2>&1; then
  fail "the role ran beside an older version's data"
fi
grep -q "/var/lib/postgresql/15 holds a database cluster of another" /tmp/older.log || { cat /tmp/older.log; fail "the role names the older cluster"; }
[ ! -e /var/lib/postgresql/17 ] || fail "nothing was installed beside the older data"
rm -r /var/lib/postgresql/15
pass "beside an older PostgreSQL's data that was not moved over, the role stops and says so"

converge postgres.yml -e @/tmp/vars.yml

export PGPASSWORD="it's a test"
verified="host=postgres.nullbill.internal hostaddr=127.0.0.1 dbname=forgejo user=forgejo sslmode=verify-full sslrootcert=/etc/nullbill/tls/root_ca.crt"
psql "$verified" -tAc "SELECT current_user" | grep -qx forgejo \
  || fail "client login over TLS"
pass "forgejo logs in to its database over TLS, verifying the server's certificate"

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
