"""Several servers of one game (docs/adr/0016-more-game-servers.md): what
config.yaml's `games.<name>.game` becomes in the model and the plan, the
role's variables, the page's rows and form, and the agent's tools."""

import copy
import json
import time
from pathlib import Path

import pytest
import test_apply
import test_chat
import test_web
import yaml
from fixturedata import fixture_config, games_config

from nullbill import apply, custom, games
from nullbill.errors import NullBillError
from nullbill.firewall import render_firewall
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.tailnet import render_tailnet
from nullbill.web import services, workloads
from nullbill.web.server import App

site = test_web.site
agent = test_chat.agent
ctx = test_apply.ctx
CREATIVE = {"game": "minecraft", "id": 64, "title": "Creative world", "settings": {"gamemode": "creative"}, "admins": ["owner_b"]}


def more_config() -> dict:
    """The games fixture with a second Minecraft a friend is given, a
    second Factorio on demand and a second Terraria that is off."""
    config = games_config()
    config["games"] |= {
        "minecraft-creative": copy.deepcopy(CREATIVE),
        "factorio-2": {"game": "factorio", "id": 65, "title": "Second factory", "space_age": True},
        "terraria-master": {"game": "terraria", "id": 66, "title": "Master mode", "settings": {"difficulty": 2}},
    }
    config["intent"]["workloads"] += ["minecraft-creative", "factorio-2"]
    config["people"]["friend-c"]["services"] = ["minecraft-creative", "terraria"]
    config["overrides"] = {"minecraft-creative": {"memory_mib": 6144}}
    config["power"] = {"factorio-2": {"run": "on-demand"}}
    return config


def planned(facts, config=None):
    model = build_model(config or more_config())
    return strip_notes(build_plan(model, facts("large"))), model


def saved(app):
    return yaml.safe_load((app.home / "config.yaml").read_text())


# The model and the plan.
def test_a_further_server_is_a_game_server_like_the_catalogs(facts):
    plan, model = planned(facts)
    assert games.names(model) == ["minecraft", "terraria", "factorio", "minecraft-creative", "factorio-2", "terraria-master"]
    assert games.more(model) == ["minecraft-creative", "factorio-2", "terraria-master"]
    assert [games.kind(model, n) for n in games.names(model)] == ["minecraft", "terraria", "factorio", "minecraft", "factorio", "terraria"]
    svc, first = model["services"]["minecraft-creative"], model["services"]["minecraft"]
    # The game's own guest, port and allow-list; its own address, workload and title.
    assert (svc["id"], svc["workload"], svc["share"]["title"]) == (64, "minecraft-creative", "Creative world")
    assert all(svc[k] == first[k] for k in ("kind", "zone", "floor", "ports", "proxy_allow"))
    assert svc["share"]["note"] == first["share"]["note"]
    # What is the game's is inherited: the pinned download, Java, the agreement. Settings and admins are each server's.
    game, base = model["games"]["minecraft-creative"], model["games"]["minecraft"]
    assert all(game[k] == base[k] for k in ("version", "url", "sha256", "java", "eula"))
    assert game["settings"] == {"gamemode": "creative"} and game["admins"] == ["owner_b"] and base["admins"] == ["owner_a"]
    assert model["games"]["terraria-master"]["settings"] == {"difficulty": 2} and "admins" not in model["games"]["terraria-master"]
    assert model["power"]["factorio-2"] == {"title": "Second factory", "run": "on-demand", "stop": "auto", "idle_minutes": 30}
    assert model["power"]["minecraft-creative"]["title"] == "Creative world"
    # They are not services of the owner's own: no setup script, no custom stage.
    assert custom.names(model) == [] and custom.workloads(model) == [] and not plan.get("custom")

    assert games.planned(plan, model) == ["minecraft", "terraria", "factorio", "minecraft-creative", "factorio-2"]
    assert plan["games"]["minecraft-creative"] == {
        "title": "Creative world",
        "version": "26.3",
        "port": "tcp/25565",
        "game": "minecraft",
        "heap_mib": 6144 - games.JVM_OVERHEAD_MIB,
        "admins": ["owner_b"],
    }
    assert plan["games"]["factorio-2"]["space_age"] is True and plan["games"]["factorio"]["space_age"] is False
    assert "terraria-master" not in plan["games"] and "game" not in plan["games"]["minecraft"]
    guest = plan["guests"]["minecraft-creative"]
    assert (guest["zone"], guest["ip"], guest["memory_mib"]) == ("games", "10.42.8.64", 6144)
    assert plan["guests"]["minecraft"]["ip"] == "10.42.8.25"


