"""nullbill apply: bring the host and its guests to the plan.

Stages run in order and stop at the first failure (docs/adr/0005-apply.md).
Each stage is idempotent, so running apply again resumes. Everything that
leaves the controller goes through a Reach; the checks pass a stand-in.

Ansible reaches the host over SSH as target.ssh, containers through the
host with `pct exec` (community.proxmox's proxmox_pct_remote connection),
and VMs over SSH as `nullbill` with the host as jump host.
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from nullbill.artifacts import write_build
from nullbill.errors import NullBillError
from nullbill.home import REPO_ROOT
from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.secretfile import Secrets
from nullbill.verify import run_verify

ANSIBLE = REPO_ROOT / "ansible"
PCT_CONNECTION = "community.proxmox.proxmox_pct_remote"
PVE_FIREWALL = "/etc/pve/firewall"


class Reach(Protocol):
    def host(self, command: str, stdin: str | None = None) -> tuple[int, str, str]:
        """Run a shell command on the PVE host as target.ssh."""

    def playbook(self, playbook: str, hosts: dict[str, dict], variables: dict) -> int:
        """Run ansible/<playbook> against `hosts` (name -> host vars)."""


class ApplyError(NullBillError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"apply stopped at {stage}: {message}")


@dataclass
class Context:
    plan: dict  # with notes, as plan.yaml has it
    model: dict
    home: Path
    target: str
    reach: Reach
    secrets: Secrets
    files: dict[str, str]  # render_all output, by build/ path
    close_lan: bool = False
    run: Runner | None = None  # verify's runner; the real one is ssh_run
    log: list[str] = field(default_factory=list)

    @property
    def bare(self) -> dict:
        return strip_notes(self.plan)

    def say(self, line: str) -> None:
        self.log.append(line)
        print(line, flush=True)


def split_target(target: str) -> tuple[str, str]:
    user, _, host = target.rpartition("@")
    return user or "root", host


def connection(ctx: Context, name: str | None) -> dict:
    """Ansible host vars that reach the host (name None) or a guest."""
    user, host = split_target(ctx.target)
    if name is None:
        return {"ansible_host": host, "ansible_user": user, "ansible_python_interpreter": "/usr/bin/python3"}
    guest = ctx.bare["guests"][name]
    if guest["kind"] == "lxc":
        return {
            "ansible_connection": PCT_CONNECTION,
            "ansible_host": host,
            "ansible_user": user,
            "proxmox_vmid": guest["vmid"],
            "ansible_python_interpreter": "/usr/bin/python3",
        }
    address = guest["ips"][guest["zone"]] if "ips" in guest else guest["ip"]
    # VM host keys are new on first boot; pin each on first sight in a file
    # of NullBill's own, not the owner's known_hosts.
    known = shlex.quote(str(ctx.home / "known_hosts"))
    return {
        "ansible_host": address,
        "ansible_user": "nullbill",
        "ansible_become": True,
        "ansible_ssh_common_args": f"-o ProxyJump={ctx.target} -o UserKnownHostsFile={known} -o StrictHostKeyChecking=accept-new",
        "ansible_python_interpreter": "/usr/bin/python3",
    }


def run_playbook(ctx: Context, stage: str, playbook: str, name: str | None, variables: dict | None = None) -> None:
    label = name or "PVE host"
    ctx.say(f"{stage}: {playbook} on {label}")
    rc = ctx.reach.playbook(playbook, {name or "pve": connection(ctx, name)}, variables or {})
    if rc != 0:
        raise ApplyError(stage, f"{playbook} failed on {label} (exit {rc})")


def host_files(ctx: Context, stage: str, directory: str, files: dict[str, str]) -> None:
    """Write files under `directory` on the host, rewriting only those that differ."""
    for rel, text in files.items():
        path = shlex.quote(f"{directory}/{rel}")
        swap = f'then echo same; else cat "$t" > {path} && echo changed; fi'
        command = f't=$(mktemp) && cat > "$t" && if cmp -s "$t" {path}; {swap}; rm -f "$t"'
        rc, out, err = ctx.reach.host(command, stdin=text)
        if rc != 0:
            raise ApplyError(stage, f"writing {directory}/{rel}: {(err.strip() or f'exit {rc}')[:200]}")
        if out.strip() == "changed":
            ctx.say(f"{stage}: wrote {directory}/{rel}")


def stage_render(ctx: Context) -> None:
    write_build(ctx.files, ctx.home / "build")
    ctx.say(f"render: {len(ctx.files)} files in {ctx.home / 'build'}")


def stage_host(ctx: Context) -> None:
    run_playbook(ctx, "host", "host-baseline.yml", None)
    prefix = "firewall/"
    files = {rel.removeprefix(prefix): text for rel, text in ctx.files.items() if rel.startswith(prefix)}
    # Until VPN reach is confirmed (#9), the LAN keeps PVE's default access to
    # 8006 and 22; `apply --close-lan` installs the closed cluster.fw.
    open_variant = files.pop("cluster-lan-open.fw")
    if not ctx.close_lan:
        files["cluster.fw"] = open_variant
    host_files(ctx, "host", PVE_FIREWALL, files)


def stage_verify(ctx: Context) -> None:
    results = run_verify(ctx.plan, ctx.model, ctx.target, ctx.run)
    for result in results:
        ctx.say(result.line())
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        raise ApplyError("verify", f"{len(failed)} checks failed")


Stage = tuple[str, Callable[[Context], None]]
STAGES: list[Stage] = [
    ("render", stage_render),
    ("host", stage_host),
    ("verify", stage_verify),
]


def run_apply(ctx: Context, stages: list[Stage] = STAGES, only: str | None = None) -> None:
    names = [n for n, _ in stages]
    if only is not None and only not in names:
        raise NullBillError(f"unknown stage {only!r}; stages are {', '.join(names)}")
    for name, stage in stages:
        if only in (None, name):
            stage(ctx)


class SshReach:
    """The real Reach: ssh and ansible-playbook on the controller."""

    def __init__(self, target: str, collections: Path, extra: dict | None = None):
        self.target = target
        self.collections = collections
        # Variables under every stage's own; the checks turn off service
        # management with them, since their stand-ins have no init.
        self.extra = extra or {}

    def host(self, command: str, stdin: str | None = None) -> tuple[int, str, str]:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", self.target, command]
        proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=900)
        return proc.returncode, proc.stdout, proc.stderr

    def playbook(self, playbook: str, hosts: dict[str, dict], variables: dict) -> int:
        # Secrets reach Ansible through a 0600 file in a private directory that
        # is removed when the run ends; never through argv or build/.
        work = Path(tempfile.mkdtemp(prefix="nullbill-apply-"))
        try:
            inventory = work / "inventory.json"
            values = work / "vars.json"
            for path, data in ((inventory, {"all": {"hosts": hosts}}), (values, self.extra | variables)):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
            env = os.environ | {"ANSIBLE_COLLECTIONS_PATH": str(self.collections), "ANSIBLE_RETRY_FILES_ENABLED": "false"}
            argv = ["ansible-playbook", "-i", str(inventory), "-e", f"@{values}", str(ANSIBLE / playbook)]
            try:
                return subprocess.run(argv, env=env, check=False).returncode
            except FileNotFoundError as exc:
                raise NullBillError("ansible-playbook not found; install requirements-apply.txt (docs/runbooks/controller.md)") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
