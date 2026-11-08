import copy
import hashlib
import json
import shlex
import time
from pathlib import Path

import pytest
from conftest import fixture_config

from nullbill import apply, backups, ci, lan
from nullbill.apply import ApplyError, Context, connection, run_apply
from nullbill.artifacts import render_all
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.results import Result
from nullbill.secretfile import Secrets
from nullbill.tailnet import routes

TARGET = "root@pve.internal"


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

    def __init__(self, fail_playbook=None, fail_local=None, flaky=None):
        self.calls = []
        # A local command, by the start of its command line (or all of it,
        # ending in $), and how often it fails before it works.
        self.flaky = dict(flaky or {})
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
        self.backup_disk = None
        self.pbs_fingerprint = "aa:bb:cc"
        self.backed_up = set()
        self.revert = None  # the LAN revert timer: None, "armed" or "gone"
        self.timeouts = []
        self.dispatcher_stopped = False
        # Kept disks on the host (nb-legacy-disk): name -> (uuid, mode, fstype, state).
        self.legacy = {}
        # OpenTofu makes the files guest (VMID 2023) on its first apply.
        self.files_ct = {"exists": False, "status": "running", "mps": {}, "reboots": 0}
        # nb-legacy-copy: its records, the runs started, polls left per unit, and whether the next copy fails.
        self.copies = {}
        self.copy_runs = []
        self.copy_active = {}
        self.copy_fails = False
        # Old volumes of moved copies (nb-legacy-copy moved), volumes freed, storage disks made.
        self.moved = []
        self.freed = []
        self.storage_disks = {}
        # VMs as /cluster/resources lists them, for retiring CI clones.
        self.vms = []
        self.isos = []

    def host(self, command, stdin=None, timeout=900):
        self.calls.append(("host", command))
        if command == apply.CLUSTER_VMS:
            return 0, json.dumps(self.vms), ""
        if command.startswith("qm stop ") and "qm destroy " in command:
            vmid = int(command.split()[2])
            self.vms = [vm for vm in self.vms if vm["vmid"] != vmid]
            return 0, "", ""
        if command.startswith((apply.COPY, "systemd-run ", "systemctl is-active nb-legacy-copy-", "journalctl -q -u nb-legacy-copy-")):
            return self.copy(command)
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
        if command == apply.ISO_LIST:
            return 0, "".join(f"Volid Format Type Size VMID\n{v} iso iso 5819484160\n" for v in self.isos), ""
        if command == lan.ARMED:
            return 0, ("armed" if self.revert == "armed" else "gone") + "\n", ""
        if command == lan.arm_command():
            self.revert = "armed"
            return 0, "", ""
        if command.startswith(lan.DISARM):
            self.revert = None
            return 0, "", ""
        if command == "cat /etc/ssh/ssh_host_ed25519_key.pub":
            return 0, "ssh-ed25519 AAAAhostkey root@pve\n", ""
        if command.startswith(("systemctl ", "mkdir -p ")):
            return 0, "", ""
        if command.startswith("rm -f "):
            self.files.pop(command.split()[2], None)
            return 0, "", ""
        if command.startswith(apply.LEGACY) or command.startswith(("findmnt ", "pct status 2023", "pct shutdown 2023", "pct start 2023")):
            return self.kept(command)
        if command.startswith(("pct reboot 2023", "pct set 2023 ")) or command == "pct config 2023":
            return self.kept(command)
        if command.startswith("pct config "):
            return 0, "hostname: nullbill\n" + "".join(f"net{n}: {v}\n" for n, v in sorted(self.nets.items())), ""
        if command.startswith("pct set 2002 --net1 "):
            self.nets[1] = shlex.split(command)[4]
            return 0, "", ""
        if command.startswith("cat ") and command.endswith(" 2>/dev/null || true"):
            return 0, self.files.get(command.split()[1], ""), ""
        if command.startswith("bash -c ") and "qm status" in command:
            self.timeouts.append(timeout)
            return 0, "", ""
        if command.startswith("qm config "):
            return 0, "template: 1\n" if command.split()[2] in self.templates else "name: x\n", ""
        if command.startswith("qm shutdown "):
            self.templates.add(command.split()[2])
            return 0, "", ""
        if command.startswith(("qm status ", "timeout 600 bash -c ")):
            return 0, "", ""
        if command.startswith(apply.STORAGE_DISK + " "):
            _, disk, wipe, name = shlex.split(command)
            made = disk not in self.storage_disks
            self.storage_disks[disk] = (wipe, name)
            return 0, "made\n" if made else "kept\n", ""
        if command.startswith(apply.PVESM_FREE + " "):
            self.freed.append(shlex.split(command)[2])
            return 0, "", ""
        if command.startswith("/usr/local/sbin/nb-backup-disk "):
            made = self.backup_disk is None
            self.backup_disk = shlex.split(command)[1:]
            return 0, "made\n" if made else "kept\n", ""
        if command.startswith("pvesh get /nodes/localhost/storage/nb-pbs/content "):
            return 0, json.dumps([{"vmid": v} for v in sorted(self.backed_up)]), ""
        if command.startswith("vzdump "):
            self.timeouts.append(timeout)
            self.backed_up |= {int(a) for a in shlex.split(command)[1:] if a.isdigit()}
            return 0, "", ""
        if command == backups.METRICS:
            return 0, "14 guests, 14 with a backup\n", ""
        if "proxmox-backup-client key create --kdf none" in command:
            return 0, json.dumps({"kdf": None, "data": f"key-{len(self.calls)}"}) + "\n", ""
        if command.startswith("pct exec "):
            return self.guest(shlex.split(command)[2], shlex.split(command)[4:])
        return 127, "", f"fake host: unexpected {command}"

    def copy(self, command):
        """nb-legacy-copy, run as a transient unit that stays active for two polls."""
        argv = shlex.split(command)
        if command == f"{apply.COPY} list":
            return 0, "".join(line + "\n" for _, line in sorted(self.copies.items())), ""
        if command == f"{apply.COPY} moved":
            return 0, "".join(f"{n} {v}\n" for n, v in self.moved), ""
        if argv[:2] == [apply.COPY, "forget"]:
            self.moved = [(n, v) for n, v in self.moved if v != argv[2]]
            return 0, "", ""
        if argv[0] == "systemd-run":
            unit = argv[1].removeprefix("--unit=")
            assert self.files_ct["status"] != "running" or not self.files_ct["exists"], "the files guest runs during a copy"
            what, *args = argv[argv.index(apply.COPY) + 1 :]
            self.copy_runs.append((unit, what, *args) if what == "move" else (unit, *args))
            self.copy_active[unit] = 2
            return 0, "", ""
        unit = argv[2]
        if argv[:2] == ["systemctl", "is-active"]:
            if self.copy_active.get(unit, 0) == 0:
                return 0, "inactive\n", ""  # the command ends in || true
            self.copy_active[unit] -= 1
            if self.copy_active[unit] == 0 and not self.copy_fails:
                run = [r for r in self.copy_runs if r[0] == unit][-1]
                if run[1] == "move":
                    _, _, name, old, storage, vmid = run
                    fields = self.copies[name].split()
                    fields[1] = f"{storage}:vm-{vmid}-disk-0"
                    self.copies[name] = " ".join(fields)
                    self.moved.append((name, old))
                else:
                    n = len(self.copies)
                    self.copies[run[1]] = f"{run[1]} {run[2]}:vm-{run[3]}-disk-{n} {run[4]} 1200 107374182400 {run[5]}"
            return 0, "active\n", ""
        if self.copy_fails:
            return 0, "nb-legacy-copy: the copy differs: missing ['IMG_7.jpg']\n", ""
        return 0, "nb-legacy-copy: 51200 MiB copied\n", ""

    def kept(self, command):
        """nb-legacy-disk, findmnt and pct for the files guest (VMID 2023)."""
        argv = shlex.split(command)
        if command.startswith(f"{apply.LEGACY} list"):
            return 0, "".join(f"{n} {' '.join(v)}\n" for n, v in sorted(self.legacy.items())), ""
        if argv[:2] == [apply.LEGACY, "mount"]:
            _, _, disk, n, uuid, fstype, mode, name, uid, gid = argv
            assert (uid, gid) == ("101000", "101000")
            want = (uuid, mode, fstype, "mounted")
            known = self.legacy.get(name) == want
            self.legacy[name] = want
            return 0, "kept\n" if known else "mounted\n", ""
        if argv[:2] == [apply.LEGACY, "unmount"]:
            self.legacy.pop(argv[2], None)
            return 0, "removed\n", ""
        if argv[0] == "findmnt":
            return 0, json.dumps({"filesystems": [{"used": 3000000000000, "size": 4000000000000}]}), ""
        if command == "pct status 2023":
            return 0, f"status: {self.files_ct['status']}\n", ""
        if argv[:2] == ["pct", "shutdown"]:
            self.files_ct["status"] = "stopped"
            return 0, "", ""
        if argv[:2] == ["pct", "start"]:
            self.files_ct["status"] = "running"
            return 0, "", ""
        if argv[:2] == ["pct", "reboot"]:
            self.files_ct["reboots"] += 1
            return 0, "", ""
        if command == "pct config 2023":
            if not self.files_ct["exists"]:
                return 2, "", "Configuration file 'nodes/pve/lxc/2023.conf' does not exist\n"
            return 0, "hostname: files\n" + "".join(f"{k}: {v}\n" for k, v in sorted(self.files_ct["mps"].items())), ""
        if argv[:3] == ["pct", "set", "2023"]:
            assert self.files_ct["status"] == "stopped", "mount points change only while the files guest is stopped"
            args = argv[3:]
            for flag, value in zip(args[::2], args[1::2], strict=True):
                if flag == "--delete":
                    for key in value.split(","):
                        old = self.files_ct["mps"].pop(key)
                        if key.startswith("unused"):
                            self.freed.append(old)
                else:
                    # PVE keeps a replaced volume as unusedN; a bind mount it just drops.
                    old = self.files_ct["mps"].get(flag[2:], "").split(",")[0]
                    if old and not old.startswith("/") and old != value.split(",")[0]:
                        n = min(i for i in range(256) if f"unused{i}" not in self.files_ct["mps"])
                        self.files_ct["mps"][f"unused{n}"] = old
                    self.files_ct["mps"][flag[2:]] = value
            return 0, "", ""
        return 127, "", f"fake host: unexpected {command}"

    def guest(self, vmid, argv):
        """Containers, reached with pct exec: step-ca answers for its CA."""
        if argv == ["sh", "-c", "systemctl stop nb-dispatcher 2>/dev/null || true"]:
            self.dispatcher_stopped = True
            return 0, "", ""
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
        if argv[0] == "ssh":
            return self.vm(argv[-2], argv[-1])
        if argv[:2] == ["tofu", "apply"] and not any(a.startswith("-target=") for a in argv):
            self.files_ct["exists"] = True
        if argv[:2] == ["tofu", "apply"] and any(a.startswith("-replace=") for a in argv):
            self.templates.clear()  # OpenTofu made a fresh, plain VM
        for start, left in self.flaky.items():
            line = " ".join(argv)
            if left and (line == start[:-1] if start.endswith("$") else line.startswith(start)):
                self.flaky[start] = left - 1
                return 1, ""
        return (2 if argv[:2] == self.fail_local else 0), ("secret-token" if capture else "")

    def vm(self, login, command):
        """VMs, reached over SSH from the controller: PBS answers for its token."""
        if login != "nullbill@10.42.1.15":
            return 255, ""
        answers = {
            "sudo cat /root/nullbill/pbs-token.json": '{"tokenid": "nb-pve@pbs!pve", "value": "pbs-token-secret"}',
            "sudo proxmox-backup-manager cert info": f"Subject: CN=pbs\nFingerprint (sha256): {self.pbs_fingerprint}\n",
        }
        return (0, answers[command]) if command in answers else (255, "")

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
    def make(fixture="mid", reach=None, config=None, probed=None, **kw):
        model = build_model(config or fixture_config(fixture))
        plan = build_plan(model, probed or facts(fixture))
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
    host = {"ansible_host": "pve.internal", "ansible_user": "root", "ansible_python_interpreter": "/usr/bin/python3"}
    assert connection(c, None) == host
    forgejo = connection(c, "forgejo")
    assert forgejo["ansible_connection"] == "community.proxmox.proxmox_pct_remote"
    assert (forgejo["ansible_host"], forgejo["proxmox_vmid"]) == ("pve.internal", 2017)
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
    assert reach.calls[0][3] == {
        "host_baseline_api_name": "pve.nullbill.internal",
        "host_baseline_api_address": "10.42.1.254",
        # ntfs-3g only when the plan keeps an NTFS disk.
        "host_baseline_ntfs": False,
    }
    assert reach.calls[0][2] == {"pve": connection(c, None)}
    expected = {f"/etc/pve/firewall/{rel.removeprefix('firewall/')}" for rel in c.files if rel.startswith("firewall/")}
    expected -= {"/etc/pve/firewall/cluster-lan-open.fw", "/etc/pve/firewall/host.fw"}
    # PVE reads the host's rules from the node's own directory only.
    expected.add("/etc/pve/local/host.fw")
    # The open variant, kept on the host for the timer that undoes a close.
    expected.add(lan.SAVED_OPEN)
    # The unit that opens the LAN at boot after a restart during a close.
    expected.add(f"/etc/systemd/system/{lan.BOOT_UNIT}")
    assert set(reach.files) == expected
    assert reach.files["/etc/pve/local/host.fw"] == c.files["firewall/host.fw"]
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


