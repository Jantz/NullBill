#!/usr/bin/env bash
# Container check for the monitoring role, run by tools/check-roles: apply
# it twice, unit-test the alert rules, then start each service and query it.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

converge monitoring.yml -e monitoring_manage_services=false

(cd /ansible/roles/monitoring/files && promtool test rules nullbill.rules.test.yml) >/tmp/rules.log 2>&1 \
  || { cat /tmp/rules.log; fail "alert rule unit tests"; }
pass "every alert fires on the series it is meant for"

# No init in the container: start each daemon the way its unit does.
# shellcheck disable=SC1091 # written by the role
. /etc/default/prometheus
# shellcheck disable=SC2086 # ARGS is a flag list, as in the unit
setpriv --reuid=prometheus --regid=prometheus --init-groups prometheus \
  --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/tmp/prom $ARGS >/tmp/prometheus.log 2>&1 &
setpriv --reuid=loki --regid=nogroup --init-groups loki -config.file=/etc/loki/config.yml >/tmp/loki.log 2>&1 &
setpriv --reuid=grafana --regid=grafana --init-groups grafana server --homepath=/usr/share/grafana \
  --config=/etc/grafana/grafana.ini cfg:default.paths.provisioning=/etc/grafana/provisioning \
  cfg:default.paths.data=/tmp/grafana >/tmp/grafana.log 2>&1 &

wait_for() {
  for _ in $(seq 60); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
wait_for http://127.0.0.1:9090/-/ready || { cat /tmp/prometheus.log; fail "prometheus ready"; }
curl -fsS http://127.0.0.1:9090/api/v1/rules | grep -q CertificateExpiresSoon || fail "prometheus loaded the rules"
# The operator queries it from another guest; the guest firewall limits who.
curl -fsS "http://$(hostname -i | cut -d' ' -f1):9090/-/ready" >/dev/null || fail "prometheus answers on the guest address"
pass "prometheus is up on the guest address with the rules loaded"

wait_for http://127.0.0.1:3100/ready || { tail -20 /tmp/loki.log; fail "loki ready"; }
now=$(date +%s%N)
curl -fsS -H 'Content-Type: application/json' http://127.0.0.1:3100/loki/api/v1/push --data \
  "{\"streams\":[{\"stream\":{\"job\":\"auth\",\"host\":\"forgejo\"},\"values\":[[\"$now\",\"sshd: Failed password for root\"]]}]}" \
  || fail "push a log line"
found=0
for _ in $(seq 20); do
  curl -fsS -G http://127.0.0.1:3100/loki/api/v1/query_range --data-urlencode 'query={job="auth"} |~ "Failed password"' \
    | grep -q "Failed password for root" && { found=1; break; }
  sleep 1
done
[ "$found" = 1 ] || fail "query the pushed line back"
curl -fsS http://127.0.0.1:3100/prometheus/api/v1/rules | grep -q FailedLogins || fail "loki ruler loaded FailedLogins"
pass "loki stores and returns logs, and the failed-login rule is loaded"

wait_for http://127.0.0.1:3000/api/health || { tail -20 /tmp/grafana.log; fail "grafana health"; }
pw=$(cat /etc/grafana/admin-password)
curl -fsS -u "admin:$pw" http://127.0.0.1:3000/api/datasources | grep -q '"type":"loki"' || fail "grafana data sources"
stock="admin"  # Grafana's out-of-the-box admin password
code=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$stock" http://127.0.0.1:3000/api/datasources)
[ "$code" = 401 ] || fail "default admin password still works ($code)"
pass "grafana provisions both data sources and refuses the default password"
