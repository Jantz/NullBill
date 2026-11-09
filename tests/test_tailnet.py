import json

import pytest
from conftest import fixture_config

from nullbill.errors import NullBillError
from nullbill.model import build_model, merge
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.tailnet import nameserver, render_tailnet, routes


def person(name: str, role: str) -> dict:
    return {name: {"role": role, "login": f"{name}@example.internal"}}


def build(facts, cls: str, config: dict) -> tuple[dict, dict]:
    model = build_model(merge(fixture_config(cls), config))
    return build_plan(model, facts(cls)), model


def errors_of(facts, cls: str, config: dict) -> list[str]:
    with pytest.raises(NullBillError) as exc:
        build(facts, cls, config)
    return exc.value.messages


def policy(facts, cls: str, config: dict | None = None) -> dict:
    plan, model = build(facts, cls, config or {})
    return json.loads(render_tailnet(plan, model)["policy.json"])


def test_owner_is_required(facts):
    config = fixture_config("small")
    del config["people"]
    with pytest.raises(NullBillError) as exc:
        build_plan(build_model(config), facts("small"))
    assert exc.value.messages[0].startswith("people: no person has role owner")


def test_free_plan_user_limit(facts):
    people = {}
    for i in range(6):
        people |= person(f"dev-{i}", "default")
    assert errors_of(facts, "small", {"people": people}) == ["tailnet: 7 users, but Tailscale's free Personal plan allows 6"]


def test_free_plan_group_limit(facts):
    roles = {r: {"reach": ["service:forgejo"]} for r in ("qa", "ops")}
    people = person("dev-b", "default") | person("qa-c", "qa") | person("ops-d", "ops")
    messages = errors_of(facts, "small", {"roles": roles, "people": people})
    assert messages == ["tailnet: 4 groups, but Tailscale's free Personal plan allows 3"]


def test_only_owner_reaches_mgmt(facts):
    config = {"services": {"monitoring": {"ports": [{"port": 3000, "proto": "tcp", "from": ["role:default"]}]}}}
    assert errors_of(facts, "small", config) == ["role:default -> monitoring tcp/3000: only the owner role reaches mgmt and the PVE host"]


def test_role_flow_must_be_within_reach(facts):
    config = {"services": {"postgres": {"ports": [{"port": 5432, "proto": "tcp", "from": ["role:default"]}]}}}
    assert errors_of(facts, "small", config) == ["role:default -> postgres tcp/5432: outside roles.default.reach"]


def test_default_role_gets_only_its_flows(facts):
    pol = policy(facts, "large")
    assert pol["groups"] == {"group:nb-default": ["dev-b@example.internal"], "group:nb-owner": ["owner-a@example.internal"]}
    default = [g for g in pol["grants"] if g["src"] == ["group:nb-default"]]
    assert {d for g in default for d in g["dst"]} == {"10.42.2.17", "10.42.3.19"}
    host = [g for g in pol["grants"] if "10.42.1.254" in g["dst"]]
    assert host == [{"src": ["group:nb-owner"], "dst": ["10.42.1.254"], "ip": ["tcp:8006", "tcp:22"]}]


def test_policy_tests_deny_host_to_default_role(facts):
    tests = {t["src"]: t for t in policy(facts, "large")["tests"]}
    assert tests["dev-b@example.internal"]["deny"] == ["10.42.1.254:8006", "10.42.1.254:22", "10.42.1.1:22"]
    assert "10.42.1.254:8006" in tests["owner-a@example.internal"]["accept"]
    assert "deny" not in tests["owner-a@example.internal"]


def test_a_remote_agent_tag_reaches_the_page_and_nothing_else(facts):
    plan, model = build(facts, "small", {})
    p = json.loads(render_tailnet(plan, model)["policy.json"])
    assert p["tagOwners"]["tag:nb-agent"] == ["group:nb-owner"]
    bare = strip_notes(plan)
    page = bare["guests"]["nullbill"]["ip"]
    agent = [g for g in p["grants"] if "tag:nb-agent" in g["src"]]
    assert agent == [{"src": ["tag:nb-agent"], "dst": [page], "ip": ["tcp:8443"]}]
    (test,) = [t for t in p["tests"] if t["src"] == "tag:nb-agent"]
    assert test["accept"] == [f"{page}:8443"] and f"{bare['zones']['mgmt']['host_ip']}:22" in test["deny"]


def test_every_person_asks_the_router_for_names_and_nothing_more(facts):
    """Split DNS sends nullbill.internal to the router's leg in the access
    zone: every group may reach it on 53, which the vpn guest advertises as
    a single address, and Tailscale's tests check it for each role."""
    plan, model = build(facts, "large", {})
    pol = json.loads(render_tailnet(plan, model)["policy.json"])
    assert nameserver(plan) == "10.42.6.1" == strip_notes(plan)["zones"]["access"]["gateway"]
    dns = [g for g in pol["grants"] if g["dst"] == ["10.42.6.1"]]
    assert dns == [{"src": ["group:nb-default", "group:nb-owner"], "dst": ["10.42.6.1"], "ip": ["udp:53", "tcp:53"]}]
    for test in pol["tests"]:
        if test["src"] != "tag:nb-agent":
            assert "10.42.6.1:53" in test["accept"]
    assert routes(plan, model)[-1] == "10.42.6.1/32"
    assert "10.42.6.0/24" not in routes(plan, model)


@pytest.mark.parametrize("cls", ["small", "mid", "large"])
def test_every_group_the_policy_names_is_defined(facts, cls):
    """Tailscale refuses a policy naming a group it does not define ("group
    not found"), which a role nobody holds would be: the owner alone, as on
    a first install, gets no grant for the default role."""
    people = {"people": {"owner": {"role": "owner", "login": "owner-a@example.internal"}}}
    plan, model = build(facts, cls, people)
    model["people"] = people["people"]
    pol = json.loads(render_tailnet(plan, model)["policy.json"])
    named = {s for g in pol["grants"] for s in g["src"] if s.startswith("group:")}
    named |= {o for owners in pol["tagOwners"].values() for o in owners}
    assert named <= set(pol["groups"]) == {"group:nb-owner"}
