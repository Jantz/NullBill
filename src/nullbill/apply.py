"""nullbill apply: bring the host and its guests to the plan.

Stages run in order and stop at the first failure (docs/adr/0005-apply.md).
Each stage is idempotent, so running apply again resumes. Everything that
leaves the controller goes through a Reach; the checks pass a stand-in.

Ansible reaches the host over SSH as target.ssh, containers through the
host with `pct exec` (community.proxmox's proxmox_pct_remote connection,
pipelined: ansible/connection_plugins/nb_pct.py), and VMs over SSH as
`nullbill` with the host as jump host. A playbook whose inputs are the same
as at its last good run on a machine is not run there again (applied.py).
"""

import base64
import hashlib
import hmac
import json
import os
import re
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

from nullbill import (
    applied,
    backups,
    browserca,
    ci,
    custom,
    direct,
    games,
    hostroot,
    lan,
    legacy,
    llm,
    media,
    names,
    owner,
    power,
    tailscale,
)
from nullbill.artifacts import write_build
from nullbill.errors import NullBillError
from nullbill.flows import TAILSCALE_PORT, mac
from nullbill.home import REPO_ROOT
from nullbill.model import NODE_EXPORTER, node_exporter_guest
from nullbill.probe import NTFS_CHECK, Runner, ntfs_state
from nullbill.proxy import PROXY, proxy_port
from nullbill.render import strip_notes
from nullbill.secretfile import Secrets
from nullbill.tailnet import TAG, nameserver, routes
from nullbill.tailscale import MissingScope, Tailnet, TailscaleError
from nullbill.verify import run_verify

ANSIBLE = REPO_ROOT / "ansible"
# community.proxmox's proxmox_pct_remote with pipelining (ansible/connection_plugins).
PCT_CONNECTION = "nb_pct"
PVE_FIREWALL = "/etc/pve/firewall"
# The node's host firewall; PVE reads /etc/pve/nodes/<node>/host.fw, which
# /etc/pve/local points to on every node.
PVE_HOST_FIREWALL = "/etc/pve/local"
# Alerts go to a ntfy.sh topic named by a random string NullBill makes (no
# account needed); the page makes the topic, apply hands it to monitoring.
NTFY = "https://ntfy.sh/"
DOWNLOAD_RESOURCE = "proxmox_download_file"
GUEST_RESOURCES = ("proxmox_virtual_environment_vm", "proxmox_virtual_environment_container")
# The full apply's saved plan, encrypted like the state (tofu.ENCRYPTION).
PLAN_FILE = "nullbill.tfplan"
# Guests the last apply stopped before deleting, until the owner confirms.
REMOVALS = "removals.json"
# The guests the last OpenTofu apply that finished left on the server, so
# the page can say what a changed choice would delete before setup runs.
GUESTS = "guests.json"


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
    media_stopped: bool = False  # likewise the Jellyfin guest
    legacy_mounted: bool = False  # a kept disk was mounted in this run
    kept_before: set[str] | None = None  # disks disks.json protects, as this run found it
    full: bool = False  # run every playbook, even where its inputs are unchanged (applied.py)
    remove_guests: bool = False  # the owner confirmed deleting the guests REMOVALS names
    pinned: set[str] = field(default_factory=set)  # VMs whose host key this run checked through PVE
    held: bool = False  # this run had nb-power start a guest that runs on demand (ensure_on)
    shut_down: set[str] = field(default_factory=set)  # guests this run shut down itself, to start again

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
        } | _download_proxy(ctx, name)
    # A pool is reached at its golden image's reserved lease while apply bakes it.
    address = vm_address(guest)
    # VM host keys are new on first boot; pin each on first sight in a file
    # of NullBill's own, not the owner's known_hosts (pin_host_key).
    known = shlex.quote(str(ctx.home / "known_hosts"))
    return {
        "ansible_host": address,
        "ansible_user": "nullbill",
        "ansible_become": True,
        "ansible_ssh_common_args": f"-o ProxyJump={ctx.target} -o UserKnownHostsFile={known} -o StrictHostKeyChecking=accept-new",
        "ansible_python_interpreter": "/usr/bin/python3",
    } | _download_proxy(ctx, name)


def vm_address(guest: dict) -> str:
    """Where the controller reaches a VM over SSH. A pool is reached at its
    golden image's reserved lease while apply bakes it."""
    return guest["ips"][guest["zone"]] if "ips" in guest else guest.get("ip") or guest["template_ip"]


HOST_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}")
AGENT_WAIT_S = 120


def agent_host_key(vmid: int) -> str:
    """Host command printing the VM's ed25519 host key as the VM's own guest
    agent reads it, the channel PVE has into the VM, or nothing when the
    agent does not answer (not installed yet, or the VM is still booting)."""
    ping = f"qm guest cmd {vmid} ping >/dev/null 2>&1"
    wait = f"end=$((SECONDS + {AGENT_WAIT_S})); until {ping} || [ $SECONDS -ge $end ]; do sleep 5; done"
    read = f"{ping} && qm guest exec {vmid} --timeout 15 -- cat /etc/ssh/ssh_host_ed25519_key.pub"
    return f"qm status {vmid} | grep -q 'status: running' || exit 0; {wait}; {read} || true"


def pin_host_key(ctx: Context, stage: str, name: str) -> None:
    """Before reaching a VM over SSH: when the key pinned for it is not the
    one the VM itself holds, pin the VM's. A VM's host keys change without
    OpenTofu making it again when cloud-init sees a new instance (PVE derives
    the instance from the cloud-init settings, such as the router's NICs, and
    VMs from before the updates role kept their keys regenerate them). The
    key comes over the guest agent, through the host apply already trusts,
    so an impostor on the zone's network is still refused. Without an
    answering agent the pin stays, and ssh decides."""
    guest = ctx.bare["guests"][name]
    if guest["kind"] != "vm" or name in ctx.pinned:
        return
    ctx.pinned.add(name)
    address = vm_address(guest)
    known = ctx.home / "known_hosts"
    lines = known.read_text().splitlines() if known.exists() else []
    pinned = [line for line in lines if line.strip() and _names(line.split(" ", 1)[0], address)]
    if not pinned:
        return  # first sight: ssh pins it (StrictHostKeyChecking=accept-new)
    rc, out, _ = ctx.reach.host(agent_host_key(guest["vmid"]))
    try:
        found = HOST_KEY.search(json.loads(out).get("out-data", "")) if rc == 0 and out.strip() else None
    except (ValueError, AttributeError):
        found = None
    if found is None:
        return
    key = found.group(0)
    if any(" ".join(line.split()[1:3]) == key for line in pinned):
        return
    forget_host_key(ctx, address)
    with known.open("a") as out_file:
        out_file.write(f"{address} {key}\n")
    ctx.say(f"{stage}: {name} has a new SSH host key; pinned the one its guest agent reads inside the VM")


def _download_proxy(ctx: Context, name: str) -> dict:
    """downloads_proxy for a guest in a proxied zone: its pinned downloads
    (the downloads role's fetch) go through egress-proxy, which resolves the
    names; such a guest cannot resolve outside names itself. A role that
    passes its own downloads_proxy still wins."""
    proxy = proxy_url(ctx, name)
    return {"downloads_proxy": proxy} if proxy else {}


def ensure_on(ctx: Context, stage: str, guests: list[str]) -> None:
    """Before apply reaches guests that run on demand (power.py): the host's
    nb-power starts those that are off, together, if the host has the
    memory, stopping first what this run started before and nobody uses.
    It holds them until the run ends (end_hold). Guests that always run are
    passed over."""
    demand = power.on_demand(ctx.bare)
    wanted = [g for g in guests if g in demand and ctx.bare["guests"][g]["kind"] != "vm-pool"]
    if not wanted:
        return
    rc, out, err = ctx.reach.host(shlex.join([power.COMMAND, "ensure", *wanted]), timeout=900)
    if rc == 127:
        raise ApplyError(stage, f"{wanted[0]} runs on demand and the host has no nb-power yet; run setup in full once")
    # Also when it failed part way: what it did start is stopped again.
    ctx.held = True
    if rc != 0:
        raise ApplyError(stage, (err.strip().removeprefix("nb-power: ") or out.strip() or f"nb-power ensure: exit {rc}")[:500])
    for line in out.splitlines():
        what, _, name = line.partition(" ")
        if what == "started":
            ctx.say(f"{stage}: started {name}, which runs on demand, to set it up")
        elif what == "stopped":
            ctx.say(f"{stage}: stopped {name} again to make room; this run is done with it")


def end_hold(ctx: Context) -> None:
    """When the run ends, however it ends: nb-power stops again what was
    off before it and nobody took up meanwhile."""
    if not ctx.held:
        return
    ctx.held = False
    rc, out, err = ctx.reach.host(f"{power.COMMAND} setup end", timeout=900)
    if rc != 0:
        why = (err.strip() or out.strip() or f"exit {rc}")[:200]
        ctx.say(f"on demand: could not stop again what setup started ({why}); the host does so within {power.HOLD_S // 3600} h")
        return
    for line in out.splitlines():
        ctx.say(f"on demand: {line}")


def batches(ctx: Context, guests: list[str | None]) -> list[list[str | None]]:
    """A playbook for several machines runs on them at once, which needs
    them all up. Where the plan says not everything on demand fits at once,
    it runs on the machines that always run first, then on each on-demand
    workload's by itself."""
    found = ctx.bare.get("power") or {}
    if found.get("together", True):
        return [guests]
    demand = power.on_demand(ctx.bare)
    parts: dict[str | None, list[str | None]] = {}
    for name in guests:
        parts.setdefault(demand.get(name) if name else None, []).append(name)
    return [parts[key] for key in sorted(parts, key=lambda k: (k is not None, k or ""))]


def run_playbook(
    ctx: Context, stage: str, playbook: str, name: str | None, variables: dict | None = None, facts: str = "", always: bool = False
) -> None:
    """playbook on the host (name None) or a guest, unless it ran there
    before with the same inputs (applied.py). facts: what the run also
    depends on that is read from the machine. always: run it regardless,
    for a playbook whose work depends on time, as a certificate's does."""
    run_playbooks(ctx, stage, playbook, {name: {}}, variables or {}, facts={name: facts}, always=always)


def run_playbooks(
    ctx: Context,
    stage: str,
    playbook: str,
    targets: dict[str | None, dict],
    variables: dict,
    facts: dict[str | None, str] | None = None,
    always: bool = False,
    force: set[str | None] | None = None,
    fresh: dict[str | None, dict] | None = None,
) -> None:
    """One playbook run on several machines at once, each with its own
    host vars on top of its connection. A machine where the playbook's last
    good run had the same inputs is left out, unless it is in force.
    fresh: host vars made anew for each run (a one-time token), passed on
    but not part of the inputs."""
    parts = batches(ctx, list(targets))
    if len(parts) > 1:
        for part in parts:
            some = {name: targets[name] for name in part}
            new = {name: extra for name, extra in (fresh or {}).items() if name in some}
            run_playbooks(ctx, stage, playbook, some, variables, facts, always, force, new)
        return
    ensure_on(ctx, stage, [name for name in targets if name is not None])
    for name in targets:
        if name is not None:
            pin_host_key(ctx, stage, name)  # before any SSH to it, marks included
    hosts = {name or "pve": connection(ctx, name) | extra for name, extra in targets.items()}
    names = {name or "pve": name for name in targets}
    full = always or ctx.full or (ctx.home / FULL_NEXT).exists()
    key = ctx.secrets.ensure("applied_key", lambda: secrets.token_hex(32))
    digests = {h: applied.digest(key, playbook, hosts[h], variables, (facts or {}).get(names[h], "")) for h in hosts}
    for name, extra in (fresh or {}).items():
        hosts[name or "pve"] |= extra
    # A pool's golden image is baked from a fresh VM each time, then sealed.
    marked = {h for h in hosts if names[h] is None or ctx.bare["guests"][names[h]]["kind"] != "vm-pool"}
    forced = {name or "pve" for name in force or ()}
    same = set() if full else {h for h in sorted(marked - forced) if applied_mark(ctx, names[h], playbook) == digests[h]}
    for h in sorted(same):
        ctx.say(f"{stage}: {playbook} on {names[h] or 'PVE host'} is unchanged since its last run")
    run = {h: v for h, v in hosts.items() if h not in same}
    if not run:
        return
    label = ", ".join(names[h] or "PVE host" for h in run)
    for h in sorted(run.keys() & marked):
        # Gone while it runs: one that stops part way runs whole next time.
        set_mark(ctx, names[h], playbook, None)
    ctx.say(f"{stage}: {playbook} on {label}")
    rc = ctx.reach.playbook(playbook, run, variables)
    if rc != 0:
        raise ApplyError(stage, f"{playbook} failed on {label} (exit {rc})")
    for h in sorted(run.keys() & marked):
        set_mark(ctx, names[h], playbook, digests[h])


