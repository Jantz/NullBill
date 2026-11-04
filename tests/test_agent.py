"""Agent access: the pool, the token's grants and the operator's reach."""

import json

import pytest
from conftest import fixture_config

from nullbill import agent
from nullbill.artifacts import render_all
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.tofu import render_tofu
from nullbill.verify import probes

PUBLIC = "192.0.2.80"
NEVER = ("router", "pbs", "step-ca", "operator", "vpn")


def planned(facts, cls="large", config=None):
    model = build_model({**fixture_config(cls), **(config or {})})
    return build_plan(model, facts(cls)), model


def test_pool_holds_only_agent_managed_guests(facts):
    plan, model = planned(facts)
    config = json.loads(render_tofu(plan, model)["main.tf.json"])
    res = config["resource"]
    in_pool = {
        name
        for kind in ("proxmox_virtual_environment_vm", "proxmox_virtual_environment_container")
        for name, guest in res[kind].items()
        if guest.get("pool_id") == agent.POOL
    }
    assert in_pool == set(agent.managed(strip_notes(plan)))
    assert in_pool and not in_pool & set(NEVER)
    assert {"workspace", "linux-runner", "inference"} <= in_pool
    assert not in_pool & {"runner-dispatcher", "model-gateway", "windows-dev", "forgejo", "postgres"}


def test_token_is_separated_and_holds_two_grants(facts):
    plan, model = planned(facts, "small")
    res = json.loads(render_tofu(plan, model)["main.tf.json"])["resource"]
    assert res["proxmox_virtual_environment_role"]["agent"]["privileges"] == ["VM.PowerMgmt", "VM.Snapshot"]
    assert res["proxmox_user_token"]["agent"]["privileges_separation"] is True
    grants = {(a.get("user_id") or a.get("token_id"), a["path"], a["role_id"]) for a in res["proxmox_acl"].values()}
    expected = {("/", "PVEAuditor"), (f"/pool/{agent.POOL}", agent.ROLE)}
    assert grants == {(who, p, r) for who in (agent.USER, agent.TOKEN_ID) for p, r in expected}
    assert "password" not in res["proxmox_virtual_environment_user"]["agent"]


def test_agent_managed_in_mgmt_is_refused(facts):
    model = build_model({**fixture_config("small"), "services": {"monitoring": {"agent_managed": True}}})
    with pytest.raises(NullBillError, match="services.monitoring.agent_managed: mgmt guests stay out"):
        build_plan(model, facts("small"))


def test_operator_reaches_only_the_api_on_the_host(facts):
    plan, model = planned(facts, "small")
    files = render_all(plan, model)
    host = files["firewall/host.fw"]
    assert "IN ACCEPT -source +nb-svc-operator -p tcp -dport 8006" in host
    assert "dport 22" not in host
    # The management ipset, which opens every PVE port, stays the VPN alone.
    cluster = files["firewall/cluster.fw"]
    vpn = strip_notes(plan)["guests"]["vpn"]["ip"]
    assert f"[IPSET management]\n{vpn}\n\n" in cluster


def test_verify_probes_the_operator(facts):
    plan, model = planned(facts)
    names = [p.name for p in probes(plan, model, PUBLIC)[0]]
    for expected in (
        "operator (mgmt) reaches the PVE host tcp/8006",
        "operator (mgmt) is refused the PVE host tcp/22",
        "operator (mgmt) reaches workspace tcp/22",
        "operator (mgmt) reaches monitoring tcp/9090",
        "operator (mgmt) is refused forgejo tcp/22",
        "operator (mgmt) is refused postgres tcp/5432",
        "step-ca (mgmt) is refused monitoring tcp/9090",
        "step-ca (mgmt) is refused workspace tcp/22",
    ):
        assert expected in names, expected
