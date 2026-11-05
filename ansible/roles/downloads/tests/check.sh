#!/usr/bin/env bash
# Container check for the downloads role, run by tools/check-roles: a mirror
# that fails each file's first requests, then serves it. apt and a pinned
# fetch both get through; without retries, or with a wrong checksum, they
# fail.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh
export ANSIBLE_ROLES_PATH=/ansible/roles

www=/tmp/mirror
mkdir -p "$www/pkg/DEBIAN" /tmp/plays
printf 'Package: nb-retry-test\nVersion: 1.0\nArchitecture: all\nMaintainer: none <none@example.internal>\nDescription: retry check\n' >"$www/pkg/DEBIAN/control"
dpkg-deb --build "$www/pkg" "$www/nb-retry-test.deb" >/dev/null
rm -rf "$www/pkg"
deb="$www/nb-retry-test.deb"
{
  sed -n '1,4p' <(dpkg-deb -f "$deb")
  echo "Filename: ./nb-retry-test.deb"
  echo "Size: $(stat -c %s "$deb")"
  echo "SHA256: $(sha256sum "$deb" | cut -d' ' -f1)"
  echo
} >"$www/Packages"
packages_sum=$(sha256sum "$www/Packages" | cut -d' ' -f1)
printf 'Origin: nb\nLabel: nb\nSuite: check\nCodename: check\nArchitectures: all amd64\nSHA256:\n %s %s Packages\n' \
  "$packages_sum" "$(stat -c %s "$www/Packages")" >"$www/Release"
echo "pinned file" >"$www/blob"

# Each path fails its first $1 requests with 503, then is served; the
# number of requests per path lands in /tmp/mirror.log.
cat >/tmp/flaky.py <<'PY'
import http.server, sys, collections
fail = int(sys.argv[1])
seen = collections.Counter()
class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        seen[self.path] += 1
        with open("/tmp/mirror.log", "a") as log:
            print(self.path, seen[self.path], file=log)
        if seen[self.path] <= fail:
            self.send_error(503)
            return
        super().do_GET()
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", 8000), lambda *a: Handler(*a, directory="/tmp/mirror")).serve_forever()
PY
mirror() {
  [ -f /tmp/mirror.pid ] && kill "$(cat /tmp/mirror.pid)" && sleep 0.2
  : >/tmp/mirror.log
  python3 /tmp/flaky.py "$1" &
  echo $! >/tmp/mirror.pid
  for _ in $(seq 20); do curl -s -o /dev/null http://127.0.0.1:8000/ && break; sleep 0.2; done
  : >/tmp/mirror.log
}
trap '[ -f /tmp/mirror.pid ] && kill "$(cat /tmp/mirror.pid)"' EXIT

cat >/tmp/plays/apt.yml <<'YML'
- name: Retries and one package
  hosts: all
  roles: [downloads]
  tasks:
    - name: Package from the flaky mirror
      ansible.builtin.apt:
        name: nb-retry-test
        update_cache: true
YML
cat >/tmp/plays/fetch.yml <<'YML'
- name: One pinned fetch
  hosts: all
  tasks:
    - name: Fetch
      ansible.builtin.include_role:
        name: downloads
        tasks_from: fetch
      vars:
        downloads_url: http://127.0.0.1:8000/blob
        downloads_dest: /tmp/blob
        downloads_checksum: "sha256:{{ blob_sha256 }}"
        downloads_delay: 1
YML
play() { ansible-playbook -i localhost, -c local "$@"; }

echo "deb [trusted=yes] http://127.0.0.1:8000/ ./" >/etc/apt/sources.list.d/nb-retry.list
mirror 2
play /tmp/plays/apt.yml >/tmp/apt.log 2>&1 || { cat /tmp/apt.log /tmp/mirror.log; fail "apt through a mirror that fails twice"; }
dpkg -s nb-retry-test >/dev/null || fail "package installed"
grep -qx "/./nb-retry-test.deb 3" /tmp/mirror.log || { cat /tmp/mirror.log; fail "the package took three tries"; }
pass "apt gets a package through a mirror that fails each file twice"

apt-get remove -qq -y nb-retry-test >/dev/null
rm -rf /var/lib/apt/lists/127.0.0.1*
mirror 2
apt-get update -qq -o Acquire::Retries=0 >/tmp/noretry.log 2>&1 || true
grep -q "503" /tmp/noretry.log || { cat /tmp/noretry.log; fail "without retries the mirror's 503 shows"; }
apt-get install -qq -y -o Acquire::Retries=0 nb-retry-test >/dev/null 2>&1 && fail "without retries apt fails"
pass "without retries the same mirror fails"
rm /etc/apt/sources.list.d/nb-retry.list

sum=$(sha256sum "$www/blob" | cut -d' ' -f1)
mirror 3
play /tmp/plays/fetch.yml -e blob_sha256="$sum" >/tmp/fetch.log 2>&1 || { cat /tmp/fetch.log; fail "fetch through a mirror that fails three times"; }
[ "$(cat /tmp/blob)" = "pinned file" ] || fail "fetched content"
[ "$(grep -c '^/blob ' /tmp/mirror.log)" = 4 ] || { cat /tmp/mirror.log; fail "four tries"; }
pass "a pinned fetch gets through a mirror that fails three times"

play /tmp/plays/fetch.yml -e blob_sha256="$sum" >/tmp/again.log 2>&1 || { cat /tmp/again.log; fail "second fetch"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/again.log || fail "second fetch changed something"
[ "$(grep -c '^/blob ' /tmp/mirror.log)" = 4 ] || fail "a kept file is not fetched again"
pass "a kept file with the right checksum is not fetched again"

rm /tmp/blob
mirror 0
play /tmp/plays/fetch.yml -e blob_sha256="$(printf '0%.0s' $(seq 64))" -e downloads_retries=2 >/tmp/bad.log 2>&1 && fail "a wrong checksum fails"
[ "$(grep -c '^/blob ' /tmp/mirror.log)" = 3 ] || { cat /tmp/mirror.log; fail "three tries, then give up"; }
test ! -e /tmp/blob || fail "nothing kept from a wrong checksum"
pass "a wrong checksum fails after the last try and keeps nothing"