# Left by a run whose checks failed: the next runs every playbook in full,
# in case something on a guest changed under unchanged inputs.
FULL_NEXT = "apply-full-next"


def _on_machine(ctx: Context, name: str | None, command: str) -> tuple[int, str]:
    """A root shell command on the host, a container or a VM. The exit
    status is returned, not raised: a guest that does not answer yet just
    has no marks, and its playbook runs and reports."""
    if name is None:
        rc, out, _ = ctx.reach.host(command)
        return rc, out
    guest = ctx.bare["guests"][name]
    if guest["kind"] == "lxc":
        rc, out, _ = ctx.reach.host(shlex.join(["pct", "exec", str(guest["vmid"]), "--", "sh", "-c", command]))
        return rc, out
    conn = connection(ctx, name)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *shlex.split(conn["ansible_ssh_common_args"])]
    argv += [f"nullbill@{conn['ansible_host']}", shlex.join(["sudo", "sh", "-c", command])]
    return ctx.reach.local(argv, ctx.home, {}, capture=True)


def applied_mark(ctx: Context, name: str | None, playbook: str) -> str:
    """The digest of playbook's last good run on a machine, or ""."""
    rc, out = _on_machine(ctx, name, f"cat {shlex.quote(f'{applied.DIR}/{Path(playbook).name}')} 2>/dev/null || true")
    return out.strip() if rc == 0 else ""


def set_mark(ctx: Context, name: str | None, playbook: str, digest: str | None) -> None:
    """Leave digest for playbook on a machine, or take it away (None)."""
    path = shlex.quote(f"{applied.DIR}/{Path(playbook).name}")
    _on_machine(ctx, name, f"rm -f {path}" if digest is None else f"mkdir -p -m 0700 {applied.DIR} && echo {digest} > {path}")


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
    address = ctx.bare["zones"]["mgmt"]["host_ip"]
    api = {
        "host_baseline_api_name": f"pve.{ctx.bare['domain']}",
        "host_baseline_api_address": address,
        "host_baseline_ntfs": any(m["fstype"] == "ntfs" for m in legacy.mounts(ctx.bare)),
    }
    # The browser CA's certificate names the mgmt address only, so apply
    # and the owner reach Proxmox there: only once the LAN is closed.
    if lan.state(ctx.home) == lan.CLOSED and not ctx.open_lan:
        cert, key = browser_cert(ctx, "host", "pve", address)
        api |= {"host_baseline_api_cert": cert, "host_baseline_api_key": key}
    # What runs on demand, for the host's nb-power.
    api["host_baseline_power"] = power.render_policy(ctx.plan)
    run_playbook(ctx, "host", "host-baseline.yml", None, api)
    # Before the firewall: a close in this run ends the LAN path apply came by.
    run_playbook(ctx, "host", "console.yml", None, console_vars(ctx))
    prefix = "firewall/"
    files = {rel.removeprefix(prefix): text for rel, text in ctx.files.items() if rel.startswith(prefix)}
    open_variant = files.pop("cluster-lan-open.fw")
    host_fw = files.pop("host.fw")
    before = lan.state(ctx.home)
    state = lan_state(ctx, open_variant)
    if state == lan.OPEN:
        files["cluster.fw"] = open_variant
    # host.fw first: it admits the controller at the mgmt address a close moves it to.
    host_files(ctx, "host", PVE_HOST_FIREWALL, {"host.fw": host_fw})
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
        lan.write(ctx.home, lan.OPEN, lapsed=lan.read(ctx.home).get("deadline"))
        state = lan.OPEN
    host_out(ctx, "host", f"mkdir -p -m 0700 {lan.SAVED_OPEN.rsplit('/', 1)[0]}")
    host_files(ctx, "host", lan.SAVED_OPEN.rsplit("/", 1)[0], {lan.SAVED_OPEN.rsplit("/", 1)[1]: open_variant})
    host_files(ctx, "host", "/etc/systemd/system", {lan.BOOT_UNIT: lan.boot_unit()})
    # Acted on at boot only, so enabling is enough; enable also reloads a running systemd.
    host_out(ctx, "host", f"systemctl enable -q {lan.BOOT_UNIT}")
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
        lan.write(ctx.home, lan.OPEN, lapsed=lan.read(ctx.home).get("deadline"))
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


def browser_root(ctx: Context) -> Path:
    return browserca.root(ctx.home, ctx.bare["domain"], ctx.model["network"]["base"])


def browser_cert(ctx: Context, stage: str, name: str, address: str) -> tuple[str, str]:
    """name's certificate and key for browsers, which open it by address."""
    directory = ctx.home / browserca.DIR
    crt, key = directory / f"{name}.crt", directory / f"{name}.key"
    if browserca.issue(ctx.home, browser_root(ctx), crt, key, browserca.sans([fqdn(ctx, name)], [address])):
        ctx.say(f"{stage}: made {name}'s certificate for browsers")
    return crt.read_text(), key.read_text()


def page_certificate(ctx: Context) -> None:
    """The page's certificate from the browser CA, for the key it has, so the
    on-site screen's and the agents' pins of that key still hold. The page
    takes it up within a minute (web/server.py)."""
    web = ctx.home / "web"
    wanted = browserca.sans([fqdn(ctx, "nullbill")], [ctx.bare["guests"]["nullbill"]["ip"]])
    if browserca.issue(ctx.home, browser_root(ctx), web / "cert.pem", web / "key.pem", wanted):
        ctx.say("ca: gave the page a certificate from the browser CA")


def pve_ca(ctx: Context, stage: str) -> str:
    """What PVE API clients trust: PVE's CA, and the browser CA, whose
    certificate the host serves once the LAN is closed (stage_host)."""
    return host_out(ctx, stage, "cat /etc/pve/pve-root-ca.pem") + browser_root(ctx).read_text()


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


# Every ISO image on a storage that holds them, as `pvesm list` prints it
# (volume id first). The owner uploads the Windows installer in the web UI.
ISO_LIST = "for s in $(pvesm status --content iso --enabled 1 | awk 'NR > 1 {print $1}'); do pvesm list \"$s\" --content iso; done"


def windows_iso(ctx: Context, stage: str, say: bool = False) -> str | None:
    """The volume the Windows guests install from: TF_VAR_windows_iso when
    set, otherwise the one ISO in Proxmox whose file name holds "win"."""
    variables = json.loads(ctx.files["tofu/main.tf.json"])["variable"]
    if "windows_iso" not in variables:
        return None
    if os.environ.get("TF_VAR_windows_iso"):
        return os.environ["TF_VAR_windows_iso"]
    volumes = [line.split()[0] for line in host_out(ctx, stage, ISO_LIST).splitlines() if ":iso/" in line]
    found = sorted({v for v in volumes if "win" in v.split(":iso/", 1)[1].lower()})
    if not found:
        raise ApplyError(
            stage,
            "Windows guests are planned but Proxmox holds no Windows ISO; upload one in the web UI "
            "(storage local, ISO Images, Upload) or turn off the Windows workloads",
        )
    if len(found) > 1:
        raise ApplyError(stage, f"more than one Windows ISO in Proxmox ({', '.join(found)}); remove all but one")
    if say:
        ctx.say(f"{stage}: Windows guests install from {found[0]}")
    return found[0]


def tofu_env(ctx: Context, stage: str = "tofu") -> dict[str, str]:
    """What OpenTofu needs, all through the environment: nothing is written
    in build/ or the state directory in plaintext."""
    _, host = split_target(ctx.target)
    variables = json.loads(ctx.files["tofu/main.tf.json"])["variable"]
    install = json.loads((ctx.home / "install.json").read_text()) if (ctx.home / "install.json").exists() else {}
    iso = windows_iso(ctx, stage)
    keys = [line for line in host_out(ctx, stage, "cat /root/.ssh/authorized_keys").splitlines() if line.startswith("ssh-")]
    ca = ctx.home / "pve-root-ca.pem"
    ca.write_text(pve_ca(ctx, stage))
    key = Path.home() / ".ssh" / "id_ed25519"
    env = {
        "PROXMOX_VE_API_TOKEN": api_token(ctx),
        # The provider reaches the host over SSH for disk imports.
        "PROXMOX_VE_SSH_USERNAME": "root",
        "PROXMOX_VE_SSH_PRIVATE_KEY": key.read_text() if key.exists() else "",
        # The provider reads known_hosts under $HOME and fails without it;
        # systemd gives the page's service none.
        "HOME": str(Path.home()),
        "TF_VAR_pve_ssh_address": host,
        # PVE's certificate is signed by its own CA, or the browser CA once the LAN is closed.
        "SSL_CERT_FILE": str(ca),
        "TF_VAR_pve_endpoint": f"https://{host}:8006/",
        "TF_VAR_node_name": host_out(ctx, stage, "hostname").strip(),
        "TF_VAR_lan_bridge": install.get("bridge", "vmbr0"),
        "TF_VAR_ssh_authorized_keys": json.dumps(keys),
        "TF_VAR_state_passphrase": ctx.secrets.ensure("tofu_state_passphrase"),
        "TF_IN_AUTOMATION": "1",
    }
    if iso:
        env["TF_VAR_windows_iso"] = iso
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
    # The full apply, of a saved plan that deletes no guest the owner did not
    # confirm: an unticked box under What to run must not take a VM's disk with it.
    planned = work / PLAN_FILE
    try:
        ctx.say(f"{stage}: tofu plan")
        argv = ["tofu", "plan", "-input=false", "-no-color", f"-out={PLAN_FILE}", *(f"-replace={a}" for a in replace)]
        rc, _ = ctx.reach.local(argv, work, env)
        if rc != 0:
            raise ApplyError(stage, f"tofu plan failed (exit {rc})")
        rc, shown = ctx.reach.local(["tofu", "show", "-json", PLAN_FILE], work, env, capture=True)
        if rc != 0:
            raise ApplyError(stage, f"tofu show failed (exit {rc})")
        check_removals(ctx, stage, guest_removals(json.loads(shown or "{}"), replace))
        ctx.say(f"{stage}: tofu apply")
        rc, _ = ctx.reach.local(["tofu", "apply", "-input=false", "-no-color", PLAN_FILE], work, env)
        if rc != 0:
            raise ApplyError(stage, f"tofu apply failed (exit {rc})")
        (ctx.home / GUESTS).write_text(json.dumps({"guests": sorted(ctx.bare["guests"])}, indent=2) + "\n")
    finally:
        planned.unlink(missing_ok=True)


