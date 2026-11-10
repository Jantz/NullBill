"""A game server's worlds (docs/adr/0019-game-worlds.md): nb-game's world
commands against a stand-in for the server and its unit, what the page
makes of its form and of nb-game's answers, the page's requests with a
stand-in for the host, and the agent's tools. The role checks run the same
commands against the real servers."""

import gzip
import io
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
import test_chat
import test_web
from fixturedata import games_config
from test_agent_reach import approved, call, chosen, result
from test_chat import kinds
from test_game_tools import ROLES, load
from test_web import built, sign_in, wait

from nullbill import games
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.web import gameroom

agent = test_chat.agent
site = test_web.site
WLD = b"\x17\x01\x00\x00relogic\x02" + b"\x00" * 40


class Server:
    """Stands in for a Terraria server under its unit: started, it makes
    the world its config asks for when it has none, and answers."""

    def __init__(self, game: dict):
        self.game, self.running, self.count, self.broken, self.restarts, self.started_with = game, True, 0, False, 0, []

    def systemctl(self, *args: str) -> subprocess.CompletedProcess:
        out = ""
        if args[0] == "stop":
            self.running = False
        elif args[0] == "start":
            world = self.game["world"]
            new = Path(world["new_config"])
            config = dict(line.split("=", 1) for line in (new if new.exists() else Path(world["config"])).read_text().splitlines())
            self.started_with.append(config)
            place = Path(world["home"]) / world["dir"]
            have = [f.stem for f in place.glob("*.wld")] or [config["worldname"]]
            if self.broken is True or self.broken in have:
                self.restarts += 2
            else:
                if not list(place.glob("*.wld")):
                    (place / f"{config['worldname'].replace(' ', '_')}.wld").write_bytes(WLD)
                self.running = True
        elif args[0] == "show":
            out = f"{self.restarts}\n"
        return subprocess.CompletedProcess(args, 0 if args[0] != "is-active" or self.running else 3, out, "")

    def players(self, game: dict) -> int:
        if not self.running:
            raise self.NotAnswering("nothing listens on 7777")
        return self.count


@pytest.fixture
def guest(monkeypatch, tmp_path):
    """(nb-game, the game's config, its server) for a Terraria server that
    setup made, with its first world."""
    nb = load(monkeypatch, "game_server", "nb-game")
    home = tmp_path / "terraria"
    (home / "worlds").mkdir(parents=True)
    (home / "worlds" / "First.wld").write_bytes(WLD)
    (tmp_path / "serverconfig.txt").write_text("worldname=First\nautocreate=1\ndifficulty=2\nseed=theirs\nport=7777\n")
    game = {
        "name": "terraria",
        "title": "Terraria",
        "version": "1.4.5.8",
        "unit": "terraria",
        "players": {"how": "connections", "port": 7777},
        "world": {
            "how": "terraria",
            "home": str(home),
            "dir": "worlds",
            "options": {"autocreate": "2", "difficulty": "0", "seed": ""},
            "config": str(tmp_path / "serverconfig.txt"),
            "new_config": str(tmp_path / "new-world.txt"),
        },
    }
    server = Server(game)
    server.NotAnswering = nb.NotAnswering
    for name, value in (("WORLDS", tmp_path / "state" / "worlds.json"), ("TRANSFER", tmp_path / "transfer"), ("LOCK", tmp_path / "lock")):
        monkeypatch.setattr(nb, name, value)
    monkeypatch.setattr(nb, "systemctl", server.systemctl)
    monkeypatch.setattr(nb, "players", server.players)
    monkeypatch.setattr(nb, "log", lambda game, lines=60: ["Error: the world did not load"])
    monkeypatch.setattr(nb.time, "sleep", lambda s: None)
    # The files are the game user's on a guest; here they stay the test's.
    monkeypatch.setattr(shutil, "chown", lambda *a, **k: None)
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    return nb, game, server


def titles(listed: dict) -> list[tuple[str, str, bool]]:
    return [(w["id"], w["title"], w["active"]) for w in listed["worlds"]]


def refused(nb, game, *args) -> str:
    with pytest.raises(nb.Refused) as caught:
        nb.world(game, list(args))
    return str(caught.value)


def test_the_world_setup_made_is_the_first_and_a_new_one_sets_it_aside(guest):
    nb, game, server = guest
    home = Path(game["world"]["home"])
    listed = nb.world(game, ["list"])
    assert titles(listed) == [("w1", "First world", True)] and listed["worlds"][0]["origin"] == "setup"
    assert listed["most"] == 24 and listed["options"] == ["autocreate", "difficulty", "seed"]
    made = nb.world(game, ["new", "Expert  run", "difficulty=1", "seed=nullbill"])
    assert made["id"] == "w2" and titles(made) == [("w1", "First world", False), ("w2", "Expert run", True)]
    assert made["worlds"][1]["options"] == {"difficulty": "1", "seed": "nullbill"} and made["worlds"][1]["origin"] == "new"
    # The server started once with the new world's options and name, beside its own settings.
    assert server.started_with == [{"worldname": "Expert run", "autocreate": "2", "difficulty": "1", "port": "7777", "seed": "nullbill"}]
    assert not Path(game["world"]["new_config"]).exists() and server.running
    # What is not asked for is the game's own start, not the server's settings for its first world.
    nb.world(game, ["new", "Plain"])
    assert server.started_with[-1] == {"worldname": "Plain", "autocreate": "2", "difficulty": "0", "port": "7777"}
    nb.world(game, ["use", "w2"])
    nb.world(game, ["delete", "w3"])
    # The first world is whole, where the server cannot read it.
    assert (home / "kept-worlds" / "w1" / "First.wld").read_bytes() == WLD
    assert [f.name for f in (home / "worlds").iterdir()] == ["Expert_run.wld"]
    # And runs again when asked; nothing was lost on the way.
    assert titles(nb.world(game, ["use", "w1"])) == [("w1", "First world", True), ("w2", "Expert run", False)]
    assert (home / "worlds" / "First.wld").exists() and (home / "kept-worlds" / "w2" / "Expert_run.wld").exists()
    assert server.started_with[-1]["worldname"] == "First"


