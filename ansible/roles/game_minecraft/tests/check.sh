#!/usr/bin/env bash
# Container check for the game_minecraft role, run by tools/check-roles:
# refuse without the EULA, converge twice with the variables apply passes
# (tests/vars.json), then run the real server as its unit does behind a
# stand-in for the egress proxy and prove that it reaches Mojang only
# through nb-tls-tunnel, asks Mojang about a player who joins, takes console
# lines from nb-game, keeps the owner's settings and saves when stopped.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

tests=/ansible/roles/game_minecraft/tests
# The egress proxy with this guest's allow-list (catalog.yaml's
# services.minecraft.proxy_allow, less the download's host).
python3 /ansible/roles/game_server/tests/connect-proxy.py 3128 discovery.minecraftservices.com api.minecraftservices.com \
  sessionserver.mojang.com api.mojang.com >/tmp/proxy.out 2>&1 &
here='{"game_minecraft_proxy": "127.0.0.1:3128", "game_minecraft_manage_services": false, "game_server_manage_services": false}'

if ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" -e '{"game_minecraft_eula": false}' \
  /ansible/game-minecraft.yml >/tmp/eula.log 2>&1; then
  fail "the role ran without the EULA"
fi
grep -q 'https://aka.ms/MinecraftEULA' /tmp/eula.log || { cat /tmp/eula.log; fail "the role says where the EULA is"; }
test ! -e /opt/minecraft || fail "nothing is installed without the EULA"
pass "without games.minecraft.eula the role installs nothing and names the EULA"

converge game-minecraft.yml -e "@$tests/vars.json" -e "$here"

for unit in minecraft.service nb-tls-tunnel.service nb-game-metrics.service nb-game-metrics.timer; do
  systemd-analyze verify "/etc/systemd/system/$unit" >/tmp/unit.log 2>&1 || { cat /tmp/unit.log; fail "$unit"; }
done
pass "the units pass systemd-analyze"
[ "$(stat -c '%U:%G %a' /var/lib/minecraft)" = "game:game 750" ] || fail "the world is the game user's alone"
[ "$(stat -c '%U' /opt/minecraft/start /opt/minecraft/server-*.jar | sort -u)" = root ] || fail "the server's files are root's"
getent passwd game | grep -q ':/usr/sbin/nologin$' || fail "the game user has no login"
pass "the server's files are root's, the world the game user's, who has no login"

# Behind a TLS-intercepting proxy, Java needs its CA as the system does.
if [ -s /usr/local/share/ca-certificates/extra-ca.crt ]; then
  java=$(dirname "$(readlink -f /opt/minecraft/java-*/bin/java)")
  awk -v dir=/tmp/extra-ca '/BEGIN CERT/{n++} {print > (dir "-" n ".crt")}' /usr/local/share/ca-certificates/extra-ca.crt
  for cert in /tmp/extra-ca-*.crt; do
    "$java/keytool" -importcert -noprompt -cacerts -storepass changeit -alias "$(basename "$cert")" -file "$cert" >/dev/null 2>&1
  done
fi

# As in the games zone: the game user has no way out but the proxy.
loopback_only game
pass "the game user reaches nothing but this guest's own address"

# The tunnel as its unit runs it: user game, allowed port 443 and no more.
setpriv --reuid=game --regid=game --init-groups --inh-caps=+net_bind_service --ambient-caps=+net_bind_service \
  /usr/local/sbin/nb-tls-tunnel serve 127.0.0.1:3128 >/tmp/tunnel.log 2>&1 &
for _ in $(seq 20); do nb-tls-tunnel check sessionserver.mojang.com >/tmp/tunnel-check.log 2>&1 && break; sleep 0.5; done
[ "$(cat /tmp/tunnel-check.log)" = ok ] || { cat /tmp/tunnel-check.log /tmp/tunnel.log; fail "the tunnel reaches Mojang"; }
if nb-tls-tunnel check example.org >/tmp/tunnel-check.log 2>&1; then fail "the tunnel reached a name the proxy does not allow"; fi
grep -q '^CONNECT example.org:443 ' /tmp/proxy.log || fail "the proxy was asked for the refused name"
pass "the tunnel reaches Mojang through the proxy, and no name the proxy refuses"

