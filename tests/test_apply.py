import json
import shlex

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
from nullbill.tailnet import routes

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
        self.fresh = True
        self.templates = set()
        self.nets = {0: "name=eth0,bridge=vmbr0,hwaddr=02:4E:42:00:00:99,ip=dhcp,type=veth"}
        self.nullbill = []
        self.tailnet = "NeedsLogin"

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
        if command.startswith("pct config "):
            return 0, "hostname: nullbill\n" + "".join(f"net{n}: {v}\n" for n, v in sorted(self.nets.items())), ""
        if command.startswith("pct set 2002 --net1 "):
            self.nets[1] = shlex.split(command)[4]
            return 0, "", ""
        if command.startswith("qm config "):
            return 0, "template: 1\n" if command.split()[2] in self.templates else "name: x\n", ""
        if command.startswith("qm shutdown "):
            self.templates.add(command.split()[2])
            return 0, "", ""
        if command.startswith(("qm status ", "timeout 600 bash -c ")):
            return 0, "", ""
        if command.startswith("pct exec "):
            return self.guest(shlex.split(command)[2], shlex.split(command)[4:])
        return 127, "", f"fake host: unexpected {command}"

    def guest(self, vmid, argv):
        """Containers, reached with pct exec: step-ca answers for its CA."""
        if vmid == "2002" and argv[:2] == ["sh", "-c"]:
            self.nullbill.append(argv[2])
            return 0, "", ""
        if argv == ["cat", "/etc/step-ca/certs/root_ca.crt"]:
            return 0, "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n", ""
        if argv[:5] == ["env", "STEPPATH=/etc/step-ca", "step", "ca", "token"]:
            return 0, f"token-for-{argv[5]}\n", ""
        if argv in (["cat", "/etc/grafana/admin-password"], ["cat", "/etc/forgejo/admin-password"]):
            return 0, f"password-of-{vmid}\n", ""
        if argv == ["cat", "/etc/step-ca/secrets/agent-password"]:
            return 0, "agent-password\n", ""
        if argv == ["cat", "/etc/step-ca/certs/ssh_user_ca_key.pub"]:
            return 0, "ecdsa-sha2-nistp256 AAAAuserca\n", ""
        if argv[:5] == ["/usr/local/sbin/nb-forgejo", "admin", "user", "generate-access-token", "--username"]:
            return 0, f"forgejo-token-{argv[7]}\n", ""
        if argv == ["tailscale", "status", "--json"]:
            return 0, json.dumps({"BackendState": self.tailnet, "Self": {"DNSName": "nullbill.example-tailnet.ts.net."}}), ""
        return 127, "", f"fake guest {vmid}: unexpected {argv}"

    def local(self, argv, cwd, env, capture=False):
        self.calls.append(("local", argv, cwd))
        self.envs.append(env)
        if argv[0] == "openssl":
            return self.openssl(argv[1:], cwd)
        return (2 if argv[:2] == self.fail_local else 0), ("secret-token" if capture else "")

    def openssl(self, args, cwd):
        """Writes what -keyout and -out name; -checkend passes while self.fresh."""
        if "-checkend" in args:
            return (0 if self.fresh else 1), ""
        for flag in ("-keyout", "-out"):
            if flag in args:
                name = args[args.index(flag) + 1]
                (cwd / name).write_text(f"{flag[1:]} {name} #{len(self.calls)}\n")
        return 0, ""

    def playbook(self, playbook, hosts, variables):
        self.calls.append(("playbook", playbook, hosts, variables))
        if playbook == self.fail_playbook:
            return 2
        if playbook == "vpn.yml" and variables["vpn_auth_key"].startswith("tskey-auth-"):
            self.tailnet = "Running"
        return 0


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
    # Guests reach the API by its mgmt name and address; its certificate names both.
    assert reach.calls[0][3] == {"host_baseline_api_name": "pve.nullbill.internal", "host_baseline_api_address": "10.42.1.254"}
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


def test_host_stage_switches_the_on_site_screen_off_by_default(ctx):
    c = ctx("small")
    apply.stage_host(c)
    console = [call for call in c.reach.calls if call[:2] == ("playbook", "console.yml")]
    assert console == [("playbook", "console.yml", {"pve": connection(c, None)}, {"console_enabled": False})]


def test_host_stage_points_the_on_site_screen_at_the_page_with_its_key(ctx):
    c = ctx("small")
    c.model["host"]["console"]["enabled"] = True
    apply.stage_host(c)
    (variables,) = [call[3] for call in c.reach.calls if call[:2] == ("playbook", "console.yml")]
    assert variables == {"console_enabled": True, "console_url": "https://10.42.1.2:8443/", "console_page_spki": "secret-token"}
    (spki,) = [call for call in c.reach.calls if call[0] == "local"]
    assert spki[1][:3] == ["sh", "-c", apply.SPKI] and spki[1][4] == str(c.home / "web" / "cert.pem")


