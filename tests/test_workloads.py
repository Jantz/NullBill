"""What to run: every choice the catalog offers is explained, and its
memory adds up the way plan counts it."""

import pytest
from fixturedata import CLASSES, fixture_config, games_config
from pinned import MINECRAFT, TERRARIA

from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import FILES_WORKLOAD, build_plan
from nullbill.render import strip_notes
from nullbill.web import workloads


def test_every_workload_in_the_catalog_is_a_choice_with_its_words():
    catalog = {svc["workload"] for svc in build_model({})["services"].values()} - {"core", FILES_WORKLOAD}
    assert catalog == set(workloads.WORKLOADS)
    assert set(workloads.TEXT) == {*workloads.WORKLOADS, workloads.BACKUPS, workloads.CONSOLE}
    for text in workloads.TEXT.values():
        assert all(text[key] for key in ("title", "what", "on", "off")) and isinstance(text["needs"], list)
    assert set(workloads.UNFINISHED) <= set(workloads.WORKLOADS)


@pytest.mark.parametrize("cls", CLASSES)
def test_the_meter_counts_memory_as_the_plan_does(cls, facts):
    model = build_model(fixture_config(cls))
    plan = strip_notes(build_plan(model, facts(cls)))
    view = workloads.options(model, facts(cls), existing=None)
    memory = view["memory"]
    assert view["errors"] == [] and view["adds"] is None and view["removes"] is None
    assert memory["reserve_mib"] == plan["host"]["reserve_mib"]
    # Pools grow into free memory, so the plan's guests take at least the floors.
    assert 0 <= memory["free_mib"] and memory["base_mib"] + memory["chosen_mib"] <= plan["host"]["guests_mib"]
    chosen = {i["name"] for i in view["items"] if i["chosen"]}
    assert chosen == {*fixture_config(cls)["intent"]["workloads"], workloads.BACKUPS}
    rows = sum(i["memory_mib"] for i in view["items"] if i["chosen"] and i["name"] != workloads.CONSOLE)
    assert rows == memory["chosen_mib"]
    for name in plan["guests"]:
        option = workloads.option_of(name, model["services"][name])
        assert (option is None) == (name in memory["base"])


def test_a_plan_that_does_not_fit_still_says_what_each_choice_takes(facts):
    facts = facts("small")
    config = fixture_config("small") | {"intent": {"workloads": ["agents", "linux-ci", "media", "windows-dev", "windows-ci"]}}
    model = build_model(config)
    with pytest.raises(NullBillError):
        build_plan(model, facts)
    view = workloads.options(model, facts, existing={"router", "workspace", "inference"})
    need = view["memory"]["base_mib"] + view["memory"]["chosen_mib"]
    assert view["memory"]["free_mib"] < 0 and any(f"planned guests need {need} MiB" in e for e in view["errors"])
    by_name = {i["name"]: i for i in view["items"]}
    assert by_name["windows-dev"]["memory_mib"] == 4096 and by_name["windows-dev"]["guests"][0]["disk_gib"] == 96
    assert by_name["agents"]["on_server"] == ["workspace"] and by_name["media"]["on_server"] == []
    # Guests on the server that these choices no longer plan, and those they add.
    assert view["removes"] == ["inference"] and "jellyfin" in view["adds"] and "router" not in view["adds"]
    assert "Jellyfin" in workloads.loss("jellyfin", model) and "everything on its disk" in workloads.loss("router", model)


