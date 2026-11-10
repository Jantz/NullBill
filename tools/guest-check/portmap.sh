#!/usr/bin/env bash
# The path a phone on mobile data takes to the vpn guest (nullbill/direct.py),
# walked with the small class's rendered router files and the role's own
# nb-portmap, Debian 12's natpmpc and upnpc, in network namespaces:
#
#   out (a device on the internet) - home (a home router) - router - vpn
#
# The router is NullBill's, with /build/small/router/etc/nftables.conf loaded.
# The home router is tools/guest-check/home-router for NAT-PMP, and then
# miniupnpd on an address a provider shares between customers, where it
# answers UPnP and NAT-PMP but opens nothing. upnpc and miniupnpd make a
# UPnP mapping only on a public address, which the documentation addresses
# here are not: what upnpc prints then is in tests/test_portmap.py. Needs a
# privileged container; it has no network of its own.
set -euo pipefail

build=/build/small/router/etc
settings=$build/nullbill/portmap.json
port=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["port"])' "$settings")
ssdp=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["ssdp_port"])' "$settings")
vpn_ip=$(sed -n 's/.* dnat to \([0-9.]*\):.*/\1/p' "$build/nftables.conf")
zone=${vpn_ip%.*}
# RFC 5737 for the internet, RFC 6598 for the provider's shared range.
public=203.0.113.7 device=203.0.113.9 shared=100.64.7.2 lan=192.168.1.1 uplink=192.168.1.57
state=/var/lib/nullbill-portmap/state.json
home_pid=

ns() { ip netns exec "$@"; }
ok() { echo "ok   portmap: $*"; }
fail() {
  echo "FAIL portmap: $*"
  cat "$state" 2>/dev/null || true
  tail -n 20 /tmp/home-router.log /tmp/nb-portmap.log 2>/dev/null || true
  exit 1
}

for name in out home router vpn; do
  ip netns add "$name"
  ns "$name" ip link set lo up
done
link() {
  ip link add "$2" netns "$1" type veth peer name "$4" netns "$3"
  ns "$1" ip link set "$2" up
  ns "$3" ip link set "$4" up
}
link out eth0 home wan
link home lan router nb-uplink
link router nb-access vpn eth0
ns out ip addr add "$device/24" dev eth0
ns home ip addr add "$public/24" dev wan
ns home ip addr add "$lan/24" dev lan
ns router ip addr add "$uplink/24" dev nb-uplink
ns router ip route add default via "$lan"
ns router ip addr add "$zone.1/24" dev nb-access
ns vpn ip addr add "$vpn_ip/24" dev eth0
ns vpn ip route add default via "$zone.1"
for name in home router; do
  ns "$name" sh -c 'echo 1 >/proc/sys/net/ipv4/ip_forward'
done

# A home router: its own devices out, answers back in, and in from the
# internet only what a port mapping names. The *_miniupnpd chains are the
# ones miniupnpd fills.
ns home nft -f - <<'NFT'
table inet filter {
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iifname != "wan" accept
    }
    chain forward {
        type filter hook forward priority filter; policy drop;
        ct state established,related accept
        iifname "lan" oifname "wan" accept
        ct status dnat accept
        jump miniupnpd
    }
    chain miniupnpd { }
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        jump prerouting_miniupnpd
    }
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;
        jump postrouting_miniupnpd
        oifname "wan" masquerade
    }
    chain prerouting_miniupnpd { }
    chain postrouting_miniupnpd { }
}
NFT
ns router nft -f "$build/nftables.conf"

# tailscaled's stand-in on the vpn guest: it answers each packet with the
# address it came from. On the port next door too, which must stay shut.
cat >/tmp/listen.py <<'PY'
import socket, sys, threading

def serve(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))
    while True:
        _, sender = sock.recvfrom(64)
        sock.sendto(sender[0].encode(), sender)

for port in sys.argv[1:]:
    threading.Thread(target=serve, args=(int(port),), daemon=True).start()
threading.Event().wait()
PY
ns vpn python3 /tmp/listen.py "$port" "$((port - 1))" &