def test_ca_stage_issues_each_tls_service_a_certificate_from_a_one_time_token(ctx):
    c = ctx("small")
    apply.stage_ca(c)
    playbooks = [call[1:] for call in c.reach.calls if call[0] == "playbook"]
    assert playbooks[0] == ("step-ca.yml", {"step-ca": connection(c, "step-ca")}, {"step_ca_domain": "nullbill.internal"})
    certs = {next(iter(hosts)): v for name, hosts, v in playbooks[1:] if name == "tls-cert.yml"}
    assert set(certs) == {"postgres", "monitoring", "forgejo", "runner-dispatcher"}
    assert certs["postgres"] == {
        "tls_cert_name": "postgres.nullbill.internal",
        "tls_cert_ca_url": "https://step-ca.nullbill.internal",
        "tls_cert_ca_root": "-----BEGIN CERTIFICATE-----\nroot\n-----END CERTIFICATE-----\n",
        "tls_cert_token": "token-for-postgres.nullbill.internal",
        "tls_cert_group": "postgres",
        "tls_cert_reload": "systemctl reload postgresql",
    }
    # Tokens are made on the CA guest, by pct exec through the host.
    tokens = [call[1] for call in c.reach.calls if call[0] == "host" and " token " in call[1]]
    assert all(t.startswith("pct exec 2012 -- env STEPPATH=/etc/step-ca step ca token ") for t in tokens) and len(tokens) == 4


def playbook_vars(c, playbook):
    (variables,) = [call[3] for call in c.reach.calls if call[0] == "playbook" and call[1] == playbook]
    return variables


def test_postgres_stage_gives_each_client_its_database_and_a_kept_password(ctx):
    c = ctx("small")
    apply.stage_postgres(c)
    variables = playbook_vars(c, "postgres.yml")
    password = c.secrets.data["postgres_forgejo"]
    assert variables == {
        "postgres_listen": ["10.42.2.13"],
        "postgres_tls_name": "postgres.nullbill.internal",
        "postgres_databases": [{"name": "forgejo", "client": "10.42.2.17", "password": password}],
    }
    apply.stage_forgejo(c)
    assert playbook_vars(c, "forgejo.yml")["forgejo_db_password"] == password
    assert c.secrets.data["forgejo_admin_password"] == "password-of-2017"


def test_monitoring_stage_watches_every_guest_and_alerts_the_owner(ctx):
    c = ctx("small")
    c.secrets.set("ntfy_topic", "nullbill-topic")
    c.secrets.set("healthchecks_url", "https://hc-ping.com/example-check")
    apply.stage_monitoring(c)
    variables = playbook_vars(c, "monitoring.yml")
    nodes = variables["monitoring_node_targets"]
    assert "forgejo.nullbill.internal:9100" in nodes and "nullbill.nullbill.internal:9100" in nodes
    assert not [n for n in nodes if n.startswith("linux-runner")]
    assert variables["monitoring_host_target"] == "pve.nullbill.internal:9100"
    assert variables["monitoring_tls_targets"] == [
        "step-ca.nullbill.internal:443",
        "monitoring.nullbill.internal:3100",
        "forgejo.nullbill.internal:443",
        "runner-dispatcher.nullbill.internal:8443",
    ]
    assert variables["monitoring_ca_file"] == "/etc/nullbill/tls/root_ca.crt"
    assert variables["monitoring_ntfy_url"] == "https://ntfy.sh/nullbill-topic"
    assert variables["monitoring_healthchecks_url"] == "https://hc-ping.com/example-check"
    assert variables["monitoring_https_proxy"] == "http://10.42.2.10:3128"
    assert variables["monitoring_shipper_ca"] == (c.home / "shipper-ca" / "ca.crt").read_text()
    assert c.secrets.data["grafana_admin_password"] == "password-of-2014"


def test_monitoring_stage_runs_without_a_topic_and_says_so(ctx):
    c = ctx("small")
    apply.stage_monitoring(c)
    assert playbook_vars(c, "monitoring.yml")["monitoring_ntfy_url"] == ""
    assert any("no ntfy topic yet" in line for line in c.log)


def test_shipper_certificates_are_kept_until_they_near_expiry(ctx):
    c = ctx("small")
    first = apply.shipper_cert(c, "logs", "operator")
    ca = (c.home / "shipper-ca" / "ca.crt").read_text()
    assert apply.shipper_cert(c, "logs", "operator") == first
    c.reach.fresh = False
    renewed = apply.shipper_cert(c, "logs", "operator")
    assert renewed != first and (c.home / "shipper-ca" / "ca.crt").read_text() == ca
    assert (c.home / "shipper-ca").stat().st_mode & 0o777 == 0o700


