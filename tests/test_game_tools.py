"""The scripts the game roles install (ansible/roles/game_*/files), against
stand-ins on localhost: nb-game's three ways to count players, its console
and metrics, nb-properties, nb-factorio-mods and nb-tls-tunnel's reading of
a TLS handshake. The role checks run them against the real servers."""

import importlib.machinery
import importlib.util
import io
import json
import os
import socket
import ssl
import struct
import sys
import threading
from pathlib import Path

import pytest

ROLES = Path(__file__).parent.parent / "ansible" / "roles"


def load(monkeypatch, role: str, name: str):
    # No __pycache__ next to the script the role copies.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROLES / role / "files" / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


@pytest.fixture
def nb_game(monkeypatch, tmp_path):
    module = load(monkeypatch, "game_server", "nb-game")
    monkeypatch.setattr(module, "ACTIVE", tmp_path / "state" / "game-last-active")
    monkeypatch.setattr(module, "TEXTFILES", tmp_path)
    monkeypatch.setattr(module, "TIMEOUT_S", 2)
    return module


def serve(answer):
    """A TCP server on localhost that hands each connection to answer."""
    server = socket.create_server(("127.0.0.1", 0))

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                try:
                    answer(conn)
                except (OSError, EOFError):
                    pass

    threading.Thread(target=loop, daemon=True).start()
    return server


def test_minecraft_players_come_from_the_server_list_ping(nb_game):
    asked = []

    def answer(conn):
        asked.append(conn.recv(1024))
        status = json.dumps({"version": {"name": "26.3", "protocol": 777}, "players": {"max": 20, "online": 3}}).encode()
        body = b"\x00" + nb_game.varint(len(status)) + status
        conn.sendall(nb_game.varint(len(body)) + body)

    with serve(answer) as server:
        port = server.getsockname()[1]
        assert nb_game.players({"port": port, "players": {"how": "minecraft"}}) == 3
    # A handshake for status (next state 1) and the status request.
    assert asked[0].endswith(struct.pack(">H", port) + b"\x01\x01\x00")