def test_each_server_gets_its_games_role_with_its_own_settings(facts):
    plan, model = planned(facts)
    first, second = games.variables(plan, model, "minecraft"), games.variables(plan, model, "minecraft-creative")
    assert (
        games.playbook(model, "minecraft-creative") == "game-minecraft.yml" and games.playbook(model, "factorio-2") == "game-factorio.yml"
    )
    # The same variables by name, so the role needs to know nothing of it.
    assert set(first) == set(second)
    assert second["game_minecraft_port"] == 25565 and second["game_minecraft_heap_mib"] == 5120 and second["game_minecraft_eula"] is True
    assert second["game_minecraft_settings"] == {
        "white-list": False,
        "motd": "Creative world",
        "gamemode": "creative",
        "server-port": 25565,
        "online-mode": True,
        "enable-rcon": False,
        "enable-query": False,
    }
    # The first server's are untouched by the second's.
    assert first["game_minecraft_settings"]["motd"] == "Minecraft on NullBill" and first["game_minecraft_settings"]["difficulty"] == "hard"
    assert "gamemode" not in first["game_minecraft_settings"]
    factory = games.variables(plan, model, "factorio-2")
    assert factory["game_factorio_space_age"] is True and factory["game_factorio_settings"]["name"] == "Second factory"
    assert games.admin_command(model, "minecraft-creative", "owner_b") == "op owner_b"
    assert games.address(plan, "minecraft-creative", model, by_name=True) == "minecraft-creative.nullbill.internal:25565"
    labels = [label for label, _, _ in games.commands(plan, model)]
    assert "minecraft-creative runs version 26.3 and answers" in labels
    assert "minecraft-creative reaches sessionserver.mojang.com through the proxy" in labels
    assert "minecraft-creative's admins are those in config.yaml" in labels
    assert not any(label.startswith("terraria-master") for label in labels)


def test_the_firewall_and_the_tailnet_treat_it_as_any_game_server(facts):
    plan, model = planned(facts)
    files = render_firewall(plan, model)
    vmid = plan["guests"]["minecraft-creative"]["vmid"]
    rules = files[f"{vmid}.fw"]
    assert "-dport 25565" in rules and "-dport 22 " not in rules
    policy = json.loads(render_tailnet(plan, model)["policy.json"])
    given = [(g["dst"], g["ip"]) for g in policy["grants"] if g["src"] == ["friend-c@example.internal"]]
    # The friend was given the second Minecraft and Terraria, never the first Minecraft.
    assert sorted(given) == [(["10.42.8.26"], ["tcp:7777"]), (["10.42.8.64"], ["tcp:25565"])]


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        ({"game": "chess", "id": 64}, "games.x"),
        ({"game": "minecraft"}, "'id' is a required property"),
        ({"game": "minecraft", "id": 30}, "games.x.id"),
        ({"game": "minecraft", "id": 64, "eula": True}, "the agreement is given once, as games.minecraft.eula"),
        ({"game": "terraria", "id": 64, "admins": ["a_b_c"]}, "games.x"),
        ({"game": "minecraft", "id": 64, "settings": {"motd": "a\nb"}}, "games.x: settings.motd"),
    ],
)
def test_a_further_server_that_is_not_one_is_refused(entry, said):
    config = games_config()
    config["games"]["x"] = entry
    with pytest.raises(NullBillError, match=said):
        build_model(config)


