"""Game servers: the plan's games section, the roles' variables and
verify's checks, on the large class with every game planned."""

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from fixturedata import games_config
from pinned import FACTORIO, MINECRAFT, TERRARIA

from nullbill import games, people, power
from nullbill.cli import plan_text
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

ROOT = Path(__file__).parent.parent
GOLDEN = Path(__file__).parent / "golden"
GAMES = ["minecraft", "terraria", "factorio"]


def planned(facts, config=None):
    model = build_model(config or games_config())
    return strip_notes(build_plan(model, facts("large"))), model


def errors(facts, config) -> str:
    with pytest.raises(NullBillError) as raised:
        build_plan(build_model(config), facts("large"))
    return str(raised.value)


def test_golden_games(facts):
    """Set UPDATE_GOLDEN=1 to rewrite; review the diff before committing it."""
    text = plan_text(build_model(games_config()), json.dumps(facts("large"), sort_keys=True).encode())
    path = GOLDEN / "large-games.plan.yaml"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text()


@pytest.mark.parametrize("name", GAMES)
def test_role_check_runs_with_the_plans_variables(facts, name):
    """Each game role's container check converges with tests/vars.json: keep
    it equal to what apply would pass. UPDATE_GOLDEN=1 rewrites it."""
    plan, model = planned(facts)
    want = json.dumps(games.variables(plan, model, name), indent=2) + "\n"
    path = ROOT / "ansible" / "roles" / f"game_{name}" / "tests" / "vars.json"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(want)
    assert path.read_text() == want


def test_every_game_has_a_service_a_role_and_a_playbook(facts):
    _, model = planned(facts)
    assert games.names(model) == GAMES
    for name in GAMES:
        assert model["services"][name]["workload"] == name
        assert model["services"][name]["zone"] == "games"
        assert (ROOT / "ansible" / games.playbook(model, name)).is_file()
        assert (ROOT / "ansible" / "roles" / f"game_{name}" / "tasks" / "main.yml").is_file()


def test_no_game_is_planned_unless_asked_for(facts):
    config = games_config()
    config["intent"]["workloads"] = [w for w in config["intent"]["workloads"] if w not in GAMES]
    del config["games"]
    plan, model = planned(facts, config)
    assert plan["games"] == {}
    assert games.planned(plan, model) == []
    assert "games" not in plan["zones"]
    assert games.commands(plan, model) == []


def test_one_game_alone(facts):
    config = games_config()
    config["intent"]["workloads"] = [w for w in config["intent"]["workloads"] if w not in ("minecraft", "factorio")]
    plan, model = planned(facts, config)
    assert list(plan["games"]) == ["terraria"]
    assert games.planned(plan, model) == ["terraria"]


def test_minecraft_waits_for_the_eula(facts):
    config = games_config()
    del config["games"]["minecraft"]["eula"]
    assert "games.minecraft.eula: set it to true once you agree to Minecraft's EULA (https://aka.ms/MinecraftEULA)" in errors(facts, config)


def test_the_eula_is_not_asked_of_a_server_nobody_planned(facts):
    config = games_config()
    config["intent"]["workloads"].remove("minecraft")
    del config["games"]["minecraft"]
    plan, _ = planned(facts, config)
    assert "minecraft" not in plan["games"]


@pytest.mark.parametrize(
    ("name", "key", "value"),
    [
        ("minecraft", "online-mode", False),
        ("minecraft", "server-port", 25566),
        ("minecraft", "enable-rcon", True),
        ("minecraft", "management-server-enabled", True),
        ("terraria", "upnp", 1),
        ("terraria", "worldpath", "/tmp"),
        ("factorio", "visibility", "public"),
        ("factorio", "require_user_verification", False),
        ("factorio", "token", "x"),
    ],
)
def test_settings_that_would_open_a_server_are_refused(facts, name, key, value):
    config = games_config()
    config["games"][name]["settings"][key] = value
    assert f"games.{name}.settings.{key}: NullBill sets this itself" in errors(facts, config)


def test_a_setting_is_one_line(facts):
    config = games_config()
    config["games"]["terraria"]["settings"]["motd"] = "hello\nupnp=1"
    with pytest.raises(NullBillError, match="games.terraria.settings.motd"):
        build_model(config)


def test_settings_are_defaults_then_the_owners_then_nullbills(facts):
    _, model = planned(facts)
    minecraft = games.settings(model, "minecraft")
    assert minecraft["motd"] == "Minecraft on NullBill"
    assert minecraft["difficulty"] == "hard"
    assert minecraft["online-mode"] is True
    assert minecraft["server-port"] == 25565
    terraria = games.settings(model, "terraria")
    assert terraria["worldname"] == "Fixture"
    assert terraria["autocreate"] == 1
    assert terraria["port"] == 7777
    assert games.settings(model, "factorio")["visibility"] == {"public": False, "lan": False}