def guest_removals(shown: dict, replace: tuple[str, ...] = ()) -> list[str]:
    """Guests a saved plan (tofu show -json) deletes, made again or not,
    besides those apply replaces on purpose (a CI pool's golden image)."""
    return sorted(
        {
            change["name"]
            for change in shown.get("resource_changes") or []
            if change.get("type") in GUEST_RESOURCES
            and "delete" in change.get("change", {}).get("actions", [])
            and change.get("address") not in replace
        }
    )


def check_removals(ctx: Context, stage: str, removing: list[str]) -> None:
    """Stop before OpenTofu deletes a guest, unless the owner confirmed
    deleting exactly those (the page's Delete them, apply --remove-guests).
    The names wait in REMOVALS for that confirmation."""
    path = ctx.home / REMOVALS
    confirmed = set(json.loads(path.read_text()).get("guests", [])) if path.exists() and ctx.remove_guests else set()
    if not removing:
        path.unlink(missing_ok=True)
        return
    disks = "its disk" if len(removing) == 1 else "their disks"
    if set(removing) <= confirmed:
        ctx.say(f"{stage}: deleting {_and(removing)} with everything on {disks}, as you confirmed")
        path.unlink()
        return
    planned = ctx.bare["guests"]
    gone = [n for n in removing if n not in planned]
    remade = [n for n in removing if n in planned]
    why = []
    if gone:
        why.append(f"{_and(gone)} {'is' if len(gone) == 1 else 'are'} no longer in the plan (a box under What to run is not ticked)")
    if remade:
        why.append(f"OpenTofu would have to make {_and(remade)} again")
    # gone: the page stops showing those once a saved plan has them again.
    path.write_text(json.dumps({"guests": removing, "gone": gone}, indent=2) + "\n")
    raise ApplyError(
        stage,
        f"setup would delete {_and(removing)} with everything on {disks}, "
        f"because {' and '.join(why)}; nothing was deleted. "
        "To keep it, tick it again under Setup, What to run and press Save and plan; "
        "to delete it, press Delete and run setup on the Setup page, under Build",
    )


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def backup_disk(ctx: Context) -> None:
    """The disk the plan pins for PBS, as the LVM storage the pbs VM's
    datastore disk is made on; before OpenTofu makes that VM."""
    if not backups.enabled(ctx.plan):
        return
    disk_id, _ = backups.disk(ctx.plan)
    keep_files(ctx, disk_id)
    wipe = "1" if ctx.model.get("disks", {}).get(disk_id, {}).get("wipe") else "0"
    out = host_out(ctx, "tofu", shlex.join(["/usr/local/sbin/nb-backup-disk", disk_id, wipe])).strip()
    converted(ctx, disk_id)
    if out == "made":
        ctx.say(f"tofu: {disk_id} is now the {backups.DISK_STORAGE} storage for PBS")


def storage_disks(ctx: Context) -> None:
    """Each disk the plan pins as storage, as an LVM-thin PVE storage of its
    own (nb-storage-disk); before OpenTofu, like the backup disk."""
    for disk_id, disk in sorted(ctx.bare["disks"].items()):
        if disk.get("use") != "storage":
            continue
        keep_files(ctx, disk_id)
        wipe = "1" if ctx.model.get("disks", {}).get(disk_id, {}).get("wipe") else "0"
        out = host_out(ctx, "tofu", shlex.join([STORAGE_DISK, disk_id, wipe, disk["storage"]])).strip()
        converted(ctx, disk_id)
        if out == "made":
            ctx.say(f"tofu: {disk_id} is now the LVM-thin storage {disk['storage']}")


def keep_files(ctx: Context, disk_id: str) -> None:
    """Before a disk is wiped: the probe may have missed a kept disk's mount
    (the disk was out); what apply had mounted before this run keeps its data
    from the wipe, unless the plan converts it with a verified copy, or the
    owner discards it."""
    kept_before = kept_disks(ctx) if ctx.kept_before is None else ctx.kept_before
    pin = ctx.model.get("disks", {}).get(disk_id, {})
    if disk_id in kept_before and not pin.get("discard_data"):
        names = [c["name"] for c in (ctx.bare["disks"][disk_id].get("converts") or {}).get("copies", [])]
        copied = {line.split()[0] for line in host_out(ctx, "tofu", f"{COPY} list").splitlines() if line.strip()}
        if not names or not set(names) <= copied:
            raise ApplyError(
                "tofu",
                f"{disk_id} was kept as legacy and has no verified copy; it is not erased. Plug it in and probe again "
                "to copy it (evacuate), or set discard_data: true if you have its files elsewhere",
            )


# Disks kept from a previous OS (legacy.py): the host mounts them before
# OpenTofu, and the files guest gets each as a bind mount point after.
LEGACY = "/usr/local/sbin/nb-legacy-disk"
COPY = "/usr/local/sbin/nb-legacy-copy"
STORAGE_DISK = "/usr/local/sbin/nb-storage-disk"
# A volume no guest config names any more (a moved copy's old place).
PVESM_FREE = "pvesm free"
# A kept disk's copy runs as a transient unit on the host, so a dropped SSH
# connection does not stop it; apply waits for it, at the pace of a slow
# USB 2 disk, then leaves it running and says so.
COPY_POLL_S = 60
COPY_MIB_PER_S = 25
DISKS_FILE = "disks.json"
FILES = "files"


def read_disks(ctx: Context) -> dict:
    path = ctx.home / DISKS_FILE
    return json.loads(path.read_text()) if path.exists() else {"mounts": []}


def kept_disks(ctx: Context) -> set[str]:
    """Disks whose files apply keeps from a wipe ($NULLBILL_HOME/disks.json):
    each disk it last mounted, and each it ever mounted that no conversion
    has taken over since. A disk stays protected when a run stops keeping
    it, so a refused wipe still refuses when the run is repeated."""
    data = read_disks(ctx)
    return {m["disk"] for m in data["mounts"]} | set(data.get("unconverted", []))


def write_disks(ctx: Context, data: dict) -> None:
    path = ctx.home / DISKS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def converted(ctx: Context, disk_id: str) -> None:
    """A conversion keep_files allowed is done: the disk holds no kept files."""
    data = read_disks(ctx)
    if disk_id in data.get("unconverted", []):
        data["unconverted"] = [d for d in data["unconverted"] if d != disk_id]
        write_disks(ctx, data)
    if ctx.kept_before is not None:
        ctx.kept_before.discard(disk_id)


def files_vmid(ctx: Context) -> int:
    return ctx.model["network"]["vmid_base"] + ctx.model["services"][FILES]["id"]


def files_running(ctx: Context) -> bool:
    rc, out, _ = ctx.reach.host(f"pct status {files_vmid(ctx)}")
    return rc == 0 and "running" in out


def stop_files(ctx: Context) -> None:
    """Before a kept mount changes under it: a container keeps its own copy of
    the mount, so it must not run while the host unmounts."""
    if not ctx.files_stopped and files_mount_points(ctx) is not None and files_running(ctx):
        shut_down(ctx, "tofu", FILES, files_vmid(ctx))
        ctx.say("tofu: stopped the files guest while its disks change")
    ctx.files_stopped = True


def copy_disks(ctx: Context) -> None:
    """Before a kept disk the plan converts is unmounted or wiped: copy each
    of its kept partitions to a volume of the files guest (evacuate), and
    wait until nb-legacy-copy has checked and recorded each copy."""
    todo = [(disk_id, d["converts"]) for disk_id, d in sorted(ctx.bare["disks"].items()) if (d.get("converts") or {}).get("evacuate")]
    if not todo:
        return
    copied = {line.split()[0] for line in host_out(ctx, "tofu", f"{COPY} list").splitlines() if line.strip()}
    vmid = str(files_vmid(ctx))
    for disk_id, converts in todo:
        for c in converts["copies"]:
            if c["name"] in copied:
                continue
            # Nothing may change the files while they are copied.
            stop_files(ctx)
            unit = f"nb-legacy-copy-{c['name']}"
            argv = [COPY, "copy", c["name"], converts["evacuate"], vmid, str(c["size_gib"]), disk_id]
            run_unit(ctx, unit, argv)
            where = f"{legacy.LEGACY_ROOT}/{c['name']} ({c['used_gib']} GiB)"
            ctx.say(f"tofu: copying {where} to {converts['evacuate']} before {disk_id} is converted")
            wait_unit(ctx, unit, c["used_gib"])
            record = copy_records(ctx).get(c["name"])
            if record is None:
                why = host_out(ctx, "tofu", f"journalctl -q -u {unit} -n 5 -o cat || true").strip()
                raise ApplyError("tofu", f"copying {c['name']} failed; the kept disk is as it was and is not converted: {why}")
            mib = int(record["bytes"]) // 2**20
            ctx.say(f"tofu: {c['name']}: {record['files']} files, {mib} MiB copied to {record['volume']} and checked")


def copy_records(ctx: Context) -> dict[str, dict]:
    """nb-legacy-copy's records: name -> volume, size_gib, files, bytes, disk."""
    out = {}
    for line in host_out(ctx, "tofu", f"{COPY} list").splitlines():
        if line.strip():
            name, volume, size_gib, files, size, disk = line.split()
            out[name] = {"volume": volume, "size_gib": int(size_gib), "files": files, "bytes": size, "disk": disk}
    return out


def run_unit(ctx: Context, unit: str, argv: list[str]) -> None:
    """argv as a transient unit on the host, unless it runs already."""
    if host_out(ctx, "tofu", f"systemctl is-active {unit} || true").strip() not in ("active", "activating"):
        host_out(ctx, "tofu", shlex.join(["systemd-run", f"--unit={unit}", "--collect", "--quiet", "--no-block", *argv]))


def wait_unit(ctx: Context, unit: str, used_gib: int) -> None:
    """Until a copy's unit ends, saying its last line every minute."""
    deadline = time.monotonic() + 1800 + used_gib * 1024 // COPY_MIB_PER_S
    while host_out(ctx, "tofu", f"systemctl is-active {unit} || true").strip() in ("active", "activating"):
        if time.monotonic() > deadline:
            raise ApplyError("tofu", f"{unit} is still copying on the host; run apply again once it is done (journalctl -u {unit})")
        sleep(COPY_POLL_S)
        last = host_out(ctx, "tofu", f"journalctl -q -u {unit} -n 1 -o cat || true").strip()
        if last:
            ctx.say(f"tofu: {last}")