def test_what_is_asked_wrongly_is_refused_before_the_server_stops(guest):
    nb, game, server = guest
    nb.world(game, ["new", "Second"])
    stops = len(server.started_with)
    assert "another world of this server is called second already" in refused(nb, game, "new", "second")
    assert "a world's name is 1 to 40 characters" in refused(nb, game, "new", "x" * 41)
    assert "'worldpath' is not something a new world of this game takes; it takes autocreate, difficulty, seed" in refused(
        nb, game, "new", "Third", "worldpath=/etc"
    )
    assert "one line" in refused(nb, game, "new", "Third", "seed=a\nport=1")
    assert "Second is the world that runs" in refused(nb, game, "use", "w2")
    assert "run another before you delete it" in refused(nb, game, "delete", "w2")
    assert "this server has no world 'w9'" in refused(nb, game, "use", "w9")
    assert "this server has no world '../../etc'" in refused(nb, game, "delete", "../../etc")
    assert "list, new, use" in refused(nb, game, "burn")
    assert len(server.started_with) == stops and server.running


def test_players_are_disconnected_only_when_the_caller_says_so(guest):
    nb, game, server = guest
    server.count = 2
    with pytest.raises(nb.Refused) as caught:
        nb.world(game, ["new", "Second"])
    assert str(caught.value) == "2 players are in Terraria and would be disconnected" and caught.value.more == {"players": 2}
    assert titles(nb.world(game, ["list"])) == [("w1", "First world", True)] and server.started_with == []
    assert titles(nb.world(game, ["new", "Second", "--kick"]))[-1] == ("w2", "Second", True)
    server.count = 1
    assert "1 player is in Terraria" in refused(nb, game, "use", "w1")


def test_a_world_that_does_not_start_leaves_the_earlier_one_running(guest):
    nb, game, server = guest
    home = Path(game["world"]["home"])
    nb.world(game, ["new", "Second"])
    server.broken = "Third"
    with pytest.raises(nb.Refused) as caught:
        nb.world(game, ["new", "Third"])
    assert str(caught.value) == "Terraria stopped again each time it was started; Second runs again"
    assert caught.value.more == {"log": ["Error: the world did not load"]}
    assert titles(nb.world(game, ["list"])) == [("w1", "First world", False), ("w2", "Second", True)]
    assert [f.name for f in (home / "worlds").iterdir()] == ["Second.wld"] and not Path(game["world"]["new_config"]).exists()
    assert server.running and server.players(game) == 0
    # A kept world that does not load goes back among the kept ones.
    server.broken = "First"
    with pytest.raises(nb.Refused, match="Second runs again"):
        nb.world(game, ["use", "w1"])
    assert (home / "kept-worlds" / "w1" / "First.wld").exists() and (home / "worlds" / "Second.wld").exists()
    assert json.loads(nb.WORLDS.read_text())["active"] == "w2" and server.running
    # A server that does not come back either is said to be down, with the world in its place.
    server.broken = True
    with pytest.raises(nb.Refused, match="Second is back in its place, but the server does not answer"):
        nb.world(game, ["new", "Third"])
    assert (home / "worlds" / "Second.wld").exists() and not server.running
    server.broken = False
    assert nb.world(game, ["new", "Third"])["id"] == "w3"


def test_rename_and_delete_touch_one_world(guest):
    nb, game, _ = guest
    home = Path(game["world"]["home"])
    nb.world(game, ["new", "Second"])
    assert titles(nb.world(game, ["rename", "w1", "Before the wall"]))[0] == ("w1", "Before the wall", False)
    assert "another world of this server is called Second already" in refused(nb, game, "rename", "w1", "Second")
    # Its own name again is no clash.
    nb.world(game, ["rename", "w1", "before the wall"])
    gone = nb.world(game, ["delete", "w1"])
    assert gone["deleted"] == "before the wall" and titles(gone) == [("w2", "Second", True)]
    assert not (home / "kept-worlds" / "w1").exists() and (home / "worlds" / "Second.wld").exists()


def test_a_server_keeps_only_so_many_worlds(guest, monkeypatch):
    nb, game, _ = guest
    monkeypatch.setattr(nb, "MAX_WORLDS", 2)
    nb.world(game, ["new", "Second"])
    assert "keeps at most 2 worlds" in refused(nb, game, "new", "Third")
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(WLD)))
    assert "keeps at most 2 worlds" in refused(nb, game, "receive", str(len(WLD)), "Third")


def test_one_change_at_a_time(guest):
    import fcntl

    nb, game, _ = guest
    nb.LOCK.touch()
    with nb.LOCK.open() as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert "another change to this server's worlds is still running" in refused(nb, game, "list")


def send(monkeypatch, nb, game, data: bytes, title="From my computer", size=None) -> dict:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    return nb.world(game, ["receive", str(len(data) if size is None else size), title])