def test_minecrafts_heap_follows_the_containers_memory(facts):
    config = games_config()
    plan, _ = planned(facts, config)
    assert plan["games"]["minecraft"]["heap_mib"] == plan["guests"]["minecraft"]["memory_mib"] - games.JVM_OVERHEAD_MIB
    config["overrides"] = {"minecraft": {"memory_mib": 6144}}
    plan, model = planned(facts, config)
    assert plan["games"]["minecraft"]["heap_mib"] == 5120
    assert games.variables(plan, model, "minecraft")["game_minecraft_heap_mib"] == 5120


def test_a_moved_port_reaches_the_server_and_the_plan(facts):
    config = games_config()
    config["services"] = {"terraria": {"ports": [{"port": 7778, "proto": "tcp", "from": ["role:owner", "shared"]}]}}
    plan, model = planned(facts, config)
    assert plan["games"]["terraria"]["port"] == "tcp/7778"
    assert games.settings(model, "terraria")["port"] == 7778
    assert games.address(plan, "terraria", model) == f"{plan['guests']['terraria']['ip']}:7778"
    assert games.address(plan, "terraria", model, by_name=True) == f"terraria.{plan['domain']}:7778"
    # And the friend who was given it.
    assert "terraria.nullbill.internal, port 7778" in people.instructions(model, plan, "friend-c", names=True)


def test_a_game_server_has_one_port(facts):
    config = games_config()
    both = [{"port": 7777, "proto": "tcp", "from": ["role:owner", "shared"]}, {"port": 7778, "proto": "tcp", "from": ["role:owner"]}]
    config["services"] = {"terraria": {"ports": both}}
    assert "services.terraria.ports: a game server has one port, for its players" in errors(facts, config)


def test_the_sign_in_services_must_pass_the_proxy(facts):
    config = games_config()
    config["services"] = {"minecraft": {"proxy_allow": ["piston-data.mojang.com"]}, "factorio": {"proxy_allow": ["factorio.com"]}}
    text = errors(facts, config)
    for host in games.MOJANG:
        assert f"services.minecraft.proxy_allow: {host} is missing" in text
    assert "services.factorio.proxy_allow: auth.factorio.com is missing" in text


def test_friends_reach_only_the_games_picked_for_them(facts):
    plan, model = planned(facts)
    assert plan["people"]["friend-c"] == {"role": "friend", "services": ["minecraft", "terraria"]}
    flows = plan["flows"]
    # Every game's port can be given to a person, and to nobody by a role but the owner.
    assert {f for f in flows if " -> minecraft " in f or " -> terraria " in f or " -> factorio " in f} == {
        "role:owner -> minecraft tcp/25565",
        "shared -> minecraft tcp/25565",
        "service:monitoring -> minecraft tcp/9100",
        "role:owner -> terraria tcp/7777",
        "shared -> terraria tcp/7777",
        "service:monitoring -> terraria tcp/9100",
        "role:owner -> factorio udp/34197",
        "shared -> factorio udp/34197",
        "service:monitoring -> factorio tcp/9100",
    }
    # A game guest starts nothing but its way to the proxy.
    assert [f for f in flows if f.startswith(("zone:games", "service:minecraft", "service:terraria", "service:factorio"))] == [
        "zone:games -> egress-proxy tcp/3128"
    ]
    assert people.reach(model, plan, "friend-c") == {"minecraft": ["tcp/25565"], "terraria": ["tcp/7777"]}


def test_a_friend_is_told_the_address_and_the_version(facts):
    model = build_model(games_config())
    plan = build_plan(model, facts("large"))
    text = people.instructions(model, plan, "friend-c", names=True)
    assert "minecraft.nullbill.internal:25565" in text and "If the name does not open: 10.42.8.25:25565" in text
    assert f"You need Minecraft: Java Edition {MINECRAFT}." in text
    assert "terraria.nullbill.internal, port 7777" in text and f"You need Terraria {TERRARIA} on a computer." in text
    assert "Factorio" not in text
    opens = people.opens(model, plan, "factorio", ["udp/34197"], names=False)
    assert opens["where"] == "10.42.8.27:34197" and f"You need Factorio {FACTORIO}," in opens["note"]


def test_admins_are_made_on_the_console_only_when_missing(facts):
    _, model = planned(facts)
    assert games.missing_admins(model, "minecraft", "") == ["owner_a"]
    assert games.missing_admins(model, "minecraft", "notch\nowner_a\n") == []
    assert games.missing_admins(model, "terraria", "") == []
    assert games.admin_command(model, "minecraft", "owner_a") == "op owner_a"
    assert games.admin_command(model, "factorio", "owner_a") == "/promote owner_a"
    config = games_config()
    config["games"]["minecraft"]["admins"] = ["Owner_A"]
    assert games.missing_admins(build_model(config), "minecraft", "owner_a\n") == []


