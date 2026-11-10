#!/usr/bin/env bash
# Container check for the game_factorio role, run by tools/check-roles:
# converge twice with the variables apply passes (tests/vars.json), then
# run the real server as its unit does behind a stand-in for the egress
# proxy and prove that it reaches Factorio's sign-in service only through
# the proxy, runs the base game without the expansion's mods, answers
# nb-game over RCON on the loopback address alone, takes console lines and
# saves when stopped.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

tests=/ansible/roles/game_factorio/tests
# The egress proxy with this guest's allow-list (catalog.yaml's
# services.factorio.proxy_allow, less the download's hosts).
python3 /ansible/roles/game_server/tests/connect-proxy.py 3128 auth.factorio.com >/tmp/proxy.out 2>&1 &
here='{"game_factorio_proxy": "http://127.0.0.1:3128", "game_factorio_manage_services": false, "game_server_manage_services": false}'
converge game-factorio.yml -e "@$tests/vars.json" -e "$here"

for unit in factorio.service nb-game-metrics.service nb-game-metrics.timer; do
  systemd-analyze verify "/etc/systemd/system/$unit" >/tmp/unit.log 2>&1 || { cat /tmp/unit.log; fail "$unit"; }
done
pass "the units pass systemd-analyze"
[ "$(stat -c '%U:%G %a' /etc/factorio/rcon-password)" = "root:game 640" ] || fail "the RCON password is root's, readable by the server"
[ "$(stat -c '%U:%G %a' /etc/factorio)" = "root:game 750" ] || fail "/etc/factorio is closed to others"
[ "$(stat -c '%U:%G %a' /var/lib/factorio/saves)" = "game:game 750" ] || fail "the saves are the game user's alone"
if runuser -u game -- find /opt/factorio /etc/factorio -writable | grep -q .; then fail "the game user can write the server's files"; fi
test -s /var/lib/factorio/saves/nullbill.zip || fail "the role made a first map"
pass "the server's files are root's and read-only to it, the saves its own; a first map is made"
# The settings the page offers by name (games.py's OPTIONS; a test keeps
# this list equal to it): Wube's own example file names each.
options="name description max_players game_password auto_pause autosave_interval afk_autokick_interval"
python3 - /opt/factorio/*/data/server-settings.example.json "$options" <<'PY' || fail "Wube's example settings name every setting the page offers"
import json
import sys

known = json.load(open(sys.argv[1]))
missing = [key for key in sys.argv[2].split() if key not in known]
if missing:
    sys.exit(f"not in {sys.argv[1]}: {', '.join(missing)}")
PY
pass "the server knows every setting the page offers by name"
mods() {
  python3 -c 'import json, sys; print(" ".join(sorted(m["name"] for m in json.load(open("/var/lib/factorio/mods/mod-list.json"))["mods"] if m["enabled"])))'
}
[ "$(mods)" = base ] || { cat /var/lib/factorio/mods/mod-list.json; fail "only the base game is on"; }
pass "space_age false: only the base game's mod is on, so nobody needs the expansion"

# As in the games zone: the game user has no way out but the proxy.
loopback_only game
pass "the game user reaches nothing but this guest's own address"

if nb-game status | grep -q '"running": true'; then fail "nb-game says a server runs before one does"; fi
start_game factorio
nb-game wait 120 || { tail -40 /tmp/factorio.log; fail "the server answers"; }
version=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["game_factorio_version"])' "$tests/vars.json")
nb-game status | python3 -c 'import json, sys; s = json.load(sys.stdin); assert (s["game"], s["version"], s["running"], s["players"]) == ("factorio", sys.argv[1], True, 0), s' "$version" \
  || fail "nb-game status"
grep -q "Loading map /var/lib/factorio/saves/nullbill.zip" /tmp/factorio.log || { head -40 /tmp/factorio.log; fail "the server loaded the role's map"; }
pass "the server starts as its unit starts it and answers nb-game over RCON: version $version, no players"
grep -q '^CONNECT auth.factorio.com:443 ' /tmp/proxy.log || { cat /tmp/proxy.log; tail -30 /tmp/factorio.log; fail "the server asked Factorio's sign-in service through the proxy"; }
pass "the server reaches Factorio's sign-in service through the proxy"

# Port 34197 (858B) for players on every address, RCON's 27015 (6987) on
# the loopback address alone.
grep -Eq '^ *[0-9]+: 00000000:8595 ' /proc/net/udp || { cat /proc/net/udp; fail "the server listens for players on UDP 34197"; }
[ "$(awk '$2 ~ /:6987$/ && $4 == "0A" {print $2}' /proc/net/tcp)" = "0100007F:6987" ] || { cat /proc/net/tcp; fail "RCON listens on 127.0.0.1 alone"; }
pass "players' port is open; RCON listens on the loopback address alone"

nb-game console "hello from nb-game"
game_says factorio '\[CHAT\] <server>: hello from nb-game' 15 || fail "the console line reached the server"
# How apply makes an admin.
nb-game console "/promote owner_a"
game_says factorio '\[PROMOTE\] owner_a was added to the admin list' 15 || fail "/promote on the console"
python3 -c 'import json; assert json.load(open("/var/lib/factorio/server-adminlist.json")) == ["owner_a"]' \
  || { cat /var/lib/factorio/server-adminlist.json; fail "server-adminlist.json names the admin"; }
pass "nb-game types on the server's console; /promote makes an admin and server-adminlist.json names them"

nb-game metrics
grep -qx 'nb_game_up{game="factorio"} 1' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_up 1"
grep -qx 'nb_game_players{game="factorio"} 0' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_players 0"
[ -z "$(/etc/nullbill/busy.d/game)" ] || fail "an empty server leaves its guest idle"
pass "metrics: up, no players"

# The server rewrote its mod list in its own layout; the role finds nothing
# to change.
ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-factorio.yml >/tmp/third.log 2>&1 || { cat /tmp/third.log; fail "third run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/third.log || { grep -B3 changed: /tmp/third.log; fail "the role changed a running server's files"; }
[ "$(mods)" = base ] || fail "the base game alone is still on"
pass "the role changes nothing under a running server"

# The unit stops it with SIGTERM: the server saves the map and goes.
saved=$(stat -c %Y /var/lib/factorio/saves/nullbill.zip)
sleep 1
pid=$(pidof factorio)
kill -TERM "$pid"
for _ in $(seq 60); do test -e "/proc/$pid" || break; sleep 0.5; done
test ! -e "/proc/$pid" || fail "the server is still running after SIGTERM"
grep -q 'Goodbye' /tmp/factorio.log || { tail -20 /tmp/factorio.log; fail "the server ended in order"; }
[ "$(stat -c %Y /var/lib/factorio/saves/nullbill.zip)" -gt "$saved" ] || { ls -la /var/lib/factorio/saves; tail -20 /tmp/factorio.log; fail "the server saved its map"; }
nb-game metrics
grep -qx 'nb_game_up{game="factorio"} 0' /var/lib/prometheus/node-exporter/nullbill-game.prom || fail "nb_game_up 0 once stopped"
pass "SIGTERM has the server save its map and exit; metrics then say it is down"

# With the expansion: every mod the server brings is on.
ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" -e '{"game_factorio_space_age": true}' /ansible/game-factorio.yml >/tmp/space.log 2>&1 \
  || { cat /tmp/space.log; fail "run with space_age"; }
[ "$(mods)" = "base elevated-rails quality recycler space-age" ] || { cat /var/lib/factorio/mods/mod-list.json; fail "space_age true turns the expansion's mods on"; }
pass "space_age true: the expansion's mods are on"

# Worlds (docs/adr/0019-game-worlds.md), with a stand-in for systemctl
# that starts and stops the real server as its unit does. What New map
# takes (games.py's NEW_WORLD; a test keeps these lists equal to it), and
# Wube's own list of presets names each the page offers:
new_world="seed preset"
presets="rich-resources marathon death-world death-world-marathon rail-world ribbon-world island"
for preset in $presets; do
  grep -Fq "[\"$preset\"] =" /opt/factorio/*/data/base/prototypes/map-gen-presets.lua || fail "the server has no map preset $preset"
