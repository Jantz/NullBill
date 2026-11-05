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
from nullbill.model import node_exporter_guest
from nullbill.probe import Runner
from nullbill.proxy import PROXY, proxy_port
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

    def local(self, argv: list[str], cwd: Path, env: dict[str, str], capture: bool = False) -> tuple[int, str]:
        """Run a program on the controller; output goes to the log unless captured."""


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
    run_playbook(ctx, "host", "console.yml", None, console_vars(ctx))


SPKI = 'openssl x509 -in "$1" -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64'


def console_vars(ctx: Context) -> dict:
    """The on-site screen: on or off, and the page it shows with its key pinned."""
    if not ctx.model["host"]["console"]["enabled"]:
        return {"console_enabled": False}
    cert = ctx.home / "web" / "cert.pem"
    rc, out = ctx.reach.local(["sh", "-c", SPKI, "sh", str(cert)], ctx.home, dict(os.environ), capture=True)
    if rc != 0 or not out.strip():
        raise ApplyError("host", f"cannot read the page's key from {cert} (exit {rc}); is `nullbill web` set up?")
    return {
        "console_enabled": True,
        "console_url": f"https://{ctx.bare['guests']['nullbill']['ip']}:8443/",
        "console_page_spki": out.strip(),
    }


TOKEN_ID = "root@pam!nullbill"
ROUTES_UNIT = "nullbill-routes.service"


def host_out(ctx: Context, stage: str, command: str) -> str:
    rc, out, err = ctx.reach.host(command)
    if rc != 0:
        raise ApplyError(stage, f"{command.split()[0]} on the host: {(err.strip() or f'exit {rc}')[:200]}")
    return out


def api_token(ctx: Context) -> str:
    """OpenTofu's PVE token: root@pam!nullbill, not privilege-separated, kept in
    the secrets file. Made again if the host no longer has it."""
    listed = json.loads(host_out(ctx, "tofu", "pveum user token list root@pam --output-format json") or "[]")
    stored = ctx.secrets.data.get("pve_api_token")
    if stored and any(t.get("tokenid") == "nullbill" for t in listed):
        return stored
    if any(t.get("tokenid") == "nullbill" for t in listed):
        host_out(ctx, "tofu", "pveum user token remove root@pam nullbill")
    made = json.loads(
        host_out(ctx, "tofu", "pveum user token add root@pam nullbill --privsep 0 --comment 'NullBill apply' --output-format json")
    )
    token = f"{TOKEN_ID}={made['value']}"
    ctx.secrets.set("pve_api_token", token)
    ctx.say("tofu: made PVE API token root@pam!nullbill")
    return token


def tofu_env(ctx: Context, stage: str = "tofu") -> dict[str, str]:
    """What OpenTofu needs, all through the environment: nothing is written
    in build/ or the state directory in plaintext."""
    _, host = split_target(ctx.target)
    variables = json.loads(ctx.files["tofu/main.tf.json"])["variable"]
    install = json.loads((ctx.home / "install.json").read_text()) if (ctx.home / "install.json").exists() else {}
    if "windows_iso" in variables and not os.environ.get("TF_VAR_windows_iso"):
        raise ApplyError(stage, "Windows guests are planned but no Windows ISO is set; turn off the Windows workloads for now")
    keys = [line for line in host_out(ctx, stage, "cat /root/.ssh/authorized_keys").splitlines() if line.startswith("ssh-")]
    ca = ctx.home / "pve-root-ca.pem"
    ca.write_text(host_out(ctx, stage, "cat /etc/pve/pve-root-ca.pem"))
    key = Path.home() / ".ssh" / "id_ed25519"
    env = {
        "PROXMOX_VE_API_TOKEN": api_token(ctx),
        # The provider reaches the host over SSH for disk imports.
        "PROXMOX_VE_SSH_USERNAME": "root",
        "PROXMOX_VE_SSH_PRIVATE_KEY": key.read_text() if key.exists() else "",
        # PVE's certificate is signed by its own CA.
        "SSL_CERT_FILE": str(ca),
        "TF_VAR_pve_endpoint": f"https://{host}:8006/",
        "TF_VAR_node_name": host_out(ctx, stage, "hostname").strip(),
        "TF_VAR_lan_bridge": install.get("bridge", "vmbr0"),
        "TF_VAR_ssh_authorized_keys": json.dumps(keys),
        "TF_VAR_state_passphrase": ctx.secrets.ensure("tofu_state_passphrase"),
        "TF_IN_AUTOMATION": "1",
    }
    return env


