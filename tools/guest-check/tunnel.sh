#!/usr/bin/env bash
# The way the download client's traffic leaves behind the owner's VPN
# (nullbill/torrents.py), walked with the large-torrents class's rendered
# router ruleset, the role's own nb-torrents and a real WireGuard tunnel,
# in network namespaces:
#
#   guest - router - home (a home router) - net (the internet) - vpn (the provider's server)
#                                                              \ web (somewhere on the internet)
#
# The router is NullBill's, with /build/large-torrents/router/etc/nftables.conf
# loaded. web answers each connection with the address it came from, so
# what leaves through the tunnel is seen to arrive from the VPN and not
# from the home. Needs a privileged container without a network of its own.
# WireGuard is the kernel's where it has it (CI, and every PVE host); a
# kernel without it gets wireguard-go at both ends, which nb-torrents finds
# as the interface it would otherwise make.
set -euo pipefail

build=/build/large-torrents/router/etc
rule=$(grep "WireGuard to the owner's VPN" "$build/nftables.conf")
server=$(sed -n 's/.* ip daddr \([0-9.]*\) udp dport \([0-9]*\) .*/\1/p' <<<"$rule")
port=$(sed -n 's/.* ip daddr \([0-9.]*\) udp dport \([0-9]*\) .*/\2/p' <<<"$rule")
guest_ip=$(sed -n 's/.* ip daddr { \([0-9.]*\) } tcp dport 8080 .*/\1/p' "$build/nftables.conf" | head -1)
zone=${guest_ip%.*}
[[ -n $server && -n $port && -n $guest_ip ]] || { echo "FAIL tunnel: the rendered ruleset names no VPN server"; exit 1; }
# RFC 5737 for the internet; the VPN's server is the fixture's.
home_wan=192.0.2.7 web=198.51.100.80 lan=192.168.1.1 uplink=192.168.1.57
inside=10.2.0.2 inside_gw=10.2.0.1

ns() { ip netns exec "$@"; }
ok() { echo "ok   tunnel: $*"; }
fail() {
  echo "FAIL tunnel: $*"
  ns guest ip rule show 2>/dev/null || true
  ns guest ip route show table 51820 2>/dev/null || true
  ns guest wg show 2>/dev/null | grep -v 'private key' || true
  exit 1
}

for name in guest router home net vpn web; do
  ip netns add "$name"
  ns "$name" ip link set lo up
done
link() {
  ip link add "$2" netns "$1" type veth peer name "$4" netns "$3"
  ns "$1" ip link set "$2" up
  ns "$3" ip link set "$4" up
}
link guest eth0 router nb-torrents
link router nb-uplink home lan
link home wan net home
link net vpn vpn eth0
link net web web eth0
ns guest ip addr add "$guest_ip/24" dev eth0
ns guest ip route add default via "$zone.1"
ns router ip addr add "$zone.1/24" dev nb-torrents
ns router ip addr add "$uplink/24" dev nb-uplink
ns router ip route add default via "$lan"
ns home ip addr add "$lan/24" dev lan
ns home ip addr add "$home_wan/24" dev wan
ns home ip route add default via "${home_wan%.*}.1"
ns net ip addr add "${home_wan%.*}.1/24" dev home
ns net ip addr add "${server%.*}.1/24" dev vpn
ns net ip addr add "${web%.*}.1/24" dev web
ns vpn ip addr add "$server/24" dev eth0
ns vpn ip route add default via "${server%.*}.1"
ns web ip addr add "$web/24" dev eth0
ns web ip route add default via "${web%.*}.1"
for name in router home net vpn; do
  ns "$name" sh -c 'echo 1 >/proc/sys/net/ipv4/ip_forward'
done
ns home nft -f - <<'NFT'
table ip nat {
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "wan" masquerade
    }
}
NFT
ns vpn nft -f - <<'NFT'
table ip nat {
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        oifname "eth0" masquerade
    }
}
NFT
ns router nft -f "$build/nftables.conf"

# Somewhere on the internet: it says where each connection came from.
cat >/tmp/web.py <<'PY'
import socket

server = socket.create_server(("0.0.0.0", 443))
while True:
    conn, peer = server.accept()
    conn.sendall(peer[0].encode())
    conn.close()
PY
ns web python3 /tmp/web.py &

# WireGuard at both ends.
kernel=1
ip link add wgprobe type wireguard 2>/dev/null && ip link del wgprobe || kernel=
wireguard() { # namespace interface
  if [[ -n $kernel ]]; then
    ns "$1" ip link add "$2" type wireguard
  else
    # It runs on Linux only when told that the kernel's is not wanted.
    WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 ns "$1" wireguard-go "$2" >/dev/null 2>&1 || fail "wireguard-go did not start in $1"
  fi
}
umask 077
server_key=$(wg genkey) client_key=$(wg genkey)
# The server's interface has a name of its own: the namespaces share one
# /var/run/wireguard, where wireguard-go keeps a socket per interface name.
wireguard vpn wgvpn
echo "$server_key" >/tmp/server.key
ns vpn wg set wgvpn listen-port "$port" private-key /tmp/server.key peer "$(wg pubkey <<<"$client_key")" allowed-ips "$inside/32"
ns vpn ip addr add "$inside_gw/24" dev wgvpn
ns vpn ip link set wgvpn up