def test_an_uploaded_world_joins_the_kept_ones_and_goes_back_out_as_it_came(guest, monkeypatch):
    nb, game, server = guest
    home = Path(game["world"]["home"])
    theirs = WLD + b"their world"
    got = send(monkeypatch, nb, game, theirs)
    assert got["id"] == "w2" and titles(got) == [("w1", "First world", True), ("w2", "From my computer", False)]
    assert got["worlds"][1]["origin"] == "upload" and got["worlds"][1]["bytes"] == len(theirs)
    assert (home / "kept-worlds" / "w2" / "world.wld").read_bytes() == theirs and list(nb.TRANSFER.iterdir()) == []
    # The server was not touched: an upload runs only once asked.
    assert server.started_with == [] and (home / "worlds" / "First.wld").exists()
    out = nb.world(game, ["export", "w2"])
    assert out["name"] == "From-my-computer.wld" and out["bytes"] == len(theirs) and Path(out["file"]).read_bytes() == theirs
    assert Path(out["file"]).parent == nb.TRANSFER and re.fullmatch(r"[0-9a-f]{16}\.wld", Path(out["file"]).name)
    assert os.stat(out["file"]).st_mode & 0o777 == 0o600
    # Only a file of this kind in that place is removed when the page says it was sent.
    other = home / "worlds" / "First.wld"
    nb.world(game, ["sent", str(other)])
    nb.world(game, ["sent", str(nb.TRANSFER / "../terraria/worlds/First.wld")])
    assert other.exists()
    nb.world(game, ["sent", out["file"]])
    assert list(nb.TRANSFER.iterdir()) == []


def test_what_is_not_a_world_is_refused_and_leaves_nothing_behind(guest, monkeypatch):
    nb, game, _ = guest
    home = Path(game["world"]["home"])
    for data, size, why in (
        (b"MZ" + b"\x00" * 60, None, "that is not a Terraria world: upload the .wld file from your Worlds folder"),
        (WLD[:11] + b"\x03" + WLD[12:], None, "that is not a Terraria world"),
        (WLD, len(WLD) + 5, "the file did not arrive whole; send it again"),
        (WLD + b"more", len(WLD), "the file did not arrive whole; send it again"),
        (b"", 0, "no room"),
    ):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
        assert why in refused(nb, game, "receive", str(len(data) if size is None else size), "Theirs")
    assert titles(nb.world(game, ["list"])) == [("w1", "First world", True)]
    assert list(nb.TRANSFER.iterdir()) == [] and not list((home / "kept-worlds").iterdir())


def level(data_version: int) -> bytes:
    return gzip.compress(b"\x0a\x00\x00\x0a\x00\x04Data\x03\x00\x0bDataVersion" + struct.pack(">i", data_version) + b"\x00\x00")


def zipped(files: dict, link: str | None = None) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        if link:
            info = zipfile.ZipInfo(link)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/shadow")
    return data.getvalue()


@pytest.fixture
def minecraft(guest, tmp_path):
    nb, game, server = guest
    home = tmp_path / "minecraft"
    (home / "world").mkdir(parents=True)
    (home / "world" / "level.dat").write_bytes(level(5023))
    with zipfile.ZipFile(tmp_path / "server.jar", "w") as jar:
        jar.writestr("version.json", json.dumps({"name": "26.3", "world_version": 5023}))
    game |= {"name": "minecraft", "title": "Minecraft"}
    game["world"] = {
        "how": "minecraft",
        "home": str(home),
        "dir": "world",
        "options": {"level-seed": ""},
        "jar": str(tmp_path / "server.jar"),
    }
    server.running = False
    return nb, game, home


def test_a_minecraft_world_is_unpacked_from_its_folder_and_packed_into_one(minecraft, monkeypatch):
    nb, game, home = minecraft
    region = os.urandom(3000)
    world = {"My World/level.dat": level(4000), "My World/region/r.0.0.mca": region, "My World/session.lock": b"x", "notes.txt": b"n"}
    got = send(monkeypatch, nb, game, zipped(world))
    kept = home / "kept-worlds" / got["id"]
    # The world's folder alone, without the lock of whatever ran it.
    assert sorted(str(f.relative_to(kept)) for f in kept.rglob("*") if f.is_file()) == ["level.dat", "region/r.0.0.mca"]
    assert (kept / "region" / "r.0.0.mca").read_bytes() == region
    # A zip of the folder's contents is a world as well.
    flat = send(monkeypatch, nb, game, zipped({"level.dat": level(5023), "data/x.dat": b"1"}), "Flat zip")
    assert (home / "kept-worlds" / flat["id"] / "data" / "x.dat").exists()
    out = nb.world(game, ["export", got["id"]])
    with zipfile.ZipFile(out["file"]) as archive:
        assert sorted(archive.namelist()) == ["From-my-computer/level.dat", "From-my-computer/region/r.0.0.mca"]
        assert archive.read("From-my-computer/region/r.0.0.mca") == region
    assert out["name"] == "From-my-computer.zip"


