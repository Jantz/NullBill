#!/usr/bin/env bash
# Container check for the monitoring role, run by tools/check-roles: apply
# it twice, unit-test the alert rules, then start each service and query it.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

test_pki /tmp/pki
# What tls_cert leaves behind, from a throwaway CA instead of step-ca.
install -d -m 0755 /etc/nullbill/tls
install -m 0644 /tmp/pki/loki.crt /etc/nullbill/tls/monitoring.nullbill.internal.crt
install -m 0640 /tmp/pki/loki.key /etc/nullbill/tls/monitoring.nullbill.internal.key
vars=$(mktemp)
python3 - "$vars" <<'PY'
import json, sys
read = lambda f: open(f"/tmp/pki/{f}").read()
json.dump({
    "monitoring_shipper_ca": read("shipper-ca.crt"),
    # A stand-in for ntfy.sh and Healthchecks.io, started below.
    "monitoring_ntfy_url": "http://127.0.0.1:8099/nullbill-topic",
    "monitoring_healthchecks_url": "http://127.0.0.1:8099/ping/check",
    "monitoring_watchdog_interval": "1m",
}, open(sys.argv[1], "w"))
PY
converge monitoring.yml -e monitoring_manage_services=false -e "@$vars"

(cd /ansible/roles/monitoring/files && promtool test rules nullbill.rules.test.yml) >/tmp/rules.log 2>&1 \
  || { cat /tmp/rules.log; fail "alert rule unit tests"; }
pass "every alert fires on the series it is meant for"

test "$(stat -c %a /etc/nullbill/alert-relay.json)" = 600 || fail "alert relay configuration is root only"
amtool check-config /etc/prometheus/alertmanager.yml >/dev/null || fail "alertmanager configuration"

# The stand-in records each POST it gets: path, then the ntfy headers, body.
cat >/tmp/receiver.py <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        keep = {k: self.headers[k] for k in ("Title", "Priority", "Tags") if k in self.headers}
        with open("/tmp/received.jsonl", "a") as f:
            f.write(json.dumps({"path": self.path, "headers": keep, "body": body}) + "\n")
        self.send_response(200)
        self.end_headers()

HTTPServer(("127.0.0.1", 8099), H).serve_forever()
PY
python3 /tmp/receiver.py >/tmp/receiver.log 2>&1 &

# No init in the container: start each daemon the way its unit does.
# shellcheck disable=SC1091 # written by the role
. /etc/default/prometheus
# shellcheck disable=SC2086 # ARGS is a flag list, as in the unit
setpriv --reuid=prometheus --regid=prometheus --init-groups prometheus \
  --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/tmp/prom $ARGS >/tmp/prometheus.log 2>&1 &
# shellcheck disable=SC1091 # written by the role
. /etc/default/prometheus-alertmanager
# shellcheck disable=SC2086 # ARGS is a flag list, as in the unit
setpriv --reuid=prometheus --regid=prometheus --init-groups prometheus-alertmanager \
  --config.file=/etc/prometheus/alertmanager.yml --storage.path=/tmp/am $ARGS >/tmp/alertmanager.log 2>&1 &
# As the unit does: a copy of the configuration only the relay can read.
install -d -m 0700 -o nobody /tmp/creds
install -m 0400 -o nobody /etc/nullbill/alert-relay.json /tmp/creds/config
setpriv --reuid=nobody --regid=nogroup --clear-groups /usr/local/bin/nb-alert-relay /tmp/creds/config >/tmp/relay.log 2>&1 &
setpriv --reuid=loki --regid=nogroup --init-groups loki -config.file=/etc/loki/config.yml >/tmp/loki.log 2>&1 &
nginx
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

wait_for http://127.0.0.1:9093/-/ready || { cat /tmp/alertmanager.log; fail "alertmanager ready"; }
received() { # received PATH: wait until the stand-in got a POST to PATH
  for _ in $(seq 90); do grep -qF "\"path\": \"$1\"" /tmp/received.jsonl 2>/dev/null && return 0; sleep 1; done
  return 1
}
received /ping/check || { tail /tmp/alertmanager.log /tmp/relay.log; fail "Watchdog pings Healthchecks.io"; }
pass "Watchdog reaches Healthchecks.io through Alertmanager and the relay"
curl -fsS -H 'Content-Type: application/json' http://127.0.0.1:9093/api/v2/alerts -d \
  '[{"labels": {"alertname": "SecurityUpdatesPending", "instance": "forgejo:9100", "severity": "page"}, "annotations": {"summary": "forgejo:9100 has had security updates waiting"}}]' \
  || fail "post an alert to alertmanager"
