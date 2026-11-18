"""Root's helpers in the game and download guests against what the
service's own user may leave in the directories it owns: links to any
place in the guest, a FIFO, a level.dat that unpacks to far more than it
is. nb-game (game_server), nb-properties (game_minecraft) and nb-torrents
(qbittorrent) never write, own or read what such a link names. The role
checks plant the same in the real guests."""

import gzip
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
from test_game_tools import ROLES, load
from test_game_worlds import WLD, guest, level, minecraft, refused, send, titles, zipped

__all__ = ["guest", "minecraft"]

EULA = json.dumps({"eula": True})
as_root = pytest.mark.skipif(os.geteuid() != 0, reason="changes owners, which only root may")


@pytest.fixture
def reachable():
    """A directory another user may pass through, as /var/lib is in a guest
    (pytest's own are root's alone)."""
    path = Path(tempfile.mkdtemp(prefix="nb-links-"))
    path.chmod(0o755)
    yield path
    shutil.rmtree(path)


def untouched(path: Path, text: str) -> None:
    assert path.read_text() == text and path.stat().st_uid == os.geteuid()


# nb-properties


def test_properties_are_never_read_or_written_through_a_link(monkeypatch, tmp_path, capsys):
    module = load(monkeypatch, "game_minecraft", "nb-properties")
    secret = tmp_path / "shadow"
    secret.write_text("root:secret\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "server.properties").symlink_to(secret)
    monkeypatch.setattr(sys, "argv", ["nb-properties", str(home / "server.properties")])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"motd": "x"})))
    with pytest.raises(SystemExit, match="a link is not followed"):
        module.main(sys.argv)
    untouched(secret, "root:secret\n")
    # The name it once wrote to first, before renaming: a link there is
    # no longer where anything goes.
    (home / "server.properties").unlink()
    (home / "server.properties.nullbill").symlink_to(secret)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"motd": "x"})))
    assert module.main(sys.argv) == 0
    capsys.readouterr()
    untouched(secret, "root:secret\n")
    assert (home / "server.properties").read_text() == "motd=x\n" and not (home / "server.properties").is_symlink()
    assert oct((home / "server.properties").stat().st_mode & 0o777) == "0o640"


@as_root
def test_properties_are_set_as_the_owner_of_the_servers_directory(reachable):
    tmp_path = reachable
    nobody = pwd.getpwnam("nobody")
    home = tmp_path / "home"
    home.mkdir(mode=0o750)
    os.chown(home, nobody.pw_uid, nobody.pw_gid)
    script = ROLES / "game_minecraft" / "files" / "nb-properties"
    run = subprocess.run([sys.executable, "-I", str(script), str(home / "eula.txt")], input=EULA, capture_output=True, text=True)
    assert run.stdout == "changed\n", run.stderr
    made = (home / "eula.txt").stat()
    assert (made.st_uid, made.st_gid, made.st_mode & 0o777) == (nobody.pw_uid, nobody.pw_gid, 0o640)
    # A link to root's file is refused by the owner, who cannot read it.
    secret = tmp_path / "secret"
    secret.write_text("x=1\n")
    secret.chmod(0o600)
    (home / "eula.txt").unlink()
    (home / "eula.txt").symlink_to(secret)
    run = subprocess.run([sys.executable, "-I", str(script), str(home / "eula.txt")], input=EULA, capture_output=True, text=True)
    assert run.returncode != 0 and "a link is not followed" in run.stderr
    untouched(secret, "x=1\n")


# nb-game


def test_a_link_left_in_a_world_is_removed_when_the_world_comes_back(guest, tmp_path):
    nb, game, server = guest
    home = Path(game["world"]["home"])
    secret = tmp_path / "shadow"
    secret.write_text("root:secret\n")
    (home / "worlds" / "shadow").symlink_to(secret)
    (home / "worlds" / "etc").symlink_to(tmp_path, target_is_directory=True)
    nb.world(game, ["new", "Second"])
    assert (nb.KEPT / "w1" / "shadow").is_symlink()
    nb.world(game, ["use", "w1"])
    # Made the server's again in root's place, where nothing changes under it:
    # the links went, what they named did not.
    assert sorted(f.name for f in (home / "worlds").iterdir()) == ["First.wld"]
    untouched(secret, "root:secret\n")
    assert titles(nb.world(game, ["list"]))[0] == ("w1", "First world", True)