def test_a_minecraft_upload_that_would_not_run_or_would_not_stay_in_its_place_is_refused(minecraft, monkeypatch):
    nb, game, home = minecraft
    for data, why in (
        (b"not a zip", "that is not a zip file; zip the world's folder"),
        (zipped({"a/b/level.dat": level(5023)}), "level.dat is not at its top or one folder down"),
        (zipped({"a/level.dat": level(5023), "level.dat": level(5023)}), "more than one world"),
        (zipped({"level.dat": level(5024)}), "newer Minecraft than this server's 26.3"),
        (zipped({"level.dat": b"bedrock"}), "a Bedrock world does not run on this server"),
        (zipped({"level.dat": gzip.compress(b"\x0a\x00\x00")}), "does not say which Minecraft it is from"),
        (zipped({"level.dat": level(5023), "../../etc/cron.d/x": b"* * * * * root id"}), "a path that leaves its own folder"),
        (zipped({"level.dat": level(5023), "/etc/passwd": b"x"}), "a path that leaves its own folder"),
        (zipped({"level.dat": level(5023)}, link="region"), "holds a link"),
    ):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
        assert why in refused(nb, game, "receive", str(len(data)), "Theirs"), why
    assert titles(nb.world(game, ["list"])) == [("w1", "First world", True)]
    assert not list((home / "kept-worlds").iterdir()) and list(nb.TRANSFER.iterdir()) == []


# Paper (docs/adr/0021-minecraft-mods.md; found with the real servers: a world Paper has opened
# does not open under Mojang's server or Fabric).


def test_a_world_paper_has_opened_runs_only_under_paper_and_is_set_aside_not_lost(minecraft):
    nb, game, home = minecraft
    world = home / "world"
    assert nb.world(game, ["list"])["worlds"][0].get("paper") is None
    assert nb.world(game, ["fit"]) == {"aside": None}
    # What Paper does to a world the first time it opens it.
    for held in (nb.MOJANG_HOLDS, nb.PAPER_HOLDS):
        (world / held).parent.mkdir(parents=True, exist_ok=True)
    (world / nb.MOJANG_HOLDS).write_bytes(b"settings")
    os.rename(world / nb.MOJANG_HOLDS, world / nb.PAPER_HOLDS)
    assert nb.world(game, ["list"])["worlds"][0]["paper"] is True
    # A Paper server keeps running it.
    assert nb.world({**game, "server": "paper"}, ["fit"]) == {"aside": None}
    # Any other sets it aside, kept, and makes a new one at its next start.
    assert nb.world({**game, "server": "fabric"}, ["fit"]) == {"aside": "w1", "title": "First world"}
    assert not world.exists() and (home / nb.KEPT / "w1" / nb.PAPER_HOLDS).is_file()
    world.mkdir()
    (world / "level.dat").write_bytes((home / nb.KEPT / "w1" / "level.dat").read_bytes())
    listed = nb.world(game, ["list"])["worlds"]
    assert [(w["id"], w["title"], w["active"], w.get("paper")) for w in listed] == [
        ("w1", "First world", False, True),
        ("w2", "New world after Paper", True, None),
    ]
    assert nb.world(game, ["fit"]) == {"aside": None}
    with pytest.raises(nb.Refused, match="First world was opened by Paper.*only a Paper server opens it now"):
        nb.world(game, ["use", "w1"])
    assert (world / "level.dat").is_file() and (home / nb.KEPT / "w1").is_dir()


def test_a_factorio_save_is_kept_as_the_file_it_is(guest, monkeypatch, tmp_path):
    nb, game, server = guest
    home = tmp_path / "factorio"
    (home / "saves").mkdir(parents=True)
    (home / "saves" / "world.zip").write_bytes(zipped({"world/level.dat0": b"x"}))
    game["world"] = {"how": "factorio", "home": str(home), "dir": "saves", "options": {"seed": "", "preset": ""}}
    server.running = False
    save = zipped({"Our factory/level.dat0": b"data", "Our factory/level-init.dat": b"init", "Our factory/info.json": b"{}"})
    got = send(monkeypatch, nb, game, save, "Our factory")
    assert (home / "kept-worlds" / got["id"] / "Our-factory.zip").read_bytes() == save
    out = nb.world(game, ["export", got["id"]])
    assert Path(out["file"]).read_bytes() == save and out["name"] == "Our-factory.zip"
    for data in (b"PK nothing", zipped({"level.dat0": b"x"}), zipped({"mods/mod-list.json": b"{}"})):
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
        assert "not a Factorio save" in refused(nb, game, "receive", str(len(data)), "Other")


def test_the_log_shows_a_count_in_percent_as_its_last_line(guest, monkeypatch):
    nb, game, _ = guest
    nb = load(monkeypatch, "game_server", "nb-game")
    said = "Terraria Server\nGenerating terrain 1%\nGenerating terrain 50%\nGenerating terrain 99%\nPlacing traps 3.5%\n\n"
    said += "New RCON connection from 127.0.0.1\nListening on port 7777\n"
    asked = []

    def run(argv, **more):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, said, "")

    monkeypatch.setattr(nb.subprocess, "run", run)
    assert nb.log(game | {"log_skip": "New RCON connection"}) == [
        "Terraria Server",
        "Generating terrain 99%",
        "Placing traps 3.5%",
        "Listening on port 7777",
    ]
    assert asked[0][:3] == ["journalctl", "-u", "terraria"]


