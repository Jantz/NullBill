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
    """A group per role that some port names and someone holds; a role
    that reaches nothing by itself, such as friend, takes none."""
    roles = {r: {"reach": ["service:workspace"]} for r in ("qa", "ops")}
    ssh = {"port": 22, "proto": "tcp", "from": ["role:owner", "role:default", "role:qa", "role:ops"]}
    people = person("dev-b", "default") | person("qa-c", "qa") | person("ops-d", "ops")
    config = {"roles": roles, "people": people, "services": {"workspace": {"ports": [ssh]}}}
    assert errors_of(facts, "small", config) == ["tailnet: 4 groups, but Tailscale's free Personal plan allows 3"]
    friends = {f"friend-{i}": {"role": "friend", "login": f"friend-{i}@example.internal", "services": ["jellyfin"]} for i in range(3)}
    plan, _ = build(facts, "small", {"people": person("dev-b", "default") | friends})
    assert strip_notes(plan)["tailnet"] == {"users": "5 of 6", "groups": "2 of 3", "tagged_devices": "1 of 50"}


def test_only_owner_reaches_mgmt(facts):
    config = {"services": {"monitoring": {"ports": [{"port": 3000, "proto": "tcp", "from": ["role:default"]}]}}}
    assert errors_of(facts, "small", config) == ["role:default -> monitoring tcp/3000: only the owner role reaches mgmt and the PVE host"]


def test_role_flow_must_be_within_reach(facts):
    config = {"services": {"postgres": {"ports": [{"port": 5432, "proto": "tcp", "from": ["role:default"]}]}}}
    assert errors_of(facts, "small", config) == ["role:default -> postgres tcp/5432: outside roles.default.reach"]


def test_default_role_gets_only_its_flows(facts):
    pol = policy(facts, "large")
    # friend-c holds the friend role, which no port names: no group for it.
    assert pol["groups"] == {"group:nb-default": ["dev-b@example.internal"], "group:nb-owner": ["owner-a@example.internal"]}
    default = [g for g in pol["grants"] if g["src"] == ["group:nb-default"]]
    assert {d for g in default for d in g["dst"]} == {"10.42.2.17", "10.42.3.19"}
    host = [g for g in pol["grants"] if "10.42.1.254" in g["dst"]]
    assert host == [{"src": ["group:nb-owner"], "dst": ["10.42.1.254"], "ip": ["tcp:8006", "tcp:22"]}]


def test_policy_tests_deny_host_to_default_role(facts):
    tests = {t["src"]: t for t in policy(facts, "large")["tests"]}
    assert tests["dev-b@example.internal"]["deny"][:3] == ["10.42.1.254:8006", "10.42.1.254:22", "10.42.1.1:22"]
    assert "10.42.1.254:8006" in tests["owner-a@example.internal"]["accept"]
    assert "deny" not in tests["owner-a@example.internal"]


def test_tailscale_tests_every_person_against_every_port_people_can_hold(facts):
    """One test per person: each port any person can reach is either in
    their accept list or in their deny list, so Tailscale refuses a policy
    that gives anyone a port they were not given."""
    plan, model = build(facts, "large", {})
    pol = json.loads(render_tailnet(plan, model)["policy.json"])
    tests = {t["src"]: t for t in pol["tests"] if t["src"] != "tag:nb-agent"}
    assert list(tests) == [p["login"] for p in model["people"].values()]
    everything = set(tests["owner-a@example.internal"]["accept"])
    for login, test in tests.items():
        assert set(test["accept"]) | set(test.get("deny", [])) == everything, login
        assert not set(test["accept"]) & set(test.get("deny", []))


def friend(services: list[str]) -> dict:
    return {"people": {"friend-c": {"role": "friend", "login": "friend-c@example.internal", "services": services}}}


def test_a_person_gets_the_shared_ports_of_their_services_and_nothing_else(facts):
    pol = policy(facts, "large")
    mine = [g for g in pol["grants"] if g["src"] == ["friend-c@example.internal"]]
    assert mine == [
        {"src": ["friend-c@example.internal"], "dst": ["10.42.2.17"], "ip": ["tcp:443", "tcp:22"]},
        {"src": ["friend-c@example.internal"], "dst": ["10.42.4.21"], "ip": ["tcp:4000"]},
    ]
    (test,) = [t for t in pol["tests"] if t["src"] == "friend-c@example.internal"]
    assert test["accept"] == ["10.42.2.17:22", "10.42.2.17:443", "10.42.4.21:4000", "10.42.6.1:53"]
    # The host, the router, mgmt, and what others hold but friend-c does not:
    # the workspace (the default role's) and the Windows desktop (the owner's).
    assert {"10.42.1.254:8006", "10.42.1.254:22", "10.42.1.1:22", "10.42.1.2:8443", "10.42.3.19:22", "10.42.5.22:3389"} <= set(test["deny"])
    # Names: by login, since friend has no group.
    (dns,) = [g for g in pol["grants"] if g["dst"] == ["10.42.6.1"]]
    assert dns["src"] == ["group:nb-default", "group:nb-owner", "friend-c@example.internal"]


