"""The runner dispatcher: its pool, its token's grants and its reach."""

import json

import pytest
from conftest import fixture_config

from nullbill import agent, ci
from nullbill.artifacts import render_all
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.tofu import render_tofu
from nullbill.verify import probes

PUBLIC = "192.0.2.80"


def planned(facts, cls="small", config=None):
    model = build_model({**fixture_config(cls), **(config or {})})
    return build_plan(model, facts(cls)), model


def resources(facts, cls="small", config=None):
    plan, model = planned(facts, cls, config)
    return json.loads(render_tofu(plan, model)["main.tf.json"]), strip_notes(plan)


def test_dispatcher_token_can_clone_the_template_and_manage_only_the_ci_pool(facts):
    config, plan = resources(facts)
    res = config["resource"]
    assert res["proxmox_virtual_environment_role"]["dispatcher"]["privileges"] == ci.PRIVILEGES
    assert res["proxmox_virtual_environment_role"]["clone_source"]["privileges"] == ["VM.Audit", "VM.Clone"]
    assert res["proxmox_user_token"]["dispatcher"]["privileges_separation"] is True
    assert "password" not in res["proxmox_virtual_environment_user"]["dispatcher"]
    runner = plan["guests"]["linux-runner"]
    expected = {
        (f"/pool/{ci.POOL}", ci.ROLE),
        (f"/vms/{runner['template_vmid']}", ci.SOURCE_ROLE),
        (f"/storage/{plan['disk']['storage']}", "PVEDatastoreUser"),
        (f"/sdn/zones/localnetwork/{plan['zones']['build']['bridge']}", "PVESDNUser"),
    }
    grants = {(a.get("user_id") or a.get("token_id"), a["path"], a["role_id"]) for a in res["proxmox_acl"].values()}
    assert {g for g in grants if g[0] in (ci.USER, ci.TOKEN_ID)} == {(who, p, r) for who in (ci.USER, ci.TOKEN_ID) for p, r in expected}
    # Nothing on /, /vms or the agent's pool, and no snapshot or config rights beyond the serial.
    assert not {p for _, p, _ in grants if _ in (ci.USER, ci.TOKEN_ID)} & {"/", "/vms", f"/pool/{agent.POOL}"}
    assert config["output"]["dispatcher_token"]["sensitive"] is True


def test_template_is_in_no_pool_and_clones_have_their_own_vmids(facts):
    config, plan = resources(facts, "mid")
    template = config["resource"]["proxmox_virtual_environment_vm"]["linux-runner"]
    assert "pool_id" not in template
    assert config["resource"]["proxmox_virtual_environment_pool"]["ci"]["pool_id"] == ci.POOL
    runner = plan["guests"]["linux-runner"]
    first, last = (int(v) for v in runner["clone_vmids"].split("-"))
    assert last - first + 1 == runner["count"]
    others = {g.get("vmid", g.get("template_vmid")) for g in plan["guests"].values()}
    assert not others & set(range(first, last + 1))


def test_no_dispatcher_without_a_pool(facts):
    config, _ = resources(facts, config={"intent": {"workloads": ["agents"]}})
    assert "dispatcher" not in config["resource"].get("proxmox_user_token", {})
    assert "ci" not in config["resource"]["proxmox_virtual_environment_pool"]
    assert "dispatcher_token" not in config["output"]


def test_clones_cannot_outnumber_the_dhcp_range(facts):
    with pytest.raises(NullBillError, match="linux-runner: 63 clones; its zone's DHCP range has 62 leases"):
        planned(facts, "large", {"overrides": {"linux-runner": {"count": 63}}})


def test_dispatcher_reaches_the_api_forgejo_and_build_reaches_it(facts):
    plan, model = planned(facts)
    files = render_all(plan, model)
    host = files["firewall/host.fw"]
    assert "IN ACCEPT -source +nb-svc-runner-dispatcher -p tcp -dport 8006" in host
    assert "-source +nb-svc-runner-dispatcher -p tcp -dport 22" not in host
    names = [p.name for p in probes(plan, model, PUBLIC)[0]]
    for expected in (
        "runner-dispatcher (services) reaches the PVE host tcp/8006",
        "runner-dispatcher (services) reaches forgejo tcp/443",
        "runner-dispatcher (services) is refused the PVE host tcp/22",
        "runner-dispatcher (services) is refused postgres tcp/5432",
        "workspace (build) reaches runner-dispatcher tcp/8443",
    ):
        assert expected in names, expected
