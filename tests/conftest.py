import json
import re
import time

import pytest
from fixturedata import CLASSES, FIXTURES, fixture_config, write_raw  # noqa: F401

from nullbill.probe import parse
from nullbill.tailscale import MissingScope, TailscaleError


@pytest.fixture
def facts(tmp_path):
    def load(fixture: str) -> dict:
        return parse(write_raw(tmp_path / fixture / "raw", fixture))

    return load


def backup_host(plan: dict, age_h: float = 3, encrypted: bool = True, hostname: str | None = None):
    """A runner answering verify's backup commands like a host whose nightly
    job ran age_h hours ago; restores return the guest's name as hostname."""
    from nullbill import backups

    now = time.time()
    guests = backups.guests(plan)
    by_vmid = {g["vmid"]: n for n, g in guests.items()}
    content = [
        {
            "volid": f"{backups.STORAGE}:backup/{'ct' if g['kind'] == 'lxc' else 'vm'}/{g['vmid']}/2027-01-15T02:00:00Z",
            "vmid": g["vmid"],
            # The lowest VMID finished last.
            "ctime": now - age_h * 3600 - g["vmid"],
            **({"encrypted": "aa:bb"} if encrypted else {}),
        }
        for g in guests.values()
    ]
    api = agent_api(plan)
    services = service_answers(plan)
    storage = {"server": "10.42.1.15", "datastore": backups.DATASTORE, "username": backups.TOKEN_ID, "fingerprint": "aa:bb"}

    def run(target, command):
        if command.startswith(f"pvesh get /nodes/localhost/storage/{backups.STORAGE}/content"):
            return 0, json.dumps(content), ""
        if command == f"pvesh get /storage/{backups.STORAGE} --output-format json":
            return 0, json.dumps(storage), ""
        if "proxmox-backup-client restore" in command:
            vmid = int(re.search(r"ct/(\d+)/", command)[1])
            return 0, (hostname or by_vmid[vmid]) + "\n", ""
        if command in services:
            return 0, services[command], ""
        return api(target, command)

    return run


def service_answers(plan: dict, wrong: str = "") -> dict[str, str]:
    """What a correct host prints for verify's Forgejo and dispatcher checks;
    the check labelled `wrong` gets a wrong answer."""
    import shlex

    from nullbill import ci, direct, forge, llm, media, names
    from nullbill.render import strip_notes

    bare = strip_notes(plan)
    checks = [(label, guest, cmd, want) for label, guest, cmd, want in forge._checks(bare)] if forge.enabled(bare) else []
    if ci.enabled(bare):
        checks += [(label, "runner-dispatcher", cmd, want) for label, cmd, want in ci._service_checks(bare)]
    answers = {
        f"pct exec {bare['guests'][guest]['vmid']} -- bash -c {shlex.quote(cmd)}": ("nope" if label == wrong else want) + "\n"
        for label, guest, cmd, want in checks
    }
    if llm.enabled(bare):
        answers |= {cmd: ("nope" if label == wrong else want) + "\n" for label, cmd, want in llm.commands(bare)}
    if media.enabled(bare):
        answers |= {cmd: ("nope" if label == wrong else want) + "\n" for label, cmd, want in media.commands(bare)}
    if names.nameserver(bare):
        server = "192.0.2.53" if wrong == names.LABEL else names.nameserver(bare)
        answers[names.command(bare)] = dns_status(bare["domain"], server)
    if "vpn" in bare["guests"]:
        # The home router opened the port, unless this is the wrong answer.
        state = portmap_state(method=None, external=None, why={"UPnP": "no answer"}) if wrong == direct.LABEL else None
        answers |= direct_answers(bare, state)
    return answers


HOME_PUBLIC = "203.0.113.7"
ROUTER_CLOCK = 1_800_000_000


def portmap_state(**changes) -> dict:
    """What nb-portmap writes on the router once the home router opened the
    port over UPnP, a minute before ROUTER_CLOCK."""
    from nullbill.flows import TAILSCALE_PORT

    state = {
        "at": ROUTER_CLOCK - 60,
        "enabled": True,
        "port": TAILSCALE_PORT,
        "address": "192.168.1.57",
        "gateway": "192.168.1.1",
        "method": "UPnP",
        "external": f"{HOME_PUBLIC}:{TAILSCALE_PORT}",
        "why": {"NAT-PMP": "no answer"},
    }
    return state | changes


def guest_exec(text: str, exitcode: int = 0, err: str = "") -> str:
    """What `qm guest exec` prints for a command that wrote text in the VM."""
    answer = {"exitcode": exitcode, "exited": 1} | ({"out-data": text} if text else {}) | ({"err-data": err} if err else {})
    return json.dumps(answer, indent=3)


def tailscale_status(addrs: list[str] | None = None, backend: str = "Running") -> str:
    """`tailscale status --json` on the vpn guest, as far as direct.py reads it."""
    from nullbill.flows import TAILSCALE_PORT

    if addrs is None:
        addrs = [f"{HOME_PUBLIC}:{TAILSCALE_PORT}", f"10.42.6.11:{TAILSCALE_PORT}", f"[2001:db8::11]:{TAILSCALE_PORT}"]
    return json.dumps({"BackendState": backend, "Self": {"DNSName": "nullbill.example-tailnet.ts.net.", "Addrs": addrs}})


def direct_answers(plan: dict, state: dict | None = None, addrs: list[str] | None = None, backend: str = "Running") -> dict[str, str]:
    """The two commands direct.py runs on the host and what they print."""
    from nullbill import direct

    state = portmap_state() if state is None else state
    return {
        direct.vpn_command(plan): tailscale_status(addrs, backend),
        direct.router_command(plan): guest_exec(f"{json.dumps(state)}\n{ROUTER_CLOCK}\n"),
    }