# From the device on the internet: one packet to the home's public address,
# and what came back ("none" after two seconds of nothing).
probe() {
  ns out python3 - "$public" "$1" <<'PY'
import socket, sys

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(2)
sock.sendto(b"hello", (sys.argv[1], int(sys.argv[2])))
try:
    print(sock.recv(64).decode())
except OSError:
    print("none")
PY
}

home_router() {
  if [[ -n $home_pid ]]; then
    kill "$home_pid"
    wait "$home_pid" 2>/dev/null || true
  fi
  # Not through ns: the function would leave a shell between this one and the router.
  ip netns exec home "$@" >/tmp/home-router.log 2>&1 &
  home_pid=$!
  for _ in $(seq 100); do
    ns home ss -Hlun | grep -q ':5351 ' && return 0
    sleep 0.1
  done
  fail "the home router did not start: $*"
}

# nb-portmap as its unit runs it, then one field of what it wrote.
portmap() {
  mkdir -p "$(dirname "$state")"
  STATE_DIRECTORY=$(dirname "$state") ns router python3 /usr/local/sbin/nb-portmap "${1:-$settings}" >/tmp/nb-portmap.log 2>&1 ||
    fail "nb-portmap exited $?"
}
field() { python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))[sys.argv[2]]))' "$state" "$1"; }
python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1])) | {"enabled": False}))' "$settings" >/tmp/off.json

[[ $(probe "$port") == none ]] || fail "UDP $port reached the vpn guest before the home router forwards it"
ok "nothing from the internet reaches the vpn guest on UDP $port before the home router forwards it"

home_router python3 /usr/local/bin/home-router "$lan" "$public"
portmap
[[ $(field method) == '"NAT-PMP"' && $(field external) == "\"$public:$port\"" ]] || fail "no mapping: $(cat /tmp/nb-portmap.log)"
# The sender's own address arrives: Tailscale answers there.
[[ $(probe "$port") == "$device" ]] || fail "UDP $port did not reach the vpn guest, or not from the sender's address"
ok "the home router opens UDP $port for nb-portmap (NAT-PMP), and a device on the internet reaches the vpn guest at it"

# A port forwarded for anything else ends at the router.
ns home nft add rule ip portmap prerouting iifname wan udp dport "$((port - 1))" dnat to "$uplink:$((port - 1))"
[[ $(probe "$((port - 1))") == none ]] || fail "UDP $((port - 1)) reached the vpn guest"
ok "another port the home router forwards never passes the router"

portmap /tmp/off.json
[[ $(field method) == null && $(field held) == '[]' && $(probe "$port") == none ]] || fail "the mapping stayed after tailnet.port_mapping: false"
ok "switched off, nb-portmap gives the port back"

# The download client's port, when it sends from the home connection
# (nullbill/torrents.py): the large class's render with that choice, a
# client guest behind the router, and the same nb-portmap asking for the
# port in TCP and UDP besides Tailscale's.
home_build=/build/large-torrents-home/router/etc
peers=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["more"]["torrents"]["port"])' "$home_build/nullbill/portmap.json")
client_ip=$(sed -n "s/.* tcp dport $peers dnat to \([0-9.]*\):.*/\1/p" "$home_build/nftables.conf")
[[ -n $client_ip ]] || fail "the render with downloads from home sends no port to the client"
ip netns add client
ns client ip link set lo up
link router nb-torrents client eth0
ns router ip addr add "${client_ip%.*}.1/24" dev nb-torrents
ns client ip addr add "$client_ip/24" dev eth0
ns client ip route add default via "${client_ip%.*}.1"
ns router nft -f "$home_build/nftables.conf"
# qBittorrent's stand-in: it tells each peer the address it came from, over
# TCP as over UDP, and on the port next door, which must stay shut.
cat >/tmp/peers.py <<'PY'
import socket, sys, threading

def serve(port):
    server = socket.create_server(("0.0.0.0", port))
    while True:
        conn, peer = server.accept()
        conn.sendall(peer[0].encode())
        conn.close()

for port in sys.argv[1:]:
    threading.Thread(target=serve, args=(int(port),), daemon=True).start()
