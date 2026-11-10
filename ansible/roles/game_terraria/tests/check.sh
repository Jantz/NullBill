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
