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
    """Records every call; the PVE host's files live in self.files and its
    API tokens in self.tokens."""

    def __init__(self, fail_playbook=None, fail_local=None):
        self.calls = []
        self.files = {}
        self.tokens = []
        self.envs = []
        self.fail_playbook = fail_playbook
        self.fail_local = fail_local

    def host(self, command, stdin=None):
        self.calls.append(("host", command))
        if "if cmp -s" in command:
            path = command.split('if cmp -s "$t" ')[1].split(";")[0]
            if self.files.get(path) == stdin:
                return 0, "same\n", ""
            self.files[path] = stdin
            return 0, "changed\n", ""
        if command == "pveum user token list root@pam --output-format json":
            return 0, json.dumps([{"tokenid": t} for t in self.tokens]), ""
        if command.startswith("pveum user token add root@pam nullbill"):
            self.tokens.append("nullbill")
            return 0, json.dumps({"full-tokenid": "root@pam!nullbill", "value": f"uuid-{len(self.calls)}"}), ""
        if command == "pveum user token remove root@pam nullbill":
            self.tokens.remove("nullbill")
            return 0, "", ""
        answers = {
            "cat /root/.ssh/authorized_keys": "ssh-ed25519 AAAAexample nullbill@nullbill\n# comment\n",
            "cat /etc/pve/pve-root-ca.pem": "-----BEGIN CERTIFICATE-----\nexample\n-----END CERTIFICATE-----\n",
            "hostname": "pve\n",
        }
        if command in answers:
            return 0, answers[command], ""
        if command.startswith("systemctl "):
            return 0, "", ""
        return 127, "", f"fake host: unexpected {command}"

    def local(self, argv, cwd, env, capture=False):
        self.calls.append(("local", argv, cwd))
        self.envs.append(env)
        return (2 if argv[:2] == self.fail_local else 0), ("secret-token" if capture else "")

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


def test_router_stage_hands_the_router_its_rendered_files(ctx):
    c = ctx("small")
    apply.stage_router(c)
    (kind, playbook, hosts, variables), *rest = c.reach.calls
    assert (kind, playbook, rest) == ("playbook", "router.yml", [])
    assert hosts == {"router": connection(c, "router")}
    files = variables["router_files"]
    assert files["/etc/nftables.conf"] == c.files["router/etc/nftables.conf"]
    assert set(files) == {"/" + rel.removeprefix("router/") for rel in c.files if rel.startswith("router/")}


def test_proxy_stage_hands_squid_its_config(ctx):
    c = ctx("small")
    apply.stage_proxy(c)
    (_, playbook, hosts, variables), *rest = c.reach.calls
    assert (playbook, list(hosts), rest) == ("egress-proxy.yml", ["egress-proxy"], [])
    assert variables == {"egress_proxy_files": {"/etc/squid/squid.conf": c.files["egress-proxy/etc/squid/squid.conf"]}}


def test_updates_stage_sets_up_every_linux_guest_once(ctx):
    c = ctx("large")
    apply.stage_updates(c)
    (_, playbook, hosts, variables), *rest = c.reach.calls
    assert (playbook, variables, rest) == ("updates.yml", {}, [])
    # Pool clones are short-lived and Windows has no apt; the installed
    # NullBill container is a guest like any other.
    assert "linux-runner" not in hosts and "windows-dev" not in hosts and "windows-runner" not in hosts
    assert {"router", "nullbill", "egress-proxy", "workspace"} <= set(hosts)
    proxy = "http://10.42.2.10:3128"
    assert hosts["nullbill"]["updates_apt_proxy"] == proxy  # mgmt: proxied
    assert hosts["forgejo"]["updates_apt_proxy"] == ""  # services: direct
    assert hosts["router"]["updates_apt_proxy"] == ""  # its own uplink
    assert hosts["forgejo"]["updates_auto_reboot"] and hosts["forgejo"]["updates_reboot_time"] == "04:30"
    assert not hosts["workspace"]["updates_auto_reboot"]  # agents' long jobs
    assert hosts["forgejo"]["ansible_connection"] == "community.proxmox.proxmox_pct_remote"


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


def test_tofu_stage_runs_init_and_apply_with_everything_in_the_environment(ctx):
    c = ctx("small")
    apply.stage_tofu(c)
    local = [call for call in c.reach.calls if call[0] == "local"]
    assert [call[1][:2] for call in local] == [["tofu", "init"], ["tofu", "apply"]]
    assert all(call[2] == c.home / "tofu" for call in local)
    assert (c.home / "tofu" / "main.tf.json").read_text() == c.files["tofu/main.tf.json"]
    init_env, env = c.reach.envs
    assert "SSL_CERT_FILE" not in init_env
    assert env["SSL_CERT_FILE"] == str(c.home / "pve-root-ca.pem")
    assert env["PROXMOX_VE_API_TOKEN"].startswith("root@pam!nullbill=uuid-")
    assert env["PROXMOX_VE_API_TOKEN"] == c.secrets.get("pve_api_token")
    assert env["TF_VAR_state_passphrase"] == c.secrets.get("tofu_state_passphrase")
    assert env["TF_VAR_pve_endpoint"] == "https://pve.example.internal:8006/"
    assert env["TF_VAR_node_name"] == "pve"
    assert json.loads(env["TF_VAR_ssh_authorized_keys"]) == ["ssh-ed25519 AAAAexample nullbill@nullbill"]
    # No secret lands in the state directory or build/.
    for path in (c.home / "tofu").rglob("*"):
        assert env["PROXMOX_VE_API_TOKEN"] not in path.read_text()
    unit = c.reach.files["/etc/systemd/system/nullbill-routes.service"]
    assert "ExecStart=/sbin/ip route replace 10.42.0.0/16 via 10.42.1.1" in unit


def test_tofu_token_is_kept_and_remade_when_the_host_lost_it(ctx):
    c = ctx("small")
    first = apply.api_token(c)
    assert apply.api_token(c) == first
    adds = [call for call in c.reach.calls if call[1].startswith("pveum user token add")]
    assert len(adds) == 1
    c.reach.tokens.clear()
    assert apply.api_token(c) != first


def test_tofu_failure_stops_apply(ctx):
    c = ctx("small", reach=FakeReach(fail_local=["tofu", "apply"]))
    with pytest.raises(ApplyError, match="apply stopped at tofu: tofu apply failed"):
        apply.stage_tofu(c)
    assert "/etc/systemd/system/nullbill-routes.service" not in c.reach.files


def test_windows_needs_an_iso_before_tofu(ctx, monkeypatch):
    monkeypatch.delenv("TF_VAR_windows_iso", raising=False)
    c = ctx("mid")
    with pytest.raises(ApplyError, match="no Windows ISO"):
        apply.stage_tofu(c)
    assert [call for call in c.reach.calls if call[0] == "local"] == []
