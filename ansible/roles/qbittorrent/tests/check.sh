#!/usr/bin/env bash
# Container check for the qbittorrent role, run by tools/check-roles:
# converge twice, run qBittorrent as its unit would, then drive nb-torrents
# as apply's torrents stage, the every-minute timer, nb-busy and verify do.
# The page never takes the password a new qBittorrent ships with; downloads
# pause when the disk runs low and exactly those resume; and with a VPN
# chosen, what the client's user sends has no way out but the tunnel, also
# when the tunnel cannot come up. The tunnel itself, with a real WireGuard
# peer and the rendered firewall, is walked in tools/guest-check/tunnel.sh.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

# As apply passes them: numbers as numbers.
home=(-e '{"qbittorrent_manage_services": false, "qbittorrent_password": "first-password", "qbittorrent_keep_free_gib": 1}')
converge qbittorrent.yml "${home[@]}"
[ "$(id -u qbt):$(id -g qbt)" = 1000:1000 ] || fail "qbt is uid and gid 1000"
[ "$(getent passwd qbt | cut -d: -f7)" = /usr/sbin/nologin ] || fail "qbt has no login"
[ "$(stat -c '%U %a' /etc/nullbill/torrents-password)" = "root 600" ] || fail "the password file is root's alone"
[ "$(stat -c '%U %a' /var/lib/qbittorrent/qBittorrent/config/qBittorrent.conf)" = "qbt 600" ] || fail "qBittorrent's settings are its own"
[ ! -e /etc/nullbill/wg0.conf ] || fail "no VPN file without a VPN"
grep -q '^ConditionPathIsMountPoint=/downloads$' /etc/systemd/system/qbittorrent.service || fail "the unit waits for the kept disk"
if grep -q 'nb-tunnel\|BindReadOnlyPaths' /etc/systemd/system/qbittorrent.service; then fail "no tunnel in the unit without a VPN"; fi
pass "qBittorrent as uid 1000 without a login, its settings made before its first start"

# tools/check-roles mounts a tmpfs at /downloads, the kept disk's stand-in.
findmnt -rno TARGET --mountpoint /downloads >/dev/null || fail "/downloads is a mount in this check"
chown 1000:1000 /downloads
# A guest's client is not on the internet in this check.
loopback_only qbt