def test_a_server_that_is_not_minecraft_is_not_answering(nb_game):
    with serve(lambda conn: conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")) as server:
        port = server.getsockname()[1]
        with pytest.raises(nb_game.NotAnswering, match="not a status answer"):
            nb_game.players({"port": port, "players": {"how": "minecraft"}})
    # A port nothing listens on.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    with pytest.raises(nb_game.NotAnswering, match="Connection refused"):
        nb_game.players({"port": port, "players": {"how": "minecraft"}})


TCP_TABLE = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:1E61 00000000:0000 0A 00000000:00000000 00:00000000 00000000   997        0 1 1 0 100 0 0 10 0
   1: 0A2A081A:1E61 0A2A060B:C001 01 00000000:00000000 00:00000000 00000000   997        0 2 1 0 20 4 30 10 -1
   2: 0A2A081A:1E61 0A2A060B:C002 01 00000000:00000000 00:00000000 00000000   997        0 3 1 0 20 4 30 10 -1
   3: 0A2A081A:1E61 0A2A060B:C003 08 00000000:00000000 00:00000000 00000000   997        0 4 1 0 20 4 30 10 -1
   4: 0A2A081A:23F0 0A2A010E:D001 01 00000000:00000000 00:00000000 00000000     0        0 5 1 0 20 4 30 10 -1
"""


def test_terraria_players_are_the_established_connections_to_its_port(nb_game, monkeypatch, tmp_path):
    (tmp_path / "tcp").write_text(TCP_TABLE)
    monkeypatch.setattr(nb_game, "TABLES", (str(tmp_path / "tcp"), str(tmp_path / "tcp6")))
    game = {"port": 7777, "players": {"how": "connections"}}
    # Two established; one closing and node_exporter's scrape do not count.
    assert nb_game.players(game) == 2
    with pytest.raises(nb_game.NotAnswering, match="nothing listens on port 7778"):
        nb_game.players(game | {"port": 7778})


def rcon_server(password: str, answer: str, seen: list):
    def packet(ident: int, kind: int, body: str) -> bytes:
        data = struct.pack("<ii", ident, kind) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def read(conn):
        (size,) = struct.unpack("<i", conn.recv(4))
        data = conn.recv(size)
        return *struct.unpack("<ii", data[:8]), data[8:-2].decode()

    def serve_one(conn):
        ident, kind, body = read(conn)
        seen.append((kind, body))
        conn.sendall(packet(ident if body == password else -1, 2, ""))
        if body != password:
            return
        ident, kind, body = read(conn)
        seen.append((kind, body))
        conn.sendall(packet(ident, 0, answer))

    return serve(serve_one)


def test_factorio_players_come_from_rcon_with_its_password(nb_game, tmp_path):
    (tmp_path / "rcon-password").write_text("check-rcon-password\n")
    seen = []
    ask = {
        "how": "rcon",
        "password_file": str(tmp_path / "rcon-password"),
        "command": "/players online count",
        "pattern": "players [(]([0-9]+)[)]",
    }
    with rcon_server("check-rcon-password", "Online players (2):\n", seen) as server:
        assert nb_game.players({"port": 34197, "players": ask | {"port": server.getsockname()[1]}}) == 2
    assert seen == [(3, "check-rcon-password"), (2, "/players online count")]
    with rcon_server("another-password", "", []) as server:
        with pytest.raises(nb_game.NotAnswering, match="refused the RCON password"):
            nb_game.players({"port": 34197, "players": ask | {"port": server.getsockname()[1]}})
    with rcon_server("check-rcon-password", "Unknown command", []) as server:
        with pytest.raises(nb_game.NotAnswering, match="no player count in 'Unknown command'"):
            nb_game.players({"port": 34197, "players": ask | {"port": server.getsockname()[1]}})


def test_the_console_takes_a_line_only_while_a_server_reads_it(nb_game, tmp_path):
    fifo = tmp_path / "console"
    game = {"console": str(fifo), "stop": "stop"}
    with pytest.raises(nb_game.NotAnswering, match="console is not open"):
        nb_game.console(game, "say nobody home")
    os.mkfifo(fifo)
    # Nobody holds the other end: refused at once, not written for later.
    with pytest.raises(nb_game.NotAnswering, match="console is not open"):
        nb_game.console(game, "say nobody home")
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        nb_game.console(game, "say hello")
        assert os.read(reader, 100) == b"say hello\n"
    finally:
        os.close(reader)


def test_admins_are_read_from_either_games_list(nb_game, tmp_path):
    ops = tmp_path / "ops.json"
    assert nb_game.admins({"admins": str(ops)}) == []
    ops.write_text(json.dumps([{"uuid": "x", "name": "Owner_A", "level": 4}, {"uuid": "y", "name": "friend_b", "level": 4}]))
    assert nb_game.admins({"admins": str(ops)}) == ["friend_b", "owner_a"]
    ops.write_text(json.dumps(["Owner-A"]))
    assert nb_game.admins({"admins": str(ops)}) == ["owner-a"]
    ops.write_text("not json")
    with pytest.raises(nb_game.NotAnswering, match="no list of admins"):
        nb_game.admins({"admins": str(ops)})
    # Terraria: no admins, no list.
    with pytest.raises(nb_game.NotAnswering):
        nb_game.admins({})


def test_metrics_keep_the_time_a_player_was_last_seen(nb_game, monkeypatch, tmp_path):
    now, count = [1000], [0]
    monkeypatch.setattr(nb_game.time, "time", lambda: now[0])

    def players(game):
        if count[0] is None:
            raise nb_game.NotAnswering("down")
        return count[0]

    monkeypatch.setattr(nb_game, "players", players)

    def metrics() -> dict:
        nb_game.metrics({"name": "terraria"})
        lines = (tmp_path / "nullbill-game.prom").read_text().splitlines()
        return {line.split("{")[0]: int(line.split()[-1]) for line in lines if not line.startswith("#")}

    # First seen empty: the clock starts now.
    assert metrics() == {"nb_game_up": 1, "nb_game_players": 0, "nb_game_last_active_timestamp_seconds": 1000}
    now[0] = 1600
    assert metrics()["nb_game_last_active_timestamp_seconds"] == 1000
    count[0] = 2
    assert metrics() == {"nb_game_up": 1, "nb_game_players": 2, "nb_game_last_active_timestamp_seconds": 1600}
    now[0], count[0] = 2200, 0
    assert metrics()["nb_game_last_active_timestamp_seconds"] == 1600
    # Down: no players, and the time stands.
    now[0], count[0] = 2800, None
    assert metrics() == {"nb_game_up": 0, "nb_game_players": 0, "nb_game_last_active_timestamp_seconds": 1600}
    assert oct((tmp_path / "nullbill-game.prom").stat().st_mode & 0o777) == "0o644"


def test_status_is_one_json_line_also_for_a_server_that_is_down(nb_game, monkeypatch, tmp_path, capsys):
    config = tmp_path / "game.json"
    config.write_text(
        json.dumps({"name": "minecraft", "title": "Minecraft", "version": "26.3", "port": 1, "players": {"how": "minecraft"}})
    )
    monkeypatch.setattr(nb_game, "CONFIG", config)
    assert nb_game.main(["nb-game", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["game"] == "minecraft" and status["version"] == "26.3" and status["running"] is False and status["players"] == 0
    assert nb_game.main(["nb-game", "players"]) == 1
    assert capsys.readouterr().err.startswith("nb-game: ")
    # A server that does not answer may be making its world: in use.
    assert nb_game.main(["nb-game", "busy"]) == 0
    assert capsys.readouterr().out == "Minecraft does not answer\n"
    assert nb_game.main(["nb-game", "unknown"]) == 2


def test_a_guest_is_in_use_while_anyone_plays_and_idle_once_empty(nb_game, monkeypatch, tmp_path, capsys):
    config = tmp_path / "game.json"
    config.write_text(json.dumps({"name": "terraria", "title": "Terraria", "version": "1", "port": 1, "players": {"how": "connections"}}))
    monkeypatch.setattr(nb_game, "CONFIG", config)
    for count, said in ((2, "2 players in Terraria\n"), (1, "1 player in Terraria\n"), (0, "")):
        monkeypatch.setattr(nb_game, "players", lambda game, count=count: count)
        assert nb_game.main(["nb-game", "busy"]) == 0
        assert capsys.readouterr().out == said


def run_properties(monkeypatch, module, path: Path, values: dict, capsys) -> str:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(values)))
    assert module.main(["nb-properties", str(path)]) == 0
    return capsys.readouterr().out.strip()


def test_properties_change_only_the_keys_asked_for(monkeypatch, tmp_path, capsys):
    module = load(monkeypatch, "game_minecraft", "nb-properties")
    path = tmp_path / "server.properties"
    # As Minecraft writes it: a comment, its own escaping, every option.
    theirs = ["#Minecraft server properties", "#Mon Nov 09 2026", "difficulty=easy", "level-seed=", "motd=A Minecraft Server"]
    path.write_text("\n".join([*theirs, "online-mode=false", "server-port=25565"]) + "\n")
    path.chmod(0o640)
    want = {"online-mode": True, "difficulty": "hard", "server-port": 25565, "max-players": 8, "motd": "Größe: 8\\9 = ?"}
    assert run_properties(monkeypatch, module, path, want, capsys) == "changed"
    lines = path.read_text().splitlines()
    assert lines[:2] == ["#Minecraft server properties", "#Mon Nov 09 2026"]
    assert "difficulty=hard" in lines and "online-mode=true" in lines and "level-seed=" in lines and "max-players=8" in lines
    assert "motd=Gr\\u00f6\\u00dfe: 8\\\\9 = ?" in lines
    assert oct(path.stat().st_mode & 0o777) == "0o640"
    assert run_properties(monkeypatch, module, path, want, capsys) == "same"
    # The server rewrote it in its own order and escaping: still the same.
    path.write_text("#Minecraft server properties\n" + "\n".join(sorted(lines[2:], reverse=True)).replace("motd=", "motd = ") + "\n")
    assert run_properties(monkeypatch, module, path, want, capsys) == "same"
    # A new file gets its keys in one order every time.
    fresh = tmp_path / "new.properties"
    assert run_properties(monkeypatch, module, fresh, want, capsys) == "changed"
    assert [line.split("=")[0] for line in fresh.read_text().splitlines()] == sorted(want)


def test_factorio_mods_follow_the_expansion_and_keep_the_owners(monkeypatch, tmp_path, capsys):
    module = load(monkeypatch, "game_factorio", "nb-factorio-mods")
    data = tmp_path / "data"
    for name, expansion in (("core", False), ("base", False), ("quality", True), ("space-age", True)):
        (data / name).mkdir(parents=True)
        (data / name / "info.json").write_text(json.dumps({"name": name} | ({"expansion_required": True} if expansion else {})))
    mods = tmp_path / "mod-list.json"

    def run(expansion: str) -> tuple[str, dict]:
        assert module.main(["nb-factorio-mods", str(data), str(mods), expansion]) == 0
        return capsys.readouterr().out.strip(), {m["name"]: m["enabled"] for m in json.loads(mods.read_text())["mods"]}

    assert run("off") == ("changed", {"base": True, "quality": False, "space-age": False})
    assert run("off")[0] == "same"
    # Factorio rewrote the list in its own layout, and the owner added a mod.
    mods.write_text(
        json.dumps(
            {
                "mods": [
                    {"name": "space-age", "enabled": False},
                    {"name": "base", "enabled": True},
                    {"name": "quality", "enabled": False},
                    {"name": "owners-mod", "enabled": True},
                ]
            }
        )
    )
    assert run("off")[0] == "same"
    assert run("on") == ("changed", {"space-age": True, "base": True, "quality": True, "owners-mod": True})
    assert module.main(["nb-factorio-mods", str(data), str(mods), "maybe"]) == 2


def client_hello(name: str | None) -> bytes:
    """The handshake message a TLS client opens with, without its record header."""
    context = ssl.create_default_context()
    context.check_hostname = name is not None
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    record = outgoing.read()
    assert record[0] == 22
    return record[5 : 5 + int.from_bytes(record[3:5])]


def test_the_tunnel_reads_the_name_a_tls_client_asks_for(monkeypatch):
    module = load(monkeypatch, "game_minecraft", "nb-tls-tunnel")
    assert module.server_name(client_hello("sessionserver.mojang.com")) == "sessionserver.mojang.com"
    # No name, not a ClientHello, cut short: no tunnel.
    assert module.server_name(client_hello(None)) is None
    assert module.server_name(b"\x02" + client_hello("api.mojang.com")[1:]) is None
    assert module.server_name(client_hello("api.mojang.com")[:60]) is None
    assert module.NAME.fullmatch("api.mojang.com")
    for bad in ("localhost", "api.mojang.com:443", "a b.example", "-x.example", "x..example"):
        assert not module.NAME.fullmatch(bad), bad
