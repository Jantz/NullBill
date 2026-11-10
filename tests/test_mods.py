"""Which server runs a Minecraft game server and its plugins or mods
(docs/adr/0021-minecraft-mods.md): what mods.py makes of Modrinth's answers
(replayed from tests/fixtures/modrinth.json, which tools/record-modrinth
writes), the list in config.yaml, plan's rules, the role's variables, the
page's requests and the agent's tool. The Minecraft role's check installs
the same plugin and mods on the real Paper and Fabric."""

import copy
import json
import os
import urllib.parse
from pathlib import Path

import pytest
import test_chat
import test_web
import yaml
from fixturedata import games_config
from test_agent_reach import approved, call, chosen, result
from test_chat import kinds
from test_game_tools import load
from test_game_worlds import REPORT, GameHost
from test_web import built, sign_in, wait

from nullbill import games, mods
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

agent = test_chat.agent
site = test_web.site
ROOT = Path(__file__).parent.parent
RECORDED = json.loads((Path(__file__).parent / "fixtures" / "modrinth.json").read_text())
TESTS = ROOT / "ansible" / "roles" / "game_minecraft" / "tests"


def modrinth(path: str, params: dict | None = None) -> object:
    """Modrinth as recorded; what was never asked is not there."""
    key = path + (" " + json.dumps(params, sort_keys=True) if params else "")
    if key not in RECORDED:
        raise NullBillError("Modrinth has nothing of that name")
    return copy.deepcopy(RECORDED[key])


def with_kind(kind: str, *projects: str, config: dict | None = None, name: str = "minecraft") -> dict:
    """config.yaml with a Minecraft server of that kind and these added."""
    config = copy.deepcopy(config or games_config())
    config, _ = mods.set_server(config, build_model(config), name, kind)
    for project in projects:
        model = build_model(config)
        config = mods.put(config, model, name, mods.resolve(modrinth, model, name, project))
    return config


def listed(config: dict, name: str = "minecraft") -> list[dict]:
    return build_model(config)["games"][name]["mods"]


def test_the_catalog_pins_paper_and_fabric_for_the_minecraft_it_pins():
    model = build_model({})
    game = model["games"]["minecraft"]
    assert mods.server(model, "minecraft") == "vanilla" and mods.files(model, "minecraft") == [] and game["mods"] == []
    assert game["servers"]["paper"]["version"] == game["servers"]["fabric"]["version"] == game["version"]
    assert game["servers"]["paper"]["url"].endswith(f"/paper-{game['version']}-{game['servers']['paper']['build']}.jar")
    assert any(lib["file"] == f"fabric-loader-{game['servers']['fabric']['loader']}.jar" for lib in game["servers"]["fabric"]["libraries"])
    # The guest's proxy lets it fetch each of them, and Modrinth's files.
    allowed = model["services"]["minecraft"]["proxy_allow"]
    urls = [game["servers"]["paper"]["url"], *(lib["url"] for lib in game["servers"]["fabric"]["libraries"])]
    assert {urllib.parse.urlsplit(url).hostname for url in urls} | {"cdn.modrinth.com"} <= set(allowed)
    assert "api.modrinth.com" in model["services"]["nullbill"]["proxy_allow"]
    assert not mods.takes(model, "terraria") and mods.view(model, "terraria") == {"name": "terraria", "takes": False}


def test_search_lists_what_the_servers_kind_and_minecraft_can_run():
    model = build_model(with_kind("fabric", "lithium"))
    hits = mods.search(modrinth, model, "minecraft", "  lithium ")
    assert hits[0]["project"] == "lithium" and hits[0]["added"] and hits[0]["link"] == "https://modrinth.com/project/lithium"
    assert hits[0]["what"] and hits[0]["downloads"] > 1000 and hits[0]["players_need"] is False
    # Nothing typed: the most used, for this kind.
    assert len(mods.search(modrinth, model, "minecraft", "")) > 5
    assert mods.search(modrinth, build_model(with_kind("paper")), "minecraft", "chunky")[0]["project"] == "chunky"
    for wrong, said in (("x" * 81, "at most 80 characters"), ("a\x00b", "at most 80 characters")):
        with pytest.raises(NullBillError, match=said):
            mods.search(modrinth, model, "minecraft", wrong)
    with pytest.raises(NullBillError, match="runs Mojang's own server, which takes none; choose Paper"):
        mods.search(modrinth, build_model(games_config()), "minecraft", "lithium")
    with pytest.raises(NullBillError, match="only a Minecraft server"):
        mods.search(modrinth, model, "terraria", "lithium")