def test_a_name_something_else_has_is_refused_and_so_is_what_nullbill_sets(facts):
    config = games_config()
    config["games"]["forgejo"] = {"game": "minecraft", "id": 64}
    with pytest.raises(NullBillError, match="games.forgejo: a service has this name already"):
        build_model(config)
    config = games_config()
    config["games"]["minecraft"]["game"] = "minecraft"
    with pytest.raises(NullBillError, match="games.minecraft: Additional properties are not allowed"):
        build_model(config)
    config = more_config()
    config["games"]["minecraft-creative"]["settings"]["online-mode"] = False
    with pytest.raises(NullBillError, match="games.minecraft-creative.settings.online-mode: NullBill sets this itself"):
        build_plan(build_model(config), facts("large"))
    # Two servers at one address: plan's own rule for every service.
    config = more_config()
    config["games"]["factorio-2"]["id"] = 64
    with pytest.raises(NullBillError, match="64"):
        build_plan(build_model(config), facts("large"))
    # The agreement is asked once, however many Minecraft servers wait for it.
    config = more_config()
    del config["games"]["minecraft"]["eula"]
    with pytest.raises(NullBillError) as raised:
        build_plan(build_model(config), facts("large"))
    assert sum("games.minecraft.eula" in m for m in raised.value.messages) == 1
    # A service of the owner's own cannot take a game server's name.
    with pytest.raises(NullBillError, match="one of NullBill's own"):
        custom.define(more_config(), build_model(more_config()), "minecraft-creative", {"title": "X"})


def test_twenty_minecraft_servers_plan_and_render(facts):
    config = games_config()
    model = build_model(config)
    for n in range(1, 21):
        config, name = games.add(config, model, "minecraft", f"World {n}")
        config["intent"]["workloads"].append(name)
        config.setdefault("power", {})[name] = {"run": "on-demand"}
        model = build_model(config)
    plan = strip_notes(build_plan(model, facts("large")))
    worlds = [n for n in plan["games"] if n.startswith("minecraft-world-")]
    assert len(worlds) == 20 and len({plan["guests"][n]["ip"] for n in worlds}) == 20
    assert len(render_firewall(plan, model)) > 20
    view = workloads.options(model, facts("large"), existing=set())
    assert [i["group"] for i in view["items"] if i["name"] in worlds] == ["games"] * 20


# Adding, changing and forgetting one.
def test_add_names_the_server_from_its_title_and_gives_it_a_free_address():
    config = games_config()
    model = build_model(config)
    new, name = games.add(config, model, "minecraft", "  Creative   world ")
    assert name == "minecraft-creative-world" and new["games"][name] == {"game": "minecraft", "id": 64, "title": "Creative world"}
    assert config == games_config()
    again, second = games.add(new, build_model(new), "terraria", "Terraria: hard!")
    assert second == "terraria-hard" and again["games"][second]["id"] == 65
    # A title that gives no name, and one that gives a name in use.
    assert games.add(config, model, "factorio", "!!!")[1] == "factorio-2"
    with pytest.raises(NullBillError, match="another game server is called minecraft already"):
        games.add(config, model, "minecraft", "minecraft")
    with pytest.raises(NullBillError, match="another game server is called creative WORLD already"):
        games.add(new, build_model(new), "minecraft", "creative WORLD")
    with pytest.raises(NullBillError, match="NullBill has no server for 'valheim'; it has minecraft, terraria, factorio"):
        games.add(config, model, "valheim", "Vikings")
    with pytest.raises(NullBillError, match="NullBill has no server for 'minecraft-creative-world'"):
        games.add(new, build_model(new), name, "Third")
    with pytest.raises(NullBillError, match="1 to 40 characters"):
        games.add(config, model, "minecraft", "x" * 41)
    full = {**model, "services": {**model["services"], **{f"s{i}": {"id": i, "workload": f"s{i}"} for i in custom.OWN_IDS}}}
    with pytest.raises(NullBillError, match="all 126 addresses for further game servers and services of your own are taken"):
        games.add(config, full, "minecraft", "One more")


def test_forget_takes_every_trace_of_a_further_server_and_no_other():
    config = more_config()
    model = build_model(config)
    out = games.forget(config, model, "minecraft-creative")
    assert "minecraft-creative" not in out["games"] and "overrides" not in out and "minecraft-creative" not in out["intent"]["workloads"]
    assert out["people"]["friend-c"]["services"] == ["terraria"] and out["games"]["minecraft"] == config["games"]["minecraft"]
    assert "power" not in games.forget(config, model, "factorio-2")
    with pytest.raises(NullBillError, match="minecraft is not a further game server"):
        games.forget(config, model, "minecraft")
    build_model(out)


