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

# A guest in a zone with egress: proxy has no route out and resolves no
# outside names. Give user $1 the same here: it reaches the loopback
# address, where a check's stand-in for the proxy listens, and nothing else.
loopback_only() {
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nftables >/dev/null 2>&1
  nft -f - <<EOF
table inet role_check {
  chain output {
    type filter hook output priority 0;
    meta skuid $1 oifname != "lo" reject
  }
}
EOF
  if runuser -u "$1" -- python3 -c 'import socket; socket.create_connection(("deb.debian.org", 443), 5)' 2>/dev/null; then
    fail "user $1 still reaches the internet"
  fi
}

# After a game role (ansible/roles/game_*): start the server the way its
# unit does, with the runtime directory and console FIFO systemd would make
# and nothing in its environment but HOME. Its output goes to /tmp/$1.log.
start_game() {
  local name=$1
  install -d -o game -g game -m 0750 "/run/$name"
  rm -f "/run/$name/console"
  runuser -u game -- mkfifo -m 0600 "/run/$name/console"
  (cd "/var/lib/$name" && runuser -u game -- env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME="/var/lib/$name" "/opt/$name/start" >>"/tmp/$name.log" 2>&1 &)
}

# No systemd in the container: a systemctl for a game's unit ($1) that
# starts the server as start_game does and stops it as the unit's ExecStop
# or SIGTERM would, so nb-game's world commands run the real server. The
# server's output stays in /tmp/$1.log, and journalctl prints its end.
fake_systemctl() {
  local name=$1
  cat >/usr/local/sbin/systemctl <<FAKE
#!/usr/bin/env bash
source /usr/local/lib/role-check.sh
unit=\${!#}
[ "\$unit" = $name ] || { echo "systemctl stand-in: only $name, not \$unit" >&2; exit 1; }
pid=\$(pidof -s java TerrariaServer.bin.x86_64 factorio || true)
case \$1 in
  # exec: nothing of the server holds on to the caller's output.
  start) [ -n "\$pid" ] || { exec >/dev/null 2>&1 </dev/null; start_game $name; } ;;
  stop)
    [ -n "\$pid" ] || exit 0
    if grep -q '"stop"' /etc/nullbill/game.json; then nb-game stop "\$pid"; else kill -TERM "\$pid"; fi
    for _ in \$(seq 120); do [ -e "/proc/\$pid" ] || exit 0; sleep 0.5; done
    kill -KILL "\$pid" ;;
  restart) "\$0" stop $name && "\$0" start $name ;;
  is-active) [ -n "\$pid" ] ;;
  show) echo 0 ;;
  *) exit 1 ;;
esac
FAKE
  cat >/usr/local/sbin/journalctl <<FAKE
#!/bin/sh
tail -n 240 /tmp/$name.log
FAKE
  chmod 0755 /usr/local/sbin/systemctl /usr/local/sbin/journalctl
}

# `nb-game world ...`: its one line of JSON is kept in /tmp/world.json, and
# world_is holds a Python expression about it (w).
world() {
  nb-game world "$@" >/tmp/world.json
}

world_is() {
  python3 -c 'import json, sys; w = json.load(open("/tmp/world.json")); sys.exit(0 if eval(sys.argv[1]) else repr(w))' "$1"
}

# The line $2 in a game's output, within $3 seconds (default 60).
game_says() {
  local name=$1 line=$2
  for _ in $(seq "${3:-60}"); do grep -Eq -- "$line" "/tmp/$name.log" && return 0; sleep 1; done
  tail -30 "/tmp/$name.log"
  return 1
}

# Throwaway PKI for checks, written to $1:
# ca.crt (stands in for step-ca) with loki.crt/loki.key for $2 (default
# monitoring.nullbill.internal) and <name>.crt/<name>.key for each further
# server name; shipper-ca.crt with shipper.crt/shipper.key;
# rogue.crt/rogue.key from a CA nobody trusts.
test_pki() {
  local d=$1 server=${2:-monitoring.nullbill.internal}
  local more=("${@:3}")
  mkdir -p "$d"
  (
    cd "$d" || exit 1
    for ca in ca shipper-ca rogue-ca; do
      # Key usage as real CAs carry it; Python 3.13 (Debian 13) refuses a CA without.
      openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -subj "/CN=test $ca" -keyout "$ca.key" -out "$ca.crt" 2>/dev/null
    done
    leaf() { # name issuer cn [san]
      openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$3" \
        -keyout "$1.key" -out "$1.csr" 2>/dev/null
      openssl x509 -req -in "$1.csr" -CA "$2.crt" -CAkey "$2.key" -CAcreateserial -days 2 \
        -extfile <(printf 'authorityKeyIdentifier=keyid\nbasicConstraints=CA:FALSE\n%s\n' "${4:+subjectAltName=$4}") \
        -out "$1.crt" 2>/dev/null
    }
    leaf loki ca "$server" "DNS:$server"
    for name in "${more[@]}"; do leaf "$name" ca "$name" "DNS:$name"; done
    leaf shipper shipper-ca test.shipper
    leaf rogue rogue-ca test.shipper
  )
}

# What src/nullbill/browserca.py makes, written to $1: browser-ca.crt,
# constrained to nullbill.internal and address $3, and browser.crt/.key for
# DNS:$2 and IP:$3.
browser_pki() {
  local d=$1 name=$2 ip=$3
  mkdir -p "$d"
  (
    cd "$d" || exit 1
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 -subj "/CN=test browser CA" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "nameConstraints=critical,permitted;DNS:nullbill.internal,permitted;IP:$ip/255.255.255.255" \
      -keyout browser-ca.key -out browser-ca.crt 2>/dev/null
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$name" -keyout browser.key -out browser.csr 2>/dev/null
    openssl x509 -req -in browser.csr -CA browser-ca.crt -CAkey browser-ca.key -CAcreateserial -days 2 \
      -extfile <(printf 'basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName=DNS:%s,IP:%s\n' "$name" "$ip") \
      -out browser.crt 2>/dev/null
  )
}

# After the postgres and forgejo roles: start Forgejo the way its unit does,
# and nginx in front, and wait until it answers over HTTPS. $1 is the CA
# that signed its certificate.
start_forgejo() {
  (cd /var/lib/forgejo && setpriv --reuid=git --regid=git --init-groups env HOME=/var/lib/forgejo USER=git \
    /usr/local/bin/forgejo web --config /etc/forgejo/app.ini --work-path /var/lib/forgejo >/tmp/forgejo.log 2>&1 &)
  nginx
  for _ in $(seq 60); do curl -fsS --cacert "$1" https://forgejo.nullbill.internal/api/healthz >/dev/null 2>&1 && return 0; sleep 1; done
  tail -30 /tmp/forgejo.log
  fail "forgejo healthy over HTTPS"
}