def test_adding_takes_the_newest_file_for_the_server_with_modrinths_hash_and_what_it_needs():
    config = with_kind("fabric", "lithium", "chunky")
    lithium, chunky, api = listed(config)
    version = build_model(config)["games"]["minecraft"]["version"]
    assert (lithium["project"], lithium["title"], lithium["file"]) == ("lithium", "Lithium", "lithium-fabric-0.26.3+mc26.3.jar")
    assert lithium["url"].startswith("https://cdn.modrinth.com/data/") and len(lithium["sha512"]) == 128
    assert "fabric" in lithium["loaders"] and version in lithium["game_versions"]
    assert "for_others" not in lithium and "needs" not in lithium and "players_need" not in lithium
    # Chunky's Fabric file does not run without Fabric API, which came with it.
    assert chunky["needs"] == ["fabric-api"] and chunky["file"].startswith("Chunky-Fabric-")
    assert api["project"] == "fabric-api" and api["for_others"] is True
    assert mods.problems(build_model(config), "minecraft") == []
    # The same project on Paper is another file, and needs nothing.
    (plugin,) = listed(with_kind("paper", "chunky"))
    assert plugin["file"].startswith("Chunky-Paper-") and "needs" not in plugin and "paper" in plugin["loaders"]
    model = build_model(config)
    for wrong, said in (
        ("lithium", "Lithium is in Minecraft's list already"),
        ("no-such-mod", "Modrinth has nothing of that name"),
        ("../etc", "name the mod as Modrinth does in its address"),
        ("", "name the mod as Modrinth does"),
    ):
        with pytest.raises(NullBillError, match=said):
            mods.resolve(modrinth, model, "minecraft", wrong)


def test_what_modrinth_answers_is_taken_only_in_the_shape_the_guest_may_fetch():
    model = build_model(with_kind("fabric"))

    def tampered(change):
        def ask(path, params=None):
            answer = modrinth(path, params)
            if path.endswith("/version"):
                for found in answer:
                    change(found)
            elif path.startswith("project/"):
                change(answer)
            return answer

        return ask

    def elsewhere(found):
        if "files" in found:
            found["files"][0]["url"] = "https://files.example.org/lithium.jar"

    def odd_name(found):
        if "files" in found:
            found["files"][0]["filename"] = "../../start"

    def no_hash(found):
        if "files" in found:
            found["files"][0]["hashes"] = {"sha1": "0" * 40}

    for change in (elsewhere, no_hash):
        with pytest.raises(NullBillError, match="its file is not one NullBill takes"):
            mods.resolve(tampered(change), model, "minecraft", "lithium")
    with pytest.raises(NullBillError, match="has no .jar file|not one NullBill takes"):
        mods.resolve(tampered(odd_name), model, "minecraft", "lithium")
    with pytest.raises(NullBillError, match="Lithium is for a player's own game only"):
        mods.resolve(tampered(lambda found: found.update(server_side="unsupported")), model, "minecraft", "lithium")
    with pytest.raises(NullBillError, match="Lithium has no version for Fabric on Minecraft"):
        mods.resolve(tampered(lambda found: found.update(status="draft")), model, "minecraft", "lithium")
    # What every player needs too is said with the entry.
    (entry,) = mods.resolve(tampered(lambda found: found.update(client_side="required")), model, "minecraft", "lithium")
    assert entry["players_need"] is True and "every player needs it in their own game too" in mods.describe([entry])[0]
    # A beta is taken where there is no release, and called one.
    (entry,) = mods.resolve(tampered(lambda found: found.update(version_type="beta")), model, "minecraft", "lithium")
    assert entry["version"].endswith(" (beta)")
    # config.yaml takes no file from anywhere else, whoever writes it.
    config = with_kind("fabric", "lithium")
    config["games"]["minecraft"]["mods"][0]["url"] = "https://files.example.org/data/a/versions/b/lithium.jar"
    with pytest.raises(NullBillError, match="mods"):
        build_model(config)


