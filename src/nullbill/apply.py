"""nullbill apply: bring the host and its guests to the plan.

Stages run in order and stop at the first failure (docs/adr/0005-apply.md).
Each stage is idempotent, so running apply again resumes. Everything that
leaves the controller goes through a Reach; the checks pass a stand-in.

Ansible reaches the host over SSH as target.ssh, containers through the
host with `pct exec` (community.proxmox's proxmox_pct_remote connection),
and VMs over SSH as `nullbill` with the host as jump host.
"""

import hashlib
import json
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from nullbill import backups, ci, lan, legacy
from nullbill.artifacts import write_build
from nullbill.errors import NullBillError
from nullbill.flows import mac
from nullbill.home import REPO_ROOT
from nullbill.model import node_exporter_guest
from nullbill.probe import Runner
from nullbill.proxy import PROXY, proxy_port
from nullbill.render import strip_notes
from nullbill.secretfile import Secrets
from nullbill.tailnet import TAG, routes
from nullbill.tailscale import Tailnet, TailscaleError
from nullbill.verify import run_verify

ANSIBLE = REPO_ROOT / "ansible"
PCT_CONNECTION = "community.proxmox.proxmox_pct_remote"
PVE_FIREWALL = "/etc/pve/firewall"
# Alerts go to a ntfy.sh topic named by a random string NullBill makes (no
# account needed); the page makes the topic, apply hands it to monitoring.
NTFY = "https://ntfy.sh/"
DOWNLOAD_RESOURCE = "proxmox_download_file"


class Reach(Protocol):
    def host(self, command: str, stdin: str | None = None, timeout: int = 900) -> tuple[int, str, str]:
        """Run a shell command on the PVE host as target.ssh, for at most timeout seconds."""

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
    open_lan: bool = False
    lan_host: str | None = None  # the host's LAN address, which verify checks is closed
    known_hosts: Path | None = None  # the controller's own, ~/.ssh/known_hosts
    run: Runner | None = None  # verify's runner; the real one is ssh_run
    log: list[str] = field(default_factory=list)
    files_stopped: bool = False  # apply stopped the files guest for a disk change
    legacy_mounted: bool = False  # a kept disk was mounted in this run

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
    # A pool is reached at its golden image's reserved lease while apply bakes it.
    address = guest["ips"][guest["zone"]] if "ips" in guest else guest.get("ip") or guest["template_ip"]
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
    api = {
        "host_baseline_api_name": f"pve.{ctx.bare['domain']}",
        "host_baseline_api_address": ctx.bare["zones"]["mgmt"]["host_ip"],
        "host_baseline_ntfs": any(m["fstype"] == "ntfs" for m in legacy.mounts(ctx.bare)),
    }
    run_playbook(ctx, "host", "host-baseline.yml", None, api)
    # Before the firewall: a close in this run ends the LAN path apply came by.
    run_playbook(ctx, "host", "console.yml", None, console_vars(ctx))
    prefix = "firewall/"
    files = {rel.removeprefix(prefix): text for rel, text in ctx.files.items() if rel.startswith(prefix)}
    open_variant = files.pop("cluster-lan-open.fw")
    before = lan.state(ctx.home)
    state = lan_state(ctx, open_variant)
    if state == lan.OPEN:
        files["cluster.fw"] = open_variant
    host_files(ctx, "host", PVE_FIREWALL, files)
    if state == lan.CLOSING and before == lan.OPEN:
        page = f"https://{ctx.bare['guests']['nullbill']['ip']}:8443"
        minutes = lan.REVERT_AFTER_S // 60
        ctx.say(f"host: Proxmox is closed to the home network. Within {minutes} minutes, open {page} over Tailscale")
        ctx.say("host: and press Keep it closed; otherwise the host opens it again by itself.")


def trust_mgmt_address(ctx: Context) -> None:
    """The host's key under its mgmt address too, read over the connection
    already trusted, so the controller can reach it there once the LAN closes."""
    key = " ".join(host_out(ctx, "host", "cat /etc/ssh/ssh_host_ed25519_key.pub").split()[:2])
    line = f"{ctx.bare['zones']['mgmt']['host_ip']} {key}"
    known = ctx.known_hosts or Path.home() / ".ssh" / "known_hosts"
    lines = known.read_text().splitlines() if known.exists() else []
    if line not in lines:
        known.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        known.write_text("".join(f"{x}\n" for x in [*lines, line]))


def lan_state(ctx: Context, open_variant: str) -> str:
    """Whether the LAN keeps Proxmox access (lan.py), after acting on
    --open-lan and --close-lan; the open file is kept on the host for the
    revert timer."""
    state = lan.state(ctx.home)
    if state == lan.CLOSING and host_out(ctx, "host", lan.ARMED).strip() == "gone":
        ctx.say("host: nobody pressed Keep it closed in time, so the host opened the home network again")
        lan.write(ctx.home, lan.OPEN)
        state = lan.OPEN
    host_out(ctx, "host", f"mkdir -p -m 0700 {lan.SAVED_OPEN.rsplit('/', 1)[0]}")
    host_files(ctx, "host", lan.SAVED_OPEN.rsplit("/", 1)[0], {lan.SAVED_OPEN.rsplit("/", 1)[1]: open_variant})
    if ctx.open_lan:
        host_out(ctx, "host", f"{lan.DISARM} 2>/dev/null || true")
        lan.write(ctx.home, lan.OPEN)
        ctx.say("host: Proxmox is open to the home network again")
        return lan.OPEN
    if ctx.close_lan and state == lan.OPEN:
        trust_mgmt_address(ctx)
        host_out(ctx, "host", lan.arm_command())
        lan.write(ctx.home, lan.CLOSING, lan.deadline())
        return lan.CLOSING
    return state


def confirm_lan(ctx: Context) -> None:
    """Keep the LAN closed: stop the revert timer while it still runs."""
    if lan.state(ctx.home) != lan.CLOSING:
        raise NullBillError("the home network is not being closed; close it first")
    if host_out(ctx, "confirm", lan.ARMED).strip() == "gone":
        lan.write(ctx.home, lan.OPEN)
        raise NullBillError("too late: the host already opened the home network again; close it once more")
    host_out(ctx, "confirm", lan.DISARM)
    lan.write(ctx.home, lan.CLOSED)
    ctx.say("Proxmox stays closed to the home network; reach it over Tailscale")


SPKI = 'openssl x509 -in "$1" -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64'


def page_spki(ctx: Context, stage: str) -> str:
    """The page's public key, for clients that pin it."""
    cert = ctx.home / "web" / "cert.pem"
    rc, out = ctx.reach.local(["sh", "-c", SPKI, "sh", str(cert)], ctx.home, dict(os.environ), capture=True)
    if rc != 0 or not out.strip():
        raise ApplyError(stage, f"cannot read the page's key from {cert} (exit {rc}); is `nullbill web` set up?")
    return out.strip()