def test_settings_from_a_form_keep_only_what_the_owner_changed():
    model = build_model(more_config())
    name = "minecraft-creative"
    options = {o["key"]: o for o in games.options(model, name)}
    assert options["gamemode"]["value"] == "creative" and options["gamemode"]["default"] == "survival"
    assert options["gamemode"]["choices"][1] == ["creative", "Creative"] and options["max-players"]["type"] == "number"
    assert options["motd"]["default"] == "Creative world" and options["white-list"] | {"label": ""} == {
        "key": "white-list",
        "label": "",
        "type": "switch",
        "value": False,
        "default": False,
        "new_world": False,
    }
    assert options["level-seed"]["new_world"] and options["hardcore"]["type"] == "switch"
    terraria = {o["key"]: o for o in games.options(model, "terraria-master")}
    assert terraria["difficulty"]["value"] == 2 and terraria["autocreate"]["choices"] == [[1, "Small"], [2, "Medium"], [3, "Large"]]
    # As the page sends them: every named option, with its start where the owner changed nothing.
    form = {o["key"]: o["value"] for o in options.values()} | {
        "max-players": "6",
        "difficulty": "Hard",
        "hardcore": True,
        "level-seed": "42",
    }
    got = games.read_settings(
        model, name, form, "spawn-protection = 0\n# a note\n\nallow-flight=true\nresource-pack = https://example.internal/p.zip\n"
    )
    assert got == {
        "spawn-protection": 0,
        "allow-flight": True,
        "resource-pack": "https://example.internal/p.zip",
        "gamemode": "creative",
        "difficulty": "hard",
        "max-players": 6,
        "hardcore": True,
        "level-seed": "42",
    }
    assert games.read_settings(model, "terraria-master", {"autocreate": "3", "difficulty": 2, "maxplayers": 8}) == {
        "autocreate": 3,
        "difficulty": 2,
    }
    # What the box holds comes back as it was typed.
    model["games"][name]["settings"] = got
    assert (
        games.other_settings(model, name) == "spawn-protection = 0\nallow-flight = true\nresource-pack = https://example.internal/p.zip\n"
    )
    with pytest.raises(NullBillError) as raised:
        games.read_settings(
            model,
            name,
            {"gamemode": "easy", "max-players": 0, "hardcore": "maybe", "online-mode": False, "bad key": 1, "motd": "x" * 201},
            "server-port = 1\nno equals sign\n",
        )
    assert raised.value.messages == [
        "Creative world, other settings, line 2: write it as name = value",
        "Creative world: NullBill sets server-port itself, so that only the people you let in reach the server",
        "Creative world: Game mode (gamemode) is one of survival, creative, adventure, spectator",
        "Creative world: Players at once (max-players) is a whole number from 1 to 200",
        "Creative world: Hardcore: one life each (hardcore) is true or false",
        "Creative world: NullBill sets online-mode itself, so that only the people you let in reach the server",
        "Creative world: 'bad key' is not the name of a setting",
        "Creative world: motd is one line of at most 200 characters",
    ]
    assert games.read_memory(model, name, 3072) is None and games.read_memory(model, name, "") is None
    assert games.read_memory(model, name, "8192") == 8192
    for bad in (1024, "much", True, 70000):
        with pytest.raises(NullBillError, match="Creative world: its memory is from 3 to 64 GiB"):
            games.read_memory(model, name, bad)


def test_every_named_option_is_one_the_schema_takes_and_nullbill_does_not_set():
    model = build_model(more_config())
    for name in games.names(model):
        game = games.kind(model, name)
        for option in games.options(model, name):
            assert not games.managed(game, option["key"]), option["key"]
            assert games.SETTING.fullmatch(option["key"])
            assert option["default"] is not None, option["key"]
            # Its start is a value it takes itself.
            assert games.read_settings(model, name, {option["key"]: option["default"]}) == {}
        assert set(games.DEFAULTS[game]) - {"secure"} <= {o["key"] for o in games.OPTIONS[game]} | {"white-list"}


def test_each_role_check_asks_the_real_server_for_the_options_the_page_offers():
    """The names in OPTIONS are the vendors', and only their servers say
    whether one exists: each role's check looks its list up in the pinned
    server, and this keeps that list the page's."""
    root = Path(__file__).parent.parent / "ansible" / "roles"
    for game, offered in games.OPTIONS.items():
        check = (root / f"game_{game}" / "tests" / "check.sh").read_text()
        (line,) = [text for text in check.splitlines() if text.startswith('options="')]
        assert line.removeprefix('options="').removesuffix('"').split() == [o["key"] for o in offered], game


