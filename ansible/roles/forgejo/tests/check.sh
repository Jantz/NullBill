#!/usr/bin/env bash
# Container check for the forgejo role, run by tools/check-roles: PostgreSQL
# and Forgejo in one container, each applied twice. Then a repository is
# pushed and cloned over HTTPS and over SSH, and nobody can sign up.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

# What tls_cert leaves behind on both guests, from one throwaway CA.
test_pki /tmp/pki forgejo.nullbill.internal postgres.nullbill.internal
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-common openssh-client >/dev/null 2>&1
getent group postgres >/dev/null || groupadd -r postgres
install -d -m 0755 /etc/nullbill/tls
install -m 0644 /tmp/pki/ca.crt /etc/nullbill/tls/root_ca.crt
install -m 0644 /tmp/pki/loki.crt /etc/nullbill/tls/forgejo.nullbill.internal.crt
install -m 0640 -g www-data /tmp/pki/loki.key /etc/nullbill/tls/forgejo.nullbill.internal.key
install -m 0644 /tmp/pki/postgres.nullbill.internal.crt /etc/nullbill/tls/
install -m 0640 -g postgres /tmp/pki/postgres.nullbill.internal.key /etc/nullbill/tls/
echo "127.0.0.1 forgejo.nullbill.internal postgres.nullbill.internal" >>/etc/hosts

# A password with the characters an ini file or a URL would trip on.
password='p@ss;w#rd "it'"'"'s" /?&=%'
python3 - "$password" >/tmp/vars.json <<'PY'
import json, sys
print(json.dumps({
    "postgres_manage_services": False,
    "postgres_databases": [{"name": "forgejo", "client": "127.0.0.1", "password": sys.argv[1]}],
    "forgejo_manage_services": False,
    "forgejo_db_password": sys.argv[1],
}))
PY
converge postgres.yml -e @/tmp/vars.json
converge forgejo.yml -e @/tmp/vars.json

systemd-analyze verify /etc/systemd/system/forgejo.service >/tmp/unit.log 2>&1 || { cat /tmp/unit.log; fail "forgejo unit"; }
pass "the forgejo unit passes systemd-analyze"
test "$(stat -c '%U %a' /etc/forgejo/admin-password)" = "root 600" || fail "admin password is root's alone"
test -s /etc/forgejo/admin-password || fail "admin password written"
nb-forgejo admin user list --admin | grep -q nullbill-admin || fail "administrator exists"

start_forgejo /tmp/pki/ca.crt
url=https://forgejo.nullbill.internal
tls=(--cacert /tmp/pki/ca.crt)
pass "forgejo answers over HTTPS with the step-ca certificate"
basic=$(printf 'nullbill-admin:%s' "$(cat /etc/forgejo/admin-password)" | base64 -w0)
curl -fsS "${tls[@]}" -H "Authorization: Basic $basic" "$url/api/v1/user" | grep -q '"is_admin":true' \
  || fail "the stored admin password logs in"
pass "the first administrator logs in with the password only root can read"

ssl=$(runuser -u postgres -- psql -tAc "SELECT bool_and(s.ssl) FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid) WHERE a.usename = 'forgejo'")
test "$ssl" = t || fail "forgejo's database connections use TLS (got '$ssl')"
# The same settings against an address the certificate does not name.
sed 's/^HOST = .*/HOST = 127.0.0.1:5432/' /etc/forgejo/app.ini >/tmp/elsewhere.ini
chown git /tmp/elsewhere.ini
out=$(cd /var/lib/forgejo && runuser -u git -- env HOME=/var/lib/forgejo forgejo --config /tmp/elsewhere.ini \
  --work-path /var/lib/forgejo admin user list 2>&1) && fail "forgejo accepted a certificate for another name"
grep -q 'failed to verify certificate' <<<"$out" || fail "unexpected database error: $out"
pass "forgejo reaches PostgreSQL over TLS and refuses a certificate for another name"

curl -sS -o /dev/null --max-time 5 "http://$(hostname -i | cut -d' ' -f1):3000/" 2>/dev/null \
  && fail "forgejo's plain HTTP port answers on the guest address"
pass "plain HTTP stays on localhost"

code=$(curl -sS -o /dev/null -w '%{http_code}' "${tls[@]}" "$url/api/v1/repos/search")
test "$code" = 401 -o "$code" = 403 || fail "anonymous API read refused (got $code)"
curl -fsS "${tls[@]}" "$url/user/sign_up" | grep -q 'Registration is disabled' || fail "sign-up page refuses"
pass "anonymous visitors see nothing and cannot sign up"

token=$(nb-forgejo admin user generate-access-token --username nullbill-admin --token-name check \
  --scopes write:repository,write:user --raw)
api() { curl -fsS "${tls[@]}" -H "Authorization: token $token" -H 'Content-Type: application/json' "$@"; }
api "$url/api/v1/user/repos" -d '{"name":"probe","private":true}' >/dev/null || fail "create a repository"
ssh-keygen -q -t ed25519 -N '' -f /tmp/id_check
api "$url/api/v1/user/keys" -d "{\"title\":\"check\",\"key\":\"$(cat /tmp/id_check.pub)\"}" >/dev/null || fail "add an SSH key"

export GIT_SSL_CAINFO=/tmp/pki/ca.crt GIT_AUTHOR_NAME=check GIT_AUTHOR_EMAIL=check@example.internal
export GIT_COMMITTER_NAME=check GIT_COMMITTER_EMAIL=check@example.internal
export GIT_SSH_COMMAND="ssh -i /tmp/id_check -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/known_hosts"
git init -q -b main /tmp/work
git -C /tmp/work commit -q --allow-empty -m "over https"
git -C /tmp/work push -q "https://nullbill-admin:$token@forgejo.nullbill.internal/nullbill-admin/probe.git" main >/tmp/push.log 2>&1 \
  || { cat /tmp/push.log; fail "push over HTTPS"; }
pass "a push over HTTPS lands"
git -C /tmp/work commit -q --allow-empty -m "over ssh"
git -C /tmp/work push -q git@forgejo.nullbill.internal:nullbill-admin/probe.git main >/tmp/push.log 2>&1 \
  || { cat /tmp/push.log; tail -20 /tmp/forgejo.log; fail "push over SSH"; }
git clone -q git@forgejo.nullbill.internal:nullbill-admin/probe.git /tmp/clone >/tmp/clone.log 2>&1 \
  || { cat /tmp/clone.log; fail "clone over SSH"; }
test "$(git -C /tmp/clone log --format=%s)" = "$(printf 'over ssh\nover https')" || fail "clone has both commits"
pass "git over SSH on port 22 pushes and clones"

GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/tmp/known_hosts" \
  git ls-remote git@forgejo.nullbill.internal:nullbill-admin/probe.git >/dev/null 2>&1 \
  && fail "SSH without a registered key reached a private repository"
pass "SSH without a registered key is refused"