def hashed(address, key):
    import base64
    import hashlib
    import hmac

    salt = b"0123456789abcdefghij"
    digest = hmac.new(salt, address.encode(), hashlib.sha1).digest()
    return f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()} ssh-ed25519 {key}\n"


def test_a_vm_opentofu_made_again_has_its_old_host_key_forgotten(ctx):
    c = ctx("small")
    ctimes = {"2001": "ctime=1700000000"}

    def host(command, stdin=None, timeout=900):
        if command.startswith("qm config "):
            return 0, f"name: x\nmeta: creation-qemu=10.0.2,{ctimes.get(command.split()[2], 'ctime=1')}\n", ""
        return 0, "", ""

    c.reach.host = host
    known = c.home / "known_hosts"
    # Hashed, as Debian's ssh_config makes them, and plain.
    pinned = hashed("10.42.1.1", "AAAArouter") + "10.42.2.10 ssh-ed25519 AAAAother\n"
    known.write_text(pinned)
    apply.forget_remade_vms(c)  # first seen: forgotten, then pinned afresh on first sight
    assert known.read_text() == "10.42.2.10 ssh-ed25519 AAAAother\n"
    known.write_text(pinned)
    apply.forget_remade_vms(c)  # the same VM: kept
    assert known.read_text() == pinned
    ctimes["2001"] = "ctime=1700000999"  # made again
    apply.forget_remade_vms(c)
    assert known.read_text() == "10.42.2.10 ssh-ed25519 AAAAother\n"


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
            "vmids": [6000, 6001],
            "labels": ["linux"],
            "runner_labels": ["linux:host"],
            "max_job_seconds": 21600,
        }
    ]

    # The host's textfile collector learns when it was baked.
    metric = f"{ci.TEXTFILE_DIR}/{ci.baked_file('linux-runner')}"
    assert time.time() - ci.baked_time(c.reach.files[metric]) < 60

    # Again: the template is kept and so is the Forgejo token.
    c.reach.calls.clear()
    apply.stage_ci(c)
    assert [call[1] for call in c.reach.calls if call[0] == "playbook"] == ["runner-dispatcher.yml"]
    assert c.secrets.data["forgejo_dispatcher_token"] == token
    assert "ci: linux-runner's golden image is baked" in c.log
    assert not c.reach.dispatcher_stopped

    # A week on, the dispatcher stops taking jobs, the old image's clones
    # finish, OpenTofu makes a fresh VM and it is baked again.
    c.reach.files[metric] = ci.baked_metric("linux-runner", time.time() - ci.REBAKE_AFTER_S - 60)
    c.reach.vms = [{"vmid": 6000, "type": "qemu", "pool": ci.POOL, "status": "running"}]
    c.reach.calls.clear()
    apply.stage_ci(c)
    assert c.reach.dispatcher_stopped
    host = [call[1] for call in c.reach.calls if call[0] == "host"]
    drain = next(h for h in host if "qm status" in h)
    assert "for v in 6000;" in drain and c.reach.timeouts[-1] > ci.MAX_JOB_S
    assert "qm stop 6000 2>/dev/null; qm destroy 6000 --purge" in host and c.reach.vms == []
    tofu = [call[1] for call in c.reach.calls if call[0] == "local" and call[1][:2] == ["tofu", "apply"]]
    assert tofu == [["tofu", "apply", "-auto-approve", "-input=false", "-no-color", "-replace=proxmox_virtual_environment_vm.linux-runner"]]
    assert [call[1] for call in c.reach.calls if call[0] == "playbook"] == [
        "ci-runner.yml",
        "agent-ssh.yml",
        "ci-runner-seal.yml",
        "runner-dispatcher.yml",
    ]
    order = [i for i, call in enumerate(c.reach.calls) if call[0] in ("local", "playbook") and call[1] in (tofu[0], "ci-runner.yml")]
    assert len(order) == 2 and c.reach.calls[order[0]][1] == tofu[0]
    assert time.time() - ci.baked_time(c.reach.files[metric]) < 60