def move_copies(ctx: Context) -> None:
    """Copies of a disk that is storage now move onto it (phase 3): each is
    copied again and checked, then the files guest takes the new volume and
    the old one is freed. A copy the owner moved in PVE stays where it is."""
    targets = {disk_id: d["storage"] for disk_id, d in ctx.bare["disks"].items() if d.get("use") == "storage"}
    if not targets or FILES not in ctx.bare["guests"]:
        return
    found = files_mount_points(ctx)
    at = volume_paths(found[2]) if found else {}
    olds = moved_volumes(ctx)
    vmid = str(files_vmid(ctx))
    for name, record in sorted(copy_records(ctx).items()):
        target = targets.get(record["disk"])
        current = at.get(f"{legacy.SHARE_ROOT}/{name}", record["volume"])
        if target is None or record["volume"].split(":", 1)[0] == target:
            continue  # not for a storage disk, or moved already (attach_shares swaps it)
        if current != record["volume"] and current not in olds:
            continue  # the owner moved it in PVE
        stop_files(ctx)
        unit = f"nb-legacy-copy-{name}"
        run_unit(ctx, unit, [COPY, "move", name, current, target, vmid])
        ctx.say(f"tofu: moving the copy {name} from {current} to {target}")
        wait_unit(ctx, unit, int(record["bytes"]) // 2**30)
        moved = copy_records(ctx).get(name, {}).get("volume", "")
        if not moved.startswith(f"{target}:"):
            why = host_out(ctx, "tofu", f"journalctl -q -u {unit} -n 5 -o cat || true").strip()
            raise ApplyError("tofu", f"moving the copy {name} to {target} failed; it stays on {current}: {why}")
        ctx.say(f"tofu: {name}: moved to {moved} and checked")


def volume_paths(volumes: "MountPoints") -> dict[str, str]:
    """The files guest's volume mount points: path in the guest -> volume."""
    return {_mp_fields(value)[1]: volume for volume, (_, value) in volumes.items()}


def moved_volumes(ctx: Context) -> set[str]:
    return {line.split()[1] for line in host_out(ctx, "tofu", f"{COPY} moved").splitlines() if line.strip()}


def free_moved(ctx: Context) -> None:
    """Old volumes of moved copies, once the files guest no longer uses them:
    PVE keeps a replaced mount point's volume as unusedN, deleted here."""
    moved = [line.split() for line in host_out(ctx, "tofu", f"{COPY} moved").splitlines() if line.strip()]
    if not moved:
        return
    vmid = files_vmid(ctx)
    config = host_out(ctx, "tofu", f"pct config {vmid}")
    for _, old in moved:
        holders = [line.split(":", 1)[0] for line in config.splitlines() if line.split(": ", 1)[-1].split(",", 1)[0] == old]
        if any(not h.startswith("unused") for h in holders):
            continue  # still mounted; attach_shares swaps it first
        stop_files(ctx)
        if holders:
            host_out(ctx, "tofu", f"pct set {vmid} --delete {','.join(holders)}")
        else:
            host_out(ctx, "tofu", f"{PVESM_FREE} {shlex.quote(old)}")
        host_out(ctx, "tofu", shlex.join([COPY, "forget", old]))
        ctx.say(f"tofu: freed {old}, the old place of a moved copy")


def check_writable(ctx: Context, m: dict) -> None:
    """Before anything stops for a partition that is to become read-write:
    read, on the host, how Windows left it (probe.NTFS_CHECK), and stop the
    run here, with the share and Jellyfin still up, when it cannot be."""
    dev = f"/dev/disk/by-id/{m['disk']}-part{m['n']}"
    _, out, _ = ctx.reach.host(f"d=$(readlink -f {shlex.quote(dev)}); {NTFS_CHECK}")
    state, journal = ntfs_state(out)
    confirmed = [m["discard_journal"]] if "discard_journal" in m else []
    problem = legacy.rw_problem(f"disks.{m['disk']}", m["n"], state, journal, confirmed)
    if problem:
        raise ApplyError("tofu", f"{problem} Nothing was changed.")


def resume_guests(ctx: Context) -> None:
    """After a mount was refused: start the guests this run stopped for it,
    so a refused change does not leave the share and Jellyfin down."""
    for name, stopped, exists, running, vmid in (
        (FILES, ctx.files_stopped, files_mount_points, files_running, files_vmid),
        (media.JELLYFIN, ctx.media_stopped, media_mount_points, media_running, media_vmid),
    ):
        # One that runs on demand and was off before the run stays off.
        was_off = name in power.on_demand(ctx.bare) and name not in ctx.shut_down
        if stopped and not was_off and exists(ctx) is not None and not running(ctx):
            rc, _, _ = ctx.reach.host(f"pct start {vmid(ctx)}")
            ctx.say(f"tofu: {'started' if rc == 0 else 'could not start'} guest {vmid(ctx)} again after the refused mount")


def legacy_disks(ctx: Context) -> None:
    """Mount each kept partition and unmount those the plan dropped. The
    host script refuses anything unsafe (nb-legacy-disk)."""
    want = {m["name"]: m for m in legacy.mounts(ctx.bare)}
    listed = {}
    for line in host_out(ctx, "tofu", f"{LEGACY} list 2>/dev/null || true").splitlines():
        name, uuid, mode, fstype, state = line.split()
        listed[name] = (uuid, mode, fstype, state)
    gone = sorted(set(listed) - set(want))
    changed = [n for n in want if n in listed and listed[n] != (want[n]["uuid"], want[n]["mode"], want[n]["fstype"], "mounted")]
    for name, m in sorted(want.items()):
        if m["mode"] == "rw" and m["fstype"] == "ntfs" and (name not in listed or name in changed):
            check_writable(ctx, m)
    detach_stale(ctx)
    detach_media(ctx)
    if gone or changed:
        stop_files(ctx)
        stop_media(ctx)
    for name in gone:
        host_out(ctx, "tofu", shlex.join([LEGACY, "unmount", name]))
        ctx.say(f"tofu: stopped keeping {legacy.LEGACY_ROOT}/{name}; its disk is left as it is")
    host_id = str(legacy.HOST_ID_OFFSET + legacy.SHARE_UID)
    for name, m in sorted(want.items()):
        argv = [LEGACY, "mount", m["disk"], str(m["n"]), m["uuid"], m["fstype"], m["mode"], name, host_id, host_id]
        # The one journal the owner let apply discard, if Windows left it.
        argv += [m["discard_journal"]] if "discard_journal" in m else []
        try:
            out = host_out(ctx, "tofu", shlex.join(argv))
        except ApplyError:
            resume_guests(ctx)
            raise
        if out.strip().endswith("mounted"):
            how = "read-write" if m["mode"] == "rw" else "read-only"
            cleared = ", after discarding the journal Windows left" if "discarded" in out else ""
            ctx.say(f"tofu: mounted partition {m['n']} of {m['disk']} {how} at {m['mount']}{cleared}")
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
    mounted = {m["disk"] for m in out}
    write_disks(ctx, {"mounts": out, "unconverted": sorted(kept_disks(ctx) - mounted)})


def media_vmid(ctx: Context) -> int:
    return ctx.model["network"]["vmid_base"] + ctx.model["services"][media.JELLYFIN]["id"]


def media_mount_points(ctx: Context) -> tuple["MountPoints", set[int]] | None:
    """The Jellyfin guest's kept-disk bind mount points (host path -> index,
    value) and every mpN index in use; None while no Jellyfin guest of
    NullBill's exists."""
    rc, config, _ = ctx.reach.host(f"pct config {media_vmid(ctx)}")
    if rc != 0 or f"hostname: {media.JELLYFIN}" not in config.splitlines():
        return None
    current, used = {}, set()
    for line in config.splitlines():
        key, _, value = line.partition(": ")
        if key.startswith("mp") and key[2:].isdigit():
            used.add(int(key[2:]))
            if value.startswith(legacy.LEGACY_ROOT + "/"):
                current[value.split(",", 1)[0]] = (int(key[2:]), value)
    return current, used


def media_running(ctx: Context) -> bool:
    rc, out, _ = ctx.reach.host(f"pct status {media_vmid(ctx)}")
    return rc == 0 and "running" in out


def stop_media(ctx: Context) -> None:
    """Like stop_files, for Jellyfin, which bind-mounts the same kept disks."""
    if ctx.media_stopped:
        return
    found = media_mount_points(ctx)
    if found is None or not found[0]:
        return
    if media_running(ctx):
        shut_down(ctx, "tofu", media.JELLYFIN, media_vmid(ctx))
        ctx.say("tofu: stopped the Jellyfin guest while its disks change")
    ctx.media_stopped = True


def detach_media(ctx: Context) -> None:
    """Before the host unmounts anything or OpenTofu removes the guest: take
    each kept disk the plan no longer shares off the Jellyfin guest."""
    found = media_mount_points(ctx)
    if found is None:
        return
    current, _ = found
    keep = {m["mount"] for m in legacy.shares(ctx.bare)} if media.enabled(ctx.bare) else set()
    stale = [f"mp{current[path][0]}" for path in sorted(set(current) - keep)]
    if stale:
        stop_media(ctx)
        host_out(ctx, "tofu", f"pct set {media_vmid(ctx)} --delete {','.join(stale)}")
        ctx.say(f"tofu: took {len(stale)} kept disk(s) off the Jellyfin guest")


def attach_media(ctx: Context) -> None:
    """Each shared kept disk as a read-only bind mount point of the Jellyfin
    guest, set as root like the files guest's, then a start so it sees them."""
    if not media.enabled(ctx.bare):
        return
    vmid = media_vmid(ctx)
    found = media_mount_points(ctx)
    if found is None:
        raise ApplyError("tofu", f"container {vmid} is not NullBill's Jellyfin guest")
    current, used = found
    sets = []
    for m in legacy.shares(ctx.bare):
        if m["mount"] in current:
            index, value = current[m["mount"]]
        else:
            index = min(i for i in range(256) if i not in used)
            used.add(index)
            value = ""
        line = media.mount_point(m)
        if not value or _mp_fields(value) != _mp_fields(line):
            sets.append(f"--mp{index} {shlex.quote(line)}")
    if sets:
        if not ctx.media_stopped and media_running(ctx):
            shut_down(ctx, "tofu", media.JELLYFIN, vmid)
        ctx.media_stopped = True
        host_out(ctx, "tofu", f"pct set {vmid} {' '.join(sets)}")
        ctx.say(f"tofu: gave the Jellyfin guest {len(sets)} kept disk(s), read-only")
    # A disk mounted on the host after the guest started shows up in it only
    # once the guest starts again.
    if not ctx.media_stopped and not (ctx.legacy_mounted and legacy.shares(ctx.bare)):
        return
    see_changes(ctx, "tofu", media.JELLYFIN, 60, wake=False)
    ctx.media_stopped = False


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


MountPoints = dict[str, tuple[int, str]]


def files_mount_points(ctx: Context) -> tuple[MountPoints, set[int], MountPoints] | None:
    """The files guest's kept-disk bind mount points (host path -> index,
    value), every mpN index in use, and its volume mount points (volume ->
    index, value); None while no files guest of NullBill's exists (a
    container of the owner's own may hold the VMID)."""
    vmid = files_vmid(ctx)
    rc, config, _ = ctx.reach.host(f"pct config {vmid}")
    if rc != 0 or "hostname: files" not in config.splitlines():
        return None
    current, volumes = {}, {}
    used = set()
    for line in config.splitlines():
        key, _, value = line.partition(": ")
        if key.startswith("mp") and key[2:].isdigit():
            used.add(int(key[2:]))
            head = value.split(",", 1)[0]
            if value.startswith(legacy.LEGACY_ROOT + "/"):
                current[head] = (int(key[2:]), value)
            elif not head.startswith("/"):
                volumes[head] = (int(key[2:]), value)
    return current, used, volumes


def detach_stale(ctx: Context) -> None:
    """Before the host unmounts anything or OpenTofu removes the guest: take
    off each mount point the plan no longer shares."""
    found = files_mount_points(ctx)
    if found is None:
        return
    current, _, _ = found
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
    current, used, volumes = found
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
    # Copies of converted kept disks: volumes the files guest owns, backed
    # up with it. apply never takes one off; that is the owner's call. Found
    # by their path in the guest: a copy the owner moved in PVE is a new
    # volume at the same path, and stays. A copy apply moved (move_copies)
    # takes its new volume here.
    at = volume_paths(volumes)
    olds = moved_volumes(ctx) if copy_records(ctx) else set()
    for name, record in sorted(copy_records(ctx).items()):
        path = f"{legacy.SHARE_ROOT}/{name}"
        current = at.get(path)
        volume = record["volume"] if current is None or current in olds else current
        line = f"{volume},mp={path},backup=1"
        index, value = volumes.get(current, (None, "")) if current else (None, "")
        if index is None:
            index = min(i for i in range(256) if i not in used)
        used.add(index)
        if not value or _mp_fields(value) != _mp_fields(line) or "backup=1" not in value.split(","):
            sets.append(f"--mp{index} {shlex.quote(line)}")
    if sets:
        stop_files(ctx)
        host_out(ctx, "tofu", f"pct set {vmid} {' '.join(sets)}")
        ctx.say(f"tofu: gave the files guest {len(sets)} mount point(s) for kept disks and their copies")
    free_moved(ctx)
    if not ctx.files_stopped and not ctx.legacy_mounted:
        return
    # A disk mounted on the host after the guest started shows up in it
    # only once the guest starts again.
    see_changes(ctx, "tofu", FILES, 60, wake=False)
    ctx.files_stopped = False


def stage_tofu(ctx: Context) -> None:
    work = ctx.home / "tofu"
    work.mkdir(mode=0o700, exist_ok=True)
    (work / "main.tf.json").write_text(ctx.files["tofu/main.tf.json"])
    windows_iso(ctx, "tofu", say=True)
    ctx.legacy_mounted = ctx.files_stopped = False
    ctx.kept_before = kept_disks(ctx)
    copy_disks(ctx)
    legacy_disks(ctx)
    backup_disk(ctx)
    storage_disks(ctx)
    run_tofu(ctx, "tofu", init=True, downloads=True)
    forget_remade_vms(ctx)
    move_copies(ctx)
    attach_shares(ctx)
    attach_media(ctx)
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


def update_guests(ctx: Context) -> dict[str | None, dict]:
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
        guests[name] = {
            "updates_apt_proxy": proxy if proxied else "",
            "updates_auto_reboot": name not in owner_reboots,
            "updates_reboot_time": ctx.bare["updates"]["reboot_at"],
            "updates_guest_agent": guest["kind"] == "vm",
            "updates_keep_host_keys": guest["kind"] == "vm",
            "updates_busy_ports": busy_ports(ctx, name),
        }
    return guests


def busy_ports(ctx: Context, name: str) -> list[int]:
    """The service's own TCP ports, where a connection from outside means
    someone is using it (the updates role's nb-busy, until the service's
    role says what use is). Not node_exporter's, which monitoring scrapes
    all day, nor SSH, whose sessions nb-busy counts by themselves."""
    ports = {p["port"] for p in ctx.model["services"][name].get("ports", []) if p["proto"] == "tcp"}
    return sorted(ports - {22, NODE_EXPORTER["port"]})


def stage_updates(ctx: Context) -> None:
    run_playbooks(ctx, "updates", "updates.yml", update_guests(ctx), {})


def guest_out(ctx: Context, stage: str, name: str, argv: list[str], stdin: str | None = None) -> str:
    """Run a command in a container through the host's pct and return its output."""
    ensure_on(ctx, stage, [name])
    vmid = ctx.bare["guests"][name]["vmid"]
    rc, out, err = ctx.reach.host(shlex.join(["pct", "exec", str(vmid), "--", *argv]), stdin=stdin)
    if rc != 0:
        raise ApplyError(stage, f"{name}: {argv[0]} {argv[1] if len(argv) > 1 else ''} failed: {(err.strip() or f'exit {rc}')[:200]}")
    return out


def shut_down(ctx: Context, stage: str, name: str, vmid: int, timeout: int = 60) -> None:
    """A clean shutdown of a container this run starts again (see_changes).
    vmid: the plan may no longer hold the guest."""
    host_out(ctx, stage, f"pct shutdown {vmid} --timeout {timeout} --forceStop 1")
    ctx.shut_down.add(name)


def start(ctx: Context, stage: str, name: str) -> None:
    """Start a container that is off: through nb-power when it runs on
    demand, so its memory is found first and it is stopped again when the
    run ends."""
    if name in power.on_demand(ctx.bare) and name not in ctx.shut_down:
        ensure_on(ctx, stage, [name])
    else:
        host_out(ctx, stage, f"pct start {ctx.bare['guests'][name]['vmid']}")
    ctx.shut_down.discard(name)


def see_changes(ctx: Context, stage: str, name: str, timeout: int, wake: bool = True) -> None:
    """Have a container see what was set for it on the host (a device, a
    mount point): restarted when it runs, started when it does not. wake
    False: one that runs on demand and was off before this run stays off;
    it sees the change when it is next started."""
    vmid = ctx.bare["guests"][name]["vmid"]
    if "status: running" in host_out(ctx, stage, f"pct status {vmid}"):
        host_out(ctx, stage, f"pct reboot {vmid} --timeout {timeout}")
    elif wake or name not in power.on_demand(ctx.bare) or name in ctx.shut_down:
        start(ctx, stage, name)


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
    llm.GATEWAY: ("www-data", "systemctl reload nginx"),
}