def page_url(ctx: Context) -> str:
    return f"https://{ctx.bare['guests']['nullbill']['ip']}:8443/"


def console_vars(ctx: Context) -> dict:
    """The on-site screen: on or off, and the page it shows with its key pinned."""
    if not ctx.model["host"]["console"]["enabled"]:
        return {"console_enabled": False}
    return {"console_enabled": True, "console_url": page_url(ctx), "console_page_spki": page_spki(ctx, "host")}


TOKEN_ID = "root@pam!nullbill"
ROUTES_UNIT = "nullbill-routes.service"


def host_out(ctx: Context, stage: str, command: str, timeout: int = 900) -> str:
    rc, out, err = ctx.reach.host(command, timeout=timeout)
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


def windows_iso(ctx: Context, stage: str) -> None:
    variables = json.loads(ctx.files["tofu/main.tf.json"])["variable"]
    if "windows_iso" in variables and not os.environ.get("TF_VAR_windows_iso"):
        raise ApplyError(stage, "Windows guests are planned but no Windows ISO is set; turn off the Windows workloads for now")


def tofu_env(ctx: Context, stage: str = "tofu") -> dict[str, str]:
    """What OpenTofu needs, all through the environment: nothing is written
    in build/ or the state directory in plaintext."""
    _, host = split_target(ctx.target)
    variables = json.loads(ctx.files["tofu/main.tf.json"])["variable"]
    install = json.loads((ctx.home / "install.json").read_text()) if (ctx.home / "install.json").exists() else {}
    windows_iso(ctx, stage)
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
    # Set by the backups stage once PBS answers; until then the storage and
    # the nightly job wait (tofu.WHEN_PBS_READY).
    for name in PBS_VARS:
        if name in variables and name in ctx.secrets.data:
            env[f"TF_VAR_{name}"] = ctx.secrets.data[name]
    return env


# The first backup of every guest, from the controller's side of one SSH call.
FIRST_BACKUP_S = 3 * 3600
PBS_VARS = ("pbs_fingerprint", "pbs_token_secret", "pbs_encryption_key")


# Pauses between tries of a download: tofu init's provider and the images
# PVE fetches for OpenTofu. A residential line drops for seconds to minutes;
# a real error still fails after the last try. Tries are idempotent.
DOWNLOAD_RETRY_S = (15, 60, 180)
sleep = time.sleep


def run_tofu(ctx: Context, stage: str, init: bool, replace: tuple[str, ...] = (), downloads: bool = False) -> None:
    """init (when asked), then, with downloads, an apply of the image
    downloads alone, both retried; then the full apply, run once."""
    work = ctx.home / "tofu"
    env = tofu_env(ctx, stage)
    # init fetches the provider from the registry, so it keeps the system CAs.
    init_env = {k: v for k, v in env.items() if k != "SSL_CERT_FILE"}
    apply_argv = ["tofu", "apply", "-auto-approve", "-input=false", "-no-color"]
    steps = [(["tofu", "init", "-input=false", "-no-color"], init_env, True)] if init else []
    if downloads:
        targets = sorted(json.loads(ctx.files["tofu/main.tf.json"])["resource"].get(DOWNLOAD_RESOURCE, {}))
        steps.append(([*apply_argv, *(f"-target={DOWNLOAD_RESOURCE}.{t}" for t in targets)], env, True))
    steps.append(([*apply_argv, *(f"-replace={a}" for a in replace)], env, False))
    for argv, extra, retried in steps:
        what = " ".join(argv[:2]) + (" (downloads)" if any(a.startswith("-target=") for a in argv) else "")
        for pause in (*DOWNLOAD_RETRY_S, None) if retried else (None,):
            ctx.say(f"{stage}: {what}")
            rc, _ = ctx.reach.local(argv, work, extra)
            if rc == 0:
                break
            if pause is None:
                raise ApplyError(stage, f"{what} failed (exit {rc})")
            ctx.say(f"{stage}: {what} failed (exit {rc}); trying again in {pause} s")
            sleep(pause)


def backup_disk(ctx: Context) -> None:
    """The disk the plan pins for PBS, as the LVM storage the pbs VM's
    datastore disk is made on; before OpenTofu makes that VM."""
    if not backups.enabled(ctx.plan):
        return
    disk_id, _ = backups.disk(ctx.plan)
    # The probe may have missed a kept disk's mount (the disk was out); what
    # apply last mounted is the record that keeps its data from the wipe.
    if any(m["disk"] == disk_id for m in kept_disks(ctx)):
        raise ApplyError("tofu", f"{disk_id} holds a disk kept as legacy; converting it needs a verified copy first, so it is not erased")
    wipe = "1" if ctx.model.get("disks", {}).get(disk_id, {}).get("wipe") else "0"
    out = host_out(ctx, "tofu", shlex.join(["/usr/local/sbin/nb-backup-disk", disk_id, wipe])).strip()
    if out == "made":
        ctx.say(f"tofu: {disk_id} is now the {backups.DISK_STORAGE} storage for PBS")


# Disks kept from a previous OS (legacy.py): the host mounts them before
# OpenTofu, and the files guest gets each as a bind mount point after.
LEGACY = "/usr/local/sbin/nb-legacy-disk"
DISKS_FILE = "disks.json"
FILES = "files"


def kept_disks(ctx: Context) -> list[dict]:
    """What apply last mounted ($NULLBILL_HOME/disks.json)."""
    path = ctx.home / DISKS_FILE
    return json.loads(path.read_text())["mounts"] if path.exists() else []


def files_vmid(ctx: Context) -> int:
    return ctx.model["network"]["vmid_base"] + ctx.model["services"][FILES]["id"]


def files_running(ctx: Context) -> bool:
    rc, out, _ = ctx.reach.host(f"pct status {files_vmid(ctx)}")
    return rc == 0 and "running" in out


def stop_files(ctx: Context) -> None:
    """Before a kept mount changes under it: a container keeps its own copy of
    the mount, so it must not run while the host unmounts."""
    if not ctx.files_stopped and files_mount_points(ctx) is not None and files_running(ctx):
        host_out(ctx, "tofu", f"pct shutdown {files_vmid(ctx)} --timeout 60 --forceStop 1")
        ctx.say("tofu: stopped the files guest while its disks change")
    ctx.files_stopped = True