def stage_tofu(ctx: Context) -> None:
    work = ctx.home / "tofu"
    work.mkdir(mode=0o700, exist_ok=True)
    (work / "main.tf.json").write_text(ctx.files["tofu/main.tf.json"])
    env = tofu_env(ctx)
    # init fetches the provider from the registry, so it keeps the system CAs.
    init_env = {k: v for k, v in env.items() if k != "SSL_CERT_FILE"}
    for argv, extra in (
        (["tofu", "init", "-input=false", "-no-color"], init_env),
        (["tofu", "apply", "-auto-approve", "-input=false", "-no-color"], env),
    ):
        ctx.say(f"tofu: {' '.join(argv[:2])}")
        rc, _ = ctx.reach.local(argv, work, extra)
        if rc != 0:
            raise ApplyError("tofu", f"{' '.join(argv[:2])} failed (exit {rc})")
    # The host reaches the zones through the router, not its LAN gateway.
    route = f"{ctx.model['network']['base']} via {ctx.bare['zones']['mgmt']['gateway']}"
    unit = (
        "[Unit]\nDescription=NullBill zone route via the router\nAfter=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/sbin/ip route replace {route}\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    host_files(ctx, "tofu", "/etc/systemd/system", {ROUTES_UNIT: unit})
    host_out(ctx, "tofu", f"systemctl daemon-reload && systemctl enable -q {ROUTES_UNIT} && systemctl restart {ROUTES_UNIT}")


def tofu_output(ctx: Context, stage: str, name: str) -> str:
    rc, out = ctx.reach.local(["tofu", "output", "-raw", name], ctx.home / "tofu", tofu_env(ctx, stage), capture=True)
    if rc != 0:
        raise ApplyError(stage, f"tofu output {name} failed (exit {rc})")
    return out


def stage_router(ctx: Context) -> None:
    """The router VM OpenTofu made: its rendered files, then its services."""
    prefix = "router/"
    files = {"/" + rel.removeprefix(prefix): text for rel, text in ctx.files.items() if rel.startswith(prefix)}
    run_playbook(ctx, "router", "router.yml", "router", {"router_files": files})


def stage_proxy(ctx: Context) -> None:
    """Squid on the egress proxy, which every proxied zone's apt needs next."""
    prefix = f"{PROXY}/"
    files = {"/" + rel.removeprefix(prefix): text for rel, text in ctx.files.items() if rel.startswith(prefix)}
    run_playbook(ctx, "proxy", "egress-proxy.yml", PROXY, {"egress_proxy_files": files})


def update_guests(ctx: Context) -> dict[str, dict]:
    """Each Linux guest that lives on, with its proxy and reboot settings."""
    owner_reboots = set(ctx.bare["updates"]["owner_reboots"])
    proxy = f"http://{ctx.bare['guests'][PROXY]['ip']}:{proxy_port(ctx.model)}"
    guests = {}
    for name, guest in ctx.bare["guests"].items():
        if not node_exporter_guest(guest):
            continue
        # The router has its own uplink; other guests in a proxied zone
        # reach the mirrors through the proxy only.
        proxied = name != "router" and ctx.bare["zones"][guest["zone"]]["egress"] == "proxy"
        guests[name] = connection(ctx, name) | {
            "updates_apt_proxy": proxy if proxied else "",
            "updates_auto_reboot": name not in owner_reboots,
            "updates_reboot_time": ctx.bare["updates"]["reboot_at"],
        }
    return guests


def stage_updates(ctx: Context) -> None:
    guests = update_guests(ctx)
    ctx.say(f"updates: updates.yml on {len(guests)} guests")
    rc = ctx.reach.playbook("updates.yml", guests, {})
    if rc != 0:
        raise ApplyError("updates", f"updates.yml failed (exit {rc}); its log names the guest")


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
    ("tofu", stage_tofu),
    ("router", stage_router),
    ("proxy", stage_proxy),
    ("updates", stage_updates),
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

    def local(self, argv: list[str], cwd: Path, env: dict[str, str], capture: bool = False) -> tuple[int, str]:
        try:
            proc = subprocess.run(argv, cwd=cwd, env=os.environ | env, capture_output=capture, text=True, check=False)
        except FileNotFoundError as exc:
            raise NullBillError(f"{argv[0]} not found; run tools/setup-controller") from exc
        return proc.returncode, proc.stdout if capture else ""

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