def test_taking_a_service_away_leaves_only_the_names(facts):
    pol = policy(facts, "large", friend([]))
    assert not [g for g in pol["grants"] if g["src"] == ["friend-c@example.internal"]]
    (test,) = [t for t in pol["tests"] if t["src"] == "friend-c@example.internal"]
    assert test["accept"] == ["10.42.6.1:53"] and "10.42.2.17:443" in test["deny"]


def test_only_a_port_that_lists_shared_goes_to_a_person(facts):
    """Forgejo's 443 and 22 list shared; an admin port beside them does
    not, and stays with the roles its `from` names."""
    ports = [
        {"port": 443, "proto": "tcp", "from": ["role:owner", "shared"]},
        {"port": 3000, "proto": "tcp", "from": ["role:owner"]},
        {"port": 34197, "proto": "udp", "from": ["role:owner", "shared"]},
    ]
    pol = policy(facts, "large", {"services": {"forgejo": {"ports": ports}}} | friend(["forgejo"]))
    (grant,) = [g for g in pol["grants"] if g["src"] == ["friend-c@example.internal"]]
    assert grant == {"src": ["friend-c@example.internal"], "dst": ["10.42.2.17"], "ip": ["tcp:443", "udp:34197"]}
    tcp, udp = [t for t in pol["tests"] if t["src"] == "friend-c@example.internal"]
    assert "10.42.2.17:3000" in tcp["deny"] and "10.42.2.17:443" in tcp["accept"]
    # A UDP port people can hold gets its own test per person.
    assert (udp["proto"], udp["accept"]) == ("udp", ["10.42.2.17:34197", "10.42.6.1:53"]) and "deny" not in udp
    (other,) = [t for t in pol["tests"] if t["src"] == "dev-b@example.internal" and t["proto"] == "udp"]
    assert other["deny"] == ["10.42.2.17:34197"]


def test_what_cannot_be_given_fails_the_plan(facts):
    assert errors_of(facts, "large", friend(["postgres", "nope"])) == [
        "people.friend-c.services: postgres has no port that lists shared in its from, so it cannot be given to a person",
        "people.friend-c.services: there is no service nope",
    ]
    grafana = {"services": {"monitoring": {"ports": [{"port": 3000, "proto": "tcp", "from": ["role:owner", "shared"]}]}}}
    assert errors_of(facts, "large", grafana) == ["shared -> monitoring tcp/3000: only the owner role reaches mgmt and the PVE host"]
    host = {"host": {"ports": [{"port": 8006, "proto": "tcp", "from": ["role:owner", "shared"]}]}}
    assert errors_of(facts, "large", host) == ["shared -> the PVE host tcp/8006: only the owner role reaches mgmt and the PVE host"]
    reach = {"roles": {"friend": {"reach": ["shared"]}}}
    assert errors_of(facts, "large", reach) == [
        "roles.friend.reach: shared is only a flow source; give a person services under people instead"
    ]


def test_one_login_is_one_person(facts):
    twice = {"people": {"again": {"role": "friend", "login": "Dev-B@example.internal"}}}
    assert errors_of(facts, "large", twice) == ["people.again.login: dev-b has the same Tailscale login; one person, one entry"]


def test_a_service_that_is_not_planned_is_kept_for_when_it_is(facts):
    """Unticking a workload does not fail the plan over the people who had
    it; they get it back with the workload."""
    pol = policy(facts, "large", friend(["jellyfin"]))
    assert not [g for g in pol["grants"] if g["src"] == ["friend-c@example.internal"]]
    plan, _ = build(facts, "large", friend(["jellyfin"]))
    assert strip_notes(plan)["people"]["friend-c"] == {"role": "friend", "services": ["jellyfin"]}


def test_a_shared_port_changes_no_firewall_rule(facts):
    """The port is open to the vpn guest for the owner already, so marking
    it shared, or giving it to someone, leaves the host and router alone."""
    from nullbill.artifacts import render_all

    plan, model = build(facts, "large", {})
    with_friend = render_all(plan, model)
    for svc in model["services"].values():
        for port in svc.get("ports", []):
            port["from"] = [r for r in port["from"] if r != "shared"]
    model["people"].pop("friend-c")
    without = render_all(build_plan(model, facts("large")), model)
    changed = {path for path in with_friend if with_friend[path] != without.get(path)}
    assert changed == {"tailnet/policy.json"}


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
    (dns,) = [g for g in pol["grants"] if g["dst"] == ["10.42.6.1"]]
    assert (dns["src"][:2], dns["ip"]) == (["group:nb-default", "group:nb-owner"], ["udp:53", "tcp:53"])
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
    model = build_model(fixture_config(cls) | {"people": {"owner": {"role": "owner", "login": "owner-a@example.internal"}}})
    plan = build_plan(model, facts(cls))
    pol = json.loads(render_tailnet(plan, model)["policy.json"])
    named = {s for g in pol["grants"] for s in g["src"] if s.startswith("group:")}
    named |= {o for owners in pol["tagOwners"].values() for o in owners}
    assert named <= set(pol["groups"]) == {"group:nb-owner"}
