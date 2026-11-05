#!/usr/bin/env bash
# Container check for the runner dispatcher and the runner image, run by
# tools/check-roles. PostgreSQL, Forgejo, the dispatcher and the runner
# image share one container; a stand-in PVE API (fake_pve.py) "clones" by
# running the image's boot-time job pickup. A push queues a real Actions
# job, and the check follows it to a destroyed clone.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

test_pki /tmp/pki monitoring.nullbill.internal forgejo.nullbill.internal postgres.nullbill.internal \
  runner-dispatcher.nullbill.internal pve.nullbill.internal
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql-common >/dev/null 2>&1
# What tls_cert leaves behind (it also makes each service's group).
install -d -m 0755 /etc/nullbill/tls
install -m 0644 /tmp/pki/ca.crt /etc/nullbill/tls/root_ca.crt
for pair in forgejo:www-data postgres:postgres runner-dispatcher:nb-dispatcher; do
  name=${pair%%:*}.nullbill.internal group=${pair#*:}
  getent group "$group" >/dev/null || groupadd -r "$group"
  install -m 0644 "/tmp/pki/$name.crt" /etc/nullbill/tls/
  install -m 0640 -g "$group" "/tmp/pki/$name.key" /etc/nullbill/tls/
done
echo "127.0.0.1 forgejo.nullbill.internal postgres.nullbill.internal runner-dispatcher.nullbill.internal pve.nullbill.internal" >>/etc/hosts

python3 - >/tmp/forge.json <<'PY'
import json
print(json.dumps({
    "postgres_manage_services": False,
    "postgres_databases": [{"name": "forgejo", "client": "127.0.0.1", "password": "check-only"}],
    "forgejo_manage_services": False,
    "forgejo_db_password": "check-only",
}))
PY
ansible-playbook -i localhost, -c local -e @/tmp/forge.json /ansible/postgres.yml >/tmp/pg.log 2>&1 || { cat /tmp/pg.log; fail "postgres role"; }
ansible-playbook -i localhost, -c local -e @/tmp/forge.json /ansible/forgejo.yml >/tmp/fj.log 2>&1 || { cat /tmp/fj.log; fail "forgejo role"; }
start_forgejo /tmp/pki/ca.crt
token=$(nb-forgejo admin user generate-access-token --username nullbill-admin --token-name dispatcher --scopes write:admin --raw)
user_token=$(nb-forgejo admin user generate-access-token --username nullbill-admin --token-name check --scopes write:repository,write:user --raw)

state=/run/fake-pve
mkdir -p "$state"
pve_token="nb-dispatcher@pve!dispatcher=$(openssl rand -hex 16)"
python3 - "$token" "$pve_token" >/tmp/vars.json <<'PY'
import json, sys
print(json.dumps({
    "ci_runner_ca_root": open("/tmp/pki/ca.crt").read(),
    "ci_runner_serial_file": "/run/fake-pve/serial",
    "ci_runner_poweroff": "true",
    "ci_runner_manage_services": False,
    "runner_dispatcher_forgejo_token": sys.argv[1],
    "runner_dispatcher_pve_token": sys.argv[2],
    "runner_dispatcher_pve_url": "https://pve.nullbill.internal:8006",
    "runner_dispatcher_pve_ca": open("/tmp/pki/ca.crt").read(),
    "runner_dispatcher_node": "pve",
    "runner_dispatcher_pools": [{"name": "linux-runner", "template": 2030, "vmids": [6000, 6001], "labels": ["linux"],
                                 "runner_labels": ["linux:host"], "max_job_seconds": 600}],
    "runner_dispatcher_interval_seconds": 2,
    "runner_dispatcher_manage_services": False,
}))
PY
converge ci-runner.yml -e @/tmp/vars.json
converge runner-dispatcher.yml -e @/tmp/vars.json
systemd-analyze verify /etc/systemd/system/nb-dispatcher.service /etc/systemd/system/nb-runner-job.service >/tmp/unit.log 2>&1 \
  || { cat /tmp/unit.log; fail "units"; }
pass "the dispatcher and job pickup units pass systemd-analyze"

# The golden image boots without a serial while apply bakes it.
out=$(/usr/local/sbin/nb-runner-job) || fail "job pickup without a serial"
grep -q 'not a dispatched clone' <<<"$out" || fail "job pickup without a serial: $out"
pass "the golden image does nothing at boot without a job"

python3 /ansible/roles/runner_dispatcher/tests/fake_pve.py /tmp/pki/pve.nullbill.internal.crt /tmp/pki/pve.nullbill.internal.key \
  "$pve_token" "$state" >/tmp/fake-pve.log 2>&1 &
setpriv --reuid=nb-dispatcher --regid=nb-dispatcher --init-groups \
  /usr/local/sbin/nb-dispatcher /etc/nullbill/dispatcher/config.json >/tmp/dispatcher.log 2>&1 &

url=https://forgejo.nullbill.internal
api() { curl -fsS --cacert /tmp/pki/ca.crt -H "Authorization: token $user_token" -H 'Content-Type: application/json' "$@"; }
api "$url/api/v1/user/repos" -d '{"name":"ci","private":true}' >/dev/null || fail "create a repository"
api -X PATCH "$url/api/v1/repos/nullbill-admin/ci" -d '{"has_actions":true}' >/dev/null || fail "enable actions"
git init -q -b main /tmp/ci
mkdir -p /tmp/ci/.forgejo/workflows
cat >/tmp/ci/.forgejo/workflows/ci.yml <<'YAML'
on: [push]
jobs:
  probe:
    runs-on: linux
    steps:
      - run: test "$(id -un)" = runner && echo "ran on the clone as runner"
YAML
git -C /tmp/ci add -A
git -C /tmp/ci -c user.name=check -c user.email=check@example.internal commit -q -m ci
GIT_SSL_CAINFO=/tmp/pki/ca.crt git -C /tmp/ci push -q "https://nullbill-admin:$user_token@forgejo.nullbill.internal/nullbill-admin/ci.git" main \
  >/tmp/push.log 2>&1 || { cat /tmp/push.log; fail "push the workflow"; }

status=""
for _ in $(seq 120); do
  status=$(api "$url/api/v1/repos/nullbill-admin/ci/actions/tasks" | python3 -c 'import json,sys; r=json.load(sys.stdin)["workflow_runs"]; print(r[0]["status"] if r else "")')
  case $status in success | failure | cancelled) break ;; esac
  sleep 2