def stage_ca(ctx: Context) -> None:
    """The page's certificate for browsers, step-ca, then a certificate for each planned TLS service."""
    page_certificate(ctx)
    domain = ctx.bare["domain"]
    run_playbook(ctx, "ca", "step-ca.yml", "step-ca", {"step_ca_domain": domain})
    root = ca_root(ctx, "ca")
    targets: dict[str | None, dict] = {
        name: {"tls_cert_name": f"{name}.{domain}", "tls_cert_group": group, "tls_cert_reload": reload}
        for name, (group, reload) in TLS.items()
        if name in ctx.bare["guests"]
    }
    # A guest without a certificate good for another hour (they last 24 h,
    # so one runs out while the server is off) gets a one-time token for a
    # new one, and the role runs there whatever its inputs; all at once.
    lapsed = {name for name in targets if name is not None and not cert_valid(ctx, name)}
    tokens: dict[str | None, dict] = {name: {"tls_cert_token": ca_token(ctx, "ca", name)} for name in sorted(lapsed)}
    variables = {"tls_cert_ca_url": f"https://step-ca.{domain}", "tls_cert_ca_root": root}
    run_playbooks(ctx, "ca", "tls-cert.yml", targets, variables, force=lapsed, fresh=tokens)


def cert_valid(ctx: Context, name: str) -> bool:
    """Whether name's tls_cert certificate is good for another hour, as the role asks."""
    ensure_on(ctx, "ca", [name])
    path = shlex.quote(f"/etc/nullbill/tls/{name}.{ctx.bare['domain']}.crt")
    rc, out = _on_machine(ctx, name, f"openssl x509 -checkend 3600 -noout -in {path} >/dev/null 2>&1 && echo valid || true")
    return rc == 0 and out.strip() == "valid"


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


def tls_targets(ctx: Context) -> list[str]:
    """Endpoints that speak TLS from the first byte, so blackbox can see their
    certificates renew; PostgreSQL negotiates TLS inside its own protocol."""
    ports = {"step-ca": 443, "monitoring": 3100, "forgejo": 443, "runner-dispatcher": 8443, llm.GATEWAY: llm.GATEWAY_PORT}
    return [f"{fqdn(ctx, n)}:{p}" for n, p in ports.items() if planned(ctx, n)]


def monitoring_vars(ctx: Context) -> dict:
    guests = ctx.bare["guests"]
    topic = ctx.secrets.data.get("ntfy_topic")
    return {
        "monitoring_node_targets": [f"{fqdn(ctx, n)}:9100" for n, g in guests.items() if node_exporter_guest(g)],
        "monitoring_host_target": f"pve.{ctx.bare['domain']}:9100",
        "monitoring_tls_targets": tls_targets(ctx),
        "monitoring_probe_ca": both_roots(ctx, "monitoring"),
        "monitoring_tls_name": fqdn(ctx, "monitoring"),
        "monitoring_shipper_ca": shipper_ca(ctx, "monitoring"),
        "monitoring_ntfy_url": NTFY + topic if topic else "",
        "monitoring_healthchecks_url": ctx.secrets.data.get("healthchecks_url", ""),
        "monitoring_https_proxy": proxy_url(ctx, "monitoring"),
    }


def stage_monitoring(ctx: Context) -> None:
    if not planned(ctx, "monitoring"):
        return
    cert, key = browser_cert(ctx, "monitoring", "monitoring", ctx.bare["guests"]["monitoring"]["ip"])
    variables = monitoring_vars(ctx) | {"monitoring_browser_cert": cert, "monitoring_browser_key": key}
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
    cert, key = browser_cert(ctx, "forgejo", "forgejo", ctx.bare["guests"]["forgejo"]["ip"])
    variables |= {"forgejo_browser_cert": cert, "forgejo_browser_key": key, "forgejo_browser_root": browser_root(ctx).read_text()}
    run_playbook(ctx, "forgejo", "forgejo.yml", "forgejo", variables)
    # Changed at first login; kept so the owner has it for that login.
    password = guest_out(ctx, "forgejo", "forgejo", ["cat", "/etc/forgejo/admin-password"]).strip()
    ctx.secrets.set("forgejo_admin_password", password)
    forgejo_owner(ctx)


NB_FORGEJO_OWNER = "/usr/local/sbin/nb-forgejo-owner"


def forgejo_owner(ctx: Context) -> None:
    """The owner's own Forgejo user with the SSH keys from the page. Its name
    comes from the owner's Tailscale login once and is kept in the secrets."""
    person = owner.person(ctx.model)
    if not person:
        return
    user = ctx.secrets.ensure("forgejo_owner_user", lambda: owner.forgejo_user(person["login"]))
    keys = owner.keys(ctx.model)
    argv = [NB_FORGEJO_OWNER, user, f"{user}@{ctx.bare['domain']}"]
    lines = guest_out(ctx, "forgejo", "forgejo", argv, stdin="".join(k + "\n" for k in keys)).strip().splitlines()
    for line in lines[:-1]:
        ctx.say(f"forgejo: Forgejo {line.replace('refused: ', 'refused your SSH key ', 1)}")
    if lines[-1:] == ["changed"]:
        ctx.say(f"forgejo: your own user {user} has {len(keys)} SSH key(s) from the page")
    # Written when setup made the user; absent if the owner made it by hand.
    kept = guest_out(ctx, "forgejo", "forgejo", ["sh", "-c", "cat /etc/forgejo/owner-password 2>/dev/null || true"]).strip()
    if kept:
        ctx.secrets.set("forgejo_owner_password", kept)


# The container's memory limit while llama.cpp builds: one file of its
# Vulkan backend took 1.8 GiB to compile (a measured peak, /usr/bin/time -v
# in a Debian 12 container), more than the inference floor leaves.
BUILD_MEMORY_MIB = 4096


def give_gpu(ctx: Context, vmid: int, gpus: list[str]) -> None:
    """The inference cards' device nodes as devN entries of the container,
    set as root over SSH (PVE gives device passthrough to root@pam alone),
    then a restart so the container sees them."""
    minors = []
    for pci in gpus:
        info = f"/proc/driver/nvidia/gpus/{pci.lower()}/information"
        out = host_out(ctx, "inference", f"awk '/^Device Minor:/ {{print $3}}' {info} 2>/dev/null || true")
        if not out.strip().isdigit():
            raise ApplyError("inference", f"the NVIDIA driver does not list the card at {pci}; nvidia-smi on the host shows what it sees")
        minors.append(int(out))
    want = llm.wanted_devices(minors)
    current = llm.current_devices(host_out(ctx, "inference", f"pct config {vmid}"))
    if sorted(current.values()) == sorted(want):
        return
    if current:
        host_out(ctx, "inference", f"pct set {vmid} --delete {','.join(f'dev{n}' for n in sorted(current))}")
    host_out(ctx, "inference", f"pct set {vmid} " + " ".join(f"--dev{n} {shlex.quote(v)}" for n, v in enumerate(want)))
    see_changes(ctx, "inference", llm.INFERENCE, 120)
    ctx.say(f"inference: gave the inference container {len(minors)} graphics card(s) and restarted it")


