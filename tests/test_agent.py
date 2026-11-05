"""Agent access: the pool, the token's grants and the operator's reach."""

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
    # A pool service's template sits in no pool; its clones join the CI pool.
    assert in_pool == {n for n, g in agent.managed(strip_notes(plan)).items() if g["kind"] != "vm-pool"}
    assert in_pool and not in_pool & set(NEVER)
    assert {"workspace", "inference"} <= in_pool
    assert not in_pool & {"runner-dispatcher", "model-gateway", "windows-dev", "forgejo", "postgres"}


def test_token_is_separated_and_holds_three_grants(facts):
    plan, model = planned(facts, "small")
    res = json.loads(render_tofu(plan, model)["main.tf.json"])["resource"]
    assert res["proxmox_virtual_environment_role"]["agent"]["privileges"] == ["VM.PowerMgmt", "VM.Snapshot"]
    assert res["proxmox_user_token"]["agent"]["privileges_separation"] is True
    grants = {(a.get("user_id") or a.get("token_id"), a["path"], a["role_id"]) for a in res["proxmox_acl"].values()}
    # The CI pool holds clones of the agent-managed linux-runner.
    expected = {("/", "PVEAuditor"), (f"/pool/{agent.POOL}", agent.ROLE), (f"/pool/{ci.POOL}", agent.ROLE)}
    grants = {g for g in grants if g[0] in (agent.USER, agent.TOKEN_ID)}
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


def _agent_results(facts, **kw):
    from conftest import agent_api

    from nullbill.agent import check

    plan, _ = planned(facts, "mid")
    return plan, {r.name: (r.status, r.detail) for r in check(plan, "root@pve.example.internal", agent_api(plan, **kw))}


def test_correct_grants_pass(facts):
    _, results = _agent_results(facts)
    assert {s for s, _ in results.values()} == {"PASS"}
    assert "agent token may power-manage and snapshot workspace, nothing else" in results
    assert "agent token cannot change pbs" in results
    assert "agent token is refused a snapshot of router" in results


def test_a_grant_outside_the_pool_fails(facts):
    plan = strip_notes(planned(facts, "mid")[0])
    pbs = plan["guests"]["pbs"]["vmid"]
    _, results = _agent_results(facts, extra={f"/vms/{pbs}": {"VM.PowerMgmt"}, "/": {"Sys.Modify"}})
    assert results["agent token cannot change pbs"] == ("FAIL", "holds VM.PowerMgmt")
    assert results["agent token cannot change the host"] == ("FAIL", "holds Sys.Modify")


def test_extra_privilege_inside_the_pool_fails(facts):
    plan = strip_notes(planned(facts, "mid")[0])
    ws = plan["guests"]["workspace"]["vmid"]
    _, results = _agent_results(facts, extra={f"/vms/{ws}": {"VM.Snapshot.Rollback"}})
    status, detail = results["agent token may power-manage and snapshot workspace, nothing else"]
    assert status == "FAIL" and "VM.Snapshot.Rollback" in detail


def test_accepted_snapshot_outside_the_pool_fails(facts):
    _, results = _agent_results(facts, snapshot_status="200")
    status, detail = results["agent token is refused a snapshot of router"]
    assert status == "FAIL" and "delete snapshot nb-verify-denied" in detail


def test_api_call_runs_in_bash(tmp_path):
    """The generated shell against a stand-in curl that checks its arguments."""
    import subprocess

    from nullbill.pveapi import _api

    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'args=" $* "; [[ $args == *" -X POST "* && $args == *" --data snapname=x "* ]] || exit 9\n'
        '[[ $args == *"PVEAPIToken=nb-agent@pve!operator=s3cret"* ]] || exit 8\n'
        'while [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done; echo \'{"data":null}\' >"$out"; echo 403\n'
    )
    curl.chmod(0o755)
    token = tmp_path / "token"
    token.write_text("nb-agent@pve!operator=s3cret")
    command = _api("10.42.1.254", "POST", "/nodes/pve/qemu/2001/snapshot", "snapname=x", str(token), agent.CA_FILE)
    done = subprocess.run(["bash", "-c", command], capture_output=True, text=True, env={"PATH": f"{bindir}:/usr/bin:/bin"})
    assert (done.returncode, done.stdout) == (0, '403\n{"data":null}\n'), done.stderr