done
pass "the server knows every map preset the page offers"
ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-factorio.yml >/tmp/base.log 2>&1 || { cat /tmp/base.log; fail "run without space_age"; }
fake_systemctl factorio
systemctl start factorio
nb-game wait 120 || fail "the server answers again"
world list
world_is "[(x['id'], x['title'], x['active'], x['origin']) for x in w['worlds']] == [('w1', 'First world', True, 'setup')] and sorted(w['options']) == sorted('$new_world'.split())" \
  || fail "the map the role made is the first"

world new "Rails" seed=123456 preset=rail-world || { cat /tmp/world.json; tail -20 /tmp/factorio.log; fail "a new map"; }
world_is "w['id'] == 'w2' and [(x['title'], x['active']) for x in w['worlds']] == [('First world', False), ('Rails', True)]" || fail "the new map runs"
grep -q "Loading map /var/lib/factorio/saves/world.zip" /tmp/factorio.log || fail "the server loaded the new map"
nb-game console "/seed"
game_says factorio '^123456$' 15 || fail "the new map has the seed asked for"
test -s /var/lib/factorio/kept-worlds/w1/nullbill.zip || fail "the first map is kept"
if runuser -u game -- ls /var/lib/factorio/kept-worlds >/dev/null 2>&1; then fail "the server reads the maps that do not run"; fi
pass "nb-game world new: the real server makes a map with the seed and preset asked for; the first map is kept, out of the server's reach"