def test_removing_takes_along_what_was_only_there_for_it_and_keeps_what_others_need():
    config = with_kind("fabric", "lithium", "chunky")
    model = build_model(config)
    with pytest.raises(NullBillError, match="Chunky needs Fabric API; remove it first"):
        mods.drop(config, model, "minecraft", "fabric-api")
    with pytest.raises(NullBillError, match="'nope' is not in Minecraft's list"):
        mods.drop(config, model, "minecraft", "nope")
    after, gone = mods.drop(config, model, "minecraft", "chunky")
    assert gone == ["Chunky", "Fabric API"] and [m["project"] for m in listed(after)] == ["lithium"]
    after, gone = mods.drop(after, build_model(after), "minecraft", "lithium")
    # Nothing left is nothing written: config.yaml keeps only the kind.
    assert gone == ["Lithium"] and after["games"]["minecraft"] == {**games_config()["games"]["minecraft"], "server": "fabric"}
    # Asked for by the owner, Fabric API stays when Chunky leaves.
    own = with_kind("fabric", "fabric-api", "chunky")
    assert "for_others" not in listed(own)[0] and listed(own)[1]["needs"] == ["fabric-api"]
    after, gone = mods.drop(own, build_model(own), "minecraft", "chunky")
    assert gone == ["Chunky"] and [m["project"] for m in listed(after)] == ["fabric-api"]


def test_another_kind_of_server_drops_the_list_and_vanilla_is_written_as_nothing():
    config = with_kind("fabric", "lithium")
    after, gone = mods.set_server(config, build_model(config), "minecraft", "paper")
    assert gone == ["Lithium"] and after["games"]["minecraft"]["server"] == "paper" and "mods" not in after["games"]["minecraft"]
    same, gone = mods.set_server(config, build_model(config), "minecraft", "fabric")
    assert gone == [] and same == config
    back, gone = mods.set_server(after, build_model(after), "minecraft", "vanilla")
    assert gone == [] and back == games_config()
    with pytest.raises(NullBillError, match="one of vanilla, paper, fabric"):
        mods.set_server(config, build_model(config), "minecraft", "forge")
    with pytest.raises(NullBillError, match="only a Minecraft server"):
        mods.set_server(config, build_model(config), "factorio", "paper")


def test_a_newer_version_replaces_the_file_and_brings_what_it_newly_needs():
    config = with_kind("fabric", "lithium", "chunky")
    model = build_model(config)
    assert mods.newer(modrinth, model, "minecraft") == []
    old = copy.deepcopy(config)
    for mod in old["games"]["minecraft"]["mods"]:
        if mod["project"] in ("lithium", "fabric-api"):
            mod |= {"sha512": "0" * 128, "version": "0.1", "file": f"old-{mod['project']}.jar"}
    found = mods.newer(modrinth, build_model(old), "minecraft")
    assert [e["project"] for e in found] == ["lithium", "fabric-api"] and found[1]["for_others"] is True
    assert mods.put(old, build_model(old), "minecraft", found) == config
    # One that Modrinth no longer has stays as it is.
    gone = copy.deepcopy(old)
    gone["games"]["minecraft"]["mods"][0] |= {"project": "withdrawn", "id": "AAAAAAAA"}
    assert [e["project"] for e in mods.newer(modrinth, build_model(gone), "minecraft")] == ["fabric-api"]