if nb-game status | grep -q '"running": true'; then fail "nb-game says a server runs before one does"; fi
if nb-game console "say nobody home" 2>/tmp/console.log; then fail "nb-game typed on a console nobody reads"; fi
grep -q "the server's console is not open" /tmp/console.log || fail "nb-game says the console is not open"
pass "with no server, nb-game says so and types nothing"

: >/tmp/proxy.log
start_game minecraft
nb-game wait 300 || { tail -40 /tmp/minecraft.log; fail "the server answers"; }
[ "$(nb-game players)" = 0 ] || fail "no players yet"
version=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["game_minecraft_version"])' "$tests/vars.json")
nb-game status | python3 -c 'import json, sys; s = json.load(sys.stdin); assert (s["game"], s["version"], s["running"], s["players"]) == ("minecraft", sys.argv[1], True, 0), s' "$version" \
  || fail "nb-game status"
pass "the server starts as its unit starts it and answers nb-game: version $version, no players"
grep -q '^CONNECT discovery.minecraftservices.com:443 ' /tmp/proxy.log || { cat /tmp/proxy.log; fail "the server asked Mojang where its services are"; }
if grep -Eiq 'UnknownHostException|Failed to request yggdrasil public key|Couldn.t connect' /tmp/minecraft.log; then
  grep -Ei 'UnknownHost|yggdrasil|connect' /tmp/minecraft.log
  fail "the server reached Mojang's services"
fi
pass "the server finds Mojang's services through the tunnel and the proxy"

# A player who is not signed in to the account they claim: the server asks
# Mojang (through the proxy) and turns them away.
python3 "$tests/login.py" 25565 NotSignedIn
game_says minecraft "Username 'NotSignedIn' tried to join with an invalid session" 30 || fail "the server refused a player Mojang does not vouch for"
grep -q '^CONNECT sessionserver.mojang.com:443 ' /tmp/proxy.log || { cat /tmp/proxy.log; fail "the server asked Mojang about the player"; }
[ "$(nb-game players)" = 0 ] || fail "the refused player is not counted"
pass "a player who is not signed in to Mojang is refused: the server asked Mojang through the proxy"

nb-game console "say hello from nb-game"
game_says minecraft '\[Server\] hello from nb-game' 15 || fail "the console line reached the server"
# How apply makes an admin: Mojang's own developer, an account that exists.
nb-game console "op jeb_"
game_says minecraft 'Made jeb_ a server operator' 30 || fail "op on the console"
python3 -c 'import json; assert [o["name"] for o in json.load(open("/var/lib/minecraft/ops.json"))] == ["jeb_"]' || fail "ops.json names the admin"
pass "nb-game types on the server's console; op makes an admin and ops.json names them"

nb-game metrics
grep -qx 'nb_game_up{game="minecraft"} 1' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_up 1"
grep -qx 'nb_game_players{game="minecraft"} 0' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_players 0"
[ -z "$(/etc/nullbill/busy.d/game)" ] || fail "an empty server leaves its guest idle"
active=$(sed -n 's/^nb_game_last_active_timestamp_seconds{game="minecraft"} //p' /var/lib/prometheus/node-exporter/nullbill-game.prom)
sleep 1
nb-game metrics
[ "$(sed -n 's/^nb_game_last_active_timestamp_seconds{game="minecraft"} //p' /var/lib/prometheus/node-exporter/nullbill-game.prom)" = "$active" ] \
  || fail "an empty server's last-active time stands still"
pass "metrics: up, no players, and the time it was first seen empty"