# A map Factorio cannot make: the one that ran runs again.
if world new "Lava" preset=lava; then fail "made a map from a preset that does not exist"; fi
world_is "'Rails runs again' in w['error'] and 'lava' in w['error']" || fail "says what Factorio said, and what runs"
[ "$(nb-game players)" = 0 ] || fail "the server answers after a map that was not made"
world list
world_is "[(x['title'], x['active']) for x in w['worlds']] == [('First world', False), ('Rails', True)]" || fail "nothing is left of the map that was not made"
pass "a map the server cannot make changes nothing: the earlier one runs again"

# Out and in again: the running map is saved first, and what comes back is the file that went out.
world export w2 || { cat /tmp/world.json; fail "export of the running map"; }
out=$(python3 -c 'import json; print(json.load(open("/tmp/world.json"))["file"])')
world_is "w['name'] == 'Rails.zip'" || fail "the export's name"
python3 -c 'import sys, zipfile; assert "world/level.dat0" in zipfile.ZipFile(sys.argv[1]).namelist()' "$out" || fail "the export is a whole save"
size=$(stat -c %s "$out")
world receive "$size" "Came back" <"$out" || { cat /tmp/world.json; fail "upload of a save"; }
world_is "w['id'] == 'w3' and w['worlds'][2]['origin'] == 'upload'" || fail "an upload joins the kept maps"
cmp "$out" /var/lib/factorio/kept-worlds/w3/Came-back.zip || fail "the upload is kept as it came"
world sent "$out"
if head -c 4096 /dev/urandom | world receive 4096 "Not a save"; then fail "took noise for a save"; fi
world use w3 || { cat /tmp/world.json; tail -20 /tmp/factorio.log; fail "running an uploaded map"; }
grep -q "Loading map /var/lib/factorio/saves/Came-back.zip" /tmp/factorio.log || fail "the server loaded the uploaded map"
world use w1 || fail "back to the first map"
world delete w3 || fail "delete"
pass "a map goes out as a file and comes back as it went; noise is refused; an uploaded map runs"

ansible-playbook -i localhost, -c local -e "@$tests/vars.json" -e "$here" /ansible/game-factorio.yml >/tmp/fourth.log 2>&1 || { cat /tmp/fourth.log; fail "fourth run"; }
grep -Eq 'localhost +: ok=[0-9]+ +changed=0 ' /tmp/fourth.log || { grep -B3 changed: /tmp/fourth.log; fail "the role changed something after the maps changed"; }
systemctl stop factorio
pass "the role leaves the maps alone"
