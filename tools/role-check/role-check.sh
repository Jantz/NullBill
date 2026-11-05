# shellcheck shell=bash
# Helpers for ansible/roles/*/tests/check.sh. Sourced, not run.

fail() {
  echo "FAIL $1"
  exit 1
}

pass() {
  echo "ok   $1"
}

# Apply a playbook twice; the second run must change nothing.
converge() {
  local playbook=$1
  shift
  ansible-playbook -i localhost, -c local "$@" "/ansible/$playbook" >/tmp/first.log 2>&1 || { cat /tmp/first.log; fail "first run of $playbook"; }
  pass "first run of $playbook"
  ansible-playbook -i localhost, -c local "$@" "/ansible/$playbook" >/tmp/second.log 2>&1 || { cat /tmp/second.log; fail "second run of $playbook"; }
  grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/second.log || { grep -B3 changed: /tmp/second.log; fail "second run of $playbook changed something"; }
  pass "second run of $playbook changes nothing"
}

# No init in the container: run journald by itself, with the syslog socket
# a booted guest has, so logger and the log shipper work as on a guest.
start_journald() {
  # As systemd-tmpfiles sets it up on a booted guest.
  mkdir -p /run/systemd/journal
  install -d -m 2755 -g systemd-journal /var/log/journal /run/log/journal
  /lib/systemd/systemd-journald >/tmp/journald.log 2>&1 &
  echo $! >/run/journald.pid
  for _ in $(seq 20); do [ -S /run/systemd/journal/dev-log ] && break; sleep 0.2; done
  ln -sf /run/systemd/journal/dev-log /dev/log
}

stop_journald() {
  kill "$(cat /run/journald.pid)"
  while kill -0 "$(cat /run/journald.pid)" 2>/dev/null; do sleep 0.2; done
}

# Throwaway PKI for checks of the Loki gate, written to $1:
# ca.crt (stands in for step-ca) with loki.crt/loki.key for $2 (default
# monitoring.nullbill.internal); shipper-ca.crt with shipper.crt/shipper.key;
# rogue.crt/rogue.key from a CA nobody trusts.
test_pki() {
  local d=$1 server=${2:-monitoring.nullbill.internal}
  mkdir -p "$d"
  (
    cd "$d" || exit 1
    for ca in ca shipper-ca rogue-ca; do
      openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
        -subj "/CN=test $ca" -keyout "$ca.key" -out "$ca.crt" 2>/dev/null
    done
    leaf() { # name issuer cn [san]
      openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$3" \
        -keyout "$1.key" -out "$1.csr" 2>/dev/null
      openssl x509 -req -in "$1.csr" -CA "$2.crt" -CAkey "$2.key" -CAcreateserial -days 2 \
        ${4:+-extfile <(printf 'subjectAltName=%s\n' "$4")} -out "$1.crt" 2>/dev/null
    }
    leaf loki ca "$server" "DNS:$server"
    leaf shipper shipper-ca test.shipper
    leaf rogue rogue-ca test.shipper
  )
}