def test_plan_refuses_what_the_server_would_not_start_with(facts):
    def problems(config) -> str:
        with pytest.raises(NullBillError) as raised:
            build_plan(build_model(config), facts("large"))
        return str(raised.value)

    good = with_kind("fabric", "lithium", "chunky")
    plan = strip_notes(build_plan(build_model(good), facts("large")))
    assert plan["games"]["minecraft"]["server"] == "fabric loader 0.20.1"
    assert plan["games"]["minecraft"]["mods"][0] == "Lithium mc26.3-0.26.3-fabric" and len(plan["games"]["minecraft"]["mods"]) == 3
    # Mojang's own server says nothing new, so plans from before stay as they were.
    assert "server" not in strip_notes(build_plan(build_model(games_config()), facts("large")))["games"]["minecraft"]
    vanilla = copy.deepcopy(good)
    del vanilla["games"]["minecraft"]["server"]
    assert "games.minecraft.mods: Mojang's own server takes no plugins or mods; choose Paper or Fabric" in problems(vanilla)
    paper = copy.deepcopy(good)
    paper["games"]["minecraft"]["server"] = "paper"
    said = problems(paper)
    assert "Lithium mc26.3-0.26.3-fabric is made for fabric, quilt, and Minecraft runs Paper; remove it on the Services page" in said
    moved = copy.deepcopy(good)
    moved["games"]["minecraft"] |= {"version": "26.4"}
    said = problems(moved)
    assert "games.minecraft.servers.fabric: it is pinned for Minecraft 26.3 and the server is 26.4; move both pins together" in said
    assert "Lithium mc26.3-0.26.3-fabric is not for Minecraft 26.4; on the Services page, Check for updates takes a newer mod" in said
    alone = copy.deepcopy(good)
    alone["games"]["minecraft"]["mods"] = [m for m in alone["games"]["minecraft"]["mods"] if m["project"] != "fabric-api"]
    assert "games.minecraft.mods: Chunky needs fabric-api, which is not in the list" in problems(alone)
    twice = copy.deepcopy(good)
    twice["games"]["minecraft"]["mods"].append(dict(twice["games"]["minecraft"]["mods"][0]))
    assert "is listed twice" in problems(twice)


def test_a_further_server_has_its_own_kind_and_list_and_the_games_pins(facts):
    config = copy.deepcopy(games_config())
    config["games"]["creative"] = {"game": "minecraft", "id": 70, "title": "Creative"}
    config["intent"]["workloads"].append("creative")
    config = with_kind("paper", "chunky", config=config, name="creative")
    model = build_model(config)
    assert mods.server(model, "minecraft") == "vanilla" and model["games"]["minecraft"]["mods"] == []
    assert mods.server(model, "creative") == "paper" and [m["project"] for m in model["games"]["creative"]["mods"]] == ["chunky"]
    assert model["games"]["creative"]["servers"] == model["games"]["minecraft"]["servers"]
    assert "cdn.modrinth.com" in model["services"]["creative"]["proxy_allow"]
    plan = strip_notes(build_plan(model, facts("large")))
    assert plan["games"]["creative"]["server"] == "paper build 248" and "server" not in plan["games"]["minecraft"]
    assert games.variables(plan, model, "creative")["game_minecraft_server"] == "paper"
    assert games.variables(plan, model, "minecraft")["game_minecraft_server"] == "vanilla"


@pytest.mark.parametrize(("kind", "projects"), [("paper", ["chunky"]), ("fabric", ["lithium", "chunky"])])
def test_the_role_check_installs_what_the_page_would_have_added(facts, kind, projects):
    """The Minecraft role's check runs Paper and Fabric with
    tests/<kind>.json over tests/vars.json: keep them equal to what apply
    would pass for a server with these added. UPDATE_GOLDEN=1 rewrites."""
    config = with_kind(kind, *projects)
    model = build_model(config)
    plan = strip_notes(build_plan(model, facts("large")))
    passed = games.variables(plan, model, "minecraft")
    base = json.loads((TESTS / "vars.json").read_text())
    differs = {key: value for key, value in passed.items() if base.get(key) != value}
    assert sorted(differs) == ["game_minecraft_mods", "game_minecraft_server", "game_minecraft_server_files"]
    assert [m["file"] for m in differs["game_minecraft_mods"]] == [m["file"] for m in model["games"]["minecraft"]["mods"]]
    assert all(set(m) == {"file", "url", "sha512"} for m in differs["game_minecraft_mods"])
    want = json.dumps(differs, indent=2) + "\n"
    path = TESTS / f"{kind}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(want)
    assert path.read_text() == want


def test_verify_asks_the_guest_which_server_and_files_it_holds(facts):
    config = with_kind("fabric", "lithium", "chunky")
    model = build_model(config)
    plan = strip_notes(build_plan(model, facts("large")))
    (check,) = [c for c in games.commands(plan, model) if c[0] == "minecraft runs Fabric with its mods"]
    assert check[1].endswith("nb-game mods") or "nb-game mods" in check[1]
    assert check[2] == "fabric Chunky-Fabric-1.5.66.jar fabric-api-0.166.0+26.3.jar lithium-fabric-0.26.3+mc26.3.jar"
    model = build_model(games_config())
    plan = strip_notes(build_plan(model, facts("large")))
    assert ("minecraft runs Mojang's own server", "vanilla") in [(c[0], c[2]) for c in games.commands(plan, model)]