def trust_ca(ctx: Context, stage: str, names: list[str]) -> None:
    """step-ca's root in each guest's system store, so its clients check the
    certificates NullBill's services present."""
    root = ca_root(ctx, stage)
    install = "cat > /usr/local/share/ca-certificates/nullbill-root.crt && update-ca-certificates >/dev/null"
    for name in names:
        guest_out(ctx, stage, name, ["sh", "-c", install], stdin=root)


def stage_inference(ctx: Context) -> None:
    """NVIDIA's driver on the host, the card in the inference container,
    llama.cpp serving the planned model there, and LiteLLM in front."""
    if not llm.enabled(ctx.bare):
        return
    section = ctx.bare["inference"]
    name, model = llm.model(ctx.bare, ctx.model)
    version, sha256 = llm.nvidia_pin()
    guest = ctx.bare["guests"][llm.INFERENCE]
    vmid = guest["vmid"]
    loaded = host_out(ctx, "inference", "cat /sys/module/nvidia/version 2>/dev/null || true").strip()
    if loaded and loaded != version:
        # The container holds the old driver open, and its user space must
        # change along with it.
        shut_down(ctx, "inference", llm.INFERENCE, vmid, 120)
        ctx.say(f"inference: stopped the inference container to replace NVIDIA's driver {loaded} with {version}")
    # The module is built for the running kernel and must be the one loaded.
    kernel = host_out(ctx, "inference", "uname -r").strip()
    run_playbook(ctx, "inference", "nvidia-host.yml", None, facts=f"{kernel} {loaded}")
    give_gpu(ctx, vmid, section["gpus"])
    if "status: running" not in host_out(ctx, "inference", f"pct status {vmid}"):
        start(ctx, "inference", llm.INFERENCE)
    backend_key = ctx.secrets.ensure("inference_key", lambda: secrets.token_urlsafe(32))
    variables = {
        "inference_nvidia_version": version,
        "inference_nvidia_sha256": sha256,
        "inference_model_name": name,
        "inference_model_url": model["url"],
        "inference_model_sha256": model["sha256"],
        "inference_context": model["context"],
        "inference_sampling": model.get("sampling", {}),
        "inference_reasoning": model.get("reasoning", "auto"),
        "inference_reasoning_effort": model.get("reasoning_effort", ""),
        "inference_cache_type": model.get("cache_type", "f16"),
        "inference_listen": guest["ip"],
        "inference_port": llm.INFERENCE_PORT,
        "inference_api_key": backend_key,
    }
    ctx.say(f"inference: {name}; the first run builds llama.cpp and downloads the model, which takes a while")
    lift = guest["memory_mib"] < BUILD_MEMORY_MIB
    if lift:
        host_out(ctx, "inference", f"pct set {vmid} --memory {BUILD_MEMORY_MIB}")
    try:
        run_playbook(ctx, "inference", "inference.yml", llm.INFERENCE, variables)
    finally:
        if lift:
            host_out(ctx, "inference", f"pct set {vmid} --memory {guest['memory_mib']}")
    gateway = {
        "model_gateway_domain": fqdn(ctx, llm.GATEWAY),
        "model_gateway_port": llm.GATEWAY_PORT,
        "model_gateway_key": ctx.secrets.ensure("model_gateway_key", lambda: "sk-" + secrets.token_urlsafe(32)),
        "model_gateway_model": name,
        "model_gateway_backend": f"http://{fqdn(ctx, llm.INFERENCE)}:{llm.INFERENCE_PORT}/v1",
        "model_gateway_backend_key": backend_key,
    }
    cert, key = browser_cert(ctx, "inference", llm.GATEWAY, ctx.bare["guests"][llm.GATEWAY]["ip"])
    gateway |= {"model_gateway_browser_cert": cert, "model_gateway_browser_key": key}
    run_playbook(ctx, "inference", "model-gateway.yml", llm.GATEWAY, gateway)
    trust_ca(ctx, "inference", llm.gateway_clients(ctx.bare, ctx.model))
    url = f"https://{fqdn(ctx, llm.GATEWAY)}:{llm.GATEWAY_PORT}/v1"
    ctx.say(f"inference: the model gateway serves {name} (also as local) at {url}; the page shows its key")


def stage_files(ctx: Context) -> None:
    """Samba on the files guest, one share per kept disk, for the owner only."""
    if not planned(ctx, FILES):
        return
    shares = [{"name": m["share"], "read_only": m["mode"] == "ro"} for m in legacy.shares(ctx.bare)]
    # A copy of a converted disk is the owner's own ext4 volume: read-write.
    shares += [{"name": c["name"], "read_only": False} for c in legacy.copies(ctx.bare)]
    password = ctx.secrets.ensure("files_share_password", lambda: secrets.token_urlsafe(24))
    variables = {"files_shares": shares, "files_user": legacy.SHARE_USER, "files_password": password}
    run_playbook(ctx, "files", "files.yml", FILES, variables)
    address = ctx.bare["guests"][FILES]["ip"]
    where = f"\\\\{address}\\<share>"
    ctx.say(f"files: from your devices on the tailnet, open {where} as user {legacy.SHARE_USER}; the page shows the password")


def give_render_node(ctx: Context, vmid: int) -> str | None:
    """The integrated GPU's render node as the container's devN, when the plan
    transcodes there, set as root (as give_gpu); none when it does not. The
    node's path, or None."""
    pci = media.igpu(ctx.bare)
    node = None
    want = []
    if pci:
        node = host_out(ctx, "media", media.render_node(pci)).strip()
        if not node:
            raise ApplyError(
                "media",
                f"the host has no render node for the integrated graphics at {pci}; "
                "probe again, or set media.transcode: cpu to transcode on the CPU",
            )
        want = [media.device(node)]
    current = media.current_devices(host_out(ctx, "media", f"pct config {vmid}"))
    if sorted(current.values()) == want:
        return node
    if current:
        host_out(ctx, "media", f"pct set {vmid} --delete {','.join(f'dev{n}' for n in sorted(current))}")
    if want:
        host_out(ctx, "media", f"pct set {vmid} --dev0 {shlex.quote(want[0])}")
    see_changes(ctx, "media", media.JELLYFIN, 120)
    what = "gave the Jellyfin guest the integrated graphics" if want else "took the graphics off the Jellyfin guest"
    ctx.say(f"media: {what} and restarted it")
    return node


NB_JELLYFIN = "/usr/local/sbin/nb-jellyfin"


def stage_media(ctx: Context) -> None:
    """Jellyfin in its guest: the integrated graphics when it transcodes there,
    the role, then nb-jellyfin finishes its first-run setup at once (until
    then anyone who reaches it could make themselves its admin), sets how it
    transcodes, adds each kept disk as a library, once, and starts one scan
    for the libraries it added."""
    if not media.enabled(ctx.bare):
        return
    guest = ctx.bare["guests"][media.JELLYFIN]
    node = give_render_node(ctx, guest["vmid"])
    run_playbook(ctx, "media", "jellyfin.yml", media.JELLYFIN, {"jellyfin_proxy": proxy_url(ctx, media.JELLYFIN)})
    guest_out(ctx, "media", media.JELLYFIN, [NB_JELLYFIN, "wait"])
    if guest_out(ctx, "media", media.JELLYFIN, [NB_JELLYFIN, "setup", media.ADMIN_USER]).strip() == "changed":
        ctx.say(f"media: finished Jellyfin's first-run setup; its admin is {media.ADMIN_USER}")
    encoding = ["qsv", node] if node else ["none"]
    if guest_out(ctx, "media", media.JELLYFIN, [NB_JELLYFIN, "encoding", *encoding]).strip() == "changed":
        ctx.say(f"media: Jellyfin transcodes {'with Quick Sync on the integrated graphics' if node else 'on the CPU'}")
    shares = media.library(ctx.bare)
    for share in shares:
        if guest_out(ctx, "media", media.JELLYFIN, [NB_JELLYFIN, "library", share, f"{media.MEDIA_ROOT}/{share}"]).strip() == "changed":
            ctx.say(f"media: Jellyfin has a library of {media.MEDIA_ROOT}/{share}")
    # One scan after the last library: Jellyfin starts none for a library
    # added while it scans, which left a second kept disk's library empty.
    scan = guest_out(ctx, "media", media.JELLYFIN, [NB_JELLYFIN, "scan"]).strip() if shares else "same"
    if scan == "started":
        ctx.say("media: Jellyfin reads its new libraries now")
    elif scan == "waiting":
        ctx.say("media: Jellyfin is still scanning; the next setup run, or Scan All Libraries on its dashboard, reads the new libraries")
    password = guest_out(ctx, "media", media.JELLYFIN, ["cat", media.PASSWORD_FILE]).strip()
    ctx.secrets.set("jellyfin_admin_password", password)
    url = f"http://{guest['ip']}:{media.PORT}"
    ctx.say(f"media: from your devices on the tailnet Jellyfin is {url}; sign in as {media.ADMIN_USER}, the page shows the password")


def ssh_user_ca(ctx: Context, stage: str) -> str:
    return guest_out(ctx, stage, "step-ca", ["cat", f"{STEP}/certs/ssh_user_ca_key.pub"]).strip()


def linux_pools(ctx: Context) -> dict[str, dict]:
    """Pools the dispatcher serves; Windows pools wait on their own image (#22)."""
    return {n: g for n, g in ctx.bare["guests"].items() if g["kind"] == "vm-pool" and g["zone"] != "windows"}


def _names(field: str, address: str) -> bool:
    """Does a known_hosts host field name this address? ssh may have hashed
    it (HashKnownHosts, on in Debian's ssh_config): |1|salt|HMAC-SHA1."""
    for name in field.split(","):
        if name.startswith("|1|"):
            try:
                salt, digest = (base64.b64decode(p) for p in name[3:].split("|", 1))
            except ValueError:
                continue
            if hmac.compare_digest(hmac.new(salt, address.encode(), hashlib.sha1).digest(), digest):
                return True
        elif name == address:
            return True
    return False


def forget_host_key(ctx: Context, address: str) -> None:
    """A golden image is a new machine each time it is baked, and a VM
    OpenTofu makes again boots with new host keys."""
    known = ctx.home / "known_hosts"
    if known.exists():
        lines = known.read_text().splitlines(keepends=True)
        known.write_text("".join(line for line in lines if not _names(line.split(" ", 1)[0], address)))


def forget_remade_vms(ctx: Context) -> None:
    """Forget the pinned host key of each VM OpenTofu made, or made again,
    since the last apply: its creation time (PVE's meta ctime) changed."""
    path = ctx.home / "vm-ctimes.json"
    known = json.loads(path.read_text()) if path.exists() else {}
    now = {}
    for name, guest in ctx.bare["guests"].items():
        if guest["kind"] != "vm":
            continue
        config = host_out(ctx, "tofu", f"qm config {guest['vmid']}")
        now[name] = next((m.split("=", 1)[1] for m in re.findall(r"ctime=\d+", config)), "")
        if now[name] != known.get(name):
            for address in guest.get("ips", {}).values() if "ips" in guest else [guest.get("ip")]:
                if address:
                    forget_host_key(ctx, address)
    path.write_text(json.dumps(now, indent=2) + "\n")


CLUSTER_VMS = "pvesh get /cluster/resources --type vm --output-format json"


def ci_clones(ctx: Context, first: int, last: int) -> list[int]:
    """The CI clones in first..last: VMs in the nb-ci pool only, so a VM of
    the owner's that happens to sit in that range is never touched."""
    vms = json.loads(host_out(ctx, "ci", CLUSTER_VMS) or "[]")
    return sorted(
        int(vm["vmid"])
        for vm in vms
        if vm.get("type") == "qemu" and vm.get("pool") == ci.POOL and not vm.get("template") and first <= int(vm["vmid"]) <= last
    )


