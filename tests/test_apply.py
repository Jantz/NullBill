import json

import pytest
from conftest import fixture_config

from nullbill import apply
from nullbill.apply import ApplyError, Context, connection, run_apply
from nullbill.artifacts import render_all
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.results import Result
from nullbill.secretfile import Secrets

TARGET = "root@pve.example.internal"


class MemoryCodec:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.saves = 0

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)
        self.saves += 1


class FakeReach:
    """Records every call; the PVE host's files live in self.files."""

    def __init__(self, fail_playbook=None):
        self.calls = []
        self.files = {}
        self.fail_playbook = fail_playbook

    def host(self, command, stdin=None):
        self.calls.append(("host", command))
        path = command.split('if cmp -s "$t" ')[1].split(";")[0]
        if self.files.get(path) == stdin:
            return 0, "same\n", ""
        self.files[path] = stdin
        return 0, "changed\n", ""

    def playbook(self, playbook, hosts, variables):
        self.calls.append(("playbook", playbook, hosts, variables))
        return 2 if playbook == self.fail_playbook else 0


@pytest.fixture
def ctx(facts, tmp_path):
    def make(fixture="mid", reach=None, **kw):
        model = build_model(fixture_config(fixture))
        plan = build_plan(model, facts(fixture))
        files = render_all(plan, model)
        return Context(plan, model, tmp_path, TARGET, reach or FakeReach(), Secrets(MemoryCodec()), files, **kw)

    return make


def test_secrets_are_made_once_and_saved_before_use():
    codec = MemoryCodec()
    secrets = Secrets(codec)
    first = secrets.ensure("forgejo_db_password")
    assert len(first) >= 40 and codec.data == {"forgejo_db_password": first}
    assert Secrets(codec).ensure("forgejo_db_password") == first
    assert codec.saves == 1
    secrets.set("forgejo_db_password", first)
    assert codec.saves == 1


def test_missing_owner_secret_names_the_file():
    with pytest.raises(NullBillError, match="anthropic_api_key.*secrets.sops.yaml"):
        Secrets(MemoryCodec()).get("anthropic_api_key")


def test_containers_are_reached_through_the_host_and_vms_by_jump(ctx):
    c = ctx()
    host = {"ansible_host": "pve.example.internal", "ansible_user": "root", "ansible_python_interpreter": "/usr/bin/python3"}
    assert connection(c, None) == host
    forgejo = connection(c, "forgejo")
    assert forgejo["ansible_connection"] == "community.proxmox.proxmox_pct_remote"
    assert (forgejo["ansible_host"], forgejo["proxmox_vmid"]) == ("pve.example.internal", 2017)
    pbs = connection(c, "pbs")
    assert (pbs["ansible_host"], pbs["ansible_user"], pbs["ansible_become"]) == ("10.42.1.15", "nullbill", True)
    assert f"-o ProxyJump={TARGET} " in pbs["ansible_ssh_common_args"]
    assert f"UserKnownHostsFile={c.home / 'known_hosts'}" in pbs["ansible_ssh_common_args"]
    assert connection(c, "router")["ansible_host"] == "10.42.1.1"


def test_host_stage_runs_the_baseline_then_installs_the_lan_open_firewall(ctx):
    c = ctx()
    apply.stage_host(c)
    reach = c.reach
    assert reach.calls[0][:2] == ("playbook", "host-baseline.yml")
    assert reach.calls[0][2] == {"pve": connection(c, None)}
    expected = {f"/etc/pve/firewall/{rel.removeprefix('firewall/')}" for rel in c.files if rel.startswith("firewall/")}
    expected.discard("/etc/pve/firewall/cluster-lan-open.fw")
    assert set(reach.files) == expected
    assert reach.files["/etc/pve/firewall/cluster.fw"] == c.files["firewall/cluster-lan-open.fw"]
    assert reach.files["/etc/pve/firewall/2017.fw"] == c.files["firewall/2017.fw"]


def test_host_stage_rewrites_only_what_changed(ctx):
    c = ctx()
    apply.stage_host(c)
    written = [line for line in c.log if "wrote" in line]
    c.log.clear()
    apply.stage_host(c)
    assert written and not [line for line in c.log if "wrote" in line]


def test_close_lan_installs_the_closed_cluster_firewall(ctx):
    reach = FakeReach()
    apply.stage_host(ctx(reach=reach))
    c = ctx(reach=reach, close_lan=True)
    apply.stage_host(c)
    assert reach.files["/etc/pve/firewall/cluster.fw"] == c.files["firewall/cluster.fw"]
    assert "[IPSET management]" in reach.files["/etc/pve/firewall/cluster.fw"]
    assert "wrote /etc/pve/firewall/cluster.fw" in "\n".join(c.log)


def test_a_failed_playbook_stops_apply_before_later_stages(ctx):
    c = ctx(reach=FakeReach(fail_playbook="host-baseline.yml"))
    with pytest.raises(ApplyError, match="apply stopped at host: host-baseline.yml failed on PVE host"):
        run_apply(c)
    assert (c.home / "build" / "tofu").is_dir()
    assert c.reach.files == {}


def test_verify_stage_fails_on_any_fail(ctx, monkeypatch):
    monkeypatch.setattr(apply, "run_verify", lambda *a: [Result("PASS", "a"), Result("FAIL", "b", "closed")])
    c = ctx()
    with pytest.raises(ApplyError, match="apply stopped at verify: 1 checks failed"):
        run_apply(c, only="verify")
    assert c.log == ["PASS     a", "FAIL     b: closed"]
    assert c.reach.calls == []


def test_only_runs_one_stage(ctx):
    c = ctx()
    run_apply(c, only="render")
    assert c.reach.calls == [] and (c.home / "build" / "firewall" / "cluster.fw").is_file()
    with pytest.raises(NullBillError, match="unknown stage 'nope'"):
        run_apply(c, only="nope")


def test_secrets_never_reach_build(ctx):
    c = ctx()
    secret = c.secrets.ensure("forgejo_db_password")
    run_apply(c, only="render")
    for path in (c.home / "build").rglob("*"):
        if path.is_file():
            assert secret not in path.read_text()
    assert secret not in json.dumps(c.files)
