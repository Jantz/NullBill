"""The apply check's driver, run in the controller container (tools/check-apply).

Runs apply's real Reach and sops codec against the stand-ins: the host
stage over SSH twice, a playbook on a container through the host's pct,
and one on a VM through the host as jump host.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/repo/tests")
from conftest import fixture_config, write_raw  # noqa: E402

from nullbill import apply  # noqa: E402
from nullbill.artifacts import render_all  # noqa: E402
from nullbill.errors import NullBillError  # noqa: E402
from nullbill.model import build_model  # noqa: E402
from nullbill.plan import build_plan  # noqa: E402
from nullbill.probe import parse  # noqa: E402
from nullbill.secretfile import Secrets, Sops  # noqa: E402

TARGET = "root@10.42.1.254"
HOME = Path(os.environ["NULLBILL_HOME"])
REACH = "/repo/tools/apply-check/reach.yml"


def ok(message: str) -> None:
    print(f"ok   {message}", flush=True)


def fail(message: str) -> None:
    print(f"FAIL {message}", flush=True)
    sys.exit(1)


def on(host: str, command: str) -> str:
    argv = ["ssh", "-o", "BatchMode=yes"]
    if host != TARGET:
        argv += ["-o", f"ProxyJump={TARGET}", "-o", f"UserKnownHostsFile={HOME / 'known_hosts'}"]
    return subprocess.run([*argv, host, command], capture_output=True, text=True, check=True).stdout


def check_secrets() -> None:
    first = Secrets(Sops(HOME)).ensure("check_password")
    if Secrets(Sops(HOME)).get("check_password") != first:
        fail("a saved secret reads back")
    text = (HOME / "secrets.sops.yaml").read_text()
    if first in text or "check_password: ENC[AES256_GCM," not in text:
        fail("the secrets file holds the value encrypted, under a readable name")
    for name in ("secrets.sops.yaml", "age.key"):
        if stat.S_IMODE((HOME / name).stat().st_mode) != 0o600:
            fail(f"{name} is mode 0600")
    ok("secrets round-trip through sops and age, encrypted at rest, mode 0600")


def context() -> apply.Context:
    raw = write_raw(HOME / "raw", "mid")
    model = build_model(fixture_config("mid"))
    plan = build_plan(model, parse(raw))
    reach = apply.SshReach(TARGET, Path("/opt/collections"), extra={"host_baseline_manage_services": False})
    return apply.Context(plan, model, HOME, TARGET, reach, Secrets(Sops(HOME)), render_all(plan, model))


def check_host(ctx: apply.Context) -> None:
    apply.stage_host(ctx)
    if on(TARGET, "cat /etc/pve/firewall/cluster.fw") != ctx.files["firewall/cluster-lan-open.fw"]:
        fail("cluster.fw is the LAN-open variant")
    if on(TARGET, "cat /etc/pve/firewall/2017.fw") != ctx.files["firewall/2017.fw"]:
        fail("guest firewall files are installed")
    if "passwordauthentication no" not in on(TARGET, "/usr/sbin/sshd -T"):
        fail("the baseline made sshd key-only")
    ok("host stage: baseline over SSH, firewall files in /etc/pve/firewall")
    ctx.log.clear()
    apply.stage_host(ctx)
    if [line for line in ctx.log if "wrote" in line]:
        fail("a second host stage rewrites nothing")
    ok("a second host stage rewrites nothing")


def check_guests(ctx: apply.Context) -> None:
    apply.run_playbook(ctx, "check", REACH, "forgejo")
    if on(TARGET, "cat /tmp/reached-forgejo") != "forgejo\n" or "exec 2017" not in on(TARGET, "cat /var/log/fake-pct.log"):
        fail("a container is reached with pct exec on its VMID")
    ok("container reached through the host with pct exec 2017")
    apply.run_playbook(ctx, "check", REACH, "pbs")
    if on("nullbill@10.42.1.15", "sudo stat -c %U:%a /tmp/reached-pbs") != "root:600\n":
        fail("a VM is reached through the host as jump host, as root")
    ok("VM reached as nullbill through the host, with sudo")
    try:
        apply.run_playbook(ctx, "check", "/repo/tools/apply-check/missing.yml", "pbs")
    except NullBillError as exc:
        if "apply stopped at check" not in str(exc):
            fail(f"a failed playbook names its stage: {exc}")
    else:
        fail("a failed playbook stops apply")
    ok("a failed playbook stops apply")


check_secrets()
ctx = context()
check_host(ctx)
check_guests(ctx)