# apply.
def test_the_games_stage_sets_up_each_server_with_its_games_playbook(ctx):
    c = ctx("large", config=more_config())
    apply.stage_games(c)
    plays = [(call[1], list(call[2])) for call in c.reach.calls if call[0] == "playbook"]
    assert plays == [
        ("game-minecraft.yml", ["minecraft"]),
        ("game-terraria.yml", ["terraria"]),
        ("game-factorio.yml", ["factorio"]),
        ("game-minecraft.yml", ["minecraft-creative"]),
        ("game-factorio.yml", ["factorio-2"]),
    ]
    servers = c.reach.game_servers
    assert servers["2064"]["typed"] == ["op owner_b"] and servers["2025"]["typed"] == ["op owner_a"]
    assert "games: Creative world 26.3 answers at 10.42.8.64:25565" in c.log
    assert "games: Second factory 2.1.24 answers at 10.42.8.65:34197" in c.log


# The page.
def test_what_to_run_has_a_row_for_each_server_with_what_the_owner_can_set(facts):
    model = build_model(more_config())
    view = workloads.options(model, facts("large"), existing={"router", "minecraft-creative"})
    rows = {i["name"]: i for i in view["items"]}
    assert [i["name"] for i in view["items"] if i["group"] == "games"] == games.names(model)
    row = rows["minecraft-creative"]
    assert (row["title"], row["chosen"], row["on_server"]) == ("Creative world", True, ["minecraft-creative"])
    assert row["what"] == rows["minecraft"]["what"] and "tick it for each friend" in row["on"] and row["off"] == rows["minecraft"]["off"]
    assert row["memory_mib"] == 3072 and "Java gets 5.0 GiB" in row["note"]
    game = row["game"]
    assert (game["kind"], game["kind_title"], game["more"], game["removable"]) == ("minecraft", "Minecraft", True, False)
    assert (game["memory_mib"], game["floor_mib"], game["file"]) == (6144, 3072, "server.properties")
    assert game["eula"]["agreed"] and game["eula"]["text"] == "I agree to the Minecraft EULA" and game["admins"] == ["owner_b"]
    assert rows["minecraft"]["game"]["more"] is False and "removable" not in rows["minecraft"]["game"]
    # Off and with no guest on the server: it can be removed. Terraria keeps no admins.
    assert rows["terraria-master"]["game"]["removable"] and "admins" not in rows["terraria-master"]["game"]
    assert rows["terraria-master"]["chosen"] is False
    assert view["games"] == {"kinds": [{"name": n, "title": t} for n, t in games.TITLE.items()], "full": None}
    when = {w["name"]: w for w in view["power"]}
    assert when["factorio-2"]["title"] == "Second factory" and when["factorio-2"]["run"] == "on-demand" and when["factorio-2"]["auto"]
    assert workloads.loss("minecraft-creative", model) == rows["minecraft"]["off"]
    assert workloads.added(model) == ["minecraft-creative", "factorio-2", "terraria-master"]


def test_the_services_page_says_how_to_join_each_server(facts):
    plan, model = planned(facts)
    items = {i["name"]: i for i in services.services(plan)["items"]}
    entry = items["minecraft-creative"]
    assert entry["title"] == "Creative world" and entry["what"].startswith("A Minecraft server for you and the people you let in.")
    assert entry["addresses"] == ["In the game: 10.42.8.64:25565"]
    assert any("Multiplayer, Add Server" in s["text"] and "Minecraft: Java Edition 26.3" in s["text"] for s in entry["steps"])
    assert any("beside Creative world" in s["text"] and s["done"] for s in entry["steps"])
    assert any(s.get("code") == "pct exec 2064 -- nb-game console say hello" for s in entry["steps"])
    factory = items["factorio-2"]
    assert any("Every player needs the Space Age expansion." in s["text"] for s in factory["steps"])
    assert any("games.factorio.space_age" in s["text"] for s in items["factorio"]["steps"])
    named = {i["name"]: i for i in services.services(plan, by_name=True)["items"]}
    assert named["minecraft-creative"]["addresses"][0] == "In the game: minecraft-creative.nullbill.internal:25565"