def legacy_disks(ctx: Context) -> None:
    """Mount each kept partition and unmount those the plan dropped. The
    host script refuses anything unsafe (nb-legacy-disk)."""
    ctx.legacy_mounted = ctx.files_stopped = False
    want = {m["name"]: m for m in legacy.mounts(ctx.bare)}
    listed = {}
    for line in host_out(ctx, "tofu", f"{LEGACY} list 2>/dev/null || true").splitlines():
        name, uuid, mode, fstype, state = line.split()
        listed[name] = (uuid, mode, fstype, state)
    gone = sorted(set(listed) - set(want))
    changed = [n for n in want if n in listed and listed[n] != (want[n]["uuid"], want[n]["mode"], want[n]["fstype"], "mounted")]
    detach_stale(ctx)
    if gone or changed:
        stop_files(ctx)
    for name in gone:
        host_out(ctx, "tofu", shlex.join([LEGACY, "unmount", name]))
        ctx.say(f"tofu: stopped keeping {legacy.LEGACY_ROOT}/{name}; its disk is left as it is")
    host_id = str(legacy.HOST_ID_OFFSET + legacy.SHARE_UID)
    for name, m in sorted(want.items()):
        argv = [LEGACY, "mount", m["disk"], str(m["n"]), m["uuid"], m["fstype"], m["mode"], name, host_id, host_id]
        if host_out(ctx, "tofu", shlex.join(argv)).strip() == "mounted":
            ctx.say(f"tofu: mounted partition {m['n']} of {m['disk']} {'read-write' if m['mode'] == 'rw' else 'read-only'} at {m['mount']}")
            ctx.legacy_mounted = True
    record_disks(ctx, list(want.values()))


def record_disks(ctx: Context, mounts: list[dict]) -> None:
    """disks.json: each kept mount with its used and total bytes, for the page
    and for converting the disk later."""
    out = []
    for m in mounts:
        found = json.loads(host_out(ctx, "tofu", f"findmnt -b -J -o USED,SIZE --mountpoint {shlex.quote(m['mount'])}") or "{}")
        sizes = (found.get("filesystems") or [{}])[0]
        keys = ("disk", "n", "uuid", "fstype", "mode", "name", "mount")
        out.append({k: m[k] for k in keys} | {"used_bytes": sizes.get("used"), "size_bytes": sizes.get("size")})
    path = ctx.home / DISKS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"mounts": out}, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def mount_point(m: dict) -> str:
    ro = "1" if m["mode"] == "ro" else "0"
    # backup=0 says so in the config; PVE never backs up a bind mount anyway.
    return f"{m['mount']},mp={legacy.SHARE_ROOT}/{m['name']},backup=0,ro={ro}"


def _mp_fields(value: str) -> tuple[str, str, str]:
    """What matters in a mount point line, however PVE orders it: the host
    path, the path in the guest and read-only."""
    head, *rest = value.split(",")
    opts = dict(item.split("=", 1) for item in rest if "=" in item)
    return head, opts.get("mp", ""), opts.get("ro", "0")


def files_mount_points(ctx: Context) -> tuple[dict[str, tuple[int, str]], set[int]] | None:
    """The files guest's kept-disk mount points (host path -> index, value)
    and every mpN index in use; None while no files guest of NullBill's
    exists (a container of the owner's own may hold the VMID)."""
    vmid = files_vmid(ctx)
    rc, config, _ = ctx.reach.host(f"pct config {vmid}")
    if rc != 0 or "hostname: files" not in config.splitlines():
        return None
    current = {}
    used = set()
    for line in config.splitlines():
        key, _, value = line.partition(": ")
        if key.startswith("mp") and key[2:].isdigit():
            used.add(int(key[2:]))
            if value.startswith(legacy.LEGACY_ROOT + "/"):
                current[value.split(",", 1)[0]] = (int(key[2:]), value)
    return current, used


def detach_stale(ctx: Context) -> None:
    """Before the host unmounts anything or OpenTofu removes the guest: take
    off each mount point the plan no longer shares."""
    found = files_mount_points(ctx)
    if found is None:
        return
    current, _ = found
    keep = {m["mount"] for m in legacy.shares(ctx.bare)} if FILES in ctx.bare["guests"] else set()
    stale = [f"mp{current[path][0]}" for path in sorted(set(current) - keep)]
    if stale:
        stop_files(ctx)
        host_out(ctx, "tofu", f"pct set {files_vmid(ctx)} --delete {','.join(stale)}")
        ctx.say(f"tofu: took {len(stale)} kept disk(s) off the files guest")


def attach_shares(ctx: Context) -> None:
    """Each shared mount as a bind mount point of the files guest, set as
    root (PVE refuses bind mounts to API tokens), keeping each mpN index."""
    if FILES not in ctx.bare["guests"]:
        return
    vmid = files_vmid(ctx)
    found = files_mount_points(ctx)
    if found is None:
        raise ApplyError("tofu", f"container {vmid} is not NullBill's files guest")
    current, used = found
    want = legacy.shares(ctx.bare)
    sets = []
    for m in want:
        if m["mount"] in current:
            index, value = current[m["mount"]]
        else:
            index = min(i for i in range(256) if i not in used)
            used.add(index)
            value = ""
        line = mount_point(m)
        if not value or _mp_fields(value) != _mp_fields(line):
            sets.append(f"--mp{index} {shlex.quote(line)}")
    if not sets and not ctx.files_stopped and not ctx.legacy_mounted:
        return
    if sets:
        stop_files(ctx)
        host_out(ctx, "tofu", f"pct set {vmid} {' '.join(sets)}")
        ctx.say(f"tofu: gave the files guest {len(want)} kept disk(s) as mount points")
    if files_running(ctx):
        # A disk mounted on the host after the guest started shows up in it
        # only once the guest starts again.
        host_out(ctx, "tofu", f"pct reboot {vmid} --timeout 60")
    else:
        host_out(ctx, "tofu", f"pct start {vmid}")
    ctx.files_stopped = False


def stage_tofu(ctx: Context) -> None:
    work = ctx.home / "tofu"
    work.mkdir(mode=0o700, exist_ok=True)
    (work / "main.tf.json").write_text(ctx.files["tofu/main.tf.json"])
    windows_iso(ctx, "tofu")
    legacy_disks(ctx)
    backup_disk(ctx)
    run_tofu(ctx, "tofu", init=True, downloads=True)
    attach_shares(ctx)
    # The host reaches the zones through the router, not its LAN gateway.
    host_files(ctx, "tofu", "/etc/systemd/system", {ROUTES_UNIT: routes_unit(ctx)})
    host_out(ctx, "tofu", f"systemctl daemon-reload && systemctl enable -q {ROUTES_UNIT} && systemctl restart {ROUTES_UNIT}")
    mgmt_leg(ctx)


def routes_unit(ctx: Context) -> str:
    route = f"{ctx.model['network']['base']} via {ctx.bare['zones']['mgmt']['gateway']}"
    return (
        "[Unit]\nDescription=NullBill zone route via the router\nAfter=network-online.target\nWants=network-online.target\n\n"
        f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/sbin/ip route replace {route}\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )


def mgmt_leg(ctx: Context) -> None:
    """install.sh gives the nullbill container a LAN leg only. Its mgmt leg
    (net1, behind the PVE firewall) reaches monitoring for the status view
    and serves the page to the on-site screen and over the VPN; its LAN
    default route stays, and the zones route through the router."""
    guest = ctx.bare["guests"]["nullbill"]
    vmid, ip = guest["vmid"], guest["ip"]
    mgmt = ctx.bare["zones"]["mgmt"]
    config = host_out(ctx, "tofu", f"pct config {vmid}")
    if "hostname: nullbill" not in config:
        raise ApplyError("tofu", f"container {vmid} is not NullBill's; the plan expects install.sh's container there")
    hwaddr = mac(ctx.model["zones"]["mgmt"]["index"], ctx.model["services"]["nullbill"]["id"])
    want = f"name=eth1,bridge={mgmt['bridge']},firewall=1,hwaddr={hwaddr},ip={ip}/{mgmt['subnet'].split('/')[1]},type=veth"
    current = next((line.split(": ", 1)[1] for line in config.splitlines() if line.startswith("net1: ")), "")
    if set(current.lower().split(",")) != set(want.lower().split(",")):
        host_out(ctx, "tofu", f"pct set {vmid} --net1 {shlex.quote(want)}")
        ctx.say(f"tofu: gave the nullbill container its mgmt leg at {ip}")
    install = f"cat > /etc/systemd/system/{ROUTES_UNIT} && systemctl daemon-reload && systemctl enable -q {ROUTES_UNIT}"
    guest_out(ctx, "tofu", "nullbill", ["sh", "-c", f"{install} && systemctl restart {ROUTES_UNIT}"], stdin=routes_unit(ctx))


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
            "updates_guest_agent": guest["kind"] == "vm",
        }
    return guests


def stage_updates(ctx: Context) -> None:
    guests = update_guests(ctx)
    ctx.say(f"updates: updates.yml on {len(guests)} guests")
    rc = ctx.reach.playbook("updates.yml", guests, {})
    if rc != 0:
        raise ApplyError("updates", f"updates.yml failed (exit {rc}); its log names the guest")


def guest_out(ctx: Context, stage: str, name: str, argv: list[str], stdin: str | None = None) -> str:
    """Run a command in a container through the host's pct and return its output."""
    vmid = ctx.bare["guests"][name]["vmid"]
    rc, out, err = ctx.reach.host(shlex.join(["pct", "exec", str(vmid), "--", *argv]), stdin=stdin)
    if rc != 0:
        raise ApplyError(stage, f"{name}: {argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {(err.strip() or f'exit {rc}')[:200]}")
    return out


STEP = "/etc/step-ca"


def ca_root(ctx: Context, stage: str) -> str:
    return guest_out(ctx, stage, "step-ca", ["cat", f"{STEP}/certs/root_ca.crt"])


def ca_token(ctx: Context, stage: str, name: str) -> str:
    """A one-time token for name's first certificate, made on the CA guest so
    the provisioner password never leaves it."""
    fqdn = f"{name}.{ctx.bare['domain']}"
    argv = ["env", f"STEPPATH={STEP}", "step", "ca", "token", fqdn, "--san", fqdn]
    argv += ["--ca-url", f"https://step-ca.{ctx.bare['domain']}", "--root", f"{STEP}/certs/root_ca.crt"]
    argv += ["--provisioner", "admin", "--provisioner-password-file", f"{STEP}/secrets/password"]
    return guest_out(ctx, stage, "step-ca", argv).strip()


# Each TLS service, the group that reads its key and what reloads it.
TLS = {
    "postgres": ("postgres", "systemctl reload postgresql"),
    "monitoring": ("www-data", "systemctl reload nginx"),
    "forgejo": ("www-data", "systemctl reload nginx"),
    "runner-dispatcher": ("nb-dispatcher", "systemctl restart nb-dispatcher"),
}


def stage_ca(ctx: Context) -> None:
    """step-ca, then a certificate for each planned TLS service."""
    domain = ctx.bare["domain"]
    run_playbook(ctx, "ca", "step-ca.yml", "step-ca", {"step_ca_domain": domain})
    root = ca_root(ctx, "ca")
    for name, (group, reload) in TLS.items():
        if name not in ctx.bare["guests"]:
            continue
        variables = {
            "tls_cert_name": f"{name}.{domain}",
            "tls_cert_ca_url": f"https://step-ca.{domain}",
            "tls_cert_ca_root": root,
            "tls_cert_token": ca_token(ctx, "ca", name),
            "tls_cert_group": group,
            "tls_cert_reload": reload,
        }
        run_playbook(ctx, "ca", "tls-cert.yml", name, variables)


def planned(ctx: Context, name: str) -> bool:
    return name in ctx.bare["guests"]


def fqdn(ctx: Context, name: str) -> str:
    return f"{name}.{ctx.bare['domain']}"


def db_name(service: str) -> str:
    return service.replace("-", "_")


def postgres_clients(ctx: Context) -> list[str]:
    """Planned services the catalog lets reach PostgreSQL: one database each."""
    sources = [s for p in ctx.model["services"]["postgres"].get("ports", []) if p["port"] == 5432 for s in p["from"]]
    return [s.removeprefix("service:") for s in sources if s.startswith("service:") and planned(ctx, s.removeprefix("service:"))]


def stage_postgres(ctx: Context) -> None:
    if not planned(ctx, "postgres"):
        return
    guests = ctx.bare["guests"]
    databases = [
        {"name": db_name(c), "client": guests[c]["ip"], "password": ctx.secrets.ensure(f"postgres_{db_name(c)}")}
        for c in postgres_clients(ctx)
    ]
    variables = {
        "postgres_listen": [guests["postgres"]["ip"]],
        "postgres_tls_name": fqdn(ctx, "postgres"),
        "postgres_databases": databases,
    }
    run_playbook(ctx, "postgres", "postgres.yml", "postgres", variables)


SHIPPER_CA = "shipper-ca"
# Renewed by the apply that finds fewer than 30 days left.
SHIPPER_DAYS = 825
EC = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"]


def openssl(ctx: Context, stage: str, args: list[str]) -> int:
    directory = ctx.home / SHIPPER_CA
    directory.mkdir(mode=0o700, exist_ok=True)
    rc, _ = ctx.reach.local(["openssl", *args], directory, {}, capture=True)
    return rc


