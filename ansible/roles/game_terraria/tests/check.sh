#!/usr/bin/env bash
# Container check for the game_terraria role, run by tools/check-roles:
# converge twice with the variables apply passes (tests/vars.json), then
# run the real server as its unit does and prove that it makes its world
# where the role says, counts a connected player, takes console lines from
# nb-game and saves when stopped.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

tests=/ansible/roles/game_terraria/tests
here='{"game_terraria_manage_services": false, "game_server_manage_services": false}'
converge game-terraria.yml -e "@$tests/vars.json" -e "$here"

for unit in terraria.service nb-game-metrics.service nb-game-metrics.timer; do
  systemd-analyze verify "/etc/systemd/system/$unit" >/tmp/unit.log 2>&1 || { cat /tmp/unit.log; fail "$unit"; }
done
pass "the units pass systemd-analyze"
[ "$(stat -c '%U:%G %a' /etc/terraria/serverconfig.txt)" = "root:game 640" ] || fail "the settings are root's, readable by the server"
[ "$(stat -c '%U:%G %a' /var/lib/terraria/worlds)" = "game:game 750" ] || fail "the worlds are the game user's alone"
if runuser -u game -- find /opt/terraria -writable | grep -q .; then fail "the game user can write the server's files"; fi
if compgen -G "/opt/terraria/*/*/Windows" >/dev/null; then fail "only the Linux server is unpacked"; fi
pass "the server's files are root's and read-only to it, the worlds its own"
for line in port=7777 upnp=0 worldname=Fixture autocreate=1 world=/var/lib/terraria/worlds/world.wld; do
  grep -qx "$line" /etc/terraria/serverconfig.txt || { cat /etc/terraria/serverconfig.txt; fail "serverconfig.txt has $line"; }
done
pass "the settings from config.yaml are in the server's config file"
# The settings the page offers by name (games.py's OPTIONS; a test keeps
# this list equal to it): Re-Logic's own example file, in the archive,
# names each.
options="worldname autocreate difficulty seed maxplayers password motd"
python3 - /opt/terraria/terraria-server-*.zip "$options" <<'PY' || fail "Re-Logic's example config names every setting the page offers"
import sys
import zipfile

archive = zipfile.ZipFile(sys.argv[1])
(example,) = [n for n in archive.namelist() if n.endswith("/serverconfig.txt")]
known = {line[1:].split("=")[0] for line in archive.read(example).decode("utf-8", "replace").splitlines() if line.startswith("#") and "=" in line}
missing = [key for key in sys.argv[2].split() if key not in known]
if missing:
    sys.exit(f"not in {example}: {', '.join(missing)}")
PY
pass "the server knows every setting the page offers by name"

# As in the games zone: the game user has no way out at all.
loopback_only game
pass "the game user reaches nothing but this guest's own address"

if nb-game status | grep -q '"running": true'; then fail "nb-game says a server runs before one does"; fi
[ "$(stat -c '%U %a' /etc/nullbill/busy.d/game)" = "root 755" ] || fail "busy.d/game is root's"
[ "$(/etc/nullbill/busy.d/game)" = "Terraria does not answer" ] || fail "a server that does not answer keeps its guest on"
start_game terraria
nb-game wait 300 || { tail -40 /tmp/terraria.log; fail "the server answers"; }
version=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["game_terraria_version"])' "$tests/vars.json")
nb-game status | python3 -c 'import json, sys; s = json.load(sys.stdin); assert (s["game"], s["version"], s["running"], s["players"]) == ("terraria", sys.argv[1], True, 0), s' "$version" \
  || fail "nb-game status"
test -s /var/lib/terraria/worlds/world.wld || { ls -la /var/lib/terraria /var/lib/terraria/worlds; fail "the world is in /var/lib/terraria/worlds"; }
pass "the server starts as its unit starts it, makes its world and answers nb-game: version $version, no players"