def body(app, **games_form):
    return test_web.OWNER | {"workloads": saved(app)["intent"]["workloads"], "games": games_form}


def test_the_page_adds_a_server_saves_its_settings_and_removes_it_once_gone(site):
    from test_web import OWNER, probed, sign_in

    app, client = site
    assert client.call("/api/game-server", {"game": "minecraft", "title": "Creative world"})[0] == 401
    sign_in(app, client)
    probed(app)
    assert client.call("/api/choices", OWNER | {"workloads": ["agents", "minecraft"], "games": {"minecraft": {"eula": True}}})[0] == 200
    status, answer = client.call("/api/game-server", {"game": "minecraft", "title": "Creative world"})
    assert status == 200 and answer["added"]["name"] == "minecraft-creative-world" and answer["added"]["title"] == "Creative world"
    name = "minecraft-creative-world"
    assert saved(app)["games"][name] == {"game": "minecraft", "id": 64, "title": "Creative world"}
    assert saved(app)["intent"]["workloads"] == ["agents", "minecraft", name]
    status, answer = client.call("/api/game-server", {"game": "minecraft", "title": "creative world"})
    assert status == 400 and "another game server is called creative world already" in answer["error"]
    assert client.call("/api/game-server", {"game": "doom", "title": "E1M1"})[0] == 400

    row = next(i for i in client.call("/api/state")[1]["options"]["items"] if i["name"] == name)
    assert row["chosen"] and row["group"] == "games" and row["game"]["more"] and not row["game"]["removable"]
    # The form as the page sends it: every named option, the box, the memory, the title.
    form = {
        "admins": "owner_a, Owner_B",
        "title": "Creative",
        "memory_mib": 4096,
        "settings": {o["key"]: o["value"] for o in row["game"]["options"]} | {"gamemode": "creative", "max-players": "6"},
        "other": "spawn-protection = 0\n",
    }
    status, view = client.call("/api/choices/preview", body(app, **{name: form}))
    assert status == 200 and next(i for i in view["items"] if i["name"] == name)["memory_mib"] == 2048
    assert client.call("/api/choices", body(app, **{name: form}))[0] == 200
    config = saved(app)
    assert config["games"][name] == {
        "game": "minecraft",
        "id": 64,
        "title": "Creative",
        "admins": ["owner_a", "Owner_B"],
        "settings": {"spawn-protection": 0, "gamemode": "creative", "max-players": 6},
    }
    assert config["overrides"] == {name: {"memory_mib": 4096}} and config["games"]["minecraft"] == {"eula": True}
    plan = yaml.safe_load((app.home / "plan.yaml").read_text())
    assert plan["games"][name]["heap_mib"] == 3072 and plan["guests"][name]["memory_mib"] == 4096
    # Saved again as shown, nothing changes; a key the form leaves out stays.
    row = next(i for i in client.call("/api/state")[1]["options"]["items"] if i["name"] == name)
    assert row["title"] == "Creative" and row["game"]["other"] == "spawn-protection = 0\n" and row["game"]["memory_mib"] == 4096
    same = {
        "admins": ", ".join(row["game"]["admins"]),
        "title": row["title"],
        "memory_mib": row["game"]["memory_mib"],
        "settings": {o["key"]: o["value"] for o in row["game"]["options"]},
        "other": row["game"]["other"],
    }
    assert client.call("/api/choices", body(app, **{name: same}))[0] == 200 and saved(app) == config
    assert client.call("/api/choices", body(app, **{name: {"memory_mib": None}}))[0] == 200
    assert "overrides" not in saved(app) and saved(app)["games"][name]["settings"] == config["games"][name]["settings"]

    # The agreement is the game's: unticked on the second server's row, it is gone for both.
    assert client.call("/api/choices", body(app, **{name: {"eula": False}}))[0] == 200
    assert "minecraft" not in saved(app)["games"]
    assert client.call("/api/choices", body(app, **{name: {"eula": True}}))[0] == 200
    assert saved(app)["games"]["minecraft"] == {"eula": True} and "eula" not in saved(app)["games"][name]

    # Every problem at once, each its own message; nothing is saved.
    before = saved(app)
    bad = {"settings": {"max-players": 0, "server-port": 1}, "memory_mib": 12, "admins": "not a name!"}
    status, answer = client.call("/api/choices", body(app, **{name: bad, "minecraft": {"settings": {"difficulty": "brutal"}}}))
    assert status == 400 and saved(app) == before
    # The page lists them under what did not happen: a line each, not one
    # long sentence, and no refusal hides the next.
    assert answer["headline"] == "Some game server settings cannot be used, so nothing was saved"
    assert answer["errors"] == [
        "Creative: admins are player names, as shown in the game, separated by commas",
        "Creative: Players at once (max-players) is a whole number from 1 to 200",
        "Creative: NullBill sets server-port itself, so that only the people you let in reach the server",
        "Creative: its memory is from 3 to 64 GiB (3072 to 65536 MiB)",
        "Minecraft: Difficulty (difficulty) is one of peaceful, easy, normal, hard",
    ]
    # One problem is one line, with no list.
    status, answer = client.call("/api/choices", body(app, **{name: {"settings": {"server-port": 1}}}))
    assert status == 400 and answer == {
        "error": "Creative: NullBill sets server-port itself, so that only the people you let in reach the server"
    }

    # On: refused. Off but still on the server: refused. Off and gone: removed.
    status, answer = client.call("/api/game-server/remove", {"name": name})
    assert status == 400 and "turned on" in answer["error"]
    assert client.call("/api/choices", OWNER | {"workloads": ["agents", "minecraft"]})[0] == 200
    assert name in saved(app)["games"] and name not in saved(app)["intent"]["workloads"]
    (app.home / "guests.json").write_text(json.dumps({"guests": ["router", name]}))
    status, answer = client.call("/api/game-server/remove", {"name": name})
    assert status == 400 and "still on the server" in answer["error"]
    (app.home / "guests.json").write_text(json.dumps({"guests": ["router"]}))
    assert client.call("/api/game-server/remove", {"name": name}) == (200, {"ok": True, "removed": "Creative"})
    assert name not in saved(app)["games"]
    assert client.call("/api/game-server/remove", {"name": "minecraft"})[0] == 400