def shipper_ca(ctx: Context, stage: str) -> str:
    """The CA whose certificates Loki's gate takes pushes from. Its key stays
    in NULLBILL_HOME, so no guest, and not the agent, can mint one."""
    crt = ctx.home / SHIPPER_CA / "ca.crt"
    if not crt.exists():
        args = ["req", "-x509", *EC, "-days", "3650", "-subj", "/CN=NullBill log shipper CA", "-keyout", "ca.key", "-out", "ca.crt"]
        if openssl(ctx, stage, args) != 0:
            raise ApplyError(stage, "openssl could not make the log shipper CA")
        ctx.say(f"{stage}: made the log shipper CA in {crt.parent}")
    return crt.read_text()


def shipper_cert(ctx: Context, stage: str, name: str) -> tuple[str, str]:
    """name's push certificate and key, made again when under 30 days remain."""
    shipper_ca(ctx, stage)
    directory = ctx.home / SHIPPER_CA
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    if not crt.exists() or openssl(ctx, stage, ["x509", "-checkend", str(30 * 86400), "-noout", "-in", crt.name]) != 0:
        steps = (
            ["req", *EC, "-subj", f"/CN={name}", "-keyout", key.name, "-out", f"{name}.csr"],
            ["x509", "-req", "-in", f"{name}.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial"]
            + ["-days", str(SHIPPER_DAYS), "-out", crt.name],
        )
        for args in steps:
            if openssl(ctx, stage, args) != 0:
                raise ApplyError(stage, f"openssl could not make {name}'s log shipper certificate")
        ctx.say(f"{stage}: made {name}'s log shipper certificate")
    return crt.read_text(), key.read_text()


ROOT_CA = "/etc/nullbill/tls/root_ca.crt"  # tls_cert writes it on every guest it runs on


def tls_targets(ctx: Context) -> list[str]:
    """Endpoints that speak TLS from the first byte, so blackbox can see their
    certificates renew; PostgreSQL negotiates TLS inside its own protocol."""
    ports = {"step-ca": 443, "monitoring": 3100, "forgejo": 443, "runner-dispatcher": 8443}
    return [f"{fqdn(ctx, n)}:{p}" for n, p in ports.items() if planned(ctx, n)]


def monitoring_vars(ctx: Context) -> dict:
    guests = ctx.bare["guests"]
    topic = ctx.secrets.data.get("ntfy_topic")
    return {
        "monitoring_node_targets": [f"{fqdn(ctx, n)}:9100" for n, g in guests.items() if node_exporter_guest(g)],
        "monitoring_host_target": f"pve.{ctx.bare['domain']}:9100",
        "monitoring_tls_targets": tls_targets(ctx),
        "monitoring_ca_file": ROOT_CA,
        "monitoring_tls_name": fqdn(ctx, "monitoring"),
        "monitoring_shipper_ca": shipper_ca(ctx, "monitoring"),
        "monitoring_ntfy_url": NTFY + topic if topic else "",
        "monitoring_healthchecks_url": ctx.secrets.data.get("healthchecks_url", ""),
        "monitoring_https_proxy": proxy_url(ctx, "monitoring"),
    }


def stage_monitoring(ctx: Context) -> None:
    if not planned(ctx, "monitoring"):
        return
    variables = monitoring_vars(ctx)
    if not variables["monitoring_ntfy_url"]:
        ctx.say("monitoring: no ntfy topic yet, so alerts stay on the server; set a password on the page to make one")
    run_playbook(ctx, "monitoring", "monitoring.yml", "monitoring", variables)
    # The role makes it once on the guest; keep it with the owner's secrets.
    password = guest_out(ctx, "monitoring", "monitoring", ["cat", "/etc/grafana/admin-password"]).strip()
    ctx.secrets.set("grafana_admin_password", password)


def stage_forgejo(ctx: Context) -> None:
    if not planned(ctx, "forgejo"):
        return
    variables = {
        "forgejo_domain": fqdn(ctx, "forgejo"),
        "forgejo_db_host": fqdn(ctx, "postgres"),
        "forgejo_db_password": ctx.secrets.ensure("postgres_forgejo"),
        "forgejo_admin_email": f"admin@{ctx.bare['domain']}",
    }
    run_playbook(ctx, "forgejo", "forgejo.yml", "forgejo", variables)
    # Changed at first login; kept so the owner has it for that login.
    password = guest_out(ctx, "forgejo", "forgejo", ["cat", "/etc/forgejo/admin-password"]).strip()
    ctx.secrets.set("forgejo_admin_password", password)


def stage_files(ctx: Context) -> None:
    """Samba on the files guest, one share per kept disk, for the owner only."""
    if not planned(ctx, FILES):
        return
    shares = [{"name": m["share"], "read_only": m["mode"] == "ro"} for m in legacy.shares(ctx.bare)]
    password = ctx.secrets.ensure("files_share_password", lambda: secrets.token_urlsafe(24))
    run_playbook(ctx, "files", "files.yml", FILES, {"files_shares": shares, "files_password": password})
    address = ctx.bare["guests"][FILES]["ip"]
    ctx.say(f"files: from your devices on the tailnet, open \\\\{address}\\<share> as user nbshare; the page shows the password")


def ssh_user_ca(ctx: Context, stage: str) -> str:
    return guest_out(ctx, stage, "step-ca", ["cat", f"{STEP}/certs/ssh_user_ca_key.pub"]).strip()


def linux_pools(ctx: Context) -> dict[str, dict]:
    """Pools the dispatcher serves; Windows pools wait on their own image (#22)."""
    return {n: g for n, g in ctx.bare["guests"].items() if g["kind"] == "vm-pool" and g["zone"] != "windows"}


def forget_host_key(ctx: Context, address: str) -> None:
    """A golden image is a new machine each time it is baked."""
    known = ctx.home / "known_hosts"
    if known.exists():
        lines = known.read_text().splitlines(keepends=True)
        known.write_text("".join(line for line in lines if line.split(" ", 1)[0] != address))


def drain_clones(first: int, last: int, limit_s: int) -> str:
    """A host script: wait up to limit_s for the clones in first..last to
    finish their jobs, stop any still running, and destroy them all."""
    return (
        f'clones() {{ qm list | awk -v a={first} -v b={last} -v want="$1" '
        "'NR > 1 && $1 >= a && $1 <= b && (want == \"\" || $3 == want) { print $1 }'; }; "
        f"end=$((SECONDS + {limit_s})); "
        'while [ -n "$(clones running)" ] && [ "$SECONDS" -lt "$end" ]; do sleep 30; done; '
        'for v in $(clones running); do qm stop "$v"; done; '
        'for v in $(clones ""); do qm destroy "$v" --purge; done'
    )