def test_ci_stage_bakes_each_golden_image_once_then_runs_the_dispatcher(ctx):
    c = ctx("small")
    (c.home / "known_hosts").write_text("10.42.3.129 ssh-ed25519 AAAAold\n10.42.1.1 ssh-ed25519 AAAArouter\n")
    apply.stage_ci(c)
    playbooks = [call[1:] for call in c.reach.calls if call[0] == "playbook"]
    assert [p[0] for p in playbooks] == ["ci-runner.yml", "agent-ssh.yml", "ci-runner-seal.yml", "runner-dispatcher.yml"]
    # Baked at its reserved lease, through the host, as a new machine.
    assert playbooks[0][1]["linux-runner"]["ansible_host"] == "10.42.3.129"
    assert (c.home / "known_hosts").read_text() == "10.42.1.1 ssh-ed25519 AAAArouter\n"
    runner = playbooks[0][2]
    assert runner["ci_runner_proxy_url"] == "http://10.42.2.10:3128"
    assert runner["ci_runner_dispatcher_url"] == "https://runner-dispatcher.nullbill.internal:8443"
    assert runner["ci_runner_ca_root"].startswith("-----BEGIN CERTIFICATE-----")
    assert playbooks[1][2] == {"agent_ssh_user_ca_key": "ecdsa-sha2-nistp256 AAAAuserca"}
    host = [call[1] for call in c.reach.calls if call[0] == "host"]
    assert host.index("qm status 2030 | grep -q running || qm start 2030") < host.index(
        "qm shutdown 2030 --timeout 180 && qm template 2030"
    )
    dispatcher = playbooks[3][2]
    token = c.secrets.data["forgejo_dispatcher_token"]
    assert token.startswith("forgejo-token-dispatcher-") and dispatcher["runner_dispatcher_forgejo_token"] == token
    assert dispatcher["runner_dispatcher_pve_url"] == "https://pve.nullbill.internal:8006"
    assert dispatcher["runner_dispatcher_node"] == "pve"
    assert dispatcher["runner_dispatcher_pve_token"] == "secret-token"
    assert dispatcher["runner_dispatcher_pools"] == [
        {
            "name": "linux-runner",
            "template": 2030,
            "vmids": [6000, 6000],
            "labels": ["linux"],
            "runner_labels": ["linux:host"],
            "max_job_seconds": 21600,
        }
    ]

    # Again: the template is kept and so is the Forgejo token.
    c.reach.calls.clear()
    apply.stage_ci(c)
    assert [call[1] for call in c.reach.calls if call[0] == "playbook"] == ["runner-dispatcher.yml"]
    assert c.secrets.data["forgejo_dispatcher_token"] == token
    assert "ci: linux-runner's golden image is baked" in c.log


def test_agents_stage_waits_for_a_credential(ctx):
    c = ctx("small")
    apply.stage_agents(c)
    assert not [call for call in c.reach.calls if call[0] == "playbook"]
    assert any("paste one on the page" in line for line in c.log)


@pytest.mark.parametrize("stored", ["anthropic_api_key", "claude_oauth_token"])
def test_agents_stage_signs_in_the_operator_and_opens_agent_guests(ctx, stored):
    c = ctx("small")
    c.secrets.set(stored, "sk-ant-credential")
    apply.stage_agents(c)
    playbooks = [call[1:] for call in c.reach.calls if call[0] == "playbook"]
    assert [(p[0], next(iter(p[1]))) for p in playbooks] == [("operator.yml", "operator"), ("agent-ssh.yml", "workspace")]
    operator = playbooks[0][2]
    kinds = {"anthropic_api_key": "operator_anthropic_api_key", "claude_oauth_token": "operator_claude_oauth_token"}
    assert operator[kinds[stored]] == "sk-ant-credential" and not [
        v for k, v in operator.items() if k in kinds.values() and k != kinds[stored] and v
    ]
    assert operator["operator_proxy_url"] == "http://10.42.2.10:3128"
    assert operator["operator_pve_token"] == "secret-token"
    assert operator["operator_step_password"] == "agent-password"
    assert operator["operator_ca_url"] == "https://step-ca.nullbill.internal"
    assert playbooks[1][2] == {"agent_ssh_user_ca_key": "ecdsa-sha2-nistp256 AAAAuserca"}


def test_logs_stage_gives_each_shipper_its_own_certificate(ctx):
    c = ctx("small")
    apply.stage_logs(c)
    playbooks = [call[1:] for call in c.reach.calls if call[0] == "playbook"]
    names = [next(iter(p[1])) for p in playbooks]
    assert names == ["router", "pbs", "operator", "workspace"]
    certs = {p[2]["log_shipper_cert"] for p in playbooks}
    assert len(certs) == 4
    workspace = playbooks[3][2]
    assert workspace["log_shipper_url"] == "https://monitoring.nullbill.internal:3100/loki/api/v1/push"
    assert workspace["log_shipper_host"] == "workspace"
    assert workspace["log_shipper_key"] == (c.home / "shipper-ca" / "workspace.key").read_text()


