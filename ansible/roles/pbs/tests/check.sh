#!/usr/bin/env bash
# Container check for the pbs role, run by tools/check-roles: apply it twice,
# back up with client-side encryption through the PVE token, restore, and
# show what the token and a missing key cannot do. Then drop the datastore
# config and the token file, as a rebuilt VM would, and apply again.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

# The container has no init. Start the two daemons the way their units do.
# The proxy binds [::]:8007, which fails on kernels without IPv6, so hand it
# an IPv4 listener through the socket-passing variable it uses on reload.
cat >/usr/local/sbin/pbs-start <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
curl -sk -o /dev/null https://127.0.0.1:8007/ && exit 0
mkdir -p /run/proxmox-backup && chown backup:backup /run/proxmox-backup
/usr/lib/x86_64-linux-gnu/proxmox-backup/proxmox-backup-api >/var/log/pbs-api.log 2>&1 &
for _ in $(seq 60); do [ -s /etc/proxmox-backup/proxy.pem ] && break; sleep 0.5; done
python3 - >/var/log/pbs-proxy.log 2>&1 <<'PY' &
import os, socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", 8007))
s.listen(128)
s.setblocking(False)
s.set_inheritable(True)
os.environ["PROXMOX_BACKUP_LISTEN_FD"] = str(s.fileno())
proxy = "/usr/lib/x86_64-linux-gnu/proxmox-backup/proxmox-backup-proxy"
os.execv("/usr/bin/setpriv", ["setpriv", "--reuid=backup", "--regid=backup", "--init-groups", proxy])
PY
for _ in $(seq 60); do curl -sk -o /dev/null https://127.0.0.1:8007/ && exit 0; sleep 0.5; done
cat /var/log/pbs-api.log /var/log/pbs-proxy.log
exit 1
EOF
chmod +x /usr/local/sbin/pbs-start
args=(-e pbs_manage_services=false -e pbs_start_command=/usr/local/sbin/pbs-start)

converge pbs.yml "${args[@]}"

test ! -e /etc/apt/sources.list.d/pbs-enterprise.list || fail "enterprise repository left in place"
if ! apt-get update >/tmp/apt.log 2>&1 || grep -Eq '^(Err|W):' /tmp/apt.log; then
  cat /tmp/apt.log
  fail "apt update after install"
fi
pass "apt update is clean with only the no-subscription repository"

test "$(stat -c '%a %U' /root/nullbill/pbs-token.json)" = "600 root" || fail "token file mode"
pass "token secret is 0600 and owned by root"

proxmox-backup-manager prune-job list --output-format json >/tmp/prune.json
if ! grep -q '"keep-daily":7' /tmp/prune.json || ! grep -q '"keep-monthly":6' /tmp/prune.json; then
  cat /tmp/prune.json
  fail "prune job"
fi
proxmox-backup-manager verify-job list --output-format json | grep -q '"outdated-after":30' || fail "verify job"
proxmox-backup-manager datastore show backups --output-format json | grep -q '"gc-schedule":"daily"' || fail "gc schedule"
pass "prune, verify and GC jobs are scheduled"

# The client ships with PVE, not with the server.
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends proxmox-backup-client >/dev/null 2>&1 || fail "install client"

# Act as the PVE host does: the token from the file, the server's
# certificate fingerprint, and an encryption key the server never sees.
token() { python3 -c 'import json; print(json.load(open("/root/nullbill/pbs-token.json"))["value"])'; }
export PBS_REPOSITORY='nb-pve@pbs!pve@127.0.0.1:8007:backups'
PBS_PASSWORD=$(token)
PBS_FINGERPRINT=$(proxmox-backup-manager cert info | sed -n 's/^Fingerprint (sha256): //p')
export PBS_PASSWORD PBS_FINGERPRINT
proxmox-backup-client key create /tmp/enc.key --kdf none >/dev/null 2>&1

marker="nullbill-plaintext-marker-$RANDOM$RANDOM"
mkdir -p /tmp/guest/etc
echo "$marker" >/tmp/guest/etc/hostname
head -c 2000000 /dev/urandom >/tmp/guest/data
proxmox-backup-client backup root.pxar:/tmp/guest --keyfile /tmp/enc.key --backup-type ct --backup-id 2010 >/tmp/backup.log 2>&1 \
  || { cat /tmp/backup.log; fail "backup with the PVE token"; }
pass "the PVE token backs up"

latest() {
  proxmox-backup-client snapshot list --output-format json | python3 -c '
import datetime, json, sys
s = max(json.load(sys.stdin), key=lambda s: s["backup-time"])
t = datetime.datetime.fromtimestamp(s["backup-time"], datetime.timezone.utc)
print("/".join([s["backup-type"], s["backup-id"], f"{t:%Y-%m-%dT%H:%M:%SZ}"]))'
}
snap=$(latest)
proxmox-backup-client snapshot files "$snap" --output-format json >/tmp/files.json
python3 -c '
import json, sys
files = json.load(open("/tmp/files.json"))
data = [f for f in files if f["filename"].endswith(".didx")]
sys.exit(0 if data and all(f["crypt-mode"] == "encrypt" for f in data) else 1)' || { cat /tmp/files.json; fail "crypt mode"; }
! grep -rqF "$marker" /mnt/datastore/backups/.chunks || fail "plaintext found in the chunk store"
pass "every archive is encrypted and the chunk store holds no plaintext"

proxmox-backup-client restore "$snap" root.pxar /tmp/restored --keyfile /tmp/enc.key --pattern etc/hostname >/dev/null 2>&1 || fail "restore"
test "$(cat /tmp/restored/etc/hostname)" = "$marker" || fail "restored content"
test ! -e /tmp/restored/data || fail "pattern restore brought back more than asked"
pass "restores one file with the key"

proxmox-backup-client restore "$snap" root.pxar /tmp/nokey >/tmp/nokey.log 2>&1 && fail "restore without the key"
grep -q "missing key" /tmp/nokey.log || { cat /tmp/nokey.log; fail "restore without the key failed for another reason"; }
pass "restore without the key is refused"

proxmox-backup-client snapshot forget "$snap" >/tmp/forget.log 2>&1 && fail "the PVE token deleted a snapshot"
grep -q "permission check failed" /tmp/forget.log || { cat /tmp/forget.log; fail "forget failed for another reason"; }
pass "the PVE token cannot delete snapshots"

# A rebuilt VM: the datastore disk is kept, the config and token file are not.
proxmox-backup-manager datastore remove backups >/dev/null 2>&1
rm /root/nullbill/pbs-token.json
old=$PBS_PASSWORD
converge pbs.yml "${args[@]}"
PBS_PASSWORD=$(token)
test "$PBS_PASSWORD" != "$old" || fail "token not replaced"
test "$(latest)" = "$snap" || fail "snapshot missing after adopting the chunk store"
proxmox-backup-client restore "$snap" root.pxar /tmp/again --keyfile /tmp/enc.key --pattern etc/hostname >/dev/null 2>&1 || fail "restore after rebuild"
test "$(cat /tmp/again/etc/hostname)" = "$marker" || fail "restored content after rebuild"
PBS_PASSWORD=$old proxmox-backup-client snapshot list >/dev/null 2>&1 && fail "old token still works"
pass "a rebuild adopts the kept chunk store and replaces the lost token"
