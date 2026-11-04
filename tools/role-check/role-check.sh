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