def drain_clones(vmids: list[int], limit_s: int) -> str:
    """A host script: wait up to limit_s for these clones to finish their
    jobs, then stop any still running."""
    listed = " ".join(str(v) for v in vmids)
    return (
        f'running() {{ for v in {listed}; do qm status "$v" 2>/dev/null | grep -q "status: running" && echo "$v"; done; true; }}; '
        f"end=$((SECONDS + {limit_s})); "
        'while [ -n "$(running)" ] && [ "$SECONDS" -lt "$end" ]; do sleep 30; done; '
        'for v in $(running); do qm stop "$v"; done'
    )


def retire(ctx: Context, name: str, guest: dict) -> None:
    """Make way for a new golden image: the dispatcher stops taking jobs
    (the stage's dispatcher run starts it again), the old image's clones
    finish, and OpenTofu replaces the template with a fresh VM from the
    pinned image. Jobs queue in Forgejo meanwhile."""
    guest_out(ctx, "ci", "runner-dispatcher", ["sh", "-c", "systemctl stop nb-dispatcher 2>/dev/null || true"])
    first, last = (int(v) for v in guest["clone_vmids"].split("-"))
    vmids = ci_clones(ctx, first, last)
    if vmids:
        ctx.say(f"ci: waiting for {name}'s running jobs to finish, at most {ci.MAX_JOB_S // 3600} h")
        host_out(ctx, "ci", f"bash -c {shlex.quote(drain_clones(vmids, ci.MAX_JOB_S))}", timeout=ci.MAX_JOB_S + 900)
    # Asked again: only what is still a clone in the pool is destroyed.
    for vmid in ci_clones(ctx, first, last):
        host_out(ctx, "ci", f"qm stop {vmid} 2>/dev/null; qm destroy {vmid} --purge")
    run_tofu(ctx, "ci", init=False, replace=(f"proxmox_virtual_environment_vm.{name}",))


# Where the host keeps a mark while a golden image is being baked.
BAKING_DIR = "/root/nullbill"


def bake(ctx: Context, name: str, guest: dict) -> bool:
    """Boot the pool's template VM, make it a one-job runner, seal it and
    convert it to a template. A template is kept until it is
    ci.REBAKE_AFTER_S old, then replaced and baked again. True if it baked."""
    vmid = guest["template_vmid"]
    runner = {
        "ci_runner_ca_root": ca_root(ctx, "ci"),
        "ci_runner_browser_root": browser_root(ctx).read_text(),
        "ci_runner_dispatcher_url": f"https://{fqdn(ctx, 'runner-dispatcher')}:8443",
        "ci_runner_proxy_url": proxy_url(ctx, name),
        "ci_runner_no_proxy": f".{ctx.bare['domain']},localhost,127.0.0.1",
    }
    # What the image was baked with; clones of an image baked before a
    # change (a new root, say) would fail their jobs until the weekly bake.
    inputs = hashlib.sha256(json.dumps(runner, sort_keys=True).encode()).hexdigest()
    inputs_file = f"{BAKING_DIR}/{name}.inputs"
    if "template: 1" in host_out(ctx, "ci", f"qm config {vmid}"):
        recorded = host_out(ctx, "ci", f"cat {ci.TEXTFILE_DIR}/{ci.baked_file(name)} 2>/dev/null || true")
        baked = ci.baked_time(recorded)
        same = host_out(ctx, "ci", f"cat {inputs_file} 2>/dev/null || true").strip() == inputs
        if baked is not None and time.time() - baked < ci.REBAKE_AFTER_S and same:
            ctx.say(f"ci: {name}'s golden image is baked")
            return False
        if same:
            ctx.say(f"ci: {name}'s golden image is over {ci.REBAKE_AFTER_S // 86400} days old; baking it again")
        else:
            ctx.say(f"ci: {name}'s golden image was baked with other settings; baking it again")
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
    run_playbook(ctx, "ci", "ci-runner.yml", name, runner)
    if guest.get("agent_managed"):
        run_playbook(ctx, "ci", "agent-ssh.yml", name, {"agent_ssh_user_ca_key": ssh_user_ca(ctx, "ci")})
    run_playbook(ctx, "ci", "ci-runner-seal.yml", name)
    host_out(ctx, "ci", f"qm shutdown {vmid} --timeout 180 && qm template {vmid}")
    host_files(ctx, "ci", ci.TEXTFILE_DIR, {ci.baked_file(name): ci.baked_metric(name, time.time())})
    host_files(ctx, "ci", BAKING_DIR, {f"{name}.inputs": inputs + "\n"})
    host_out(ctx, "ci", f"rm -f {marker}")
    ctx.say(f"ci: {name}'s golden image is a template")
    return True


def forgejo_token(ctx: Context) -> str:
    """write:admin, for Forgejo's runner endpoints; made once and kept."""

    def make() -> str:
        argv = ["/usr/local/sbin/nb-forgejo", "admin", "user", "generate-access-token", "--username", "nullbill-admin"]
        # A unique name, so a token made again after a lost secrets file never clashes.
        argv += ["--token-name", f"dispatcher-{os.urandom(4).hex()}", "--scopes", "write:admin", "--raw"]
        return guest_out(ctx, "ci", "forgejo", argv).strip()

    return ctx.secrets.ensure("forgejo_dispatcher_token", make)


def both_roots(ctx: Context, stage: str) -> str:
    """What clients of Forgejo's name trust, and monitoring's certificate
    probes: Forgejo's name has the browser CA's certificate (stage_forgejo),
    step-ca's root covers the rest."""
    return ca_root(ctx, stage).strip() + "\n" + browser_root(ctx).read_text()


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
        "runner_dispatcher_forgejo_ca": both_roots(ctx, "ci"),
        "runner_dispatcher_forgejo_token": forgejo_token(ctx),
        "runner_dispatcher_pve_url": f"https://pve.{ctx.bare['domain']}:8006",
        "runner_dispatcher_node": host_out(ctx, "ci", "hostname").strip(),
        "runner_dispatcher_pve_token": tofu_output(ctx, "ci", "dispatcher_token").strip(),
        "runner_dispatcher_pve_ca": pve_ca(ctx, "ci"),
        "runner_dispatcher_pools": pools,
    }


def stage_games(ctx: Context) -> None:
    """Each planned game server in its guest: the role, a wait until the
    server answers (it makes its world at the first start), then the admins
    config.yaml names that the server does not have yet, typed on its
    console as the owner would."""
    for name in games.planned(ctx.bare, ctx.model):
        game = ctx.model["games"][name]
        run_playbook(ctx, "games", games.playbook(ctx.model, name), name, games.variables(ctx.bare, ctx.model, name))
        guest_out(ctx, "games", name, [games.NB_GAME, "wait", str(games.START_WAIT_S)])
        # Only a game with admins in config.yaml is asked for its list:
        # Terraria keeps none, and nb-game fails there.
        missing = []
        if game.get("admins"):
            missing = games.missing_admins(ctx.model, name, guest_out(ctx, "games", name, [games.NB_GAME, "admins"]))
        for admin in missing:
            guest_out(ctx, "games", name, [games.NB_GAME, "console", games.admin_command(ctx.model, name, admin)])
        deadline = time.monotonic() + games.ADMIN_WAIT_S
        while missing and time.monotonic() < deadline:
            sleep(1)
            missing = games.missing_admins(ctx.model, name, guest_out(ctx, "games", name, [games.NB_GAME, "admins"]))
        if missing:
            # A name the game does not know. The server runs; verify names it too.
            ctx.say(f"games: {game['title']} did not make {', '.join(missing)} an admin; is the name spelled as in the game?")
        ctx.say(f"games: {game['title']} {game['version']} answers at {games.address(ctx.bare, name, ctx.model)}")


def stage_custom(ctx: Context) -> None:
    """Each planned service of the owner's own (custom.py): its setup script
    from beside config.yaml, run as root in its guest by the custom_service
    role. The playbook runs again when the script changed, and the script
    itself whenever its last run did not end well. A service without a
    script stays the Debian guest OpenTofu and the updates stage made.

    A script that fails does not stop setup: the program is not NullBill's,
    and one service's mistake must not keep the others, or the checks, from
    running. The run's log has its output, and verify fails on it by name."""
    failed = []
    for name in custom.planned(ctx.bare, ctx.model):
        text = custom.read_script(ctx.home, name)
        if text is None:
            ctx.say(f"custom: {name} has no setup script ({custom.DIR}/{name}/{custom.SCRIPT}); its guest stays as setup made it")
            continue
        variables = custom.variables(ctx.bare, ctx.model, name, text, proxy_url(ctx, name))
        try:
            run_playbook(ctx, "custom", custom.PLAYBOOK, name, variables)
        except NullBillError as exc:
            failed.append(name)
            ctx.say(f"custom: {name}'s setup script did not finish ({'; '.join(exc.messages)[:300]}); setup goes on without it")
            continue
        ctx.say(f"custom: {custom.title(ctx.model, name)} is set up in {name}")
    if failed:
        ctx.say(f"custom: not set up: {', '.join(failed)}. Their output is above; the checks name each one")


def stage_ci(ctx: Context) -> None:
    """Each Linux pool's golden image, then the dispatcher that clones it per job."""
    if not planned(ctx, "runner-dispatcher") or not linux_pools(ctx):
        return
    # On demand, the workload is on while an image bakes: a bake takes the
    # memory of one of its clones.
    ensure_on(ctx, "ci", ["runner-dispatcher"])
    baked = [bake(ctx, name, guest) for name, guest in linux_pools(ctx).items()]
    # Making way for a new image stopped the dispatcher; its run starts it again.
    run_playbook(ctx, "ci", "runner-dispatcher.yml", "runner-dispatcher", dispatcher_vars(ctx), always=any(baked))


def proxy_url(ctx: Context, name: str) -> str:
    """The egress proxy for a guest in a proxied zone, or "" where egress is
    direct. The router, which is the zones' way out, has its own uplink."""
    if name == "router" or ctx.bare["zones"][ctx.bare["guests"][name]["zone"]]["egress"] != "proxy":
        return ""
    return f"http://{ctx.bare['guests'][PROXY]['ip']}:{proxy_port(ctx.model)}"


AGENT_CREDENTIALS = {"anthropic_api_key": "operator_anthropic_api_key", "claude_oauth_token": "operator_claude_oauth_token"}


def agent_guests(ctx: Context) -> list[str]:
    """Agent-managed guests that live on; pool images get agent SSH when baked."""
    return [n for n, g in ctx.bare["guests"].items() if g.get("agent_managed") and g["kind"] != "vm-pool"]


# Root's authorized_keys on the workspace keeps the owner's keys from the
# page between these lines; keys outside them (OpenTofu's, the owner's own)
# stay. Reads the keys on stdin, prints "changed" or "same".
OWNER_KEYS = r"""
import os, sys
path, begin, end = "/root/.ssh/authorized_keys", "# nullbill: owner keys (the page's Access, SSH keys)", "# nullbill: end"
try:
    old = open(path).read()
except FileNotFoundError:
    old = ""
lines, inside, kept = old.splitlines(), False, []
for line in lines:
    if line == begin:
        inside = True
    elif line == end and inside:
        inside = False
    elif not inside:
        kept.append(line)
keys = [k.strip() for k in sys.stdin if k.strip()]
new = "".join(k + "\n" for k in kept + ([begin, *keys, end] if keys else []))
if new == old:
    print("same")
    sys.exit()
os.makedirs("/root/.ssh", mode=0o700, exist_ok=True)
fd = os.open(path + ".nb", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(new)
os.replace(path + ".nb", path)
print("changed")
"""