done
diag() { tail -20 /tmp/dispatcher.log; cat "$state"/vm-*.log 2>/dev/null | tail -30; cat "$state/calls.log"; }
test "$status" = success || { diag; fail "the job ran (status '$status')"; }
pass "a pushed workflow ran on a clone, as the unprivileged runner user"

for _ in $(seq 30); do grep -q '^DELETE /api2/json/nodes/pve/qemu/6000$' "$state/calls.log" && break; sleep 1; done
grep -q '^DELETE /api2/json/nodes/pve/qemu/6000$' "$state/calls.log" || { diag; fail "clone destroyed"; }
grep -q '^POST /api2/json/nodes/pve/qemu/2030/clone {"full": "0", "name": "nb-linux-runner-6000", "newid": "6000", "pool": "nb-ci"}$' "$state/calls.log" \
  || { diag; fail "linked clone into the CI pool"; }
grep -Eq '^PUT /api2/json/nodes/pve/qemu/6000/config \{"smbios1": "uuid=u6000,serial=[0-9a-f]{32}"\}$' "$state/calls.log" \
  || { diag; fail "serial set, uuid kept"; }
grep -E 'qemu/(2017|2030)' "$state/calls.log" | grep -v '2030/clone' && fail "touched a guest other than its clones"
pass "the clone was linked from the template into the CI pool, handed its job by serial, and destroyed"

runners=$(curl -fsS --cacert /tmp/pki/ca.crt -H "Authorization: token $token" "$url/api/v1/admin/actions/runners")
grep -q '"name":"nb-' <<<"$runners" && fail "a runner registration was left behind: $runners"
pass "no runner registration outlives its clone"

serial=$(cat "$state/serial")
code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert /tmp/pki/ca.crt "https://runner-dispatcher.nullbill.internal:8443/handoff/$serial")
test "$code" = 404 || fail "a used nonce was served again ($code)"
grep -q "$serial" /tmp/dispatcher.log && fail "the dispatcher logged a nonce"
pass "a nonce is served once and never logged"

# apply seals the golden image last, so each clone boots as a new machine.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloud-init >/dev/null 2>&1
ssh-keygen -A
mkdir -p /var/lib/cloud/instance
echo 0123456789abcdef0123456789abcdef >/etc/machine-id
ansible-playbook -i localhost, -c local /ansible/ci-runner-seal.yml >/tmp/seal.log 2>&1 || { cat /tmp/seal.log; fail "seal"; }
{ test -e /etc/machine-id && test ! -s /etc/machine-id; } || fail "machine-id kept"
compgen -G '/etc/ssh/ssh_host_*' >/dev/null && fail "SSH host keys kept"
test ! -e /var/lib/cloud/instance || fail "cloud-init instance kept"
pass "a sealed image keeps no machine-id, SSH host keys or cloud-init instance for clones to share"