def test_vpn_stage_waits_for_a_key_then_logs_in_once_and_forgets_it(ctx):
    c = ctx("small")
    apply.stage_vpn(c)
    (_, playbook, hosts, variables), _ = c.reach.calls
    assert (playbook, hosts) == ("vpn.yml", {"vpn": connection(c, "vpn")})
    assert variables["vpn_tag"] == "tag:nb-vpn" and variables["vpn_auth_key"] == ""
    # The owner reaches every zone that holds a service, mgmt included; access
    # holds only the vpn guest itself.
    assert variables["vpn_routes"] == [z["subnet"] for n, z in c.bare["zones"].items() if n != "access"]
    assert c.log[-1].startswith("vpn: waiting for a Tailscale auth key")

    c.secrets.set("tailscale_auth_key", "tskey-auth-example")
    apply.stage_vpn(c)
    assert c.reach.calls[-2][3]["vpn_auth_key"] == "tskey-auth-example"
    assert c.log[-2].startswith("vpn: on the tailnet as nullbill.example-tailnet.ts.net, advertising 10.42.1.0/24")
    assert c.log[-1] == f"vpn: from your devices on the tailnet the page is https://{c.bare['guests']['nullbill']['ip']}:8443"
    assert "tailscale_auth_key" not in c.secrets.data

    apply.stage_vpn(c)
    assert c.reach.calls[-2][3]["vpn_auth_key"] == ""
    assert c.log[-2].startswith("vpn: on the tailnet")


def test_vpn_routes_cover_only_zones_a_role_reaches(ctx):
    c = ctx("small")
    c.model["roles"]["owner"]["reach"] = ["service:forgejo"]
    c.model["host"]["ports"] = []
    for name, svc in c.model["services"].items():
        for port in svc.get("ports", []):
            port["from"] = [r for r in port["from"] if not r.startswith("role:") or (r == "role:owner" and name == "forgejo")]
    assert routes(c.plan, c.model) == [c.bare["zones"][c.bare["guests"]["forgejo"]["zone"]]["subnet"]]


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


def test_tofu_stage_gives_the_nullbill_container_its_mgmt_leg_once(ctx):
    c = ctx("small")
    apply.stage_tofu(c)
    assert c.reach.nets[1] == "name=eth1,bridge=vmbr101,firewall=1,hwaddr=02:4e:42:00:01:02,ip=10.42.1.2/24,type=veth"
    assert c.reach.nets[0].startswith("name=eth0,bridge=vmbr0,")
    assert "systemctl restart nullbill-routes.service" in c.reach.nullbill[0]
    # PVE prints the MAC in capitals; the leg is kept.
    c.reach.nets[1] = c.reach.nets[1].replace("02:4e:42:00:01:02", "02:4E:42:00:01:02")
    c.reach.calls.clear()
    apply.stage_tofu(c)
    assert not [call for call in c.reach.calls if call[0] == "host" and call[1].startswith("pct set")]


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


def test_a_full_apply_runs_every_stage_against_the_stand_ins(ctx, monkeypatch):
    monkeypatch.setattr(apply, "run_verify", lambda *a: [Result("PASS", "everything")])
    c = ctx("small")
    c.secrets.set("ntfy_topic", "nullbill-topic")
    c.secrets.set("claude_oauth_token", "sk-ant-oat01-token")
    (c.home / "web").mkdir()
    run_apply(c)
    ran = [call[1] for call in c.reach.calls if call[0] == "playbook"]
    for playbook in (
        "host-baseline.yml",
        "console.yml",
        "router.yml",
        "egress-proxy.yml",
        "updates.yml",
        "step-ca.yml",
        "tls-cert.yml",
        "postgres.yml",
        "monitoring.yml",
        "forgejo.yml",
        "ci-runner.yml",
        "ci-runner-seal.yml",
        "runner-dispatcher.yml",
        "operator.yml",
        "agent-ssh.yml",
        "log-shipper.yml",
        "vpn.yml",
    ):
        assert playbook in ran, playbook
    assert ran.index("forgejo.yml") < ran.index("runner-dispatcher.yml") and ran.index("postgres.yml") < ran.index("forgejo.yml")
    assert c.log[-1] == "PASS     everything"
    # Again: nothing is baked or made twice.
    secrets = dict(c.secrets.data)
    c.reach.calls.clear()
    run_apply(c)
    assert "ci-runner.yml" not in [call[1] for call in c.reach.calls if call[0] == "playbook"]
    assert c.secrets.data == secrets