def test_retiring_a_golden_image_destroys_only_its_pool_clones(ctx, monkeypatch):
    """A VM of the owner's inside the clone range, or a template, is never a
    clone; only VMs in the nb-ci pool and the range are stopped and destroyed."""
    monkeypatch.setattr(apply, "run_tofu", lambda *a, **kw: None)
    c = ctx("mid")
    guest = c.bare["guests"]["linux-runner"] | {"clone_vmids": "6000-6010"}
    c.reach.vms = [
        {"vmid": 6001, "type": "qemu", "status": "stopped", "name": "owner-data"},
        {"vmid": 6002, "type": "qemu", "pool": ci.POOL, "status": "running"},
        {"vmid": 6003, "type": "lxc", "pool": ci.POOL, "status": "running"},
        {"vmid": 6004, "type": "qemu", "pool": ci.POOL, "status": "stopped", "template": 1},
        {"vmid": 6020, "type": "qemu", "pool": ci.POOL, "status": "stopped"},
    ]
    apply.retire(c, "linux-runner", guest)
    host = [call[1] for call in c.reach.calls if call[0] == "host"]
    assert next(h for h in host if "qm status" in h).count("for v in 6002;") == 1
    assert [h for h in host if "qm destroy" in h] == ["qm stop 6002 2>/dev/null; qm destroy 6002 --purge"]
    assert sorted(vm["vmid"] for vm in c.reach.vms) == [6001, 6003, 6004, 6020]


def test_a_bake_that_stopped_partway_starts_again_from_a_fresh_vm(ctx):
    c = ctx("small")
    c.reach.fail_playbook = "ci-runner-seal.yml"
    with pytest.raises(ApplyError):
        apply.stage_ci(c)
    assert "/root/nullbill/linux-runner.baking" in c.reach.files
    # Fixed and run again: the sealed, half-baked VM is replaced, then baked.
    c.reach.fail_playbook = None
    c.reach.calls.clear()
    apply.stage_ci(c)
    tofu = [call[1] for call in c.reach.calls if call[0] == "local" and call[1][:2] == ["tofu", "apply"]]
    assert tofu == [["tofu", "apply", "-auto-approve", "-input=false", "-no-color", "-replace=proxmox_virtual_environment_vm.linux-runner"]]
    assert [call[1] for call in c.reach.calls if call[0] == "playbook"][0] == "ci-runner.yml"
    assert "/root/nullbill/linux-runner.baking" not in c.reach.files
    assert any("stopped partway" in line for line in c.log)
    # A clean bake leaves no mark, so the next run replaces nothing.
    c.reach.calls.clear()
    apply.stage_ci(c)
    assert not [call for call in c.reach.calls if call[0] == "local" and call[1][:2] == ["tofu", "apply"]]


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
    # verify through the page: a token whose hash only the page keeps, and the page's key to pin.
    token = c.secrets.data["agent_page_token"]
    assert operator["operator_page_token"] == token and operator["operator_page_url"] == "https://10.42.1.2:8443/"
    assert operator["operator_page_spki"] == "secret-token"
    digest = (c.home / "web" / "agent-token.sha256").read_text().strip()
    assert digest == hashlib.sha256(token.encode()).hexdigest() and token not in digest
    # Its rules list the guests and which ones it may act on.
    guests = {g["name"]: g for g in operator["operator_guests"]}
    assert guests["workspace"]["agent_managed"] and not guests["router"]["agent_managed"]
    assert guests["router"]["ip"] == "10.42.1.1" and guests["monitoring"]["fqdn"] == "monitoring.nullbill.internal"


def test_logs_stage_gives_each_shipper_its_own_certificate(ctx):
    c = ctx("small")
    apply.stage_logs(c)
    playbooks = [call[1:] for call in c.reach.calls if call[0] == "playbook"]
    names = [next(iter(p[1])) for p in playbooks]
    assert names == ["router", "pbs", "operator", "workspace", "pve"]
    certs = {p[2]["log_shipper_cert"] for p in playbooks}
    assert len(certs) == 5
    # The host ships its own SSH logins, and learns the gate's address,
    # which its LAN resolver cannot give it.
    host = playbooks[4]
    assert host[1]["pve"] == apply.connection(c, None)
    assert host[2]["log_shipper_host"] == "pve"
    assert host[2]["log_shipper_resolve"] == {"monitoring.nullbill.internal": c.bare["guests"]["monitoring"]["ip"]}
    assert "log_shipper_resolve" not in playbooks[3][2]
    workspace = playbooks[3][2]
    assert workspace["log_shipper_url"] == "https://monitoring.nullbill.internal:3100/loki/api/v1/push"
    assert workspace["log_shipper_host"] == "workspace"
    assert workspace["log_shipper_key"] == (c.home / "shipper-ca" / "workspace.key").read_text()