def retire(ctx: Context, name: str, guest: dict) -> None:
    """Make way for a new golden image: the dispatcher stops taking jobs
    (the stage's dispatcher run starts it again), the old image's clones
    finish, and OpenTofu replaces the template with a fresh VM from the
    pinned image. Jobs queue in Forgejo meanwhile."""
    guest_out(ctx, "ci", "runner-dispatcher", ["sh", "-c", "systemctl stop nb-dispatcher 2>/dev/null || true"])
    first, last = (int(v) for v in guest["clone_vmids"].split("-"))
    ctx.say(f"ci: waiting for {name}'s running jobs to finish, at most {ci.MAX_JOB_S // 3600} h")
    host_out(ctx, "ci", f"bash -c {shlex.quote(drain_clones(first, last, ci.MAX_JOB_S))}", timeout=ci.MAX_JOB_S + 900)
    run_tofu(ctx, "ci", init=False, replace=(f"proxmox_virtual_environment_vm.{name}",))


# Where the host keeps a mark while a golden image is being baked.
BAKING_DIR = "/root/nullbill"


def bake(ctx: Context, name: str, guest: dict) -> None:
    """Boot the pool's template VM, make it a one-job runner, seal it and
    convert it to a template. A template is kept until it is
    ci.REBAKE_AFTER_S old, then replaced and baked again."""
    vmid = guest["template_vmid"]
    if "template: 1" in host_out(ctx, "ci", f"qm config {vmid}"):
        recorded = host_out(ctx, "ci", f"cat {ci.TEXTFILE_DIR}/{ci.baked_file(name)} 2>/dev/null || true")
        baked = ci.baked_time(recorded)
        if baked is not None and time.time() - baked < ci.REBAKE_AFTER_S:
            ctx.say(f"ci: {name}'s golden image is baked")
            return
        ctx.say(f"ci: {name}'s golden image is over {ci.REBAKE_AFTER_S // 86400} days old; baking it again")
        retire(ctx, name, guest)
    # A bake that stopped partway may have sealed the VM (no SSH host keys)
    # or left it half set up, so it starts again from a fresh VM.
    marker = f"{BAKING_DIR}/{name}.baking"
    if host_out(ctx, "ci", f"cat {marker} 2>/dev/null || true").strip():
        ctx.say(f"ci: an earlier bake of {name} stopped partway; OpenTofu makes its VM afresh first")
        run_tofu(ctx, "ci", init=False, replace=(f"proxmox_virtual_environment_vm.{name}",))
    host_out(ctx, "ci", f"mkdir -p -m 0700 {BAKING_DIR}")
    host_files(ctx, "ci", BAKING_DIR, {f"{name}.baking": "started\n"})
    ctx.say(f"ci: baking {name}'s golden image (VM {vmid})")
    host_out(ctx, "ci", f"qm status {vmid} | grep -q running || qm start {vmid}")
    address = guest["template_ip"]
    wait = f"until (exec 3<>/dev/tcp/{address}/22) 2>/dev/null; do sleep 5; done"
    host_out(ctx, "ci", f"timeout 600 bash -c {shlex.quote(wait)}")
    forget_host_key(ctx, address)
    runner = {
        "ci_runner_ca_root": ca_root(ctx, "ci"),
        "ci_runner_dispatcher_url": f"https://{fqdn(ctx, 'runner-dispatcher')}:8443",
        "ci_runner_proxy_url": proxy_url(ctx, name),
        "ci_runner_no_proxy": f".{ctx.bare['domain']},localhost,127.0.0.1",
    }
    run_playbook(ctx, "ci", "ci-runner.yml", name, runner)
    if guest.get("agent_managed"):
        run_playbook(ctx, "ci", "agent-ssh.yml", name, {"agent_ssh_user_ca_key": ssh_user_ca(ctx, "ci")})
    run_playbook(ctx, "ci", "ci-runner-seal.yml", name)
    host_out(ctx, "ci", f"qm shutdown {vmid} --timeout 180 && qm template {vmid}")
    host_files(ctx, "ci", ci.TEXTFILE_DIR, {ci.baked_file(name): ci.baked_metric(name, time.time())})
    host_out(ctx, "ci", f"rm -f {marker}")
    ctx.say(f"ci: {name}'s golden image is a template")


def forgejo_token(ctx: Context) -> str:
    """write:admin, for Forgejo's runner endpoints; made once and kept."""

    def make() -> str:
        argv = ["/usr/local/sbin/nb-forgejo", "admin", "user", "generate-access-token", "--username", "nullbill-admin"]
        # A unique name, so a token made again after a lost secrets file never clashes.
        argv += ["--token-name", f"dispatcher-{os.urandom(4).hex()}", "--scopes", "write:admin", "--raw"]
        return guest_out(ctx, "ci", "forgejo", argv).strip()

    return ctx.secrets.ensure("forgejo_dispatcher_token", make)


def dispatcher_vars(ctx: Context) -> dict:
    pools = []
    for name, guest in linux_pools(ctx).items():
        first, last = (int(v) for v in guest["clone_vmids"].split("-"))
        labels = ctx.model["services"][name]["runs_on"]
        pools.append(
            {
                "name": name,
                "template": guest["template_vmid"],
                "vmids": [first, last],
                "labels": labels,
                "runner_labels": [f"{label}:host" for label in labels],
                "max_job_seconds": ci.MAX_JOB_S,
            }
        )
    return {
        "runner_dispatcher_tls_name": fqdn(ctx, "runner-dispatcher"),
        "runner_dispatcher_forgejo_url": f"https://{fqdn(ctx, 'forgejo')}",
        "runner_dispatcher_forgejo_token": forgejo_token(ctx),
        "runner_dispatcher_pve_url": f"https://pve.{ctx.bare['domain']}:8006",
        "runner_dispatcher_node": host_out(ctx, "ci", "hostname").strip(),
        "runner_dispatcher_pve_token": tofu_output(ctx, "ci", "dispatcher_token").strip(),
        "runner_dispatcher_pve_ca": host_out(ctx, "ci", "cat /etc/pve/pve-root-ca.pem"),
        "runner_dispatcher_pools": pools,
    }


def stage_ci(ctx: Context) -> None:
    """Each Linux pool's golden image, then the dispatcher that clones it per job."""
    if not planned(ctx, "runner-dispatcher") or not linux_pools(ctx):
        return
    for name, guest in linux_pools(ctx).items():
        bake(ctx, name, guest)
    run_playbook(ctx, "ci", "runner-dispatcher.yml", "runner-dispatcher", dispatcher_vars(ctx))


def proxy_url(ctx: Context, name: str) -> str:
    """The egress proxy for a guest in a proxied zone, or "" where egress is direct."""
    if ctx.bare["zones"][ctx.bare["guests"][name]["zone"]]["egress"] != "proxy":
        return ""
    return f"http://{ctx.bare['guests'][PROXY]['ip']}:{proxy_port(ctx.model)}"