def test_nb_game_says_which_server_runs_and_which_files_it_was_given(monkeypatch, tmp_path, capsys):
    nb = load(monkeypatch, "game_server", "nb-game")
    held = tmp_path / "mods"
    held.mkdir()
    for name in ("b.jar", "a.jar", "notes.txt"):
        (held / name).write_text("x")
    (held / "dir.jar").mkdir()
    game = {"name": "minecraft", "version": "26.3", "server": "fabric", "mods": str(held)}
    assert nb.mods(game) == ("fabric", ["a.jar", "b.jar"])
    assert nb.mods({"name": "terraria"}) == ("vanilla", [])
    monkeypatch.setattr(nb, "CONFIG", tmp_path / "game.json")
    (tmp_path / "game.json").write_text(json.dumps(game))
    assert nb.main(["nb-game", "mods"]) == 0 and capsys.readouterr().out == "fabric a.jar b.jar\n"


# The page.


def mods_site(app, client) -> GameHost:
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    choices = {
        "workloads": ["agents", "minecraft", "terraria"],
        "owner_login": "owner-a@example.internal",
        "backups": False,
        "games": {"minecraft": {"eula": True, "settings": {"difficulty": "hard"}}},
    }
    assert client.call("/api/choices", choices)[0] == 200
    assert client.call("/api/state")[1]["plan"]["errors"] == []
    host = GameHost()
    host.report = json.loads(json.dumps(REPORT)) | {"game": "minecraft"}
    app.host_run = app.host_act = app.host_long = host
    app.host_pipe = host.pipe
    app.modrinth = modrinth
    return host


def saved(app) -> dict:
    return yaml.safe_load((app.home / "config.yaml").read_text())["games"]["minecraft"]


