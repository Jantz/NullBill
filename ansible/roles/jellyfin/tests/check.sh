#!/usr/bin/env bash
# Container check for the jellyfin role, run by tools/check-roles: converge
# twice, run Jellyfin as its unit would, then drive nb-jellyfin as apply's
# media stage does. The first-run setup leaves an admin with the password
# apply keeps and nothing open to anyone else; the API key it makes works
# and is replaced, not added to, when lost; transcoding switches between
# the CPU and Quick Sync and says when nothing changed.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

converge jellyfin.yml -e jellyfin_manage_services=false -e jellyfin_proxy=http://192.0.2.10:3128
[ "$(id -u jellyfin):$(id -g jellyfin)" = 1000:1000 ] || fail "jellyfin runs as uid and gid 1000"
[ "$(getent passwd jellyfin | cut -d: -f7)" = /usr/sbin/nologin ] || fail "jellyfin has no login"
pin() { python3 -c "import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))[sys.argv[2]])" /ansible/roles/jellyfin/defaults/main.yml "$1"; }
for pkg in jellyfin-server jellyfin-web; do
  [ "$(dpkg-query -W -f '${Version}' $pkg)" = "$(pin jellyfin_version)" ] || fail "$pkg at the pinned version"
done
[ "$(dpkg-query -W -f '${Version}' "$(pin jellyfin_ffmpeg_package)")" = "$(pin jellyfin_ffmpeg_version)" ] || fail "ffmpeg at the pinned version"
grep -qx 'Environment=HTTP_PROXY=http://192.0.2.10:3128 HTTPS_PROXY=http://192.0.2.10:3128 NO_PROXY=localhost,127.0.0.1' \
  /etc/systemd/system/jellyfin.service.d/nullbill.conf || fail "proxy in the unit"
pass "pinned Jellyfin as uid 1000, without a login, looking things up through the proxy"

# Files on a kept disk: owned by uid 1000, for nobody else (nb-legacy-disk).
mkdir -p /media/old-hdd/Films
echo film >/media/old-hdd/Films/a.mkv
chown -R 1000:1000 /media/old-hdd
chmod -R go= /media/old-hdd
runuser -u jellyfin -- cat /media/old-hdd/Films/a.mkv >/dev/null || fail "jellyfin reads a kept disk's files"
pass "jellyfin reads files only uid 1000 may read"

# As the unit runs it.
(
  set -a
  # shellcheck disable=SC1091
  . /etc/default/jellyfin
  set +a
  cd /var/lib/jellyfin
  # shellcheck disable=SC2086
  setpriv --reuid=jellyfin --regid=jellyfin --init-groups /usr/bin/jellyfin $JELLYFIN_WEB_OPT $JELLYFIN_FFMPEG_OPT \
    --datadir "$JELLYFIN_DATA_DIR" --configdir "$JELLYFIN_CONFIG_DIR" --logdir "$JELLYFIN_LOG_DIR" --cachedir "$JELLYFIN_CACHE_DIR" \
    >/tmp/jellyfin.log 2>&1 &
)
nb-jellyfin wait || { tail -30 /tmp/jellyfin.log; fail "jellyfin answers"; }

[ "$(nb-jellyfin setup nullbill-admin)" = changed ] || fail "first setup"
[ "$(nb-jellyfin setup nullbill-admin)" = same ] || fail "setup again changes nothing"
[ "$(stat -c '%U %a' /etc/nullbill/jellyfin-admin-password)" = "root 600" ] || fail "password file root only"
[ "$(stat -c '%U %a' /etc/nullbill/jellyfin-api-key)" = "root 600" ] || fail "API key file root only"
pascal='Accept: application/json; profile="PascalCase"'
info=$(curl -sS -H "$pascal" http://127.0.0.1:8096/System/Info/Public)
grep -q '"StartupWizardCompleted":true' <<<"$info" || fail "first-run setup done"
pass "first-run setup done once, its password and API key root only"

code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
client='Authorization: MediaBrowser Client="check", Device="check", DeviceId="check", Version="1"'
login() { # password
  code -H "$client" -H 'Content-Type: application/json' -d "{\"Username\": \"nullbill-admin\", \"Pw\": \"$1\"}" \
    http://127.0.0.1:8096/Users/AuthenticateByName
}
[ "$(login "$(cat /etc/nullbill/jellyfin-admin-password)")" = 200 ] || fail "the admin signs in with the kept password"
[ "$(login not-the-password)" = 401 ] || fail "a wrong password is refused"
[ "$(code -H 'Content-Type: application/json' -d '{"Name": "intruder", "Password": "x"}' http://127.0.0.1:8096/Startup/User)" = 401 ] \
  || fail "the first-run setup is closed"
[ "$(code http://127.0.0.1:8096/Users)" = 401 ] || fail "users are hidden without sign-in"
key() { printf 'Authorization: MediaBrowser Token="%s"' "$(cat /etc/nullbill/jellyfin-api-key)"; }
[ "$(code -H "$(key)" http://127.0.0.1:8096/System/Configuration)" = 200 ] || fail "the API key works"
pass "the admin signs in with the kept password; a wrong one, a second setup and anonymous requests are refused"

old=$(cat /etc/nullbill/jellyfin-api-key)
rm /etc/nullbill/jellyfin-api-key
[ "$(nb-jellyfin setup nullbill-admin)" = changed ] || fail "a lost API key is made again"
[ "$(code -H "Authorization: MediaBrowser Token=\"$old\"" http://127.0.0.1:8096/System/Configuration)" = 401 ] \
  || fail "the old API key is gone"
[ "$(curl -sS -H "$pascal" -H "$(key)" http://127.0.0.1:8096/Auth/Keys | grep -o '"AppName":"NullBill"' | wc -l)" = 1 ] || fail "one NullBill key"
cp /etc/nullbill/jellyfin-admin-password /tmp/password
echo changed-by-the-owner >/etc/nullbill/jellyfin-admin-password
rm /etc/nullbill/jellyfin-api-key
if nb-jellyfin setup nullbill-admin 2>/tmp/setup.err; then fail "setup with a wrong password"; fi
grep -q "refuses nullbill-admin" /tmp/setup.err || fail "setup says the password is wrong"
cp /tmp/password /etc/nullbill/jellyfin-admin-password
[ "$(nb-jellyfin setup nullbill-admin)" = changed ] || fail "setup with the password back"
pass "a lost API key is replaced, not added to; a changed password stops setup with a reason"

encoding() { curl -sS -H "$pascal" -H "$(key)" http://127.0.0.1:8096/System/Configuration/encoding; }
[ "$(nb-jellyfin encoding none)" = same ] || fail "CPU transcoding is the default"
[ "$(nb-jellyfin encoding qsv /dev/dri/renderD128)" = changed ] || fail "Quick Sync"
[ "$(nb-jellyfin encoding qsv /dev/dri/renderD128)" = same ] || fail "Quick Sync again changes nothing"
grep -q '"HardwareAccelerationType":"qsv"' <<<"$(encoding)" || fail "Quick Sync set"
grep -q '"VaapiDevice":"/dev/dri/renderD128"' <<<"$(encoding)" || fail "render node set"
[ "$(nb-jellyfin encoding none)" = changed ] || fail "back to the CPU"
grep -q '"HardwareAccelerationType":"none"' <<<"$(encoding)" || fail "CPU set"
pass "transcoding switches between the CPU and Quick Sync, and says when nothing changed"