AGENT_CREDENTIALS = {"anthropic_api_key": "operator_anthropic_api_key", "claude_oauth_token": "operator_claude_oauth_token"}


def agent_guests(ctx: Context) -> list[str]:
    """Agent-managed guests that live on; pool images get agent SSH when baked."""
    return [n for n, g in ctx.bare["guests"].items() if g.get("agent_managed") and g["kind"] != "vm-pool"]


def stage_agents(ctx: Context) -> None:
    """The operator with the agent CLI and its tokens, and agent SSH on each
    agent-managed guest."""
    if not planned(ctx, "operator"):
        return
    stored = {var: ctx.secrets.data.get(name, "") for name, var in AGENT_CREDENTIALS.items()}
    if not any(stored.values()):
        ctx.say("agents: no API key or subscription token for the agent CLI yet; paste one on the page and run setup again")
        return
    variables = {
        **stored,
        "operator_proxy_url": proxy_url(ctx, "operator"),
        "operator_ca_url": f"https://{fqdn(ctx, 'step-ca')}",
        "operator_pve_token": tofu_output(ctx, "agents", "agent_token").strip(),
        "operator_pve_ca": host_out(ctx, "agents", "cat /etc/pve/pve-root-ca.pem"),
        "operator_ca_root": ca_root(ctx, "agents"),
        "operator_step_password": guest_out(ctx, "agents", "step-ca", ["cat", f"{STEP}/secrets/agent-password"]).strip(),
    }
    variables |= agent_page(ctx) | {"operator_guests": agent_map(ctx), "operator_domain": ctx.bare["domain"]}
    run_playbook(ctx, "agents", "operator.yml", "operator", variables)
    key = ssh_user_ca(ctx, "agents")
    for name in agent_guests(ctx):
        run_playbook(ctx, "agents", "agent-ssh.yml", name, {"agent_ssh_user_ca_key": key})


# The page keeps the SHA-256 of the agent's token, never the token itself.
AGENT_PAGE_TOKEN = "web/agent-token.sha256"


def agent_page(ctx: Context) -> dict:
    """The agent's way to run verify and read its output through the page
    (web/server.py), with the page's key pinned."""
    token = ctx.secrets.ensure("agent_page_token", lambda: secrets.token_urlsafe(32))
    path = ctx.home / AGENT_PAGE_TOKEN
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(token.encode()).hexdigest() + "\n"
    if not path.exists() or path.read_text() != digest:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(digest)
    return {"operator_page_url": page_url(ctx), "operator_page_token": token, "operator_page_spki": page_spki(ctx, "agents")}


def agent_map(ctx: Context) -> list[dict]:
    """What the agent's rules tell it about the guests: name, address, zone,
    kind, and whether it may act on it."""
    guests = ctx.bare["guests"]
    return [
        {
            "name": name,
            "fqdn": fqdn(ctx, name),
            "ip": g["ips"][g["zone"]] if "ips" in g else g.get("ip") or g.get("template_ip", ""),
            "zone": g["zone"],
            "kind": g["kind"],
            "vmid": g.get("vmid") or g.get("template_vmid"),
            "agent_managed": bool(g.get("agent_managed")),
        }
        for name, g in sorted(guests.items())
    ]


def shippers(ctx: Context) -> list[str]:
    """Guests whose agent commands and SSH logins go to Loki: the operator,
    agent-managed guests, and the VMs that run sshd."""
    guests = ctx.bare["guests"]
    vms = [n for n, g in guests.items() if g["kind"] == "vm" and g["zone"] != "windows"]
    return [n for n in guests if n in ("operator", FILES) or n in agent_guests(ctx) or n in vms]


# The host label and certificate name of the PVE host's own shipper.
HOST_SHIPPER = "pve"


def stage_logs(ctx: Context) -> None:
    """Agent commands and SSH logins to Loki, from each shipper guest and
    from the PVE host. The host asks the owner's LAN for names, so it is
    given the gate's address."""
    if not planned(ctx, "monitoring"):
        return
    root = ca_root(ctx, "logs")
    gate = fqdn(ctx, "monitoring")
    for name in [*shippers(ctx), None]:
        label = name or HOST_SHIPPER
        cert, key = shipper_cert(ctx, "logs", label)
        variables = {
            "log_shipper_url": f"https://{gate}:3100/loki/api/v1/push",
            "log_shipper_host": label,
            "log_shipper_ca": root,
            "log_shipper_cert": cert,
            "log_shipper_key": key,
        }
        if name is None:
            variables["log_shipper_resolve"] = {gate: ctx.bare["guests"]["monitoring"]["ip"]}
        run_playbook(ctx, "logs", "log-shipper.yml", name, variables)


def vm_out(ctx: Context, stage: str, name: str, command: str) -> str:
    """Run a command on a VM as nullbill over SSH, through the host as
    Ansible reaches it, and return its output."""
    conn = connection(ctx, name)
    argv = ["ssh", "-o", "BatchMode=yes", *shlex.split(conn["ansible_ssh_common_args"]), f"nullbill@{conn['ansible_host']}", command]
    rc, out = ctx.reach.local(argv, ctx.home, {}, capture=True)
    if rc != 0:
        raise ApplyError(stage, f"{name}: {command.split()[0]} {command.split()[1]} failed (exit {rc})")
    return out


def encryption_key(ctx: Context) -> str:
    """The key the host encrypts backups with, made on the host by PVE's own
    client and kept in secrets, so a copy outlives the host. A key the host
    already holds is kept rather than replaced."""
    held = f"{backups.PRIV_DIR}/{backups.STORAGE}.enc"
    make = (
        f"if [ -s {held} ]; then cat {held}; else d=$(mktemp -d) && "
        'proxmox-backup-client key create --kdf none "$d/key.json" >/dev/null && cat "$d/key.json"; rc=$?; rm -rf "$d"; exit $rc; fi'
    )

    def new() -> str:
        ctx.say("backups: made the backup encryption key; keep a paper copy (docs/runbooks/pbs-encryption-key.md)")
        return host_out(ctx, "backups", f"bash -c {shlex.quote(make)}").strip()

    return ctx.secrets.ensure("pbs_encryption_key", new)