# The server rewrote server.properties at its start: the owner's settings
# and NullBill's are in it, and the role finds nothing to change.
for line in difficulty=hard max-players=8 online-mode=true white-list=false server-port=25565 enable-rcon=false; do
  grep -qx "$line" /var/lib/minecraft/server.properties || { cat /var/lib/minecraft/server.properties; fail "server.properties has $line"; }
done
ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-minecraft.yml >/tmp/third.log 2>&1 || { cat /tmp/third.log; fail "third run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/third.log || { grep -B3 changed: /tmp/third.log; fail "the role changed a running server's files"; }
pass "the settings from config.yaml are the server's, and the role leaves the file the server rewrote alone"
# The settings the page offers by name (games.py's OPTIONS; a test keeps
# this list equal to it): the server wrote each into its file, so it knows them.
options="gamemode difficulty max-players view-distance hardcore level-seed white-list motd"
for key in $options; do
  grep -q "^$key=" /var/lib/minecraft/server.properties || { cat /var/lib/minecraft/server.properties; fail "the server has no setting $key"; }
done
pass "the server knows every setting the page offers by name"

# ExecStop: "stop" on the console, and wait for the server to save and go.
pid=$(pidof java)
nb-game stop "$pid"
test ! -e "/proc/$pid" || fail "the server is still running after nb-game stop"
grep -q 'Stopping the server' /tmp/minecraft.log || fail "the server was asked to stop on its console"
grep -q 'Saving worlds' /tmp/minecraft.log || { tail -20 /tmp/minecraft.log; fail "the server saved its world"; }
test -s /var/lib/minecraft/world/level.dat || fail "the world is in /var/lib/minecraft"
nb-game metrics
grep -qx 'nb_game_up{game="minecraft"} 0' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_up 0 once stopped"
pass "nb-game stop has the server save its world and exit; metrics then say it is down"

# Worlds (docs/adr/0019-game-worlds.md), with a stand-in for systemctl
# that starts and stops the real server as its unit does. What New world
# takes (games.py's NEW_WORLD; a test keeps these lists equal to it): the
# server wrote each into its file, and its own jar holds each world type.
new_world="level-seed level-type hardcore generate-structures"
level_types="normal flat large_biomes amplified"
for key in $new_world; do
  grep -q "^$key=" /var/lib/minecraft/server.properties || fail "the server has no setting $key"
done
python3 - /opt/minecraft/server-*.jar "$level_types" <<'PY' || fail "the server's jar holds every world type the page offers"
import io
import sys
import zipfile

outer = zipfile.ZipFile(sys.argv[1])
(inner,) = [n for n in outer.namelist() if n.startswith("META-INF/versions/") and n.endswith(".jar")]
names = zipfile.ZipFile(io.BytesIO(outer.read(inner))).namelist()
missing = [t for t in sys.argv[2].split() if f"data/minecraft/worldgen/world_preset/{t}.json" not in names]
if missing:
    sys.exit(f"no world preset: {', '.join(missing)}")
PY
pass "the server knows every option and world type the page offers for a new world"

# Whether a world is hardcore, as its level.dat says.
hardcore() {
  python3 -c 'import gzip, sys; d = gzip.open(sys.argv[1]).read(); at = d.index(b"\x01\x00\x08hardcore"); print(d[at + 11])' "$1"
}
fake_systemctl minecraft
systemctl start minecraft
nb-game wait 300 || fail "the server answers again"
world list
world_is "[(x['id'], x['title'], x['active'], x['origin']) for x in w['worlds']] == [('w1', 'First world', True, 'setup')] and sorted(w['options']) == sorted('$new_world'.split())" \
  || fail "the world the role's server made is the first"

world new "Flat hardcore" level-type=minecraft:flat hardcore=true level-seed=nullbill || { cat /tmp/world.json; tail -20 /tmp/minecraft.log; fail "a new world"; }
world_is "w['id'] == 'w2' and [(x['title'], x['active']) for x in w['worlds']] == [('First world', False), ('Flat hardcore', True)]" || fail "the new world runs"
[ "$(hardcore /var/lib/minecraft/world/level.dat)" = 1 ] || fail "the new world is hardcore"
[ "$(hardcore /var/lib/minecraft/kept-worlds/w1/level.dat)" = 0 ] || fail "the first world is kept as it was"
if runuser -u game -- ls /var/lib/minecraft/kept-worlds >/dev/null 2>&1; then fail "the server reads the worlds that do not run"; fi
pass "nb-game world new: the real server makes a flat hardcore world; the first world is kept, out of the server's reach"

# The next new world starts from the game's own options, not the last one's.
world new "Plain" || { cat /tmp/world.json; tail -20 /tmp/minecraft.log; fail "a second new world"; }
for line in 'hardcore=false' 'level-type=minecraft\\:normal' 'level-seed='; do
  grep -qx "$line" /var/lib/minecraft/server.properties || { grep -E '^(hardcore|level-)' /var/lib/minecraft/server.properties; fail "a new world's options start over: $line"; }
done
[ "$(hardcore /var/lib/minecraft/world/level.dat)" = 0 ] || fail "the plain world is not hardcore"
pass "a new world without options is the game's own, whatever the one before was"

# Out and in again: the running world is written out whole first.
world export w3 || { cat /tmp/world.json; fail "export of the running world"; }
out=$(python3 -c 'import json; print(json.load(open("/tmp/world.json"))["file"])')
game_says minecraft 'Automatic saving is now enabled' 15 || fail "saving is on again after the export"
python3 - "$out" <<'PY' || fail "the export is the world's folder, without the running server's lock"
import sys
import zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
assert "Plain/level.dat" in names and not any(n.endswith("session.lock") for n in names) and all(n.startswith("Plain/") for n in names), names
PY
size=$(stat -c %s "$out")
world receive "$size" "Came back" <"$out" || { cat /tmp/world.json; fail "upload of a world"; }
world_is "w['id'] == 'w4' and w['worlds'][3]['origin'] == 'upload' and not w['worlds'][3]['active']" || fail "an upload joins the kept worlds"
world sent "$out"
test ! -e "$out" || fail "the sent file is removed"
# A world from a Minecraft that does not exist yet.
python3 - /tmp/newer.zip <<'PY'
import gzip
import struct
import sys
import zipfile

level = b"\x0a\x00\x00\x0a\x00\x04Data\x03\x00\x0bDataVersion" + struct.pack(">i", 999999) + b"\x00\x00"
zipfile.ZipFile(sys.argv[1], "w").writestr("Future/level.dat", gzip.compress(level))
PY
size=$(stat -c %s /tmp/newer.zip)
if world receive "$size" "From the future" </tmp/newer.zip; then fail "took a world this server cannot open"; fi
world_is "'newer Minecraft than this server' in w['error']" || fail "says the world is from a newer Minecraft"
world use w4 || { cat /tmp/world.json; tail -20 /tmp/minecraft.log; fail "running an uploaded world"; }
[ "$(nb-game players)" = 0 ] || fail "the server answers with the uploaded world"
world use w1 || fail "back to the first world"
for old in w2 w3 w4; do world delete "$old" || fail "delete $old"; done
world_is "[x['id'] for x in w['worlds']] == ['w1']" || fail "only the first world is left"
pass "a world goes out as a zip and comes back; one from a newer Minecraft is refused; an uploaded world runs"

nb-game report >/tmp/world.json
world_is "w['running'] and w['names'] == [] and len(w['worlds']) == 1 and any('Done' in line for line in w['log'])" || fail "nb-game report"
ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-minecraft.yml >/tmp/fourth.log 2>&1 || { cat /tmp/fourth.log; fail "fourth run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/fourth.log || { grep -B3 changed: /tmp/fourth.log; fail "the role changed something after the worlds changed"; }
systemctl stop minecraft
pass "nb-game report says what the page shows; the role leaves the worlds alone"