def test_tofu_stage_makes_the_backup_disk_before_tofu_and_respects_wipe(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("mid")
    apply.stage_tofu(c)
    assert c.reach.backup_disk == ["ata-SYNTHETIC_HDD_0002", "0"]
    disk = next(i for i, call in enumerate(c.reach.calls) if "nb-backup-disk" in call[1])
    first_tofu = next(i for i, call in enumerate(c.reach.calls) if call[0] == "local" and call[1][0] == "tofu")
    assert disk < first_tofu
    assert "tofu: ata-SYNTHETIC_HDD_0002 is now the nb-pbs-disk storage for PBS" in c.log
    c.model["disks"] = {"ata-SYNTHETIC_HDD_0002": {"use": "pbs", "wipe": True}}
    apply.stage_tofu(c)
    assert c.reach.backup_disk == ["ata-SYNTHETIC_HDD_0002", "1"]
    # Without a disk of its own there is no PBS storage to make.
    s = ctx("small")
    apply.stage_tofu(s)
    assert s.reach.backup_disk is None


def test_backups_stage_hands_tofu_the_pbs_token_fingerprint_and_a_kept_key(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("mid")
    apply.stage_backups(c)
    playbooks = [call for call in c.reach.calls if call[0] == "playbook"]
    assert [(p[1], list(p[2])) for p in playbooks] == [("pbs.yml", ["pbs"])]
    assert playbooks[0][3] == {"pbs_disk": "/dev/disk/by-id/virtio-nb-backups", "pbs_proxy_url": "http://10.42.2.10:3128"}
    ssh = [call[1] for call in c.reach.calls if call[0] == "local" and call[1][0] == "ssh"]
    assert all(f"ProxyJump={TARGET}" in argv and argv[-2] == "nullbill@10.42.1.15" for argv in ssh)
    assert c.secrets.data["pbs_token_secret"] == "pbs-token-secret"
    assert c.secrets.data["pbs_fingerprint"] == "aa:bb:cc"
    key = c.secrets.data["pbs_encryption_key"]
    assert json.loads(key)["kdf"] is None
    assert any("paper copy" in line for line in c.log)
    local = [call for call in c.reach.calls if call[0] == "local"]
    applies = [env for env, call in zip(c.reach.envs, local, strict=True) if call[1][:2] == ["tofu", "apply"]]
    assert len(applies) == 1
    env = applies[0]
    passed = (env["TF_VAR_pbs_fingerprint"], env["TF_VAR_pbs_token_secret"], env["TF_VAR_pbs_encryption_key"])
    assert passed == ("aa:bb:cc", "pbs-token-secret", key)

    # Each guest the job covers is backed up once, right away, with time to spare.
    vzdump = [call[1] for call in c.reach.calls if call[0] == "host" and call[1].startswith("vzdump ")]
    assert len(vzdump) == 1 and " --storage nb-pbs --mode snapshot " in vzdump[0]
    assert c.reach.backed_up == {g["vmid"] for g in backups.guests(c.plan).values()}
    assert c.reach.timeouts == [apply.FIRST_BACKUP_S]
    # Then monitoring learns of the job and the backups at once.
    hosts = [call[1] for call in c.reach.calls if call[0] == "host"]
    assert hosts.index(backups.METRICS) > hosts.index(vzdump[0])

    # Nothing new: no second tofu run or backup, and the key stays.
    c.reach.calls.clear()
    apply.stage_backups(c)
    assert not [call for call in c.reach.calls if call[0] == "local" and call[1][0] == "tofu"]
    assert not [call for call in c.reach.calls if call[0] == "host" and call[1].startswith("vzdump ")]
    assert c.secrets.data["pbs_encryption_key"] == key
    # A new PBS certificate reaches the host's storage.
    c.reach.pbs_fingerprint = "dd:ee:ff"
    apply.stage_backups(c)
    assert [call for call in c.reach.calls if call[0] == "local" and call[1][:2] == ["tofu", "apply"]]
    # The tofu stage passes what the backups stage learned, so the storage stays.
    c.reach.envs.clear()
    apply.stage_tofu(c)
    assert c.reach.envs[-1]["TF_VAR_pbs_fingerprint"] == "dd:ee:ff"


def test_backups_stage_does_nothing_without_a_backup_disk(ctx):
    c = ctx("small")
    apply.stage_backups(c)
    assert c.reach.calls == []


class FakeTailnet:
    """Tailscale's API: one policy, its ETag, the devices on the tailnet."""

    def __init__(self, client_id="", client_secret=""):
        self.credential = (client_id, client_secret)
        self.live = {"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}
        self.etag = "e0"
        self.devices_list = [{"id": "1", "tags": []}]
        self.refuse = None  # a validate error message
        self.mangle = False  # store something other than what was pushed
        self.pushes = []

    def devices(self):
        return self.devices_list

    def policy(self):
        return json.loads(json.dumps(self.live)), self.etag

    def validate(self, policy):
        if self.refuse:
            raise apply.TailscaleError(f"Tailscale refused the policy: {self.refuse}")

    def set_policy(self, policy, etag):
        assert etag == self.etag
        self.pushes.append(policy)
        self.live = dict(policy, extra=True) if self.mangle else policy
        self.etag = f"e{len(self.pushes)}"


@pytest.fixture
def tailnet(monkeypatch):
    fake = FakeTailnet()

    def make(client_id, client_secret):
        fake.credential = (client_id, client_secret)
        return fake

    monkeypatch.setattr(apply, "Tailnet", make)
    return fake


def with_client(c):
    c.secrets.set("tailscale_client_id", "kExampleCNTRL")
    c.secrets.set("tailscale_client_secret", "tskey-client-kExampleCNTRL-secret")
    return c


def test_tailnet_stage_waits_for_an_oauth_client_and_then_pushes_the_policy(ctx, tailnet):
    c = ctx("small")
    apply.stage_tailnet(c)
    assert c.log == ["tailnet: no Tailscale OAuth client saved; the vpn guest stays off the tailnet until one is pasted on the page"]
    assert tailnet.pushes == [] and c.reach.calls == []

    with_client(c)
    apply.stage_tailnet(c)
    want = json.loads(c.files["tailnet/policy.json"])
    assert tailnet.credential == ("kExampleCNTRL", "tskey-client-kExampleCNTRL-secret")
    assert tailnet.pushes == [want] and tailnet.live == want
    assert c.log[-1] == "tailnet: pushed the tailnet policy; Tailscale ran its tests (1 of 50 tagged devices)"
    # In place: not pushed again.
    apply.stage_tailnet(c)
    assert len(tailnet.pushes) == 1 and c.log[-1] == "tailnet: the tailnet policy is in place (1 of 50 tagged devices)"
    assert c.reach.calls == []


def test_tailnet_stage_stops_apply_before_the_host_when_tailscale_refuses(ctx, tailnet):
    c = with_client(ctx("small"))
    tailnet.refuse = "test(s) failed | member@example.internal: address 10.42.1.254:22: want: Drop, got: Accept"
    with pytest.raises(ApplyError, match="apply stopped at tailnet: Tailscale refused the policy: test.s. failed"):
        run_apply(c)
    assert tailnet.pushes == [] and c.reach.calls == []


def test_tailnet_policy_read_back_must_match(ctx, tailnet):
    c = with_client(ctx("small"))
    tailnet.mangle = True
    with pytest.raises(ApplyError, match="read back differs"):
        apply.stage_tailnet(c)


def test_tagged_devices_are_counted_from_the_tailnet(ctx, tailnet):
    c = with_client(ctx("small"))
    tailnet.devices_list = [{"id": str(n), "tags": ["tag:other"]} for n in range(50)]
    with pytest.raises(ApplyError, match="51 tagged devices with the vpn guest; Tailscale's free plan allows 50"):
        apply.stage_tailnet(c)
    assert tailnet.pushes == []
    # The vpn guest already on it counts once.
    tailnet.devices_list[0]["tags"] = ["tag:nb-vpn"]
    apply.stage_tailnet(c)
    assert c.log[-1].endswith("(50 of 50 tagged devices)")


def test_vpn_stage_waits_for_the_policy_then_a_key_then_logs_in_once_and_forgets_it(ctx, tailnet):
    c = ctx("small")
    c.secrets.set("tailscale_auth_key", "tskey-auth-example")
    apply.stage_vpn(c)
    # Tailscale's default policy lets everyone reach every route: no login before ours.
    assert c.reach.calls == [] and c.log[-1].startswith("vpn: waiting for a Tailscale OAuth client")
    c.secrets.drop("tailscale_auth_key")

    with_client(c)
    apply.stage_vpn(c)
    assert tailnet.live == json.loads(c.files["tailnet/policy.json"])
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


def test_close_lan_arms_the_undo_before_closing_and_stays_closed_once_kept(ctx, tmp_path):
    reach = FakeReach()
    known = tmp_path / "ssh" / "known_hosts"
    apply.stage_host(ctx(reach=reach))
    assert lan.state(tmp_path) == lan.OPEN and reach.revert is None
    c = ctx(reach=reach, close_lan=True, known_hosts=known)
    apply.stage_host(c)
    cluster = "/etc/pve/firewall/cluster.fw"
    assert reach.files[cluster] == c.files["firewall/cluster.fw"]
    assert "[IPSET management]" in reach.files[cluster]
    assert reach.files[lan.SAVED_OPEN] == c.files["firewall/cluster-lan-open.fw"]
    host = [call[1] for call in reach.calls if call[0] == "host"]
    arm = host.index(lan.arm_command())
    closing = max(i for i, cmd in enumerate(host) if "/etc/pve/firewall/cluster.fw" in cmd)
    assert arm < closing, "the undo must be armed before the LAN closes"
    # A restart before "Keep it closed" opens it at boot: the unit is enabled before arming.
    assert reach.files[f"/etc/systemd/system/{lan.BOOT_UNIT}"] == lan.boot_unit()
    assert host.index(f"systemctl enable -q {lan.BOOT_UNIT}") < arm
    # The controller can reach the host at its mgmt address from now on.
    assert known.read_text() == "10.42.1.254 ssh-ed25519 AAAAhostkey\n"
    assert lan.state(tmp_path) == lan.CLOSING
    assert "open https://10.42.1.2:8443 over Tailscale" in "\n".join(c.log)
    # Another run, without --close-lan, keeps it closed while the timer runs.
    again = ctx(reach=reach)
    apply.stage_host(again)
    assert reach.files[cluster] == c.files["firewall/cluster.fw"]
    # Kept closed: the timer stops, and later runs keep it closed.
    apply.confirm_lan(again)
    assert reach.revert is None and lan.state(tmp_path) == lan.CLOSED
    apply.stage_host(ctx(reach=reach))
    assert reach.files[cluster] == c.files["firewall/cluster.fw"]
    # --open-lan gives the LAN its access back.
    apply.stage_host(ctx(reach=reach, open_lan=True))
    assert reach.files[cluster] == c.files["firewall/cluster-lan-open.fw"]
    assert lan.state(tmp_path) == lan.OPEN


def test_a_restart_during_a_close_opens_the_lan_at_boot():
    """The timer is transient and gone after a restart; the marker on the
    host's disk is not, and the boot unit opens the LAN while it is there."""
    arm = lan.arm_command()
    assert arm.index(f"touch {lan.MARKER}") < arm.index("systemd-run")
    assert f"/bin/sh -c '{lan.REOPEN}'" in arm
    assert lan.REOPEN == f"/bin/cp {lan.SAVED_OPEN} {lan.CLUSTER_FW} && /bin/rm -f {lan.MARKER}"
    unit = lan.boot_unit()
    assert f"ConditionPathExists={lan.MARKER}\n" in unit and f"ExecStart=/bin/sh -c '{lan.REOPEN}'\n" in unit
    assert "After=pve-cluster.service" in unit and "WantedBy=multi-user.target" in unit
    # Keeping it closed removes the marker, so the next boot leaves it closed.
    assert lan.DISARM.startswith(f"rm -f {lan.MARKER} && ")


def test_a_close_nobody_keeps_is_undone_and_the_controller_follows(ctx, tmp_path):
    reach = FakeReach()
    apply.stage_host(ctx(reach=reach, close_lan=True, known_hosts=tmp_path / "known_hosts"))
    reach.revert = "gone"  # the host restored the open file by itself
    with pytest.raises(NullBillError, match="too late"):
        apply.confirm_lan(ctx(reach=reach))
    assert lan.state(tmp_path) == lan.OPEN
    c = ctx(reach=reach)
    apply.stage_host(c)
    assert reach.files["/etc/pve/firewall/cluster.fw"] == c.files["firewall/cluster-lan-open.fw"]


def test_confirm_needs_a_close_first(ctx):
    with pytest.raises(NullBillError, match="not being closed"):
        apply.confirm_lan(ctx())


def test_verify_reports_an_open_lan_as_degraded_not_failed(ctx, monkeypatch):
    seen = {}
    monkeypatch.setattr(apply, "run_verify", lambda *a, **kw: seen.update(kw) or [Result("PASS", "x")])
    c = ctx()
    apply.stage_verify(c)
    assert seen.pop("say") and seen == {"lan_state": "open", "lan_host": "pve.internal"}


def test_a_failed_playbook_stops_apply_before_later_stages(ctx):
    c = ctx(reach=FakeReach(fail_playbook="host-baseline.yml"))
    with pytest.raises(ApplyError, match="apply stopped at host: host-baseline.yml failed on PVE host"):
        run_apply(c)
    assert (c.home / "build" / "tofu").is_dir()
    assert c.reach.files == {}


def test_verify_stage_fails_on_any_fail(ctx, monkeypatch):
    def run_verify(*a, say, **kw):
        found = [Result("PASS", "a"), Result("FAIL", "b", "closed")]
        for result in found:
            say(result)
        return found

    monkeypatch.setattr(apply, "run_verify", run_verify)
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
    assert [call[1][:2] for call in local] == [["tofu", "init"], ["tofu", "apply"], ["tofu", "apply"]]
    # The image downloads first, on their own, then everything.
    downloads = json.loads(c.files["tofu/main.tf.json"])["resource"]["proxmox_download_file"]
    assert local[1][1][5:] == [f"-target=proxmox_download_file.{name}" for name in sorted(downloads)]
    assert local[2][1] == ["tofu", "apply", "-auto-approve", "-input=false", "-no-color"]
    assert all(call[2] == c.home / "tofu" for call in local)
    assert (c.home / "tofu" / "main.tf.json").read_text() == c.files["tofu/main.tf.json"]
    init_env, _, env = c.reach.envs
    assert "SSL_CERT_FILE" not in init_env
    assert env["SSL_CERT_FILE"] == str(c.home / "pve-root-ca.pem")
    assert env["PROXMOX_VE_API_TOKEN"].startswith("root@pam!nullbill=uuid-")
    assert env["PROXMOX_VE_API_TOKEN"] == c.secrets.get("pve_api_token")
    assert env["TF_VAR_state_passphrase"] == c.secrets.get("tofu_state_passphrase")
    assert env["TF_VAR_pve_endpoint"] == "https://pve.internal:8006/"
    assert env["TF_VAR_node_name"] == "pve"
    # Disk imports: SSH to the host apply reaches, known_hosts under a real home.
    assert env["TF_VAR_pve_ssh_address"] == "pve.internal"
    assert env["HOME"] == str(Path.home())
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


def test_tofu_downloads_are_retried_with_growing_pauses_and_then_give_up(ctx, monkeypatch):
    pauses = []
    monkeypatch.setattr(apply, "sleep", pauses.append)
    c = ctx("small", reach=FakeReach(flaky={"tofu init": 1, "tofu apply -auto-approve -input=false -no-color -target=": 2}))
    apply.stage_tofu(c)
    assert pauses == [15, 15, 60]
    assert "tofu: tofu apply (downloads) failed (exit 1); trying again in 60 s" in c.log
    assert "/etc/systemd/system/nullbill-routes.service" in c.reach.files

    pauses.clear()
    c = ctx("small", reach=FakeReach(flaky={"tofu init": 9}))
    with pytest.raises(ApplyError, match="tofu init failed"):
        apply.stage_tofu(c)
    assert pauses == list(apply.DOWNLOAD_RETRY_S)
    assert not [call for call in c.reach.calls if call[0] == "local" and call[1][:2] == ["tofu", "apply"]]


def test_full_tofu_apply_is_not_retried(ctx, monkeypatch):
    monkeypatch.setattr(apply, "sleep", lambda s: pytest.fail("slept"))
    c = ctx("small", reach=FakeReach(flaky={"tofu apply -auto-approve -input=false -no-color$": 1}))
    with pytest.raises(ApplyError, match=r"tofu: tofu apply failed \(exit 1\)"):
        apply.stage_tofu(c)


def test_tofu_failure_stops_apply(ctx, monkeypatch):
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    c = ctx("small", reach=FakeReach(fail_local=["tofu", "apply"]))
    with pytest.raises(ApplyError, match=r"apply stopped at tofu: tofu apply \(downloads\) failed"):
        apply.stage_tofu(c)
    assert "/etc/systemd/system/nullbill-routes.service" not in c.reach.files


def test_windows_needs_an_iso_before_tofu(ctx, monkeypatch):
    monkeypatch.delenv("TF_VAR_windows_iso", raising=False)
    c = ctx("mid")
    c.reach.isos = ["local:iso/debian-13.iso"]
    with pytest.raises(ApplyError, match="Proxmox holds no Windows ISO; upload one"):
        apply.stage_tofu(c)
    assert [call for call in c.reach.calls if call[0] == "local"] == []
    c.reach.isos += ["local:iso/Win11_English_x64.iso", "nb-data:iso/windows-server.iso"]
    with pytest.raises(ApplyError, match=r"more than one Windows ISO in Proxmox \(local:iso/Win11_English_x64.iso, nb-data"):
        apply.stage_tofu(c)


def test_tofu_finds_the_windows_iso_the_owner_uploaded(ctx, monkeypatch):
    monkeypatch.delenv("TF_VAR_windows_iso", raising=False)
    c = ctx("mid")
    c.reach.isos = ["local:iso/debian-13.iso", "local:iso/Win11_English_x64.iso"]
    apply.stage_tofu(c)
    assert "tofu: Windows guests install from local:iso/Win11_English_x64.iso" in c.log
    local = [call for call in c.reach.calls if call[0] == "local"]
    envs = [env for call, env in zip(local, c.reach.envs, strict=True) if call[1][0] == "tofu"]
    assert envs and all(env["TF_VAR_windows_iso"] == "local:iso/Win11_English_x64.iso" for env in envs)
    # Without Windows in the plan, Proxmox is not asked.
    s = ctx("small")
    apply.stage_tofu(s)
    assert ("host", apply.ISO_LIST) not in s.reach.calls


def passing_verify(*_args, say, **_kw) -> list[Result]:
    say(Result("PASS", "everything"))
    return [Result("PASS", "everything")]


def test_a_full_apply_runs_every_stage_against_the_stand_ins(ctx, monkeypatch, tailnet):
    monkeypatch.setattr(apply, "run_verify", passing_verify)
    c = with_client(ctx("small"))
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


def test_a_host_that_stops_answering_fails_the_stage_instead_of_crashing(monkeypatch, tmp_path):
    def hang(argv, **kwargs):
        raise apply.subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(apply.subprocess, "run", hang)
    reach = apply.SshReach("root@192.0.2.10", tmp_path)
    assert reach.host("vzdump 2010", timeout=5) == (124, "", "no answer from the host within 5 s")


def test_ansible_waits_for_a_vm_that_is_still_booting(monkeypatch, tmp_path):
    seen = {}

    def run(argv, env, check):
        seen.update(env)
        return apply.subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(apply.subprocess, "run", run)
    assert apply.SshReach("root@192.0.2.10", tmp_path).playbook("router.yml", {}, {}) == 0
    assert seen["ANSIBLE_SSH_RETRIES"] == "12"


# Disks kept from a previous OS (docs/adr/0007-legacy-disks.md).
OLD = "ata-SYNTHETIC_HDD_0002"


def legacy_config(**pin):
    return {**fixture_config("large"), "disks": {OLD: {"use": "legacy", **pin}}}


def replan(c, config):
    """The same host, planned again from a changed config.yaml."""
    from conftest import write_raw

    from nullbill.probe import parse

    c.model = build_model(config)
    c.plan = build_plan(c.model, parse(write_raw(c.home / "raw", "large")))
    c.files = render_all(c.plan, c.model)


def host_calls(c):
    return [call[1] for call in c.reach.calls if call[0] in ("host", "local")]


def index_of(calls, start):
    found = [i for i, call in enumerate(calls) if (call if isinstance(call, str) else " ".join(call)).startswith(start)]
    assert found, start
    return found[0]


def test_a_kept_disk_is_mounted_before_tofu_then_shared_and_left_alone_next_time(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("large", config=legacy_config())
    apply.stage_host(c)
    assert c.reach.calls[0][3]["host_baseline_ntfs"] is True
    c.reach.calls.clear()
    apply.stage_tofu(c)
    apply.stage_files(c)
    calls = host_calls(c)
    mount = index_of(calls, f"{apply.LEGACY} mount {OLD} 2 0123456789ABCDEF ntfs ro data 101000 101000")
    tofu = index_of(calls, "tofu init")
    attach = index_of(calls, "pct set 2023 --mp0 ")
    assert mount < index_of(calls, "/usr/local/sbin/nb-backup-disk") < tofu < attach
    assert c.reach.files_ct["mps"] == {"mp0": "/mnt/nb-legacy/data,mp=/srv/share/data,backup=0,ro=1"}
    assert c.reach.files_ct["status"] == "running"
    files = [call for call in c.reach.calls if call[0] == "playbook" and call[1] == "files.yml"]
    (play,) = files
    assert play[2] == {"files": connection(c, "files")}
    assert play[3]["files_shares"] == [{"name": "data", "read_only": True}]
    assert play[3]["files_password"] == c.secrets.data["files_share_password"] and len(play[3]["files_password"]) >= 24
    assert json.loads((c.home / "disks.json").read_text())["mounts"] == [
        {
            "disk": OLD,
            "n": 2,
            "uuid": "0123456789ABCDEF",
            "fstype": "ntfs",
            "mode": "ro",
            "name": "data",
            "mount": "/mnt/nb-legacy/data",
            "used_bytes": 3000000000000,
            "size_bytes": 4000000000000,
        }
    ]
    assert "files" in apply.shippers(c)
    # Nothing changed: the disk is kept, the guest keeps running, the password stays.
    password = c.secrets.data["files_share_password"]
    c.reach.calls.clear()
    apply.stage_tofu(c)
    apply.stage_files(c)
    calls = host_calls(c)
    restarts = ("pct set 2023", "pct shutdown 2023", "pct reboot 2023", "pct start 2023")
    assert not [x for x in calls if isinstance(x, str) and x.startswith(restarts)]
    assert not [line for line in c.log[-3:] if "mounted partition" in line]
    assert c.secrets.data["files_share_password"] == password


def test_switching_to_read_write_stops_the_guest_before_the_host_remounts(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("large", config=legacy_config())
    apply.stage_tofu(c)
    replan(c, legacy_config(mode="rw"))
    c.reach.calls.clear()
    apply.stage_tofu(c)
    calls = host_calls(c)
    assert index_of(calls, "pct shutdown 2023") < index_of(calls, f"{apply.LEGACY} mount {OLD} 2 0123456789ABCDEF ntfs rw data")
    assert c.reach.files_ct["mps"]["mp0"].endswith(",ro=0")
    assert c.reach.files_ct["status"] == "running"


def test_dropping_a_kept_disk_detaches_it_then_unmounts_it_and_wipes_nothing(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("large", config=legacy_config())
    apply.stage_tofu(c)
    replan(c, fixture_config("large"))
    assert "files" not in c.bare["guests"]
    c.reach.calls.clear()
    apply.stage_tofu(c)
    calls = host_calls(c)
    detach = index_of(calls, "pct set 2023 --delete mp0")
    assert index_of(calls, "pct shutdown 2023") < detach < index_of(calls, f"{apply.LEGACY} unmount data") < index_of(calls, "tofu init")
    assert c.reach.legacy == {} and c.reach.files_ct["mps"] == {}
    assert not [x for x in calls if "wipefs" in " ".join(x if isinstance(x, list) else [x])]
    # No longer mounted, but still kept from a wipe until a conversion with a copy or discard_data.
    assert json.loads((c.home / "disks.json").read_text()) == {"mounts": [], "unconverted": ["ata-SYNTHETIC_HDD_0002"]}


def test_share_false_keeps_the_mount_but_no_guest(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("large", config=legacy_config(share=False))
    apply.stage_tofu(c)
    assert "data" in c.reach.legacy
    assert not [x for x in host_calls(c) if isinstance(x, str) and x.startswith("pct set 2023")]
    apply.stage_files(c)
    assert "files.yml" not in [call[1] for call in c.reach.calls if call[0] == "playbook"]


def test_a_refused_mount_stops_apply_before_tofu(ctx, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("large", config=legacy_config(mode="rw"))
    real = c.reach.kept

    def refuse(command):
        if command.startswith(f"{apply.LEGACY} mount"):
            return 1, "", "nb-legacy-disk: /dev/disk/by-id/x cannot be read-write: Windows is hibernated on it\n"
        return real(command)

    c.reach.kept = refuse
    with pytest.raises(ApplyError, match="Windows is hibernated on it"):
        apply.stage_tofu(c)
    assert not [call for call in c.reach.calls if call[0] == "local" and call[1][0] == "tofu"]


def test_the_pbs_disk_is_never_one_apply_kept(ctx, monkeypatch, tmp_path):
    """Safety rule 2 at apply: even if the probe missed the mount, a disk that
    disks.json records as kept is not handed to nb-backup-disk, however often
    the refused run is repeated."""
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = ctx("mid")
    kept = json.dumps({"mounts": [{"disk": "ata-SYNTHETIC_HDD_0002", "name": "data"}]})
    (c.home / "disks.json").write_text(kept)
    c.reach.legacy = {"data": ("AB", "ro", "ntfs", "unmounted")}
    # Through the whole stage: unmounting the dropped disk rewrites disks.json first.
    with pytest.raises(ApplyError, match="was kept as legacy and has no verified copy; it is not erased"):
        apply.stage_tofu(c)
    assert json.loads((c.home / "disks.json").read_text())["unconverted"] == ["ata-SYNTHETIC_HDD_0002"]
    # The same run again, and again from a fresh context, refuses too.
    with pytest.raises(ApplyError, match="it is not erased"):
        apply.stage_tofu(c)
    with pytest.raises(ApplyError, match="it is not erased"):
        apply.stage_tofu(ctx("mid", reach=c.reach))
    assert c.reach.backup_disk is None
    # The owner's word that the files are elsewhere lets it go, and the disk
    # is no longer protected once it is converted.
    config = fixture_config("mid") | {"disks": {"ata-SYNTHETIC_HDD_0002": {"use": "pbs", "wipe": True, "discard_data": True}}}
    c2 = ctx("mid", config=config, reach=c.reach)
    apply.stage_tofu(c2)
    assert c.reach.backup_disk == ["ata-SYNTHETIC_HDD_0002", "1"]
    assert json.loads((c.home / "disks.json").read_text())["unconverted"] == []


def test_a_full_apply_with_a_kept_disk(ctx, monkeypatch, tailnet):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "run_verify", passing_verify)
    c = with_client(ctx("large", config=legacy_config()))
    c.secrets.set("ntfy_topic", "nullbill-topic")
    (c.home / "web").mkdir()
    run_apply(c)
    ran = [call[1] for call in c.reach.calls if call[0] == "playbook"]
    assert "files.yml" in ran and ran.index("updates.yml") < ran.index("files.yml")
    shipped = [call[2] for call in c.reach.calls if call[0] == "playbook" and call[1] == "log-shipper.yml"]
    assert any("files" in hosts for hosts in shipped)


def converting(facts, ctx, evacuate=True, use="pbs"):
    """The large host after a first apply kept its old disk: mounted, shared, recorded."""
    probed = copy.deepcopy(facts("large"))
    disk = next(d for d in probed["disks"] if d["id"] == OLD)
    disk["partitions"][1] |= {"mounts": ["/mnt/nb-legacy/data"], "used_bytes": 100 * 2**30}
    how = {"evacuate": "local-lvm"} if evacuate else {"discard_data": True}
    config = fixture_config("large") | {"disks": {OLD: {"use": use, "wipe": True, **how}}}
    c = ctx("large", config=config, probed=probed)
    c.reach.legacy = {"data": ("0123456789ABCDEF", "ro", "ntfs", "mounted")}
    c.reach.files_ct |= {"exists": True, "mps": {"mp0": "/mnt/nb-legacy/data,mp=/srv/share/data,backup=0,ro=1"}}
    (c.home / "disks.json").write_text(json.dumps({"mounts": [{"disk": OLD, "name": "data", "mount": "/mnt/nb-legacy/data"}]}))
    return c


def test_converting_a_kept_disk_copies_it_first_then_wipes_and_shares_the_copy(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    c = converting(facts, ctx)
    apply.stage_tofu(c)
    apply.stage_files(c)
    calls = host_calls(c)
    stop = index_of(calls, "pct shutdown 2023")
    run = index_of(calls, "systemd-run --unit=nb-legacy-copy-data")
    unmount = index_of(calls, f"{apply.LEGACY} unmount data")
    wipe = index_of(calls, "/usr/local/sbin/nb-backup-disk")
    assert stop < run < unmount < wipe < index_of(calls, "tofu init")
    assert c.reach.copy_runs == [("nb-legacy-copy-data", "data", "local-lvm", "2023", "106", OLD)]
    assert c.reach.backup_disk == [OLD, "1"]
    assert c.reach.files_ct["mps"] == {"mp0": "local-lvm:vm-2023-disk-0,mp=/srv/share/data,backup=1"}
    assert c.reach.files_ct["status"] == "running" and c.reach.legacy == {}
    (play,) = [call for call in c.reach.calls if call[0] == "playbook" and call[1] == "files.yml"]
    assert play[3]["files_shares"] == [{"name": "data", "read_only": False}]
    assert "tofu: data: 1200 files, 102400 MiB copied to local-lvm:vm-2023-disk-0 and checked" in c.log
    assert "tofu: nb-legacy-copy: 51200 MiB copied" in c.log
    # The next run, before a new probe: the copy is recorded, nothing is copied or changed again.
    c.reach.calls.clear()
    apply.stage_tofu(c)
    host = [call[1] for call in c.reach.calls if call[0] == "host"]
    assert len(c.reach.copy_runs) == 1 and not [x for x in host if x.startswith(("pct set", "pct shutdown"))]


def test_a_kept_disk_made_storage_takes_its_copy_back_checked(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    c = converting(facts, ctx, use="storage")
    apply.stage_tofu(c)
    calls = host_calls(c)
    run = index_of(calls, "systemd-run --unit=nb-legacy-copy-data")
    wipe = index_of(calls, apply.STORAGE_DISK)
    move = next(i for i, x in enumerate(calls) if isinstance(x, str) and x.startswith("systemd-run") and " move " in x)
    assert run < index_of(calls, f"{apply.LEGACY} unmount data") < wipe < index_of(calls, "tofu init") < move
    assert c.reach.storage_disks == {OLD: ("1", "nb-data")}
    assert c.reach.copy_runs[1] == ("nb-legacy-copy-data", "move", "data", "local-lvm:vm-2023-disk-0", "nb-data", "2023")
    # The guest takes the copy on the storage disk; the first copy is freed.
    assert c.reach.files_ct["mps"] == {"mp0": "nb-data:vm-2023-disk-0,mp=/srv/share/data,backup=1"}
    assert c.reach.freed == ["local-lvm:vm-2023-disk-0"] and c.reach.moved == []
    assert c.reach.files_ct["status"] == "running"
    assert "tofu: data: moved to nb-data:vm-2023-disk-0 and checked" in c.log
    # The next run changes nothing.
    c.reach.calls.clear()
    apply.stage_tofu(c)
    host = [call[1] for call in c.reach.calls if call[0] == "host"]
    assert len(c.reach.copy_runs) == 2 and not [x for x in host if x.startswith(("pct set", "pct shutdown", "pvesm free"))]


def test_a_move_that_fails_keeps_the_first_copy(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    c = converting(facts, ctx, use="storage")
    real = c.reach.copy

    def copy_then_fail(command):
        if " move " in command:
            c.reach.copy_fails = True
        return real(command)

    c.reach.copy = copy_then_fail
    with pytest.raises(ApplyError, match="moving the copy data to nb-data failed; it stays on local-lvm:vm-2023-disk-0"):
        apply.stage_tofu(c)
    assert c.reach.copies["data"].split()[1] == "local-lvm:vm-2023-disk-0" and c.reach.freed == []


def test_a_moved_copy_waits_for_the_guest_before_its_old_volume_is_freed(ctx, facts, monkeypatch):
    """apply stopped after the move: the record names the new volume, the guest the old one."""
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    probed = copy.deepcopy(facts("large"))
    record = "data nb-data:vm-2023-disk-1 106 1200 107374182400 " + OLD
    record_fields = {"size_gib": 106, "files": 1200, "bytes": 107374182400, "disk": OLD}
    probed["copies"] = [{"name": "data", "volume": "nb-data:vm-2023-disk-1", **record_fields}]
    config = fixture_config("large") | {"disks": {OLD: {"use": "storage", "wipe": True, "evacuate": "local-lvm"}}}
    c = ctx("large", config=config, probed=probed)
    c.reach.copies = {"data": record}
    c.reach.moved = [("data", "local-lvm:vm-2023-disk-0")]
    c.reach.storage_disks = {OLD: ("1", "nb-data")}
    c.reach.files_ct |= {"exists": True, "status": "running", "mps": {"mp0": "local-lvm:vm-2023-disk-0,mp=/srv/share/data,backup=1"}}
    apply.stage_tofu(c)
    assert c.reach.files_ct["mps"] == {"mp0": "nb-data:vm-2023-disk-1,mp=/srv/share/data,backup=1"}
    assert c.reach.freed == ["local-lvm:vm-2023-disk-0"] and c.reach.moved == []
    assert len(c.reach.copy_runs) == 0


def test_a_copy_the_owner_moved_in_pve_stays_where_it_is(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    probed = copy.deepcopy(facts("large"))
    record_fields = {"size_gib": 106, "files": 1200, "bytes": 107374182400, "disk": OLD}
    probed["copies"] = [{"name": "data", "volume": "local-lvm:vm-2023-disk-0", **record_fields}]
    config = fixture_config("large") | {"disks": {OLD: {"use": "storage", "wipe": True}}}
    c = ctx("large", config=config, probed=probed)
    c.reach.copies = {"data": "data local-lvm:vm-2023-disk-0 106 1200 107374182400 " + OLD}
    c.reach.storage_disks = {OLD: ("1", "nb-data")}
    mps = {"mp0": "tank:subvol-2023-disk-0,mp=/srv/share/data,backup=1"}
    c.reach.files_ct |= {"exists": True, "status": "running", "mps": dict(mps)}
    apply.stage_tofu(c)
    assert c.reach.files_ct["mps"] == mps and c.reach.copy_runs == [] and c.reach.freed == []


def test_a_copy_that_fails_leaves_the_kept_disk_as_it_was(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    c = converting(facts, ctx)
    c.reach.copy_fails = True
    with pytest.raises(ApplyError, match="copying data failed; the kept disk is as it was and is not converted: .*IMG_7"):
        apply.stage_tofu(c)
    calls = [call[1] for call in c.reach.calls if call[0] == "host"]
    assert not [x for x in calls if x.startswith((f"{apply.LEGACY} unmount", "/usr/local/sbin/nb-backup-disk"))]
    assert c.reach.legacy == {"data": ("0123456789ABCDEF", "ro", "ntfs", "mounted")} and c.reach.backup_disk is None


def test_a_copy_still_running_is_waited_for_not_started_twice(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    c = converting(facts, ctx)
    c.reach.copy_runs.append(("nb-legacy-copy-data", "data", "local-lvm", "2023", "106", OLD))
    c.reach.copy_active["nb-legacy-copy-data"] = 3
    apply.stage_tofu(c)
    assert len(c.reach.copy_runs) == 1 and "data" in c.reach.copies


def test_discarding_a_kept_disk_copies_nothing_and_wipes_it(ctx, facts, monkeypatch):
    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    c = converting(facts, ctx, evacuate=False)
    apply.stage_tofu(c)
    assert c.reach.copy_runs == [] and c.reach.backup_disk == [OLD, "1"] and c.reach.legacy == {}
    assert "files" not in c.bare["guests"]


def test_guests_in_proxied_zones_fetch_pinned_downloads_through_the_proxy(ctx):
    # A proxied zone resolves no outside names; get_url must go through Squid.
    c = ctx("mid")
    proxy = "http://10.42.2.10:3128"
    for name in ("step-ca", "monitoring", "operator", "pbs", "workspace"):
        assert connection(c, name)["downloads_proxy"] == proxy, name
    for name in ("router", "forgejo", "postgres", "vpn"):
        assert "downloads_proxy" not in connection(c, name), name
    apply.stage_ca(c)
    (hosts,) = [call[2] for call in c.reach.calls if call[0] == "playbook" and call[1] == "step-ca.yml"]
    assert hosts["step-ca"]["downloads_proxy"] == proxy