def workspace_keys(ctx: Context) -> None:
    """The owner's SSH keys from the page, for root on the workspace."""
    if not planned(ctx, "workspace") or not owner.person(ctx.model):
        return
    keys = owner.keys(ctx.model)
    out = guest_out(ctx, "agents", "workspace", ["python3", "-c", OWNER_KEYS], stdin="".join(k + "\n" for k in keys))
    if out.strip() == "changed":
        ctx.say(f"agents: the workspace takes {len(keys)} SSH key(s) of yours for root")


def stage_agents(ctx: Context) -> None:
    """The owner's keys on the workspace, the operator with the agent CLI and
    its tokens, and agent SSH on each agent-managed guest."""
    workspace_keys(ctx)
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
        "operator_pve_ca": pve_ca(ctx, "agents"),
        "operator_ca_root": ca_root(ctx, "agents"),
        "operator_step_password": guest_out(ctx, "agents", "step-ca", ["cat", f"{STEP}/secrets/agent-password"]).strip(),
    }
    variables |= agent_page(ctx) | {"operator_guests": agent_map(ctx), "operator_domain": ctx.bare["domain"]}
    variables |= agent_host(ctx) | agent_repo(ctx)
    run_playbook(ctx, "agents", "operator.yml", "operator", variables)
    keep_agent_key(ctx)
    key = ssh_user_ca(ctx, "agents")
    for name in agent_guests(ctx):
        run_playbook(ctx, "agents", "agent-ssh.yml", name, {"agent_ssh_user_ca_key": key})


def agent_host(ctx: Context) -> dict:
    """`ssh pve` on the operator: the host's mgmt address and its own key."""
    key = host_out(ctx, "agents", "cat /etc/ssh/ssh_host_ed25519_key.pub").split()
    return {"operator_host_ip": ctx.bare["zones"]["mgmt"]["host_ip"], "operator_host_key": " ".join(key[:2])}


def agent_repo(ctx: Context) -> dict:
    """NullBill's repository for the agent's pull requests: the owner's
    GitHub token and noreply identity (saved on the page), and identifiers
    of this server its pre-push hook refuses."""
    data = ctx.secrets.data
    login, user_id = data.get("agent_github_login", ""), data.get("agent_github_id", "")
    out = {"operator_denylist": agent_denylist(ctx), "operator_github_token": data.get("agent_github_token", "")}
    if login and user_id:
        out |= {"operator_git_name": login, "operator_git_email": f"{user_id}+{login}@users.noreply.github.com"}
    origin = subprocess.run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"], capture_output=True, text=True, check=False)
    if origin.returncode == 0 and origin.stdout.strip().startswith("https://"):
        out["operator_repo_url"] = origin.stdout.strip()
    return out


def agent_denylist(ctx: Context) -> list[str]:
    """What identifies this server or its owner and must never reach the
    public repository: the host's LAN address, people's logins, disk ids
    (they hold serials), partition UUIDs and labels, and the owner's own
    $NULLBILL_HOME/denylist.txt."""
    entries = {ctx.lan_host or ""}
    entries |= {p.get("login", "") for p in ctx.model.get("people", {}).values()}
    facts_path = ctx.home / "probe" / "latest" / "facts.json"
    facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
    for disk in facts.get("disks") or []:
        entries.add(disk.get("id", ""))
        for part in disk.get("partitions") or []:
            entries |= {str(part.get("uuid") or ""), str(part.get("label") or "")}
    own = ctx.home / "denylist.txt"
    if own.exists():
        entries |= {line.strip() for line in own.read_text().splitlines() if not line.lstrip().startswith("#")}
    # Short entries (a label like "Data") would refuse ordinary words.
    return sorted(e for e in entries if len(e) >= 6)


def keep_agent_key(ctx: Context) -> None:
    """The agent's public key for the page's root switch, and the host's
    authorized_keys line again while the switch is on: setup may have made
    the operator, and its key, anew."""
    key = guest_out(ctx, "agents", "operator", ["cat", "/home/agent/.ssh/nb-host.pub"]).strip()
    path = ctx.home / hostroot.PUBLIC_KEY
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(key + "\n")
    if hostroot.enabled(ctx.home):
        host_out(ctx, "agents", hostroot.command(hostroot.line(hostroot.public_key(ctx.home) or "", ctx.bare["guests"]["operator"]["ip"])))
        ctx.say("agents: the agent has root on the host (the page's switch is on)")


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
    ensure_on(ctx, stage, [name])
    pin_host_key(ctx, stage, name)
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


def tailnet_api(ctx: Context) -> Tailnet | None:
    """The owner's Tailscale OAuth client, or None until one is pasted."""
    return tailscale.from_secrets(ctx.secrets.data, Tailnet)


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
        changed, count = tailscale.sync_policy(api, want, TAG, limit)
    except TailscaleError as error:
        raise ApplyError(stage, str(error)) from error
    if changed:
        ctx.say(f"{stage}: pushed the tailnet policy; Tailscale ran its tests ({count} of {limit} tagged devices)")
    else:
        ctx.say(f"{stage}: the tailnet policy is in place ({count} of {limit} tagged devices)")
    return True


def split_dns(ctx: Context, stage: str, api: Tailnet) -> None:
    """Every tailnet device looks up the plan's names at the router
    (names.py). An OAuth client without the dns scope leaves them to
    addresses, which work all the same, and says how to change that."""
    domain, server = ctx.bare["domain"], nameserver(ctx.bare)
    try:
        if api.split_dns().get(domain) == [server]:
            ctx.say(f"{stage}: your devices look up {domain} names at the router ({server})")
            names.record(ctx.home, domain, server)
            return
        api.set_split_dns(domain, [server])
        back = api.split_dns().get(domain)
    except MissingScope:
        names.forget(ctx.home)
        ctx.say(
            f"{stage}: the Tailscale OAuth client cannot change DNS, so your devices reach the services by address only; {names.ADVICE}"
        )
        return
    except TailscaleError as error:
        raise ApplyError(stage, str(error)) from error
    if back != [server]:
        raise ApplyError(
            stage, f"Tailscale's split DNS for {domain} reads back as {back}, not [{server}]; check the admin console's DNS page"
        )
    names.record(ctx.home, domain, server)
    ctx.say(f"{stage}: your devices now look up {domain} names at the router ({server})")


def stage_tailnet(ctx: Context) -> None:
    """Before anything on the host changes: the tailnet's policy, so nobody
    reaches more than the plan grants once the vpn guest is in; then the
    tailnet's DNS for the plan's names."""
    if not planned(ctx, "vpn"):
        return
    if not push_policy(ctx, "tailnet"):
        ctx.say("tailnet: no Tailscale OAuth client saved; the vpn guest stays off the tailnet until one is pasted on the page")
        return
    split_dns(ctx, "tailnet", tailnet_api(ctx))


def direct_path(ctx: Context) -> None:
    """Say, and keep for the page, whether a device on mobile data reaches the
    vpn guest directly or through Tailscale's relays (direct.py). tailscaled,
    just restarted on another port, takes some seconds to learn the address
    its packets have past the home router."""
    found = None
    for pause in (0, 5, 10, 15):
        if pause:
            sleep(pause)
        found = direct.find(ctx.plan, ctx.model, ctx.target, lambda _target, command: ctx.reach.host(command))
        if found is None or found.kind != direct.UNSEEN:
            break
    direct.record(ctx.home, found)
    if found:
        ctx.say(f"vpn: {direct.line(found)}")


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
        "vpn_port": TAILSCALE_PORT,
    }
    run_playbook(ctx, "vpn", "vpn.yml", "vpn", variables)
    status = json.loads(guest_out(ctx, "vpn", "vpn", ["tailscale", "status", "--json"]))
    if status.get("BackendState") == "Running":
        name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
        ctx.say(f"vpn: on the tailnet as {name or 'nullbill'}, advertising {', '.join(variables['vpn_routes'])}")
        page = f"https://{fqdn(ctx, 'nullbill')}:8443" if names.active(ctx.home) else page_url(ctx).rstrip("/")
        ctx.say(f"vpn: from your devices on the tailnet the page is {page}")
        # A one-time key is spent once it has logged in.
        ctx.secrets.drop("tailscale_auth_key")
        direct_path(ctx)
    elif variables["vpn_auth_key"]:
        state = status.get("BackendState")
        raise ApplyError("vpn", f"Tailscale did not log in ({state}); make a new one-time auth key and paste it on the page")
    else:
        ctx.say("vpn: waiting for a Tailscale auth key; paste one on the page and run setup again")


def stage_verify(ctx: Context) -> None:
    lan_host = ctx.lan_host or split_target(ctx.target)[1]
    results = run_verify(
        ctx.plan,
        ctx.model,
        ctx.target,
        ctx.run,
        lan_state=lan.settle(ctx.home, lambda command: ctx.run(ctx.target, command)[1]),
        lan_host=lan_host,
        say=lambda r: ctx.say(r.line()),
        browser_root=ctx.home / browserca.DIR / "ca.crt",
        tailnet=lambda: tailnet_api(ctx),
        home=ctx.home,
    )
    failed = [r for r in results if r.status == "FAIL"]
    flag = ctx.home / FULL_NEXT
    if failed:
        flag.touch()
        ctx.say("verify: the next setup runs every step in full, not only what changed")
        raise ApplyError("verify", f"{len(failed)} checks failed")
    flag.unlink(missing_ok=True)


Stage = tuple[str, Callable[[Context], None]]
STAGE_MARK = "--- stage "
STAGE_TIME = "%Y-%m-%d %H:%M:%S"
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
    ("inference", stage_inference),
    ("files", stage_files),
    ("media", stage_media),
    ("games", stage_games),
    ("ci", stage_ci),
    ("agents", stage_agents),
    ("logs", stage_logs),
    ("backups", stage_backups),
    ("vpn", stage_vpn),
    # Last before the checks: nothing of NullBill's own waits for a program it does not know.
    ("custom", stage_custom),
    ("verify", stage_verify),
]


def run_apply(ctx: Context, stages: list[Stage] = STAGES, only: str | None = None) -> None:
    names = [n for n, _ in stages]
    if only is not None and only not in names:
        raise NullBillError(f"unknown stage {only!r}; stages are {', '.join(names)}")
    try:
        for name, stage in stages:
            if only in (None, name):
                # The page follows apply by these lines (nullbill/web/progress.py).
                print(f"{STAGE_MARK}{name}, {time.strftime(STAGE_TIME)}", flush=True)
                stage(ctx)
    finally:
        end_hold(ctx)


SSH_RETRIES = 12


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
            env = os.environ | {
                "ANSIBLE_COLLECTIONS_PATH": str(self.collections),
                "ANSIBLE_RETRY_FILES_ENABLED": "false",
                # A VM OpenTofu has just made, or made again, is still booting
                # when its stage starts. Ansible retries an SSH connection
                # error with a growing pause (1, 3, 7, 15, then 30 s), so
                # 12 retries wait about four minutes for sshd and cloud-init.
                "ANSIBLE_SSH_RETRIES": str(SSH_RETRIES),
                # One exec per module instead of four, to containers and VMs.
                "ANSIBLE_PIPELINING": "true",
                "ANSIBLE_CONNECTION_PLUGINS": str(ANSIBLE / "connection_plugins"),
                "ANSIBLE_PARAMIKO_PTY": "false",
                # A failed task's result as YAML, its output line by line,
                # not as one line of JSON a thousand characters long: the
                # owner reads this log on the page (progress.py reads both).
                "ANSIBLE_CALLBACK_RESULT_FORMAT": "yaml",
            }
            argv = ["ansible-playbook", "-i", str(inventory), "-e", f"@{values}", str(ANSIBLE / playbook)]
            try:
                return subprocess.run(argv, env=env, check=False).returncode
            except FileNotFoundError as exc:
                raise NullBillError("ansible-playbook not found; install requirements-apply.txt (docs/runbooks/controller.md)") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