received /nullbill-topic || { tail /tmp/alertmanager.log /tmp/relay.log; fail "a paging alert reaches ntfy"; }
python3 - <<'PY' || fail "the ntfy message carries title, priority and summary"
import json
sent = [json.loads(line) for line in open("/tmp/received.jsonl")]
msg = next(m for m in sent if m["path"] == "/nullbill-topic")
assert msg["headers"] == {"Title": "NullBill: SecurityUpdatesPending", "Priority": "high", "Tags": "warning"}, msg
assert msg["body"] == "forgejo:9100 has had security updates waiting", msg
PY
if curl -fsS -m 3 "http://$(hostname -i | cut -d' ' -f1):9093/-/ready" >/dev/null 2>&1; then fail "alertmanager answers on the guest address"; fi
pass "a paging alert reaches the owner's ntfy topic with high priority; alertmanager is local"

wait_for http://127.0.0.1:3101/ready || { tail -20 /tmp/loki.log; fail "loki ready"; }
echo "127.0.0.1 monitoring.nullbill.internal" >>/etc/hosts
gate=https://monitoring.nullbill.internal:3100
tls=(--cacert /tmp/pki/ca.crt)
shipper=(--cert /tmp/pki/shipper.crt --key /tmp/pki/shipper.key)
# push [curl options...]: push one failed-login line; prints the HTTP status.
push() {
  curl -sS -o /dev/null -w '%{http_code}' "${tls[@]}" "$@" -H 'Content-Type: application/json' "$gate/loki/api/v1/push" --data \
    "{\"streams\":[{\"stream\":{\"job\":\"auth\",\"host\":\"forgejo\"},\"values\":[[\"$(date +%s%N)\",\"sshd: Failed password for root\"]]}]}"
}
test "$(push "${shipper[@]}")" = 204 || fail "a shipper certificate pushes"
found=0
for _ in $(seq 20); do
  curl -fsS "${tls[@]}" -G "$gate/loki/api/v1/query_range" --data-urlencode 'query={job="auth"} |~ "Failed password"' \
    | grep -q "Failed password for root" && { found=1; break; }
  sleep 1
done
[ "$found" = 1 ] || fail "query the pushed line back without a certificate"
curl -fsS http://127.0.0.1:3101/prometheus/api/v1/rules | grep -q FailedLogins || fail "loki ruler loaded FailedLogins"
pass "loki stores lines a shipper pushes, anyone may query them, and the failed-login rule is loaded"

test "$(push)" = 403 || fail "push without a certificate"
code=$(push --cert /tmp/pki/rogue.crt --key /tmp/pki/rogue.key || true)
case $code in 2*) fail "push with a certificate from an untrusted CA" ;; esac
for path in "loki/api/v1/delete?query=%7Bjob%3D%22auth%22%7D&start=0" loki/api/v1/rules/x; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' "${tls[@]}" "${shipper[@]}" -X POST "$gate/$path")
  test "$code" = 403 || fail "POST /$path answered $code"
done
code=$(curl -sS -o /dev/null -w '%{http_code}' "${tls[@]}" "$gate/config")
test "$code" = 403 || fail "/config answered $code"
guest=$(hostname -i | cut -d' ' -f1)
if curl -fsS -m 3 "http://$guest:3101/ready" >/dev/null 2>&1; then fail "loki answers on the guest address"; fi
pass "no push without a shipper certificate; no deletes, rule changes or other paths; loki itself is local"

wait_for http://127.0.0.1:3000/api/health || { tail -20 /tmp/grafana.log; fail "grafana health"; }
pw=$(cat /etc/grafana/admin-password)
curl -fsS -u "admin:$pw" http://127.0.0.1:3000/api/datasources | grep -q '"type":"loki"' || fail "grafana data sources"
stock="admin"  # Grafana's out-of-the-box admin password
code=$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$stock" http://127.0.0.1:3000/api/datasources)
[ "$code" = 401 ] || fail "default admin password still works ($code)"
pass "grafana provisions both data sources and refuses the default password"