threading.Event().wait()
PY
ns client python3 /tmp/peers.py "$peers" "$((peers + 1))" &
ns client python3 /tmp/listen.py "$peers" &
sleep 0.5
tcp_probe() {
  ns out python3 - "$public" "$1" <<'PY'
import socket, sys

try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), 2) as conn:
        print(conn.recv(64).decode())
except OSError:
    print("none")
PY
}
more() { python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))["more"]["torrents"][sys.argv[2]], sort_keys=True))' "$state" "$1"; }
[[ $(tcp_probe "$peers") == none && $(probe "$peers") == none ]] || fail "port $peers reached the download client before the home router forwards it"
portmap "$home_build/nullbill/portmap.json"
[[ $(more open) == "{\"tcp\": \"$public:$peers\", \"udp\": \"$public:$peers\"}" ]] || fail "no mapping for the client's port: $(more why)"
[[ $(tcp_probe "$peers") == "$device" ]] || fail "TCP $peers did not reach the download client from the sender's address"
[[ $(probe "$peers") == "$device" ]] || fail "UDP $peers did not reach the download client from the sender's address"
[[ $(field method) == '"NAT-PMP"' && $(probe "$port") == "$device" ]] || fail "Tailscale's port was lost beside the client's"
ok "the home router opens TCP and UDP $peers for nb-portmap too, and a peer on the internet reaches the download client at them"
ns home nft add rule ip portmap prerouting iifname wan tcp dport "$((peers + 1))" dnat to "$uplink:$((peers + 1))"
[[ $(tcp_probe "$((peers + 1))") == none ]] || fail "TCP $((peers + 1)) reached the download client"
python3 -c 'import json, sys; s = json.load(open(sys.argv[1])); s["more"]["torrents"]["enabled"] = False; print(json.dumps(s))' "$home_build/nullbill/portmap.json" >/tmp/peers-off.json
portmap /tmp/peers-off.json
[[ $(more held) == '[]' && $(tcp_probe "$peers") == none && $(probe "$peers") == none ]] || fail "the client's port stayed open after torrents.port_mapping: false"
[[ $(probe "$port") == "$device" ]] || fail "Tailscale's port went with the client's"
ok "another port never reaches the client; switched off, its port is given back and Tailscale's stays"
ns router nft -f "$build/nftables.conf"
portmap /tmp/off.json

# A home router whose own address is the provider's shared one: miniupnpd,
# as on OpenWrt and many others, says so over both protocols.
ns home ip addr flush dev wan
ns home ip addr add "$shared/24" dev wan
cat >/tmp/miniupnpd.conf <<CONF
ext_ifname=wan
listening_ip=lan
enable_natpmp=yes
enable_upnp=yes
secure_mode=yes
system_uptime=yes
uuid=00000000-0000-0000-0000-000000000001
lease_file=/tmp/upnp.leases
allow 1024-65535 ${lan%.*}.0/24 1024-65535
deny 0-65535 0.0.0.0/0 0-65535
CONF
home_router miniupnpd -f /tmp/miniupnpd.conf -P /tmp/miniupnpd.pid -d
portmap
# Not "no answer": the search was answered, through the router's input chain.
want='{"NAT-PMP": "it has no public address of its own", "UPnP": "it has no public address of its own"}'
[[ $(field method) == null && $(field why) == "$want" ]] || fail "miniupnpd on a shared address: $(field why)"
ok "miniupnpd on a provider's shared address answers over NAT-PMP and UPnP, and nb-portmap reports that no port can be opened"

# The UPnP answer is a new packet to the port the search left from: the
# ruleset lets in SSDP_REPLY_PORT and nothing else, so a search from any
# other port hears nothing.
# (upnpc exits 1 here either way: this router is not one it would use.)
heard() { grep -q "IGD : http" <<<"$(ns router upnpc -m nb-uplink "$@" -l 2>&1 || true)"; }
heard -z "$ssdp" || fail "a UPnP search from port $ssdp was not answered"
if heard; then
  fail "a UPnP search from another port was answered through the router's input chain"
fi
ok "the router's input chain lets the home router's UPnP answer in on nb-portmap's port only"