def test_world_answers_are_one_line_of_json_whatever_happens(guest, monkeypatch, capsys):
    nb, game, server = guest
    monkeypatch.setattr(nb, "CONFIG", Path(game["world"]["config"]).with_name("game.json"))
    nb.CONFIG.write_text(json.dumps(game))
    assert nb.main(["nb-game", "world", "list"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    server.count = 3
    assert nb.main(["nb-game", "world", "use", "w1"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "First world is the world that runs"}
    assert nb.main(["nb-game", "world", "new", "Second"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "3 players are in Terraria and would be disconnected",
        "players": 3,
    }


# What the page makes of its form, and of nb-game's answers.


@pytest.fixture
def model():
    return build_model(games_config())


def test_a_new_worlds_form_becomes_only_what_differs_from_the_games_own_start(model):
    assert games.read_new_world(model, "terraria", None) == []
    assert games.read_new_world(model, "terraria", {"autocreate": "2", "difficulty": 0, "seed": ""}) == []
    assert games.read_new_world(model, "terraria", {"autocreate": 3, "difficulty": "1", "seed": " nullbill "}) == [
        "autocreate=3",
        "difficulty=1",
        "seed=nullbill",
    ]
    chosen = {"level-seed": "-42", "level-type": "minecraft:flat", "hardcore": True, "generate-structures": False}
    assert games.read_new_world(model, "minecraft", chosen) == [
        "level-seed=-42",
        "level-type=minecraft:flat",
        "hardcore=true",
        "generate-structures=false",
    ]
    assert games.read_new_world(model, "factorio", {"seed": "", "preset": ""}) == []
    assert games.read_new_world(model, "factorio", {"seed": "123", "preset": "rail-world"}) == ["seed=123", "preset=rail-world"]
    with pytest.raises(NullBillError) as caught:
        games.read_new_world(model, "factorio", {"seed": "many", "preset": "lava", "worldpath": "/etc"})
    assert caught.value.headline == "The new map was not made" and len(caught.value.messages) == 3
    assert "'worldpath' is not something a new Factorio world takes; it takes seed, preset" in caught.value.messages
    with pytest.raises(NullBillError, match="by name"):
        games.read_new_world(model, "terraria", ["difficulty=1"])
    # The server's own settings start the first world, never a new one.
    assert [o["value"] for o in games.new_world_options(model, "terraria")] == [2, 0, ""]
    assert model["games"]["terraria"]["settings"]["autocreate"] == 1


def test_every_game_passes_its_role_the_options_nb_game_may_take(model):
    for game, options in games.NEW_WORLD.items():
        keys = [o["key"] for o in options]
        assert list(json.loads((ROLES / f"game_{game}" / "tests" / "vars.json").read_text())[f"game_{game}_new_world"]) == keys
        # Each role's check asks the real server about exactly these.
        check = (ROLES / f"game_{game}" / "tests" / "check.sh").read_text()
        assert re.search(r'^new_world="([^"]*)"$', check, re.M)[1].split() == keys
        listed = {"factorio": ("presets", "preset", ""), "minecraft": ("level_types", "level-type", "minecraft:")}.get(game)
        if listed:
            choices = [str(c[0]).removeprefix(listed[2]) for c in next(o for o in options if o["key"] == listed[1])["choices"]]
            assert re.search(rf'^{listed[0]}="([^"]*)"$', check, re.M)[1].split() == [c for c in choices if c]


def test_the_pages_request_becomes_nb_games_arguments(model):
    args = lambda body: gameroom.arguments(model, "terraria", body)  # noqa: E731
    assert args({"action": "restart"}) == (["service", "restart"], "restarting Terraria", True)
    assert args({"action": "console", "line": " say hello there "}) == (["console", "say hello there"], "typing on the console", False)
    assert args({"action": "new", "title": " Expert  run ", "options": {"difficulty": 1}, "kick": True}) == (
        ["world", "new", "Expert run", "difficulty=1", "--kick"],
        "making the world Expert run",
        True,
    )
    # Only a plain true disconnects anyone.
    assert args({"action": "use", "id": "w3", "kick": "yes"})[0] == ["world", "use", "w3"]
    assert args({"action": "rename", "id": "w3", "title": "--kick"})[0] == ["world", "rename", "w3", "--kick"]
    assert args({"action": "delete", "id": "w12"}) == (["world", "delete", "w12"], "deleting a world", False)
    assert gameroom.arguments(model, "factorio", {"action": "use", "id": "w1"})[1] == "changing the map that runs"
    for body, why in (
        ({"action": "use", "id": "../w1"}, "name the world by its id"),
        ({"action": "delete", "id": "w1 --kick"}, "name the world by its id"),
        ({"action": "new", "title": ""}, "give the world a name"),
        ({"action": "console", "line": "say a\nstop"}, "a console line is one line"),
        ({"action": "console", "line": "x" * 201}, "a console line is one line"),
        ({"action": "stop"}, "send action: restart, console, new, use, rename, delete"),
    ):
        with pytest.raises(NullBillError, match=why):
            args(body)
    # What reaches the host's shell is quoted, whatever the title holds.
    command = games.guest_command({"guests": {"terraria": {"vmid": 2026}}}, "terraria", "world", "new", "it's; rm -rf /", "seed=$(id)")
    assert shlex.split(command) == ["pct", "exec", "2026", "--", games.NB_GAME, "world", "new", "it's; rm -rf /", "seed=$(id)"]


REPORT = {
    "game": "terraria",
    "version": "1.4.5.8",
    "running": True,
    "players": 1,
    "names": None,
    "active": True,
    "log": ["Listening on port 7777", ": Server started"],
    "worlds": [
        {"id": "w1", "active": False, "bytes": 3000092, "title": "First world", "made": 1700000000, "origin": "setup"},
        {
            "id": "w2",
            "active": True,
            "bytes": 9000000,
            "title": "Expert run",
            "made": 1700000600,
            "origin": "new",
            "options": {"difficulty": "1", "seed": "nullbill", "autocreate": "3", "later": "x"},
        },
    ],
    "free_bytes": 5 * 1024**3,
    "most": 24,
    "options": ["autocreate", "difficulty", "seed"],
}


def test_the_page_shows_a_servers_worlds_in_its_own_words(model):
    view = gameroom.view(model, "terraria", REPORT)
    assert (view["title"], view["kind"], view["word"], view["live"], view["players"], view["names"]) == (
        "Terraria",
        "terraria",
        "world",
        True,
        1,
        None,
    )
    first, expert = view["worlds"]
    assert first == {
        "id": "w1",
        "title": "First world",
        "active": False,
        "bytes": 3000092,
        "made": None,
        "origin": "Made by the first setup run",
        "options": [],
    }
    assert expert["options"] == ["Expert", "Seed nullbill", "Large"] and expert["origin"] == "Made here" and expert["made"] == 1700000600
    assert [(o["key"], o["type"]) for o in view["new_world"]] == [("autocreate", "choice"), ("difficulty", "choice"), ("seed", "text")]
    assert ".wld" in view["upload"] and view["max_upload"] == 8 * 1024**3 and view["say"]
    options = {"hardcore": "true", "generate-structures": "false", "level-type": "minecraft:flat"}
    assert gameroom.summary("minecraft", options) == ["Hardcore", "No villages, temples and other structures", "Flat"]
    off = gameroom.view(model, "factorio", None, "It is off. Start it to see its worlds.", off=True)
    assert off["live"] is False and off["off"] is True and off["word"] == "map" and "worlds" not in off


def test_an_answer_that_is_not_nb_games_is_an_error_in_its_own_words():
    assert gameroom.answer('noise\n{"ok": true, "id": "w2"}\n', "", 0) == {"ok": True, "id": "w2"}
    assert gameroom.answer('{"ok": false, "error": "no", "players": 2}', "", 1)["players"] == 2
    for out, err, rc, why in (
        ("", "nb-game: no such command\n", 2, "^no such command$"),
        ("", "", 255, r"did not answer \(exit 255\)"),
        ("[1]", "", 0, r"^\[1\]$"),
        ('{"file": "/etc/shadow"}', "", 0, "shadow"),
    ):
        with pytest.raises(NullBillError, match=why):
            gameroom.answer(out, err, rc)


# The page's requests, with a stand-in for the host.


class GameHost:
    """Answers `pct exec <vmid> -- nb-game ...` as a guest's nb-game would."""

    def __init__(self):
        self.calls, self.players, self.report, self.sent = [], 0, json.loads(json.dumps(REPORT)), b""

    def __call__(self, target, command, **more):
        argv = shlex.split(command)
        if argv[:2] != ["pct", "exec"] or argv[3:5] != ["--", games.NB_GAME]:
            return 0, "{}", ""
        self.calls.append(argv[5:])
        rest = argv[5:]
        if rest == ["report"]:
            return 0, json.dumps(self.report), ""
        if rest[0] == "console" and rest[1] == "fail":
            return 1, "", "nb-game: Terraria's console is not there\n"
        if rest[0] in ("console", "service"):
            return 0, "", ""
        if rest[:2] == ["world", "export"]:
            return 0, json.dumps({"ok": True, "file": self.file, "bytes": len(self.sent), "name": "Expert run.wld"}), ""
        if rest[:2] == ["world", "sent"]:
            return 0, '{"ok": true}', ""
        if rest[:2] == ["world", "delete"] and rest[2] == "w2":
            return 1, json.dumps({"ok": False, "error": "Expert run is the world that runs; run another before you delete it"}), ""
        if rest[:2] in (["world", "new"], ["world", "use"]):
            if self.players and "--kick" not in rest:
                return 1, json.dumps({"ok": False, "error": "in the way", "players": self.players}), ""
            if "Broken" in rest:
                answer = {
                    "ok": False,
                    "error": "Terraria did not answer within 600 s; Expert run runs again",
                    "log": ["Loading world", "Error: no room"],
                }
                return 1, json.dumps(answer), ""
        return 0, json.dumps({"ok": True, "id": "w3"}), ""

    file = "/var/lib/nullbill/world-transfer/0123456789abcdef.wld"

    def pipe(self, target, command):
        argv = shlex.split(command)
        self.calls.append(["pipe", *argv[3:]])
        if argv[4] == "cat":
            with tempfile.NamedTemporaryFile(delete=False) as held:
                held.write(self.sent)
            return subprocess.Popen(["cat", held.name], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        script = (
            "import sys, json; data = sys.stdin.buffer.read(); "
            "ok = data.startswith(bytes.fromhex(sys.argv[1])); "
            "print(json.dumps({'ok': ok, 'id': 'w3', 'error': 'that is not a Terraria world', 'got': len(data)})); sys.exit(0 if ok else 1)"
        )
        return subprocess.Popen(
            [sys.executable, "-c", script, WLD[:12].hex()], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )


def game_site(app, client) -> GameHost:
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    choices = {"workloads": ["agents", "terraria", "factorio"], "owner_login": "owner-a@example.internal", "backups": False}
    assert client.call("/api/choices", choices)[0] == 200
    assert client.call("/api/state")[1]["plan"]["errors"] == []
    host = GameHost()
    app.host_run = app.host_act = app.host_long = host
    app.host_pipe = host.pipe
    return host


def test_the_page_reads_a_game_server_only_once_setup_made_it(site):
    app, client = site
    assert (
        client.call("/api/game?name=terraria")[0] == 401 and client.call("/api/game", {"name": "terraria", "action": "restart"})[0] == 401
    )
    host = game_site(app, client)
    status, answer = client.call("/api/game?name=terraria")
    assert status == 404 and answer["error"] == "Terraria is not on the server yet: run setup" and host.calls == []
    assert "no game server 'minecraft' is planned" in client.call("/api/game?name=minecraft")[1]["error"]
    built(app, client)
    view = client.call("/api/game?name=terraria")[1]["view"]
    assert view["live"] and [w["title"] for w in view["worlds"]] == ["First world", "Expert run"] and view["at"]
    assert host.calls == [["report"]]
    # Asked again within seconds, the guest is not; the page's Refresh asks.
    client.call("/api/game?name=terraria")
    assert len(host.calls) == 1
    client.call("/api/game?name=terraria&fresh=1")
    assert len(host.calls) == 2
    # A guest that does not answer says so instead of an empty list.
    app.host_run = lambda target, command: (255, "", "ssh: connect to host 192.0.2.10 port 22: No route to host")
    view = client.call("/api/game?name=terraria&fresh=1")[1]["view"]
    assert view["live"] is False and "did not answer (ssh: connect to host" in view["why"] and "worlds" not in view


def test_the_page_changes_worlds_through_the_guest_and_asks_before_disconnecting_anyone(site, capfd):
    app, client = site
    host = game_site(app, client)
    built(app, client)
    post = lambda **body: client.call("/api/game", {"name": "terraria", **body})  # noqa: E731
    status, answer = post(action="new", title="Master", options={"difficulty": 2})
    assert status == 200 and answer["ok"] and answer["id"] == "w3" and answer["view"]["live"]
    assert host.calls[-2:] == [["world", "new", "Master", "difficulty=2"], ["report"]]
    host.players = 2
    status, answer = post(action="use", id="w1")
    assert status == 200 and answer == {"ok": False, "players": 2, "title": "Terraria"}
    assert post(action="use", id="w1", kick=True)[1]["ok"] and host.calls[-2] == ["world", "use", "w1", "--kick"]
    # A refusal with the server's last lines is a headline and a list.
    status, answer = post(action="new", title="Broken", kick=True)
    assert status == 400 and answer["headline"] == "Terraria did not answer within 600 s; Expert run runs again"
    assert answer["errors"] == ["Loading world", "Error: no room"]
    assert "run another before you delete it" in post(action="delete", id="w2")[1]["error"]
    assert post(action="rename", id="w1", title="Old")[1]["ok"] and host.calls[-2] == ["world", "rename", "w1", "Old"]
    # What the form got wrong never reaches the host.
    asked = len(host.calls)
    status, answer = post(action="new", title="Other", options={"difficulty": 9, "seed": "x" * 300})
    assert status == 400 and answer["headline"] == "The new world was not made" and len(answer["errors"]) == 2
    assert post(action="delete", id="w1; reboot")[0] == 400 and post(action="burn")[0] == 400 and len(host.calls) == asked
    # The console and a restart.
    assert post(action="console", line="say hello")[1]["ok"] and host.calls[-2] == ["console", "say hello"]
    assert post(action="console", line="fail")[1]["error"] == "Terraria's console is not there"
    assert post(action="restart")[1]["ok"] and host.calls[-2] == ["service", "restart"]
    # The journal names the action and never the console's line.
    logged = capfd.readouterr().err
    assert "nullbill-web: game terraria: console by the page" in logged and "say hello" not in logged


def test_one_change_to_a_server_at_a_time_and_the_page_says_which(site):
    app, client = site
    game_site(app, client)
    built(app, client)
    app.game_acting["terraria"] = "making the world Master"
    status, answer = client.call("/api/game", {"name": "terraria", "action": "use", "id": "w1"})
    assert status == 400 and answer["error"] == "Terraria is busy making the world Master; wait for it"
    assert client.call("/api/game?name=terraria")[1]["view"]["acting"] == "making the world Master"
    # Another server is not held up.
    assert client.call("/api/game", {"name": "factorio", "action": "use", "id": "w1"})[1]["ok"]


def raw(client, method, path, body=b"", cookie=True, header=True):
    import http.client

    conn = http.client.HTTPSConnection("127.0.0.1", client.port, context=client.tls, timeout=30)
    headers = {"Content-Type": "application/octet-stream"} | ({"X-NullBill": "1"} if header else {})
    if cookie and client.cookie:
        headers["Cookie"] = client.cookie
    conn.request(method, path, body=body or None, headers=headers)
    response = conn.getresponse()
    return response.status, dict(response.getheaders()), response.read()


def test_a_world_comes_down_as_the_file_the_guest_wrote(site):
    app, client = site
    host = game_site(app, client)
    assert raw(client, "GET", "/api/game/world?name=terraria&id=w2", cookie=False)[0] == 401
    built(app, client)
    host.sent = WLD + os.urandom(700_000)
    status, headers, data = raw(client, "GET", "/api/game/world?name=terraria&id=w2")
    assert status == 200 and data == host.sent
    assert headers["Content-Disposition"] == 'attachment; filename="terraria-Expert-run.wld"'
    assert headers["Content-Length"] == str(len(host.sent)) and headers["Content-Type"] == "application/octet-stream"
    assert host.calls[-3:] == [["world", "export", "w2"], ["pipe", "--", "cat", host.file], ["world", "sent", host.file]]
    # The guest names the file; the page reads nothing else through it.
    host.file = "/etc/shadow"
    status, _, data = raw(client, "GET", "/api/game/world?name=terraria&id=w2")
    assert status == 400 and b"not a world's" in data and host.calls[-1] == ["world", "export", "w2"]
    assert raw(client, "GET", "/api/game/world?name=terraria&id=../w2")[0] == 400


def test_a_world_goes_up_as_it_is_and_only_from_the_owner(site):
    app, client = site
    host = game_site(app, client)
    built(app, client)
    theirs = WLD + os.urandom(600_000)
    path = "/api/game/world?name=terraria&title=From%20my%20computer"
    # Nobody's upload is read before it is known who sends it.
    assert raw(client, "POST", path, theirs, cookie=False)[0] == 401 and raw(client, "POST", path, theirs, header=False)[0] == 403
    assert not any(c[0] == "pipe" for c in host.calls)
    status, _, data = raw(client, "POST", path, theirs)
    answer = json.loads(data)
    assert status == 200 and answer["ok"] and answer["id"] == "w3" and answer["view"]["live"]
    assert ["pipe", "--", games.NB_GAME, "world", "receive", str(len(theirs)), "From my computer"] in host.calls
    # What the guest refuses comes back in its words.
    status, _, data = raw(client, "POST", path, b"MZ" + b"\x00" * 100)
    assert status == 400 and json.loads(data)["error"] == "that is not a Terraria world"
    assert raw(client, "POST", "/api/game/world?name=terraria&title=", theirs)[0] == 400
    assert raw(client, "POST", "/api/game/world?name=nope&title=x", theirs)[0] == 400
    assert app.game_acting == {}


# The agent's tools.


def gamed(app, monkeypatch, players=0):
    acted = []
    view = gameroom.view(build_model(games_config()), "terraria", REPORT) | {"at": 1.0}

    def act(name, body, by="the page"):
        acted.append((name, body, by))
        if players and not body.get("kick") and body["action"] in ("new", "use"):
            return {"ok": False, "players": players, "title": "Terraria"}
        return {"ok": True, "view": view}

    monkeypatch.setattr(app, "game_view", lambda name, fresh=False: view)
    monkeypatch.setattr(app, "game_act", act)
    monkeypatch.setattr(app, "config", lambda: games_config())
    return acted


def test_the_agent_reads_a_game_server_and_changes_its_world_as_a_change(agent, monkeypatch):
    app, client, _ = chosen(agent, "auto")
    acted = gamed(app, monkeypatch)
    said = result(call(client, "game_server", name="terraria"))
    assert "Expert run" in said and "new_world_takes" in said and "option: difficulty" in said and "Server started" in said
    view = call(client, "game_world", name="terraria", action="new", title="Master", options={"difficulty": 2})
    assert kinds(view, "ran")[-1]["title"] == "New world Master on Terraria"
    body = {"action": "new", "id": "", "title": "Master", "options": {"difficulty": 2}, "kick": False}
    assert acted[-1] == ("terraria", body, "the agent") and "Terraria now has: First world (w1), Expert run (w2, runs now)" in result(view)
    assert "has no world 'w9'; game_server lists them by id" in result(call(client, "game_world", name="terraria", action="use", id="w9"))
    assert "Difficulty is" in result(call(client, "game_world", name="terraria", action="new", title="X", options={"difficulty": 9}))
    assert "action is new, use, rename or delete" in result(call(client, "game_world", name="terraria", action="upload"))
    view = call(client, "game_command", name="terraria", action="restart")
    assert kinds(view, "ran")[-1]["title"] == "Restart Terraria" and acted[-1][1] == {"action": "restart", "line": ""}
    view = call(client, "game_command", name="terraria", action="console", line="say back in five")
    assert kinds(view, "ran")[-1]["title"] == "Type on Terraria's console" and "typed it" in result(view)
    assert "no game server 'nope'" in result(call(client, "game_command", name="nope", action="restart"))


def test_the_agent_asks_in_manual_mode_and_says_what_stays(agent, monkeypatch):
    app, client, _ = chosen(agent)
    acted = gamed(app, monkeypatch)
    view = call(client, "game_world", name="terraria", action="use", id="w1")
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["title"]) == ("change", "Run First world on Terraria")
    assert proposal["lines"] == [
        "Terraria stops for a moment and starts with First world",
        "Expert run is kept in the server's list, not deleted",
    ]
    assert acted == []
    approved(client, proposal["id"])
    assert acted[-1][1]["action"] == "use"


def test_deleting_a_world_or_disconnecting_players_is_the_owners_with_a_typed_word(agent, monkeypatch):
    app, client, _ = chosen(agent, "auto")
    acted = gamed(app, monkeypatch, players=2)
    view = call(client, "game_world", name="terraria", action="use", id="w1")
    assert "2 players are in Terraria, so nothing changed" in result(view) and "ask_owner" in result(view)
    view = call(client, "game_world", name="terraria", action="use", id="w1", kick=True)
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["word"]) == ("risky", "disconnect") and "Players who are connected are disconnected" in proposal[
        "lines"
    ]
    view = call(client, "game_world", name="terraria", action="delete", id="w1")
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["word"], proposal["title"]) == ("risky", "delete", "Delete First world from Terraria")
    assert "gone for good" in proposal["lines"][0]
    assert all(body["action"] != "delete" for _, body, _ in acted)
    approved(client, proposal["id"], typed="delete")
    assert acted[-1][1] == {"action": "delete", "id": "w1", "title": "", "options": None, "kick": False}