def test_only_games_that_keep_admins_are_asked_for_them(facts):
    """nb-game admins fails in a guest whose game.json names no list
    (Terraria), so apply and verify may ask only a game config.yaml can give
    admins to. Its role's game.json, its console command and its schema
    entry must agree; apply's stand-in nb-game (test_apply.py) fails the same."""
    schema = yaml.safe_load((ROOT / "src" / "nullbill" / "data" / "schema.yaml").read_text())["$defs"]
    for name in GAMES:
        role = (ROOT / "ansible" / "roles" / f"game_{name}" / "tasks" / "main.yml").read_text()
        keeps = '"admins":' in role
        assert keeps == (name in games.ADMIN_COMMAND) == ("admins" in schema[f"game_{name}"]["properties"]), name
    plan, model = planned(facts)
    for label, command, _ in games.commands(plan, model):
        if "terraria" in label:
            assert "admins" not in command


def test_the_admins_check_reads_nb_game(facts, tmp_path, monkeypatch):
    """verify's shell line, run for real against a stand-in nb-game."""
    fake = tmp_path / "nb-game"
    fake.write_text("#!/bin/sh\ncat " + str(tmp_path / "admins") + "\n")
    fake.chmod(0o755)
    monkeypatch.setattr(games, "NB_GAME", str(fake))
    plan, model = planned(facts)
    (script,) = [c for label, c, _ in games._checks(plan, model, "minecraft") if label.endswith("config.yaml")]

    def run(listed: str) -> str:
        (tmp_path / "admins").write_text(listed)
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()

    assert run("notch\nowner_a\n") == "admins"
    assert run("owner_a\n") == "admins"
    assert run("notch\n") == "owner_a is not an admin"
    assert run("") == "owner_a is not an admin"
    assert run("owner_ab\n") == "owner_a is not an admin"


def test_verify_asks_each_planned_game(facts):
    plan, model = planned(facts)
    labels = [label for label, _, _ in games.commands(plan, model)]
    assert labels == [
        f"minecraft runs version {MINECRAFT} and answers",
        *[f"minecraft reaches {host} through the proxy" for host in games.MOJANG],
        "minecraft runs Mojang's own server",
        "minecraft's admins are those in config.yaml",
        f"terraria runs version {TERRARIA} and answers",
        f"factorio runs version {FACTORIO} and answers",
        "factorio's admins are those in config.yaml",
    ]
    vmids = {plan["guests"][name]["vmid"] for name in GAMES}
    for _, command, _ in games.commands(plan, model):
        assert int(command.removeprefix("pct exec ").split()[0]) in vmids
        assert " -- bash -c " in command


def test_verify_passes_fails_and_skips(facts):
    plan_with_notes = build_plan(build_model(games_config()), facts("large"))
    _, model = planned(facts)
    answers = {label: want for label, _, want in games.commands(strip_notes(plan_with_notes), model)}
    by_command = {command: label for label, command, _ in games.commands(strip_notes(plan_with_notes), model)}

    def run(target, command):
        label = by_command[command]
        if label.startswith("terraria"):
            return 0, f"{TERRARIA} False\n", ""
        return 0, answers[label] + "\n", ""

    results = games.check(copy.deepcopy(plan_with_notes), model, "root@host", run)
    assert [r.status for r in results].count("PASS") == len(results) - 1
    (failed,) = [r for r in results if r.status == "FAIL"]
    assert failed.name == f"terraria runs version {TERRARIA} and answers"
    assert failed.detail == f"{TERRARIA} False"
    skipped = games.skipped(plan_with_notes, model, "the host is not reachable")
    assert [r.name for r in skipped] == list(answers)
    assert {r.status for r in skipped} == {"SKIP"}


def test_a_game_runs_on_demand_and_stops_once_nobody_plays(facts):
    """The catalog's rule for a game: off by itself after 30 minutes without
    a player (nb-game busy says who plays), the other games as before."""
    plan, model = planned(facts, {**games_config(), "power": {"minecraft": {"run": "on-demand"}}})
    assert plan["power"]["groups"] == {
        "minecraft": {
            "title": "Minecraft",
            "guests": ["minecraft"],
            "memory_mib": plan["guests"]["minecraft"]["memory_mib"] // 2,
            "stop": "auto",
            "idle_minutes": 30,
        }
    }
    assert plan["guests"]["minecraft"]["run"] == "on-demand"
    assert "run" not in plan["guests"]["terraria"] and "run" not in plan["guests"]["factorio"]
    assert {name: model["power"][name]["stop"] for name in GAMES} == dict.fromkeys(GAMES, "auto")


def test_verify_skips_a_game_that_is_off_and_checks_the_others(facts):
    plan_with_notes = build_plan(build_model({**games_config(), "power": {"minecraft": {"run": "on-demand"}}}), facts("large"))
    plan, model = strip_notes(plan_with_notes), build_model(games_config())
    answers = {command: want for _, command, want in games.commands(plan, model)}
    awake = power.guard(plan, lambda target, command: (0, answers[command] + "\n", ""), {"minecraft": "Minecraft"})
    results = games.check(plan_with_notes, model, "root@host", awake)
    assert [r.name for r in results] == [label for label, _, _ in games.commands(plan, model)]
    why = "Minecraft is off (it runs on demand); start it on the page, under Services, to check it"
    assert {(r.status, r.detail) for r in results if r.name.startswith("minecraft")} == {("SKIP", why)}
    assert {r.status for r in results if not r.name.startswith("minecraft")} == {"PASS"}