def stage_backups(ctx: Context) -> None:
    """PBS on its own disk, then the host's PBS storage and nightly job,
    which OpenTofu adds once it has PBS's fingerprint and token."""
    if not backups.enabled(ctx.plan):
        return
    variables = {"pbs_disk": backups.DISK_PATH, "pbs_proxy_url": proxy_url(ctx, "pbs")}
    run_playbook(ctx, "backups", "pbs.yml", "pbs", variables)
    token = json.loads(vm_out(ctx, "backups", "pbs", "sudo cat /root/nullbill/pbs-token.json"))["value"]
    info = vm_out(ctx, "backups", "pbs", "sudo proxmox-backup-manager cert info")
    fingerprint = next((line.split(": ", 1)[1].strip() for line in info.splitlines() if line.startswith("Fingerprint (sha256):")), "")
    if not fingerprint:
        raise ApplyError("backups", "pbs: no certificate fingerprint in proxmox-backup-manager cert info")
    before = {name: ctx.secrets.data.get(name) for name in PBS_VARS}
    encryption_key(ctx)
    ctx.secrets.set("pbs_fingerprint", fingerprint)
    ctx.secrets.set("pbs_token_secret", token)
    if any(ctx.secrets.data.get(name) != value for name, value in before.items()):
        run_tofu(ctx, "backups", init=False)
    # A guest without a backup yet gets one now, so verify can check age and
    # restore from the first run on, not only after the first night.
    listing = json.loads(host_out(ctx, "backups", backups.LIST) or "[]")
    missing = backups.unbacked(ctx.plan, listing)
    if missing:
        ctx.say(f"backups: first backup of {len(missing)} guests now; this can take a while")
        argv = ["vzdump", *map(str, missing), "--storage", backups.STORAGE, "--mode", "snapshot", "--notes-template", "{{guestname}}"]
        host_out(ctx, "backups", shlex.join(argv), timeout=FIRST_BACKUP_S)
    # Monitoring sees the job and the first backups now, not within the hour.
    host_out(ctx, "backups", backups.METRICS)
    ctx.say(f"backups: nightly at {backups.SCHEDULE} to {backups.STORAGE}, encrypted on the host")


TAILSCALE_CLIENT = ("tailscale_client_id", "tailscale_client_secret")


def tailnet_api(ctx: Context) -> Tailnet | None:
    """The owner's Tailscale OAuth client, or None until one is pasted."""
    client_id, secret = (ctx.secrets.data.get(name, "") for name in TAILSCALE_CLIENT)
    return Tailnet(client_id, secret) if client_id and secret else None


def push_policy(ctx: Context, stage: str) -> bool:
    """Make the tailnet's policy the rendered one, or stop apply. False while
    no OAuth client is saved. Tailscale runs the policy's tests before it
    takes it; the policy read back must equal the rendered one."""
    api = tailnet_api(ctx)
    if api is None:
        return False
    want = json.loads(ctx.files["tailnet/policy.json"])
    limit = ctx.model["tailnet"]["free_limits"]["tagged_devices"]
    try:
        devices = api.devices()
        tagged = [d for d in devices if d.get("tags")]
        # The vpn guest counts once it has joined; until then it is about to.
        count = len(tagged) + (0 if any(TAG in d["tags"] for d in tagged) else 1)
        if count > limit:
            raise ApplyError(
                stage, f"the tailnet would have {count} tagged devices with the vpn guest; Tailscale's free plan allows {limit}"
            )
        live, etag = api.policy()
        if live == want:
            ctx.say(f"{stage}: the tailnet policy is in place ({count} of {limit} tagged devices)")
            return True
        api.validate(want)
        api.set_policy(want, etag)
        back, _ = api.policy()
    except TailscaleError as error:
        raise ApplyError(stage, str(error)) from error
    if back != want:
        raise ApplyError(stage, "the tailnet policy read back differs from the rendered one; check the admin console's Access controls")
    ctx.say(f"{stage}: pushed the tailnet policy; Tailscale ran its tests ({count} of {limit} tagged devices)")
    return True


def stage_tailnet(ctx: Context) -> None:
    """Before anything on the host changes: the tailnet's policy, so nobody
    reaches more than the plan grants once the vpn guest is in."""
    if not planned(ctx, "vpn"):
        return
    if not push_policy(ctx, "tailnet"):
        ctx.say("tailnet: no Tailscale OAuth client saved; the vpn guest stays off the tailnet until one is pasted on the page")


def stage_vpn(ctx: Context) -> None:
    """Tailscale on the vpn guest as the tagged subnet router. It logs in once
    with the owner's one-time auth key; until then it waits, logged out. It
    runs only once the tailnet's policy is the rendered one: Tailscale's
    default policy lets every device reach every route."""
    if not planned(ctx, "vpn"):
        return
    if not push_policy(ctx, "vpn"):
        ctx.say("vpn: waiting for a Tailscale OAuth client; paste one on the page and run setup again")
        return
    variables = {
        "vpn_routes": routes(ctx.plan, ctx.model),
        "vpn_tag": TAG,
        "vpn_auth_key": ctx.secrets.data.get("tailscale_auth_key", ""),
    }
    run_playbook(ctx, "vpn", "vpn.yml", "vpn", variables)
    status = json.loads(guest_out(ctx, "vpn", "vpn", ["tailscale", "status", "--json"]))
    if status.get("BackendState") == "Running":
        name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
        ctx.say(f"vpn: on the tailnet as {name or 'nullbill'}, advertising {', '.join(variables['vpn_routes'])}")
        ctx.say(f"vpn: from your devices on the tailnet the page is https://{ctx.bare['guests']['nullbill']['ip']}:8443")
        # A one-time key is spent once it has logged in.
        ctx.secrets.drop("tailscale_auth_key")
    elif variables["vpn_auth_key"]:
        state = status.get("BackendState")
        raise ApplyError("vpn", f"Tailscale did not log in ({state}); make a new one-time auth key and paste it on the page")
    else:
        ctx.say("vpn: waiting for a Tailscale auth key; paste one on the page and run setup again")


def stage_verify(ctx: Context) -> None:
    lan_host = ctx.lan_host or split_target(ctx.target)[1]
    results = run_verify(ctx.plan, ctx.model, ctx.target, ctx.run, lan_state=lan.state(ctx.home), lan_host=lan_host)
    for result in results:
        ctx.say(result.line())
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        raise ApplyError("verify", f"{len(failed)} checks failed")


Stage = tuple[str, Callable[[Context], None]]
STAGES: list[Stage] = [
    ("render", stage_render),
    ("tailnet", stage_tailnet),
    ("host", stage_host),
    ("tofu", stage_tofu),
    ("router", stage_router),
    ("proxy", stage_proxy),
    ("updates", stage_updates),
    ("ca", stage_ca),
    ("postgres", stage_postgres),
    ("monitoring", stage_monitoring),
    ("forgejo", stage_forgejo),
    ("files", stage_files),
    ("ci", stage_ci),
    ("agents", stage_agents),
    ("logs", stage_logs),
    ("backups", stage_backups),
    ("vpn", stage_vpn),
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

    def host(self, command: str, stdin: str | None = None, timeout: int = 900) -> tuple[int, str, str]:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30", self.target, command]
        try:
            proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # A stage error like any other, so the run stops with its stage named.
            return 124, "", f"no answer from the host within {timeout} s"
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