start() {
  (runuser -u qbt -- env -i HOME=/var/lib/qbittorrent PATH=/usr/bin:/bin /opt/qbittorrent/*/bin/qbittorrent-nox --profile=/var/lib/qbittorrent \
    >>/tmp/qbittorrent.log 2>&1 &)
  nb-torrents wait 60 || { tail -30 /tmp/qbittorrent.log; fail "qBittorrent answers"; }
}
start

ip=$(hostname -i | cut -d' ' -f1)
code() { curl -sS -o /dev/null -w '%{http_code}' "$@"; }
# qBittorrent 5.2 and later answer a sign-in with a status alone.
login() {
  case $(code -H "Referer: http://$ip:8080" "http://$ip:8080/api/v2/auth/login" --data-urlencode "username=$1" --data-urlencode "password=$2") in
    200 | 204) echo "Ok." ;;
    401) echo "Fails." ;;
    *) echo "?" ;;
  esac
}
[ "$(code "http://$ip:8080/api/v2/app/version")" = 403 ] || fail "the page asks for a sign-in from another address"
[ "$(login nullbill-admin first-password)" = "Ok." ] || fail "sign-in with the password apply made"
[ "$(login nullbill-admin adminadmin)" = "Fails." ] || fail "the password qBittorrent ships with is refused"
[ "$(login admin adminadmin)" = "Fails." ] || fail "the user qBittorrent ships with is refused"
pass "the page takes the password apply made, and never the one qBittorrent ships with"

pref() { curl -sS http://127.0.0.1:8080/api/v2/app/preferences | python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
[ "$(nb-torrents settings)" = changed ] || fail "settings, the first time"
[ "$(nb-torrents settings)" = same ] || fail "settings again change nothing"
[ "$(pref save_path)" = /downloads ] || fail "downloads go to /downloads"
[ "$(pref temp_path)" = /downloads/incomplete ] || fail "unfinished downloads wait in /downloads/incomplete"
[ "$(pref temp_path_enabled)" = True ] || fail "unfinished downloads are kept apart"
[ "$(pref upnp)" = False ] || fail "UPnP is off"
[ "$(pref listen_port)" = 41643 ] || fail "it listens for peers on the port the firewall passes on (it is $(pref listen_port))"
ss -Hltn | grep -q ':41643 ' || fail "the peer port is open in the guest"
[ "$(pref current_network_interface)" = "" ] || fail "no interface is forced without a VPN"
[ "$(stat -c '%U' /downloads/incomplete/.ignore)" = qbt ] || fail "Jellyfin is told to pass over unfinished downloads"
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/app/setPreferences --data-urlencode 'json={"save_path": "/tmp", "upnp": true}'
[ "$(nb-torrents settings)" = changed ] || fail "settings the owner changed are set back"
[ "$(pref save_path)" = /downloads ] || fail "downloads go to /downloads again"
echo second-password >/etc/nullbill/torrents-password
[ "$(nb-torrents settings)" = changed ] || fail "a new password is set"
[ "$(login nullbill-admin second-password)" = "Ok." ] || fail "sign-in with the new password"
[ "$(login nullbill-admin first-password)" = "Fails." ] || fail "the old password is refused"
echo first-password >/etc/nullbill/torrents-password
nb-torrents settings >/dev/null
pass "nb-torrents settings: where files go, no UPnP, the page's password; nothing when nothing differs"

# RSS: a feed is read, and a download rule adds what it offers. The feed
# is this container's own; the client's user reaches nothing else here.
[ "$(pref rss_processing_enabled)" = True ] || fail "feeds are read from the start"
[ "$(pref rss_auto_downloading_enabled)" = True ] || fail "download rules work from the start"
fed=fedcba9876543210fedcba9876543210fedcba98
mkdir -p /tmp/feed
cat >/tmp/feed/feed.xml <<XML
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>A tracker's feed</title><link>http://127.0.0.1:8099/</link><description>new</description>
<item><title>Episode one</title><guid>one</guid><pubDate>Tue, 10 Nov 2026 12:00:00 +0000</pubDate>
<enclosure url="magnet:?xt=urn:btih:$fed&amp;dn=episode-one" type="application/x-bittorrent" length="1"/></item>
</channel></rss>
XML
(cd /tmp/feed && python3 -m http.server 8099 --bind 127.0.0.1 >/dev/null 2>&1 &)
sleep 1
rss() { curl -sS "http://127.0.0.1:8080/api/v2/rss/$1" "${@:2}"; }
rss setRule --data-urlencode ruleName=everything \
  --data-urlencode 'ruleDef={"enabled": true, "mustContain": "", "affectedFeeds": ["http://127.0.0.1:8099/feed.xml"]}' >/dev/null
rss addFeed --data-urlencode url=http://127.0.0.1:8099/feed.xml --data-urlencode path=tracker >/dev/null
for _ in $(seq 40); do
  curl -sS http://127.0.0.1:8080/api/v2/torrents/info | grep -q "$fed" && break
  sleep 0.5
done
rss 'items?withData=true' | grep -q "Episode one" || { rss 'items?withData=true'; fail "the feed's articles are read"; }
curl -sS http://127.0.0.1:8080/api/v2/torrents/info | grep -q "$fed" || { tail -20 /tmp/qbittorrent.log; fail "a download rule adds what the feed offers"; }
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/torrents/delete -d "hashes=$fed" -d deleteFiles=true
for _ in $(seq 20); do
  curl -sS http://127.0.0.1:8080/api/v2/torrents/info | grep -q "$fed" || break
  sleep 0.5
done
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/app/setPreferences --data-urlencode 'json={"rss_processing_enabled": false}'
nb-torrents settings >/dev/null
[ "$(pref rss_processing_enabled)" = False ] || fail "feeds the owner switched off stay off"
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/app/setPreferences --data-urlencode 'json={"rss_processing_enabled": true}'
pass "RSS: a feed is read and a download rule adds its torrent; switched off by the owner, it stays off"

# nb-busy's question. This check's own requests came over loopback or are closed.
[ -z "$(/etc/nullbill/busy.d/torrents)" ] || { /etc/nullbill/busy.d/torrents; fail "an unused client reports nothing"; }
add() { curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/torrents/add -F "urls=magnet:?xt=urn:btih:$1&dn=$2"; }
state() { curl -sS http://127.0.0.1:8080/api/v2/torrents/info | python3 -c 'import json, sys; print(next(t["state"] for t in json.load(sys.stdin) if t["hash"] == sys.argv[1]))' "$1"; }
# qBittorrent reports a new state a moment after it is asked for one.
becomes() {
  for _ in $(seq 20); do
    [ "$(state "$1")" = "$2" ] && return 0
    sleep 0.5
  done
  return 1
}
a=0123456789abcdef0123456789abcdef01234567 b=89abcdef0123456789abcdef0123456789abcdef
add $a one
add $b two
{ becomes $a metaDL && becomes $b metaDL; } || fail "a new download starts (it is $(state $a))"
grep -qx "2 downloads in progress" <<<"$(nb-torrents busy)" || { nb-torrents busy; fail "downloads count as use"; }
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/torrents/stop -d "hashes=$b"
becomes $b stoppedDL || fail "a download pauses when asked"
grep -qx "1 download in progress" <<<"$(nb-torrents busy)" || { nb-torrents busy; fail "a paused download is no use"; }
pass "nb-torrents busy: nothing while unused, then each download in progress"

# Room on the disk. The tmpfs has under 100 GiB free and over 2.
metric() { sed -n "s/^$1 //p" /var/lib/prometheus/node-exporter/torrents.prom; }
keep() { python3 - "$1" <<'PY'
import json, sys
path = "/etc/nullbill/torrents.json"
cfg = json.load(open(path))
cfg["keep_free_gib"] = int(sys.argv[1])
json.dump(cfg, open(path, "w"))
PY
}
nb-torrents tick
[ "$(nb-torrents space)" = room ] || fail "room at first"
[ "$(metric nb_torrents_paused_for_space)" = 0 ] || fail "not paused at first"
{ [ "$(metric nb_torrents_up)" = 1 ] && [ "$(metric nb_torrents_downloading)" = 1 ]; } || fail "metrics of a client at work"
[ "$(metric nb_torrents_store_mounted)" = 1 ] || fail "the disk is seen"
keep 100
nb-torrents tick
becomes $a stoppedDL || fail "a download pauses when the disk has less free than is kept (it is $(state $a))"
grep -q "^1 download is paused: .* GiB free on the disk, and 100 GiB are kept free" <<<"$(nb-torrents space)" || { nb-torrents space; fail "space says why"; }
[ "$(metric nb_torrents_paused_for_space)" = 1 ] || fail "the metric says paused"
nb-torrents tick
keep 1
nb-torrents tick
becomes $a metaDL || fail "the download goes on once there is room (it is $(state $a))"
[ "$(state $b)" = stoppedDL ] || fail "a download the owner paused stays paused"
[ "$(nb-torrents space)" = room ] || fail "room again"
nb-torrents tick
[ "$(metric nb_torrents_paused_for_space)" = 0 ] || fail "the metric says not paused"
pass "downloads pause while the disk has less free than is kept, and exactly those go on afterwards"

# Where downloads are saved. The client's user owns /downloads and nothing
# else here, so a download told to go elsewhere fetches into incomplete and
# cannot be moved when it is done; qBittorrent says so in its log only.
nb-torrents tick
[ "$(nb-torrents paths)" = inside ] || { nb-torrents paths; fail "every download is inside /downloads at first"; }
[ "$(metric nb_torrents_outside_store)" = 0 ] || fail "the metric counts no download outside"
[ "$(metric nb_torrents_move_failed_timestamp_seconds)" = 0 ] || fail "no failed move yet"
[ "$(nb-torrents reached)" = firewalled ] || { nb-torrents reached; fail "nobody has connected from outside in this check"; }
# A whole download, already in incomplete: qBittorrent checks it, finds it
# complete and moves it to where it is to be saved.
runuser -u qbt -- python3 - /downloads/incomplete/film.bin /tmp/film.torrent <<'PY'
import hashlib, os, sys
data = os.urandom(70000)
open(sys.argv[1], "wb").write(data)
size = 16384
pieces = b"".join(hashlib.sha1(data[i : i + size]).digest() for i in range(0, len(data), size))
info = b"d6:lengthi%de4:name8:film.bin12:piece lengthi%de6:pieces%d:" % (len(data), size, len(pieces)) + pieces + b"e"
open(sys.argv[2], "wb").write(b"d4:info" + info + b"e")
PY
film=$(python3 -c 'import hashlib, sys; d = open(sys.argv[1], "rb").read(); print(hashlib.sha1(d[7:-1]).hexdigest())' /tmp/film.torrent)
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/torrents/add -F "torrents=@/tmp/film.torrent" -F savepath=/srv/films
for _ in $(seq 40); do
  curl -sS 'http://127.0.0.1:8080/api/v2/log/main?last_known_id=-1' | grep -q "Failed to move torrent" && break
  sleep 0.5
done
curl -sS 'http://127.0.0.1:8080/api/v2/log/main?last_known_id=-1' | grep -q "Failed to move torrent" \
  || { curl -sS 'http://127.0.0.1:8080/api/v2/log/main?last_known_id=-1'; echo; state "$film"; fail "qBittorrent logs a move it could not make"; }
grep -qx '"film.bin" is set to save to /srv/films, outside /downloads' <<<"$(nb-torrents paths)" || { nb-torrents paths; fail "paths names the download saved outside"; }
grep -qx 'at [0-9:]* on [0-9]* [A-Za-z]* qBittorrent could not move "film.bin" to /srv/films: Permission denied' <<<"$(nb-torrents paths)" \
  || { nb-torrents paths; fail "paths says in one short line what qBittorrent could not move, where to and why"; }
nb-torrents tick
[ "$(metric nb_torrents_outside_store)" = 1 ] || fail "the metric counts the download outside"
[ "$(metric nb_torrents_move_failed_timestamp_seconds)" -gt 1700000000 ] || fail "the metric says when the move failed"
# Once nothing is saved there any more, nothing is wrong now, and the old line of the log is no longer told.
curl -sS -o /dev/null http://127.0.0.1:8080/api/v2/torrents/delete -d "hashes=$film" -d deleteFiles=false
for _ in $(seq 20); do
  [ "$(nb-torrents paths)" = inside ] && break
  sleep 0.5
done
nb-torrents tick
[ "$(metric nb_torrents_outside_store)" = 0 ] || fail "nothing is outside once that download is gone"
[ "$(metric nb_torrents_move_failed_timestamp_seconds)" = 0 ] || fail "the failed move's metric ends with it, and so does its alert"
[ "$(nb-torrents paths)" = inside ] || { nb-torrents paths; fail "a failed move to a folder nothing is saved to any more is no longer told"; }
# The log is read from where it was left, and from its start when qBittorrent has started anew.
python3 - <<'PY'
import json
state = json.load(open("/var/lib/nullbill/torrents-log.json"))
assert state["last"] >= 0 and "Failed to move torrent" in state["failed"]["message"], state
PY
pass "a download saved outside /downloads is named with what qBittorrent could not move, and neither is told once it is put right"

# qBittorrent gone: the guest must not be stopped for want of an answer.
# shellcheck disable=SC2046
kill $(pidof qbittorrent-nox)
for _ in $(seq 30); do pidof qbittorrent-nox >/dev/null || break; sleep 1; done
grep -q "^cannot ask qBittorrent what it is doing" <<<"$(nb-torrents busy)" || { nb-torrents busy; fail "no answer counts as use"; }
nb-torrents tick
[ "$(metric nb_torrents_up)" = 0 ] || fail "the metric says it does not answer"
pass "a qBittorrent that does not answer counts as in use, and the metric says so"

# With a VPN. The keys are made here and belong to nobody.
nft delete table inet role_check
apt-get install -y -qq wireguard-tools >/dev/null 2>&1
private=$(wg genkey)
public=$(wg genkey | wg pubkey)
cat >/tmp/vpn.json <<JSON
{
  "qbittorrent_route": "vpn",
  "qbittorrent_tunnel": {"interface": "wg0", "address": "10.2.0.2/32", "dns": ["10.2.0.1"], "mtu": 1420,
                         "endpoint": "198.51.100.7:51820", "handshake_s": 180},
  "qbittorrent_wg_conf": "[Interface]\nPrivateKey = $private\n\n[Peer]\nPublicKey = $public\nAllowedIPs = 0.0.0.0/0\nEndpoint = 198.51.100.7:51820\nPersistentKeepalive = 25\n"
}
JSON
converge qbittorrent.yml "${home[@]}" -e @/tmp/vpn.json
[ "$(stat -c '%U %a' /etc/nullbill/wg0.conf)" = "root 600" ] || fail "the VPN's keys are root's alone"
grep -qx 'nameserver 10.2.0.1' /etc/nullbill/torrents-resolv.conf || fail "the VPN's DNS server"
grep -q '^Requires=nb-tunnel.service$' /etc/systemd/system/qbittorrent.service || fail "no client without the tunnel's unit"
grep -q '^BindReadOnlyPaths=/etc/nullbill/torrents-resolv.conf:/etc/resolv.conf$' /etc/systemd/system/qbittorrent.service \
  || fail "the client looks names up at the VPN's DNS"
grep -q '^After=network-online.target nb-tunnel.service$' /etc/systemd/system/qbittorrent.service || fail "the client starts after the tunnel"
grep -q '^ExecStart=/usr/local/sbin/nb-torrents tunnel up$' /etc/systemd/system/nb-tunnel.service || fail "the tunnel's unit"
pass "with a VPN: its keys root's alone, the client's unit bound to the tunnel and to the VPN's DNS"

# The routes of the client's user, on this kernel: through the tunnel, or,
# where the kernel has no WireGuard and the tunnel cannot come up, nowhere.
reach() { python3 -c 'import socket, sys; socket.create_connection(("deb.debian.org", 443), 5)' 2>/dev/null; }
reach || fail "this check reaches the internet as root"
runuser -u qbt -- python3 -c 'import socket; socket.create_connection(("deb.debian.org", 443), 5)' 2>/dev/null \
  || fail "the client's user reaches the internet before the tunnel's rules"
if nb-torrents tunnel up 2>/tmp/tunnel.err; then
  how="a tunnel"
  ip -4 route get 198.51.100.9 uid 1000 | grep -q ' dev wg0 ' || fail "the client's user is routed into the tunnel"
  [ "$(nb-torrents tunnel status)" != up ] || fail "a tunnel nobody answers is not up"
else
  grep -q "wireguard\|not supported\|Unknown device type" /tmp/tunnel.err || { cat /tmp/tunnel.err; fail "tunnel up fails only for want of WireGuard"; }
  how="no tunnel (this kernel has no WireGuard)"
  if ip -4 route get 198.51.100.9 uid 1000 2>/dev/null; then fail "without a tunnel the client's user has no route out"; fi
fi
ip -4 route get 198.51.100.9 | grep -q ' dev eth0 ' || fail "the guest's own routes stay"
ip -4 route get 10.42.6.11 uid 1000 | grep -q ' dev eth0 ' || fail "the page still answers visitors inside NullBill's network"
if runuser -u qbt -- python3 -c 'import socket; socket.create_connection(("deb.debian.org", 443), 5)' 2>/dev/null; then
  fail "with $how the client's user still reaches the internet"
fi
reach || fail "root still reaches the internet (packages come through the proxy)"
nb-torrents tunnel down
if runuser -u qbt -- python3 -c 'import socket; socket.create_connection(("deb.debian.org", 443), 5)' 2>/dev/null; then
  fail "with the tunnel taken away the client's user reaches the internet"
fi
pass "with $how, and with it taken away, the client's user reaches nothing outside; root's routes are untouched"

start
[ "$(nb-torrents settings)" = changed ] || fail "settings with a VPN"
[ "$(pref current_network_interface)" = wg0 ] || fail "the client sends on the tunnel's interface only"
# shellcheck disable=SC2046
kill $(pidof qbittorrent-nox)
for _ in $(seq 30); do pidof qbittorrent-nox >/dev/null || break; sleep 1; done
pass "with a VPN qBittorrent sends on the tunnel's interface only"

# And without one again: nothing of the tunnel is left.
converge qbittorrent.yml "${home[@]}"
for left in /etc/nullbill/wg0.conf /etc/nullbill/torrents-resolv.conf /etc/systemd/system/nb-tunnel.service; do
  [ ! -e "$left" ] || fail "the VPN's files are gone ($left is there)"
done
{ [ -z "$(ip rule show pref 110)" ] && [ -z "$(ip rule show pref 100)" ]; } || fail "the rules are gone"
[ -z "$(ip route show table 51820 2>/dev/null)" ] || fail "the routes are gone"
runuser -u qbt -- python3 -c 'import socket; socket.create_connection(("deb.debian.org", 443), 5)' 2>/dev/null \
  || fail "the client's user reaches the internet again"
pass "back without a VPN, nothing of the tunnel is left"
