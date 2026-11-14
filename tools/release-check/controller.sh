#!/usr/bin/env bash
# In a Debian 12 container: set NullBill up as install.sh does in its own
# container, upgrade that to Debian 13 with tools/upgrade-controller, and
# see that NullBill and what apply needs still run.
set -euo pipefail
fail() { echo "FAIL $*"; tail -n 40 /var/log/nb-release-upgrade.log 2>/dev/null || true; exit 1; }
pass() { echo "ok   $*"; }

tar -xf /nullbill.tar -C /opt
cd /opt/nullbill
tools/setup-controller >/tmp/setup.log 2>&1 || { tail -n 30 /tmp/setup.log; fail "setup-controller on Debian 12"; }
before=$(venv/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
venv/bin/nullbill --help >/dev/null || fail "nullbill runs on Debian 12"

tools/upgrade-controller bookworm trixie >/tmp/upgrade.log 2>&1 || { tail -n 30 /tmp/upgrade.log; fail "upgrade-controller"; }
grep -q '^VERSION_CODENAME=trixie$' /etc/os-release || fail "Debian 13 after the upgrade"
after=$(venv/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
[ "$before" != "$after" ] || fail "the environment runs the new release's Python (still $after)"
venv/bin/nullbill --help >/dev/null || fail "nullbill runs after the upgrade"
venv/bin/ansible-playbook --version >/dev/null || fail "ansible runs after the upgrade"
tofu version >/dev/null || fail "tofu"
sops --version >/dev/null 2>&1 || fail "sops"
age-keygen --version >/dev/null 2>&1 || fail "age"
pass "the NullBill container goes from Debian 12 (Python $before) to 13 (Python $after) and NullBill, Ansible, OpenTofu and sops run"

# A failed upgrade asks the host to put the container back.
mkdir -p /tmp/bin
printf '#!/bin/sh\necho "$@" >>/tmp/ssh.log\n' >/tmp/bin/ssh
chmod +x /tmp/bin/ssh
# The run that started it is still at work: the snapshot waits for it, so
# a container put back holds the run's result.
sleep 4 &
run=$!
printf '#!/bin/sh\nkill -0 %s 2>/dev/null && echo "while the run was at work: $*" >>/tmp/ssh.log\necho "$@" >>/tmp/ssh.log\n' "$run" >/tmp/bin/ssh
echo '{}' >/tmp/tried.json
if PATH=/tmp/bin:$PATH tools/upgrade-controller forky sid root@pve.example.internal 2002 nb-before-debian-13 "$run" /tmp/tried.json >/tmp/failed.log 2>&1; then
  fail "an upgrade that cannot run fails"
fi
if grep -q "while the run was at work" /tmp/ssh.log; then cat /tmp/ssh.log; fail "the host is asked nothing while the run is at work"; fi
grep -q "pct snapshot 2002 nb-before-debian-13" /tmp/ssh.log || { cat /tmp/ssh.log; fail "the host is asked for the snapshot"; }
grep -q "root@pve.example.internal .*pct rollback 2002 nb-before-debian-13 && pct start 2002" /tmp/ssh.log || { cat /tmp/ssh.log; fail "the host is asked to roll back"; }
[ -e /tmp/tried.json ] || fail "the note of the try stays, so the container put back holds it"
pass "the snapshot waits for the run to end; a failed upgrade has the host put the container back from it"

# No snapshot, no upgrade, and no note of a try that stops the next.
printf '#!/bin/sh\necho "$@" >>/tmp/ssh.log\ncase "$*" in *"pct snapshot"*) exit 255 ;; esac\n' >/tmp/bin/ssh
before=$(md5sum /etc/os-release /var/lib/dpkg/status)
if PATH=/tmp/bin:$PATH tools/upgrade-controller trixie forky root@pve.example.internal 2002 nb-before-debian-14 "" /tmp/tried.json >/tmp/nosnap.log 2>&1; then
  fail "no snapshot, no upgrade"
fi
grep -q "no snapshot could be made" /tmp/nosnap.log || { cat /tmp/nosnap.log; fail "it says why"; }
if [ -e /tmp/tried.json ] || [ "$before" != "$(md5sum /etc/os-release /var/lib/dpkg/status)" ]; then fail "nothing changed and the note is gone"; fi
if grep -q "pct rollback 2002 nb-before-debian-14" /tmp/ssh.log; then fail "nothing to put back"; fi
pass "without a snapshot nothing is upgraded, and the next run tries again"
