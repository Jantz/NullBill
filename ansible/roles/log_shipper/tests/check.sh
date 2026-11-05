#!/usr/bin/env bash
# Container check for the log_shipper role, run by tools/check-roles. Loki
# and its gate (the monitoring role), an agent-managed guest (agent_ssh)
# and the shipper share this container; lines must reach Loki once each,
# with the uid the journal recorded, across a shipper restart and a gate
# outage.
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
    "log_shipper_ca": read("ca.crt"), "log_shipper_cert": read("shipper.crt"), "log_shipper_key": read("shipper.key"),
    "log_shipper_host": "workspace",
    # As apply sets it on the PVE host, whose resolver knows no NullBill names.
    "log_shipper_resolve": {"monitoring.nullbill.internal": "127.0.0.1"},
}, open(sys.argv[1], "w"))
PY
ansible-playbook -i localhost, -c local -e monitoring_manage_services=false -e "@$vars" /ansible/monitoring.yml >/tmp/mon.log 2>&1 ||
  { cat /tmp/mon.log; fail "monitoring role"; }
setpriv --reuid=loki --regid=nogroup --init-groups loki -config.file=/etc/loki/config.yml >/tmp/loki.log 2>&1 &
nginx
for _ in $(seq 60); do curl -fsS http://127.0.0.1:3101/ready >/dev/null 2>&1 && break; sleep 1; done

work=$(mktemp -d)
ssh-keygen -q -t ed25519 -N '' -f "$work/ca"
ssh-keygen -q -t ed25519 -N '' -f "$work/key"
ssh-keygen -q -s "$work/ca" -I test -n agent -V +30m "$work/key.pub"
ansible-playbook -i localhost, -c local -e agent_ssh_manage_services=false \
  -e "agent_ssh_user_ca_key='$(cat "$work/ca.pub")'" /ansible/agent-ssh.yml >/tmp/guest.log 2>&1 ||
  { cat /tmp/guest.log; fail "agent_ssh role"; }
start_journald
/usr/sbin/sshd # logs to the journal, as on a guest

converge log-shipper.yml -e log_shipper_manage_services=false -e "@$vars"
[ "$(grep -c ' monitoring.nullbill.internal ' /etc/hosts)" = 1 ] || fail "the gate's name in /etc/hosts once"
pass "the gate's name is added for a machine whose resolver does not know it"

start_shipper() {
  setpriv --reuid=nb-shipper --regid=nb-shipper --init-groups \
    /usr/local/sbin/nb-log-shipper /etc/nullbill/shipper/config.json >>/tmp/shipper.log 2>&1 &
  echo $! >/run/shipper.pid
}
start_shipper

as_agent() {
  ssh -n -i "$work/key" -o CertificateFile="$work/key-cert.pub" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR agent@127.0.0.1 "$@"
}
# count QUERY: matching lines in Loki over the last hour.
count() {
  curl -fsS --cacert /tmp/pki/ca.crt -G https://monitoring.nullbill.internal:3100/loki/api/v1/query_range \
    --data-urlencode "query=$1" --data-urlencode "start=$(($(date +%s) - 3600))000000000" |
    python3 -c 'import json, sys; print(sum(len(s["values"]) for s in json.load(sys.stdin)["data"]["result"]))'
}
# wait_count QUERY N: wait until Loki holds exactly N such lines.
wait_count() {
  for _ in $(seq 30); do [ "$(count "$1")" = "$2" ] && return 0; sleep 1; done
  echo "$1: $(count "$1") lines, expected $2"
  tail -5 /tmp/shipper.log
  return 1
}

as_agent echo marker-one >/dev/null
uid=$(id -u agent)
wait_count "{job=\"commands\", host=\"workspace\", uid=\"$uid\"} |= \"ssh: echo marker-one\"" 1 || fail "agent command in Loki"
ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null nosuchuser@127.0.0.1 true 2>/dev/null || true
wait_count '{job="auth", host="workspace"} |= "Invalid user nosuchuser"' 1 || fail "sshd auth line in Loki"
pass "agent commands reach Loki under the agent's uid, and sshd's auth lines feed FailedLogins"

su agent -s /bin/bash -c 'logger -t nb-cmd "principal=owner forged"'
wait_count "{job=\"commands\", uid=\"$uid\"} |= \"principal=owner forged\"" 1 || fail "forged line"
pass "a line the agent writes as someone else still carries the agent's uid"

kill "$(cat /run/shipper.pid)"
logger -t nb-cmd marker-two
start_shipper
wait_count '{job="commands"} |= "marker-two"' 1 || fail "line logged while the shipper was down"
sleep 2
wait_count '{job="commands"} |= "marker-one"' 1 || fail "a restart sent a line twice"
pass "a restarted shipper sends what it missed and nothing twice"

nginx -s stop 2>/dev/null
logger -t nb-cmd marker-three
sleep 3
nginx
wait_count '{job="commands"} |= "marker-three"' 1 || fail "line logged while the gate was down"
pass "lines logged while the gate is down arrive once it is back"

if su agent -s /bin/bash -c 'cat /etc/nullbill/shipper/shipper.key' >/dev/null 2>&1; then fail "the agent reads the shipper key"; fi
id -nG agent | grep -qw systemd-journal && fail "the agent reads the whole journal"
pass "the agent cannot read the shipper key or push as the shipper"