def test_a_world_folder_swapped_for_a_link_is_not_kept_or_followed(guest, tmp_path):
    nb, game, server = guest
    home = Path(game["world"]["home"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "passwd").write_text("root:x:0:0\n")
    nb.world(game, ["list"])
    # The stand-in server makes no world when started again.
    server.broken = True
    os.rename(home / "worlds", home / "moved")
    (home / "worlds").symlink_to(elsewhere, target_is_directory=True)
    why = refused(nb, game, "new", "Second")
    assert "was not a folder but a link or a file; it was not kept" in why
    untouched(elsewhere / "passwd", "root:x:0:0\n")
    assert not os.path.lexists(nb.KEPT / "w1") and sorted(os.listdir(elsewhere)) == ["passwd"]
    # Not exported through it either.
    (home / "worlds").symlink_to(elsewhere, target_is_directory=True)
    assert "is not a folder but a link or a file" in refused(nb, game, "export", "w1")


def test_the_kept_worlds_are_outside_the_servers_home_and_move_there_once(guest):
    nb, game, server = guest
    home = Path(game["world"]["home"])
    nb.world(game, ["list"])
    # As an older nb-game kept them: root's directory in the server's home.
    old = home / nb.OLD_KEPT
    (old / "w2").mkdir(parents=True)
    (old / "w2" / "Second.wld").write_bytes(WLD)
    old.chmod(0o700)
    state = json.loads(nb.WORLDS.read_text())
    state["worlds"]["w2"] = {"title": "Second", "made": 1, "origin": "new"}
    state["next"] = 3
    nb.WORLDS.write_text(json.dumps(state))
    assert titles(nb.world(game, ["list"])) == [("w1", "First world", True), ("w2", "Second", False)]
    assert (nb.KEPT / "w2" / "Second.wld").read_bytes() == WLD and not old.exists()
    assert oct(nb.KEPT.stat().st_mode & 0o777) == "0o700"
    nb.world(game, ["use", "w2"])
    assert (home / "worlds" / "Second.wld").exists()
    # A directory of that name that is not root's alone is the server's: left where it is.
    (old / "w1").mkdir(parents=True)
    old.chmod(0o755)
    nb.world(game, ["list"])
    assert (old / "w1").is_dir() and (nb.KEPT / "w1" / "First.wld").exists()


def test_the_running_world_goes_out_without_what_its_links_name(minecraft, tmp_path):
    nb, game, home = minecraft
    secret = tmp_path / "shadow"
    secret.write_text("root:secret\n")
    (home / "world" / "shadow.dat").symlink_to(secret)
    (home / "world" / "region").mkdir()
    (home / "world" / "region" / "r.0.0.mca").write_bytes(b"region")
    out = nb.world(game, ["export", "w1"])
    with zipfile.ZipFile(out["file"]) as archive:
        assert sorted(archive.namelist()) == ["First-world/level.dat", "First-world/region/r.0.0.mca"]


def test_the_console_and_the_admins_are_opened_through_no_link(monkeypatch, tmp_path):
    nb = load(monkeypatch, "game_server", "nb-game")
    secret = tmp_path / "shadow"
    secret.write_text('["root"]\n')
    (tmp_path / "console").symlink_to(secret)
    with pytest.raises(nb.NotAnswering, match="console is not open"):
        nb.console({"console": str(tmp_path / "console")}, "stop")
    (tmp_path / "plain").write_text("")
    with pytest.raises(nb.NotAnswering, match="not a FIFO"):
        nb.console({"console": str(tmp_path / "plain")}, "stop")
    (tmp_path / "ops.json").symlink_to(secret)
    with pytest.raises(nb.NotAnswering, match="no list of admins"):
        nb.admins({"admins": str(tmp_path / "ops.json")})
    untouched(secret, '["root"]\n')


def test_a_level_dat_that_unpacks_to_far_more_is_refused_unpacked(minecraft, monkeypatch):
    nb, game, home = minecraft
    monkeypatch.setattr(nb, "MAX_LEVEL", 2**20)
    bomb = gzip.compress(b"\x00" * (2**20 + 1))
    assert len(bomb) < 4096
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data := zipped({"level.dat": bomb}))))
    assert "not a Java Edition world's" in refused(nb, game, "receive", str(len(data)), "Bomb")
    cut = level(4000)[:-6]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data := zipped({"level.dat": cut}))))
    assert "not a Java Edition world's" in refused(nb, game, "receive", str(len(data)), "Cut")
    assert send(monkeypatch, nb, game, zipped({"level.dat": level(4000)}), "Fine")["id"] == "w2"


# nb-torrents


@as_root
def test_the_clients_settings_are_written_as_the_client(monkeypatch, reachable):
    tmp_path = reachable
    nb = load(monkeypatch, "qbittorrent", "nb-torrents")
    nobody = pwd.getpwnam("nobody")
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o750)
    os.chown(profile, nobody.pw_uid, nobody.pw_gid)
    (tmp_path / "password").write_text("pw\n")
    (tmp_path / "state").mkdir()
    for name, value in (
        ("PROFILE", profile),
        ("QBT_CONF", profile / "qBittorrent/config/qBittorrent.conf"),
        ("PASSWORD", tmp_path / "password"),
        ("PASSWORD_SET", tmp_path / "state" / "mark"),
    ):
        monkeypatch.setattr(nb, name, value)
    cfg = {"user": "nobody", "store": "/downloads", "listen_port": 41643, "port": 8080, "username": "u", "route": "home"}
    # The client made its profile's folder a link to a root directory.
    target = tmp_path / "busy.d"
    target.mkdir(mode=0o755)
    (profile / "qBittorrent").symlink_to(target, target_is_directory=True)
    assert nb.first_config(cfg) == 1
    assert target.stat().st_uid == 0 and list(target.iterdir()) == []
    (profile / "qBittorrent").unlink()
    assert nb.first_config(cfg) == 0
    made = (profile / "qBittorrent/config/qBittorrent.conf").stat()
    assert (made.st_uid, made.st_mode & 0o777) == (nobody.pw_uid, 0o600)
    assert (profile / "qBittorrent").stat().st_uid == nobody.pw_uid