def dns_status(domain: str, server: str) -> str:
    """`tailscale dns status` on a tailnet whose split DNS sends domain to
    server, in the format of Tailscale 1.104's cmd/tailscale/cli/dns-status.go."""
    return (
        "\n=== 'Use Tailscale DNS' status ===\n\nTailscale DNS: disabled.\n\n=== MagicDNS configuration ===\n\n"
        "MagicDNS: disabled tailnet-wide.\n\nResolvers (in preference order):\n"
        "  (no resolvers configured, system default will be used: see 'System DNS configuration' below)\n\n"
        f"Split DNS Routes:\n  - {domain + '.':<30s} -> {server}\n\n"
    )


def agent_api(
    plan: dict,
    extra: dict[str, set] | None = None,
    snapshot_status: str = "403",
    dispatcher_extra: dict[str, set] | None = None,
    operator_ready: bool = True,
    loki_uids: str = "1001",
):
    """A runner answering the agent's API calls like a correctly set up host:
    PVEAuditor everywhere, the agent role on pool guests. `extra` adds
    privileges per path to model a mistake; the dispatcher's token answers
    from its own grants, plus `dispatcher_extra`."""
    from nullbill import agent, ci
    from nullbill.render import strip_notes

    bare = strip_notes(plan)
    auditor = {"VM.Audit", "Sys.Audit", "Datastore.Audit", "Pool.Audit"}
    grants = {"/": set(auditor)}
    for g in bare["guests"].values():
        managed = set(agent.PRIVILEGES) if g.get("agent_managed") else set()
        if g["kind"] == "vm-pool":
            grants[f"/vms/{g['template_vmid']}"] = set(auditor)
            grants[f"/pool/{ci.POOL}"] = auditor | managed
        else:
            grants[f"/vms/{g['vmid']}"] = auditor | managed
    for path, privs in (extra or {}).items():
        grants[path] = grants.get(path, set()) | privs

    # The dispatcher's token: its own roles only, no PVEAuditor.
    dispatcher = {
        f"/pool/{ci.POOL}": set(ci.PRIVILEGES),
        f"/storage/{bare['disk']['storage']}": {"Datastore.AllocateSpace", "Datastore.Audit"},
    }
    for g in ci.pools(bare).values():
        dispatcher[f"/vms/{g['template_vmid']}"] = set(ci.SOURCE_PRIVILEGES)
    for path, privs in (dispatcher_extra or {}).items():
        dispatcher[path] = dispatcher.get(path, set()) | privs

    managed = next((n for n, g in bare["guests"].items() if g.get("agent_managed") and g["kind"] != "vm-pool"), None)

    def run(target, command):
        # The operator is set up, nb-ssh reaches the managed guest as agent
        # (uid 1001) and not the router, and Loki holds the command.
        if command == "cat /root/.ssh/authorized_keys":
            # The owner's switch is off: no agent line (tests/test_hostroot.py has it on).
            return 0, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA nullbill@nullbill\n", ""
        if "test -s /etc/nullbill/agent/pve-token || echo unset" in command:
            return 0, "" if operator_ready else "unset\n", ""
        marker = re.search(r"nb-verify-[0-9a-f]+", command)
        if " runuser -l agent -c " in command and f" {managed}.{bare['domain']} " in command:
            return 0, f"{marker[0]}\nagent\n1001\n", ""
        if " runuser -l agent -c " in command:
            return 255, "", "ssh: connect to host port 22: Connection timed out\n"
        if "loki/api/v1/query_range" in command:
            return 0, loki_uids + "\n", ""
        url = re.search(r"api2/json([^';\s]+)", command)[1]
        if url.startswith("/access/permissions?path="):
            path = url.partition("=")[2]
            held = dispatcher if ci.TOKEN_FILE in command else grants
            return 0, "200\n" + json.dumps({"data": {path: {p: 1 for p in held.get(path, set())}}}), ""
        if url == "/nodes":
            return 0, "200\n" + json.dumps({"data": [{"node": "pve"}]}), ""
        if url.endswith("/snapshot"):
            return 0, snapshot_status + "\n" + json.dumps({"data": None}), ""
        raise KeyError(command)

    return run


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
        self.dns = {"example.com": ["192.0.2.53"]}  # split DNS someone else set
        self.dns_scope = True
        self.dns_sets = []
        self.users_list = []
        self.users_scope = True
        self.deleted = []

    def devices(self):
        return self.devices_list

    def policy(self):
        return json.loads(json.dumps(self.live)), self.etag

    def validate(self, policy):
        if self.refuse:
            raise TailscaleError(f"Tailscale refused the policy: {self.refuse}")

    def set_policy(self, policy, etag):
        assert etag == self.etag
        self.pushes.append(policy)
        self.live = dict(policy, extra=True) if self.mangle else policy
        self.etag = f"e{len(self.pushes)}"

    def split_dns(self):
        if not self.dns_scope:
            raise MissingScope("reading the tailnet's split DNS: the OAuth client lacks a scope (...); give it dns")
        return dict(self.dns)

    def set_split_dns(self, domain, nameservers):
        self.dns_sets.append((domain, nameservers))
        self.dns[domain] = nameservers

    def users(self):
        if not self.users_scope:
            raise MissingScope("listing the tailnet's users: the OAuth client lacks a scope (...); give it users")
        return list(self.users_list)

    def delete_user(self, user_id):
        self.users()
        self.deleted.append(user_id)
        self.users_list = [u for u in self.users_list if u["id"] != user_id]