def test_when_it_runs_is_saved_for_a_further_server_as_what_differs_from_its_game(site):
    from test_web import OWNER, probed, sign_in

    app, client = site
    sign_in(app, client)
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200
    name = app.add_game("terraria", "Second world")["name"]
    rule = {"run": "on-demand", "stop": "auto", "idle_minutes": 30}
    assert client.call("/api/choices", OWNER | {"workloads": ["agents", name], "power": {name: rule}})[0] == 200
    assert saved(app)["power"] == {name: {"run": "on-demand"}}
    assert client.call("/api/choices", OWNER | {"workloads": ["agents", name], "power": {name: rule | {"run": "always"}}})[0] == 200
    assert "power" not in saved(app)


def test_adding_one_needs_saved_choices(tmp_path):
    app = App(tmp_path, command=["true"])
    with pytest.raises(NullBillError, match="nothing is chosen yet"):
        app.add_game("minecraft", "Creative")


# The agent.
def test_the_agent_adds_game_servers_as_a_change_and_never_agrees_for_the_owner(agent):
    from test_chat import approve, kinds, say, settle, signed_in
    from test_web import OWNER, probed

    app, client, _ = signed_in(agent, "manual")
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200

    def call(**want):
        return "CALL add_game_server " + json.dumps({"why": "the owner asked for it", **want})

    # Minecraft without the owner's agreement: the plan says so, and nothing is asked or saved.
    out = kinds(say(client, call(game="minecraft", title="Creative world")), "tool_done")[-1]["output"]
    assert "games.minecraft.eula" in out and "games" not in saved(app)
    assert client.call("/api/choices", OWNER | {"games": {"minecraft": {"eula": True}}})[0] == 200
    want = {"game": "minecraft", "title": "Creative world", "settings": {"gamemode": "creative", "max-players": 6}, "admins": ["owner_a"]}
    view = say(client, call(**want, memory_mib=4096))
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["word"], proposal["act"]) == ("change", "", "game")
    assert proposal["title"] == "Add the game server Creative world"
    assert proposal["lines"] == [
        "A Minecraft 26.3 server of its own, called Creative world, with its own world.",
        "A container in the games zone: 2 cores, 4.0 GiB memory, 16 GiB disk.",
        "Opens tcp/25565 to you and the people you tick it for, as every Minecraft server does. No program comes with it "
        "but the game's own server, pinned by NullBill.",
        "Settings: gamemode creative, max-players 6.",
        "Admins: owner_a.",
        "It is on: the next setup run makes it.",
    ]
    assert list(saved(app)["games"]) == ["minecraft"]
    assert approve(client, proposal["id"])[0] == 200
    settle(client)
    name = "minecraft-creative-world"
    for _ in range(100):
        if name in saved(app).get("games", {}):
            break
        time.sleep(0.05)
    settle(client)
    assert saved(app)["games"][name] == {"game": "minecraft", "id": 64, "title": "Creative world", "admins": ["owner_a"]} | {
        "settings": {"gamemode": "creative", "max-players": 6}
    }
    assert saved(app)["overrides"] == {name: {"memory_mib": 4096}} and name in saved(app)["intent"]["workloads"]

    # What NullBill sets itself, and what is no game or no key of the tool, never reaches the owner.
    for bad, said in (
        ({"game": "minecraft", "title": "Open", "settings": {"online-mode": False}}, "NullBill sets online-mode itself"),
        ({"game": "valheim", "title": "Vikings"}, "NullBill has no server for 'valheim'"),
        ({"game": "terraria", "title": "T", "eula": True}, "add_game_server takes"),
        ({"game": "minecraft", "title": "creative world"}, "another game server is called"),
    ):
        view = say(client, call(**bad))
        assert not kinds(view, "proposal")[1:] or kinds(view, "proposal")[-1]["id"] == proposal["id"]
        assert said in kinds(view, "tool_done")[-1]["output"]
    status = yaml.safe_load(kinds(say(client, "CALL status {}"), "tool_done")[-1]["output"])
    assert next(i for i in status["what_to_run"] if i["name"] == name)["game_server_of"] == "minecraft"
    assert next(i for i in status["what_to_run"] if i["name"] == "terraria")["game_server_of"] == "terraria"