# A connection to the port, as a player's game holds one.
python3 -c 'import socket, time; s = socket.create_connection(("127.0.0.1", 7777)); time.sleep(8)' &
player=$!
sleep 2
[ "$(nb-game players)" = 1 ] || fail "one player connected"
[ "$(/etc/nullbill/busy.d/game)" = "1 player in Terraria" ] || fail "the guest is in use while a player is connected"
nb-game metrics
grep -qx 'nb_game_players{game="terraria"} 1' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_players 1"
wait "$player"
sleep 1
[ "$(nb-game players)" = 0 ] || fail "nobody connected once the player left"
[ -z "$(/etc/nullbill/busy.d/game)" ] || fail "the guest is idle once the player left"
nb-game metrics
grep -qx 'nb_game_up{game="terraria"} 1' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_up 1"
grep -qx 'nb_game_players{game="terraria"} 0' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_players 0"
pass "nb-game counts a connected player, and none once they left; metrics and busy.d follow"

nb-game console "say hello from nb-game"
game_says terraria '<Server> hello from nb-game' 15 || fail "the console line reached the server"
pass "nb-game types on the server's console"

ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-terraria.yml >/tmp/third.log 2>&1 || { cat /tmp/third.log; fail "third run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/third.log || { grep -B3 changed: /tmp/third.log; fail "the role changed a running server's files"; }
pass "the role changes nothing under a running server"

# ExecStop: "exit" on the console; the server saves the world and goes.
saved=$(stat -c %Y /var/lib/terraria/worlds/world.wld)
sleep 1
pid=$(pidof TerrariaServer.bin.x86_64)
nb-game stop "$pid"
test ! -e "/proc/$pid" || fail "the server is still running after nb-game stop"
[ "$(stat -c %Y /var/lib/terraria/worlds/world.wld)" -gt "$saved" ] || { tail -20 /tmp/terraria.log; fail "the server saved its world"; }
nb-game metrics
grep -qx 'nb_game_up{game="terraria"} 0' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_up 0 once stopped"
pass "nb-game stop has the server save its world and exit; metrics then say it is down"

# Worlds (docs/adr/0019-game-worlds.md), with a stand-in for systemctl
# that starts and stops the real server as its unit does. What New world
# takes (games.py's NEW_WORLD; a test keeps this list equal to it):
new_world="autocreate difficulty seed"
fake_systemctl terraria
systemctl start terraria
nb-game wait 300 || fail "the server answers again"
world list
world_is "[(x['id'], x['title'], x['active'], x['origin']) for x in w['worlds']] == [('w1', 'First world', True, 'setup')] and sorted(w['options']) == sorted('$new_world'.split())" \
  || fail "the world the role's server made is the first"

# What a .wld says of itself: its name, seed, width and mode.
wld() {
  python3 - "$1" <<'PY'
import struct
import sys

data = open(sys.argv[1], "rb").read()
at = struct.unpack_from("<i", data, 26)[0]


def text():
    global at
    size = shift = 0
    while True:
        byte = data[at]
        at += 1
        size |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
    at += size
    return data[at - size : at].decode()


name, seed = text(), text()
at += 8 + 16 + 4 + 16
height, width, mode = struct.unpack_from("<iii", data, at)
print(f"{name}|{seed}|{width}|{mode}")
PY
}
# The first world: small and Classic, as tests/vars.json's settings say, with a seed of the server's own.
first_says=$(wld /var/lib/terraria/worlds/world.wld)
[[ $first_says == "Fixture|"*"|4200|0" ]] || { echo "$first_says"; fail "reading the first world's header"; }

world new "Expert run" difficulty=1 seed=nullbill autocreate=1 || { cat /tmp/world.json; tail -20 /tmp/terraria.log; fail "a new world"; }
world_is "w['id'] == 'w2' and [(x['title'], x['active']) for x in w['worlds']] == [('First world', False), ('Expert run', True)]" || fail "the new world runs"
[ "$(nb-game players)" = 0 ] || fail "the server answers with the new world"
[ "$(wld /var/lib/terraria/worlds/world.wld)" = "Expert run|nullbill|4200|1" ] || { wld /var/lib/terraria/worlds/world.wld; fail "the new world has the name, seed, size and difficulty asked for"; }
# The server saved it once more as it stopped; it is the same world.
[ "$(wld /var/lib/terraria/kept-worlds/w1/world.wld)" = "$first_says" ] || fail "the first world is kept"
test ! -e /etc/terraria/new-world.txt || fail "the new world's config is gone once it is made"
[ "$(stat -c '%U:%G %a' /var/lib/terraria/kept-worlds)" = "root:root 700" ] || fail "kept worlds are root's"
if runuser -u game -- ls /var/lib/terraria/kept-worlds >/dev/null 2>&1; then fail "the server reads the worlds that do not run"; fi
pass "nb-game world new: the real server makes an Expert world with the seed asked for; the first world is kept, out of the server's reach"

if world new "Sideways" worldpath=/etc; then fail "an option the game does not take"; fi
world_is "not w['ok'] and 'is not something a new world of this game takes' in w['error']" || fail "says which options it takes"
if world delete w2; then fail "deleted the world that runs"; fi
[ "$(pidof TerrariaServer.bin.x86_64)" ] || fail "a refusal leaves the server running"
pass "what is asked wrongly is refused, and the server keeps running"

# A player in the game: nothing changes unless the caller says to disconnect them.
python3 -c 'import socket, time; s = socket.create_connection(("127.0.0.1", 7777)); time.sleep(6)' &
player=$!
sleep 2
if world use w1; then fail "changed the world under a player"; fi
world_is "w == {'ok': False, 'error': '1 player is in Terraria and would be disconnected', 'players': 1}" || fail "says who is in the way"
wait "$player"
sleep 1
world use w1 || { cat /tmp/world.json; fail "back to the first world"; }
world_is "[x['active'] for x in w['worlds']] == [True, False]" || fail "the first world runs again"
[ "$(wld /var/lib/terraria/worlds/world.wld)" = "$first_says" ] || fail "the first world is the one that was set aside"
pass "a player in the game stops a change; once they left, the first world runs again"

# Out and in again: the running world is saved first, and what comes back is the file that went out.
world export w1 || { cat /tmp/world.json; fail "export of the running world"; }
out=$(python3 -c 'import json; print(json.load(open("/tmp/world.json"))["file"])')
world_is "w['name'] == 'First-world.wld' and w['bytes'] > 1000000" || fail "the export's name and size"
[ "$(stat -c '%U %a' "$out")" = "root 600" ] || fail "the file waiting for the page is root's alone"
size=$(stat -c %s "$out")
world receive "$size" "Came back" <"$out" || { cat /tmp/world.json; fail "upload of a world"; }
world_is "w['id'] == 'w3' and w['worlds'][2]['origin'] == 'upload' and not w['worlds'][2]['active']" || fail "an upload joins the kept worlds"
cmp "$out" /var/lib/terraria/kept-worlds/w3/world.wld || fail "the upload is kept as it came"
world sent "$out"
test ! -e "$out" || fail "the sent file is removed"
if head -c 4096 /dev/urandom | world receive 4096 "Not a world"; then fail "took noise for a world"; fi
world_is "'that is not a Terraria world' in w['error']" || fail "says what a world file is"
world use w3 || { cat /tmp/world.json; fail "running an uploaded world"; }
[ "$(nb-game players)" = 0 ] || fail "the server answers with the uploaded world"
world use w1 || fail "and back"
world rename w3 "Renamed" || fail "rename"
world delete w3 || fail "delete"
world_is "w['deleted'] == 'Renamed' and [x['id'] for x in w['worlds']] == ['w1', 'w2']" || fail "deleted one world"
test ! -e /var/lib/terraria/kept-worlds/w3 || fail "the deleted world's files are gone"
pass "a world goes out as a file and comes back as it went; noise is refused; an uploaded world runs"

nb-game report >/tmp/world.json
world_is "w['running'] and w['active'] and w['players'] == 0 and len(w['worlds']) == 2 and any('Server started' in line for line in w['log'])" \
  || fail "nb-game report"
ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-terraria.yml >/tmp/fourth.log 2>&1 || { cat /tmp/fourth.log; fail "fourth run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/fourth.log || { grep -B3 changed: /tmp/fourth.log; fail "the role changed something after the worlds changed"; }
systemctl stop terraria
pass "nb-game report says what the page shows; the role leaves the worlds alone"