def test_a_game_asks_for_its_agreement_and_its_admins_beside_its_tick(facts):
    model = build_model(games_config())
    view = workloads.options(model, facts("large"), existing=None)
    by_name = {i["name"]: i for i in view["items"]}
    assert view["errors"] == []
    assert [i["name"] for i in view["items"] if i["game"] is not None] == ["minecraft", "terraria", "factorio"]
    minecraft = by_name["minecraft"]
    game = minecraft["game"]
    assert minecraft["chosen"] and game["eula"] == {
        "agreed": True,
        "url": "https://aka.ms/MinecraftEULA",
        "text": "I agree to the Minecraft EULA",
    }
    assert game["admins"] == ["owner_a"] and (game["kind"], game["more"], game["file"]) == ("minecraft", False, "server.properties")
    # Its settings by name with their values now, and its memory.
    set_now = {o["key"]: o["value"] for o in game["options"] if o["value"] != o["default"]}
    assert set_now == {"difficulty": "hard", "max-players": 8} and game["other"] == ""
    assert (game["memory_mib"], game["floor_mib"]) == (3072, 3072)
    # A container counts at half its limit: 3072 MiB for Minecraft.
    assert minecraft["memory_mib"] == 1536 and minecraft["guests"][0]["name"] == "minecraft"
    assert minecraft["note"] == (
        f"Players need Minecraft: Java Edition {MINECRAFT}, the same version as the server. "
        "Java gets 2.0 GiB of the container's memory for the world."
    )
    terraria = by_name["terraria"]["game"]
    assert "eula" not in terraria and "admins" not in terraria
    assert by_name["terraria"]["note"].startswith(f"Players need Terraria (PC) {TERRARIA}")
    assert {o["key"]: o["value"] for o in terraria["options"]}["worldname"] == "Fixture"
    assert by_name["factorio"]["game"]["admins"] == ["owner_a"] and "eula" not in by_name["factorio"]["game"]
    assert by_name["factorio"]["note"].endswith("The base game: nobody needs the Space Age expansion.")
    assert by_name["media"]["game"] is None
    assert "world" in workloads.loss("minecraft", model)


def test_minecraft_ticked_without_the_agreement_does_not_plan(facts):
    config = games_config()
    del config["games"]["minecraft"]["eula"]
    view = workloads.options(build_model(config), facts("large"), existing=None)
    assert any(e.startswith("games.minecraft.eula: set it to true once you agree") for e in view["errors"])
    minecraft = next(i for i in view["items"] if i["name"] == "minecraft")
    assert minecraft["chosen"] and minecraft["game"]["eula"]["agreed"] is False


@pytest.mark.parametrize("cls", CLASSES)
def test_what_runs_on_demand_shares_what_the_rest_leaves_as_the_plan_counts_it(cls, facts):
    on_demand = {w: {"run": "on-demand"} for w in fixture_config(cls)["intent"]["workloads"] if w not in ("linux-ci", "windows-ci")}
    model = build_model(fixture_config(cls) | {"power": on_demand})
    plan = strip_notes(build_plan(model, facts(cls)))
    view = workloads.options(model, facts(cls), existing=None)
    memory, rows = view["memory"], {w["name"]: w for w in view["power"]}
    groups = plan["power"]["groups"]
    assert {n for n, w in rows.items() if w["run"] == "on-demand"} == set(groups) == set(on_demand)
    assert all(rows[n]["memory_mib"] == g["memory_mib"] and rows[n]["guests"] == g["guests"] for n, g in groups.items())
    assert memory["demand_mib"] == sum(g["memory_mib"] for g in groups.values())
    assert memory["demand_largest_mib"] == max(g["memory_mib"] for g in groups.values())
    assert memory["together"] is plan["power"]["together"]
    # Pools grow into what is left with everything on, so the plan's share is at most the meter's.
    assert memory["demand_mib"] <= plan["power"]["headroom_mib"] <= memory["free_mib"]
    parked = {g for group in groups.values() for g in group["guests"]}
    assert not parked & set(memory["base"]) and memory["base_mib"] + memory["chosen_mib"] <= plan["host"]["guests_mib"]
    # A pool alone has nothing to stop; its dispatcher makes the CI workload stoppable.
    assert rows["linux-ci"]["blocked"] is None and rows["linux-ci"]["run"] == "always"