def test_the_page_changes_the_kind_and_the_list_and_says_whether_the_server_runs_it(site, capfd):
    app, client = site
    assert client.call("/api/game/mods?name=minecraft")[0] == 401
    assert client.call("/api/game/mods", {"name": "minecraft", "action": "search"})[0] == 401
    host = mods_site(app, client)
    post = lambda **body: client.call("/api/game/mods", {"name": "minecraft", **body})  # noqa: E731
    # Before the server exists the list can be made: nothing is asked of a guest.
    view = client.call("/api/game/mods?name=minecraft")[1]["view"]
    assert (view["server"], view["mods"], view["applied"], view["word"]) == ("vanilla", [], None, None)
    assert [k["id"] for k in view["servers"]] == ["vanilla", "paper", "fabric"] and host.calls == []
    assert client.call("/api/game/mods?name=terraria")[1]["view"] == {"name": "terraria", "takes": False}
    assert client.call("/api/game/mods?name=nope")[0] == 404
    assert "runs Mojang's own server, which takes none" in post(action="add", project="lithium")[1]["error"]
    # Mojang's server and Fabric share worlds: nothing to ask.
    status, answer = post(action="server", server="fabric")
    assert status == 200 and answer["ok"] and answer["dropped"] == [] and answer["view"]["release"] == "loader 0.20.1"
    assert saved(app) == {"eula": True, "settings": {"difficulty": "hard"}, "server": "fabric"}
    assert post(action="search", query="lithium")[1]["hits"][0]["project"] == "lithium"
    status, answer = post(action="add", project="chunky")
    assert status == 200 and answer["added"] == ["Chunky 1.5.66 (Chunky-Fabric-1.5.66.jar)", answer["added"][1]]
    assert answer["added"][1].endswith(", which another needs")
    assert [(m["project"], m["for_others"], m["needs"]) for m in answer["view"]["mods"]] == [
        ("chunky", False, ["fabric-api"]),
        ("fabric-api", True, []),
    ]
    # The plan follows at once; the server only with a setup run.
    plan = client.call("/api/state")[1]["plan"]
    assert plan["errors"] == [] and "fabric loader 0.20.1" in plan["text"] and "Chunky 1.5.66" in plan["text"]
    assert "Chunky needs Fabric API; remove it first" in post(action="remove", project="fabric-api")[1]["error"]
    # Saving the choices again keeps the kind and the list.
    choices = {"workloads": ["agents", "minecraft", "terraria"], "owner_login": "owner-a@example.internal", "backups": False}
    assert client.call("/api/choices", choices | {"games": {"minecraft": {"eula": True, "settings": {"difficulty": "easy"}}}})[0] == 200
    assert saved(app)["server"] == "fabric" and len(saved(app)["mods"]) == 2 and "settings" not in saved(app)
    # Another kind would drop them: nothing changes until the owner says so.
    status, answer = post(action="server", server="paper")
    assert status == 200 and (answer["ok"], answer["title"], answer["to"]) == (False, "Minecraft", "Paper")
    assert answer["costs"] == [
        "Chunky, Fabric API leave the list: Paper cannot run those mods. "
        "What only a mod added to the world may be gone when it is opened without.",
        "Paper stores a world its own way the first time it opens it. From then on only Paper opens that world: "
        "Fabric and Mojang's own server cannot.",
        "Download the world first (Worlds, Download) if you may want it as it is now.",
    ]
    assert saved(app)["server"] == "fabric"
    # What the guest holds says whether the list runs: asked of a built server only.
    built(app, client)
    host.report |= {"server": "fabric", "mods": ["Chunky-Fabric-1.5.66.jar"]}
    assert client.call("/api/game?name=minecraft&fresh=1")[1]["view"]["server"] == "fabric"
    view = client.call("/api/game/mods?name=minecraft")[1]["view"]
    assert view["applied"] is False and view["players"] == host.report["players"]
    host.report["mods"].append("fabric-api-0.166.0+26.3.jar")
    client.call("/api/game?name=minecraft&fresh=1")
    assert client.call("/api/game/mods?name=minecraft")[1]["view"]["applied"] is True
    # A guest from before this was built says nothing: it runs Mojang's.
    del host.report["server"], host.report["mods"]
    client.call("/api/game?name=minecraft&fresh=1")
    assert client.call("/api/game/mods?name=minecraft")[1]["view"]["applied"] is False
    assert post(action="check")[1]["updates"] == []
    status, answer = post(action="remove", project="chunky")
    assert answer["removed"] == ["Chunky", "Fabric API"] and answer["view"]["mods"] == []
    # Without a list, Paper still asks: it changes how the world is stored.
    assert len(post(action="server", server="paper")[1]["costs"]) == 2
    assert post(action="server", server="paper", yes=True)[1]["view"]["word"] == "plugin"
    assert post(action="add", project="chunky")[1]["view"]["mods"][0]["file"] == "Chunky-Paper-1.5.66.jar"
    costs = post(action="server", server="vanilla")[1]["costs"]
    assert costs[0].startswith("Chunky leaves the list: Vanilla cannot run that plugin.")
    assert costs[1] == (
        "A world Paper has opened does not open under Mojang's own server. "
        "The one that runs is kept in the list, for Paper, and the server starts with a new world."
    )
    assert post(action="server", server="vanilla", yes=True)[1]["dropped"] == ["Chunky"]
    assert saved(app) == {"eula": True}
    assert post(action="burn")[0] == 400 and post(action="server", server="forge")[0] == 400
    assert client.call("/api/game/mods", {"name": "terraria", "action": "server", "server": "paper"})[0] == 400
    logged = capfd.readouterr().err
    assert "nullbill-web: game minecraft: mods add by the page" in logged


