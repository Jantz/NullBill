#!/usr/bin/env bash
# Container check for the files role, run by tools/check-roles: converge
# twice with a read-only and a read-write share, run smbd as its unit
# would, and prove with smbclient that only the share user gets in, only
# over SMB3 with encryption, that a read-only share takes no writes, and
# that logons reach the journal for FailedLogins.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

pw=correct-horse-battery-staple
vars=/tmp/files-vars.json
shares() {
  printf '{"files_password": "%s", "files_shares": [{"name": "old-hdd", "read_only": true}, {"name": "media", "read_only": false}]}' "$1" >"$vars"
}
shares "$pw"
# The bind mount points apply sets, owned as the kept disks' files are.
mkdir -p /srv/share/old-hdd /srv/share/media
echo hello >/srv/share/old-hdd/hello.txt
chown -R 1000:1000 /srv/share/old-hdd /srv/share/media
chmod 0700 /srv/share/old-hdd /srv/share/media

start_journald
converge files.yml -e files_manage_services=false -e "@$vars"
# -v lists defaults too, such as map to guest = Never.
testparm -sv 2>/dev/null >/tmp/testparm.txt
for want in "server min protocol = SMB3" "server smb encrypt = required" "map to guest = Never" "restrict anonymous = 2"; do
  grep -qx $'\t'"$want" /tmp/testparm.txt || fail "smb.conf: $want"
done
! grep -q "guest ok = Yes" /tmp/testparm.txt || fail "a share allows guests"
[ "$(id -u nbshare)" = 1000 ] || fail "share user uid 1000"
[ "$(getent passwd nbshare | cut -d: -f7)" = /usr/sbin/nologin ] || fail "share user without a login"
pass "smb.conf: SMB3 only, encryption required, no guests; the share user has no login"

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends smbclient >/dev/null
/usr/sbin/smbd --foreground --no-process-group >/tmp/smbd.log 2>&1 &
for _ in $(seq 30); do (exec 3<>/dev/tcp/127.0.0.1/445) 2>/dev/null && break; sleep 1; done
smb() { # user%password share command [smbclient options]
  smbclient "//127.0.0.1/$2" -U "$1" --client-protection=encrypt -m SMB3 -c "$3" "${@:4}" >/tmp/smb.out 2>&1
}

cd /tmp
smb "nbshare%$pw" old-hdd "get hello.txt /tmp/got.txt" || { cat /tmp/smb.out; fail "the share user reads a share"; }
grep -qx hello /tmp/got.txt || fail "read the file back"
pass "the share user reads a kept disk over SMB3 with encryption"

echo new >/tmp/new.txt
if smb "nbshare%$pw" old-hdd "put /tmp/new.txt new.txt"; then fail "wrote to a read-only share"; fi
test ! -e /srv/share/old-hdd/new.txt || fail "a file reached the read-only share"
smb "nbshare%$pw" media "put /tmp/new.txt new.txt" || { cat /tmp/smb.out; fail "write to a read-write share"; }
[ "$(stat -c %u /srv/share/media/new.txt)" = 1000 ] || fail "written as the share user"
pass "a read-only share takes no writes; a read-write one writes as the share user"

if smb "nbshare%wrong-password" old-hdd ls; then fail "a wrong password got in"; fi
if smbclient //127.0.0.1/old-hdd -N -m SMB3 -c ls >/tmp/smb.out 2>&1; then fail "anonymous got in"; fi
if smb "guest%" old-hdd ls; then fail "a guest got in"; fi
if smb "root%$pw" old-hdd ls; then fail "another user got in with the password"; fi
pass "a wrong password, anonymous, guest and other users are refused"

if smb "nbshare%$pw" old-hdd ls --option='client max protocol=SMB2_10'; then fail "SMB 2.1 was accepted"; fi
# A client that asks for no encryption still gets an encrypted session:
# the server requires it. smbstatus shows the live session's cipher.
(echo ls; sleep 4; echo quit) | smbclient //127.0.0.1/old-hdd -U "nbshare%$pw" -m SMB3 --client-protection=off >/dev/null 2>&1 &
client=$!
sleep 2
smbstatus 2>/dev/null >/tmp/smbstatus.txt
wait "$client"
grep -qE '^old-hdd .*AES-(128|256)-(GCM|CCM)' /tmp/smbstatus.txt || { cat /tmp/smbstatus.txt; fail "the session was not encrypted"; }
pass "SMB before 3 is refused, and a client asking for no encryption is encrypted anyway"

# Into a file first: grep -q stops at the first match, and under pipefail
# journalctl's SIGPIPE would fail the pipeline.
smbd_journal() { journalctl -q --no-pager -t smbd >/tmp/smbd-journal.txt; }
for _ in $(seq 20); do smbd_journal && grep -q 'NT_STATUS_WRONG_PASSWORD' /tmp/smbd-journal.txt && break; sleep 0.5; done
smbd_journal
grep -q 'NT_STATUS_WRONG_PASSWORD' /tmp/smbd-journal.txt || { grep -E 'Auth:' /tmp/smbd-journal.txt; fail "failed logon in the journal"; }
# The same pattern Loki's FailedLogins rule counts.
pattern=$(sed -n 's/.*|~ "\([^"]*\)".*/\1/p' /ansible/roles/monitoring/files/loki-rules/nullbill.yml)
[ "$(grep -cE "$pattern" /tmp/smbd-journal.txt)" -ge 1 ] || fail "FailedLogins would not count Samba's failed logon"
[ "$(grep "nbshare" /tmp/smbd-journal.txt | grep -cvE "$pattern")" -ge 1 ] || fail "a good logon is counted as failed"
pass "logons reach the journal under smbd, and FailedLogins counts the failed ones only"

# A new password from apply replaces the old one; the old one stops working.
shares "another-long-password"
ansible-playbook -i localhost, -c local -e files_manage_services=false -e "@$vars" /ansible/files.yml >/tmp/pw.log 2>&1 || { cat /tmp/pw.log; fail "password change"; }
if smb "nbshare%$pw" old-hdd ls; then fail "the old password still works"; fi
smb "nbshare%another-long-password" old-hdd ls || fail "the new password works"
grep -q "another-long-password\|$pw" /tmp/first.log /tmp/second.log /tmp/pw.log && fail "a password reached the log"
pass "a changed password replaces the old one and never reaches the log"
stop_journald