def test_in_auto_the_agent_adds_several_at_once_and_removes_one_only_when_gone(agent):
    from test_chat import kinds, say, signed_in
    from test_web import OWNER, probed

    app, client, _ = signed_in(agent, "auto")
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200
    for title in ("Expert", "Master"):
        want = {"game": "terraria", "title": title, "settings": {"difficulty": 1 if title == "Expert" else 2}, "on": title == "Expert"}
        view = say(client, "CALL add_game_server " + json.dumps(want))
        assert kinds(view, "ran")[-1]["title"] == f"Add the game server {title}"
    config = saved(app)
    assert config["games"]["terraria-expert"]["settings"] == {"difficulty": 1} and config["games"]["terraria-master"]["id"] == 65
    assert "terraria-expert" in config["intent"]["workloads"] and "terraria-master" not in config["intent"]["workloads"]
    out = kinds(say(client, 'CALL remove_game_server {"name": "terraria-expert"}'), "tool_done")[-1]["output"]
    assert "turned on" in out and "terraria-expert" in saved(app)["games"]
    out = kinds(say(client, 'CALL remove_game_server {"name": "terraria"}'), "tool_done")[-1]["output"]
    assert "not a further game server" in out
    view = say(client, 'CALL remove_game_server {"name": "terraria-master", "why": "one is enough"}')
    assert kinds(view, "ran")[-1]["title"] == "Remove the game server Master"
    assert list(saved(app)["games"]) == ["terraria-expert"]
    # set_choices turns a further server on and off like any workload.
    view = say(client, 'CALL set_choices {"workloads": ["agents", "linux-ci"]}')
    assert "terraria-expert" not in saved(app)["intent"]["workloads"] and "terraria-expert" in saved(app)["games"]


def test_the_large_custom_fixture_carries_a_second_minecraft_for_the_container_checks(facts):
    from fixturedata import custom_config

    config = custom_config()
    model = build_model(config)
    plan = strip_notes(build_plan(model, facts("large")))
    assert games.more(model) == ["minecraft-creative"] and plan["games"]["minecraft-creative"]["game"] == "minecraft"
    assert fixture_config("large")["intent"]["workloads"] != config["intent"]["workloads"]