def test_the_page_takes_newer_versions_only_when_asked(site):
    app, client = site
    mods_site(app, client)
    post = lambda **body: client.call("/api/game/mods", {"name": "minecraft", **body})  # noqa: E731
    post(action="server", server="fabric")
    post(action="add", project="lithium")
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    config["games"]["minecraft"]["mods"][0] |= {"sha512": "0" * 128, "version": "0.1", "file": "old-lithium.jar"}
    (app.home / "config.yaml").write_text(yaml.safe_dump(config))
    answer = post(action="check")[1]
    assert answer["updates"] == [{"project": "lithium", "title": "Lithium", "to": "mc26.3-0.26.3-fabric", "from": "0.1"}]
    assert saved(app)["mods"][0]["file"] == "old-lithium.jar"
    answer = post(action="update")[1]
    assert answer["updated"][0]["from"] == "0.1" and saved(app)["mods"][0]["file"] == "lithium-fabric-0.26.3+mc26.3.jar"
    assert post(action="update")[1]["updates"] == []
    # Modrinth away: said in its own words, nothing changed.
    app.modrinth = lambda path, params=None: (_ for _ in ()).throw(NullBillError("Modrinth did not answer (timed out)"))
    status, answer = post(action="add", project="chunky")
    assert status == 400 and answer["error"] == "Modrinth did not answer (timed out)" and len(saved(app)["mods"]) == 1


# The agent.


def test_the_agent_reads_searches_and_changes_a_servers_mods_as_a_change(agent, monkeypatch):
    app, client, _ = chosen(agent, "auto")
    config = {"now": with_kind("fabric")}
    monkeypatch.setattr(app, "config", lambda: copy.deepcopy(config["now"]))
    monkeypatch.setattr(app, "_save_config", lambda new: config.update(now=new))
    monkeypatch.setattr(app, "plan", lambda: {})
    app.modrinth = modrinth
    said = result(call(client, "game_mods", name="minecraft", action="list"))
    assert "server: fabric" in said and "mods: []" in said and "not known" in said
    assert "project: lithium" in result(call(client, "game_mods", name="minecraft", action="search", query="lithium"))
    view = call(client, "game_mods", name="minecraft", action="add", project="chunky")
    assert kinds(view, "ran")[-1]["title"] == "Add Chunky to Minecraft"
    assert "saved: Minecraft runs fabric with mods: Chunky 1.5.66, Fabric API 0.166.0+26.3." in result(view)
    assert "start_job apply with stage games" in result(view)
    assert [m["project"] for m in config["now"]["games"]["minecraft"]["mods"]] == ["chunky", "fabric-api"]
    assert "updates: none" in result(call(client, "game_mods", name="minecraft", action="check"))
    assert "nothing to update" in result(call(client, "game_mods", name="minecraft", action="update"))
    # Another kind drops the list: the agent is told to ask first.
    said = result(call(client, "game_mods", name="minecraft", action="server", server="paper"))
    assert "Chunky, Fabric API leave the list" in said and "only Paper opens that world" in said and "yes=true" in said
    assert config["now"]["games"]["minecraft"]["server"] == "fabric"
    view = call(client, "game_mods", name="minecraft", action="server", server="paper", yes=True)
    assert kinds(view, "ran")[-1]["title"] == "Run Paper on Minecraft" and "mods" not in config["now"]["games"]["minecraft"]
    assert "is no Minecraft server" in result(call(client, "game_mods", name="terraria", action="list"))
    assert "action is list, search" in result(call(client, "game_mods", name="minecraft", action="upload"))
    assert "Modrinth has nothing of that name" in result(call(client, "game_mods", name="minecraft", action="add", project="nope"))


def test_the_agent_asks_in_manual_mode_with_what_comes_along(agent, monkeypatch):
    app, client, _ = chosen(agent)
    config = {"now": with_kind("fabric")}
    monkeypatch.setattr(app, "config", lambda: copy.deepcopy(config["now"]))
    monkeypatch.setattr(app, "_save_config", lambda new: config.update(now=new))
    monkeypatch.setattr(app, "plan", lambda: {})
    app.modrinth = modrinth
    view = call(client, "game_mods", name="minecraft", action="add", project="chunky")
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["title"]) == ("change", "Add Chunky to Minecraft")
    assert proposal["lines"] == [
        "Chunky 1.5.66 (Chunky-Fabric-1.5.66.jar)",
        "Fabric API 0.166.0+26.3 (fabric-api-0.166.0+26.3.jar), which another needs",
        "From Modrinth, taken by its SHA-512; someone else's program, run inside this game server only",
        "The server gets it with setup's games stage, which restarts it",
    ]
    assert "mods" not in config["now"]["games"]["minecraft"]
    approved(client, proposal["id"])
    assert len(config["now"]["games"]["minecraft"]["mods"]) == 2
