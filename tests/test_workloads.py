"""What to run: every choice the catalog offers is explained, and its
memory adds up the way plan counts it."""

import pytest
from fixturedata import CLASSES, fixture_config

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
