#!/usr/bin/env bash
# In the Debian 12 container: fill it as a guest would be, run
# nb-release-upgrade (mounted at /src) and look at what it left.
set -euo pipefail
fail() { echo "FAIL $*"; tail -n 40 /var/log/nb-release-upgrade.log 2>/dev/null || true; exit 1; }
pass() { echo "ok   $*"; }
upgrade=/src/nb-release-upgrade
as_postgres() { runuser -u postgres -- "$@"; }

grep -q '^VERSION_CODENAME=bookworm$' /etc/os-release || fail "the container starts on Debian 12"
pg_ctlcluster 15 main start
as_postgres psql -qc "CREATE ROLE forgejo LOGIN PASSWORD 'not-a-secret'" -c "CREATE DATABASE forgejo OWNER forgejo"
as_postgres psql -qd forgejo -c "CREATE TABLE kept (word text)" -c "INSERT INTO kept VALUES ('from fifteen')"
# A setting a role wrote, in PostgreSQL's and in nginx's files.
echo "password_encryption = 'scram-sha-256' # nullbill" >>/etc/postgresql/15/main/postgresql.conf
echo "# nullbill wrote this" >>/etc/nginx/nginx.conf
# A repository of a vendor's that names the release, as the roles write them.
echo "# deb [signed-by=/usr/share/keyrings/example.gpg] https://packages.example.internal/debian bookworm main" \
  >/etc/apt/sources.list.d/vendor.list

bash "$upgrade" bookworm trixie || fail "the upgrade ran to its end"
[ "$(tail -n 1 /var/lib/nullbill/release-upgrade.result)" = "done trixie" ] || fail "the result's last line says done"
grep -q '^VERSION_CODENAME=trixie$' /etc/os-release || fail "Debian 13 after the upgrade"
grep -q 'debian trixie main' /etc/apt/sources.list.d/vendor.list || fail "a vendor's repository follows the release"
[ -f /var/lib/nullbill/vendor.list.bookworm ] || fail "the sources as they were are kept"
grep -q "nullbill wrote this" /etc/nginx/nginx.conf || fail "a changed settings file is kept"
[ -z "$(dpkg --audit)" ] || fail "no package left half installed"
pass "Debian 12 becomes Debian 13 in place, settings files kept, vendors' repositories moved"

pg_lsclusters -h | grep -q '^17  *main  *5432  *online' || { pg_lsclusters; fail "PostgreSQL 17 answers on 5432"; }
pg_lsclusters -h | grep -q '^15  *main  *5433  *down' || { pg_lsclusters; fail "the PostgreSQL 15 cluster is kept, stopped"; }
[ "$(as_postgres psql -Atd forgejo -c 'SELECT word FROM kept')" = "from fifteen" ] || fail "the data is in PostgreSQL 17"
[ "$(as_postgres psql -Atc 'SHOW server_version_num' | cut -c1-2)" = 17 ] || fail "the server that answers is 17"
grep -q "^password_encryption = 'scram-sha-256' # nullbill" /etc/postgresql/17/main/postgresql.conf || fail "PostgreSQL's settings moved along"
PGPASSWORD=not-a-secret psql -h 127.0.0.1 -U forgejo -Atd forgejo -c 'SELECT 1' >/dev/null || fail "the role signs in with its password"
pass "PostgreSQL 15's cluster is moved to 17 with its data, roles and settings; the old one stays, stopped"

bash "$upgrade" bookworm trixie || fail "a second run"
[ "$(tail -n 1 /var/lib/nullbill/release-upgrade.result)" = "done trixie" ] || fail "a second run says done"
pg_lsclusters -h | grep -q '^17  *main  *5432  *online' || fail "a second run leaves PostgreSQL 17 alone"
[ "$(as_postgres psql -Atd forgejo -c 'SELECT word FROM kept')" = "from fifteen" ] || fail "the data after a second run"
pass "a second run changes nothing"

# A guest that is on neither release is not touched.
sed -i 's/^VERSION_CODENAME=.*/VERSION_CODENAME=forky/' /etc/os-release 2>/dev/null || {
  cp --remove-destination "$(readlink -f /etc/os-release)" /etc/os-release
  sed -i 's/^VERSION_CODENAME=.*/VERSION_CODENAME=forky/' /etc/os-release
}
if bash "$upgrade" bookworm trixie; then fail "another release is refused"; fi
grep -q '^failed: ' /var/lib/nullbill/release-upgrade.result || fail "a failure is in the result file"
pass "a guest on another release is refused, with the reason in the result file"