# The guest, as the qbittorrent role leaves it (nullbill/torrents.py's
# variables and wg_conf).
useradd -u 1000 -U -M -s /usr/sbin/nologin qbt
mkdir -p /etc/nullbill
cat >/etc/nullbill/torrents.json <<JSON
{"user": "qbt", "port": 8080, "username": "nullbill-admin", "store": "/downloads", "keep_free_gib": 25, "route": "vpn",
 "base": "${zone%.*}.0.0/16",
 "tunnel": {"interface": "wg0", "address": "$inside/32", "dns": ["$inside_gw"], "mtu": 1420, "endpoint": "$server:$port", "handshake_s": 180}}
JSON
cat >/etc/nullbill/wg0.conf <<CONF
[Interface]
PrivateKey = $client_key

[Peer]
PublicKey = $(wg pubkey <<<"$server_key")
AllowedIPs = 0.0.0.0/0
Endpoint = $server:$port
PersistentKeepalive = 25
CONF
[[ -n $kernel ]] || wireguard guest wg0

# What the web sees of a connection by the client's user, or by root;
# "none" when it does not get there within three seconds.
sees() { # user
  ns guest runuser -u "$1" -- python3 - "$web" <<'PY' 2>/dev/null || echo none
import socket, sys

with socket.create_connection((sys.argv[1], 443), 3) as conn:
    print(conn.recv(64).decode())
PY
}
torrents() { ns guest python3 /usr/local/sbin/nb-torrents "$@"; }

# Before the tunnel: nothing from this guest gets out, whoever sends it.
[[ $(sees qbt) == none && $(sees root) == none ]] || fail "the guest reached the internet before the tunnel"
ok "before the tunnel nothing from the download client's guest reaches the internet"

torrents tunnel up || fail "nb-torrents tunnel up"
for _ in $(seq 20); do
  [[ $(torrents tunnel status) == up ]] && break
  sleep 0.5
done
[[ $(torrents tunnel status) == up ]] || fail "the tunnel did not come up: $(torrents tunnel status)"
[[ $(sees qbt) == "$server" ]] || fail "the client's user did not arrive from the VPN's address (the web saw $(sees qbt))"
[[ $(sees root) == none ]] || fail "root in the guest reached the internet"
ok "through $([[ -n $kernel ]] && echo "the kernel's WireGuard" || echo wireguard-go) the client's user arrives from the VPN's address, never the home's; root does not get out"

[[ $(torrents tunnel through "$web") == "through the tunnel only" ]] || fail "verify's question: $(torrents tunnel through "$web")"
ns guest ip -4 route get "${zone%.*}.6.11" uid 1000 | grep -q " dev eth0 " || fail "the page's answers to visitors do not stay on NullBill's network"
ok "verify's question is answered: through the tunnel only; the page still answers inside NullBill's network"

# The guest's own routes taken away: the router alone still holds the line.
ns guest ip rule del pref 110
[[ $(sees qbt) == none ]] || fail "without the guest's rules the router let the client's user out"
torrents tunnel up
[[ $(sees qbt) == "$server" ]] || fail "the tunnel after its rules came back"
ok "with the guest's own routes taken away, the router lets nothing but the tunnel pass"

# A dropped tunnel.
[[ $(torrents tunnel drop "$web") == stopped ]] || fail "the tunnel taken down: $(torrents tunnel drop "$web")"
[[ $(sees qbt) == "$server" ]] || fail "the tunnel did not come back after the test"
ok "with the tunnel taken down the client's user reaches nothing, and the tunnel comes back"

ns guest ip link set wg0 down
[[ $(sees qbt) == none ]] || fail "interface down: the client's user got out"
torrents tunnel up
ns vpn ip link set wgvpn down
[[ $(sees qbt) == none ]] || fail "the VPN's server gone: the client's user got out"
ns vpn ip link set wgvpn up
for _ in $(seq 20); do
  [[ $(sees qbt) == "$server" ]] && break
  sleep 0.5
done
[[ $(sees qbt) == "$server" ]] || fail "the tunnel did not recover once the VPN's server was back"
ok "interface down, or the VPN's server gone: nothing gets out; with the server back the tunnel carries again"

torrents tunnel down
[[ $(sees qbt) == none ]] || fail "the tunnel's unit stopped: the client's user got out"
ok "with the tunnel's unit stopped the client's user reaches nothing"
