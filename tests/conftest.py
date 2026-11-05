import json
import re
import time
from pathlib import Path

import pytest
import yaml

from nullbill.probe import COMMANDS, parse

FIXTURES = Path(__file__).parent / "fixtures"
CLASSES = ["small", "mid", "large"]


def write_raw(raw: Path, fixture: str) -> Path:
    """Materialise a fixture as the raw directory probe.collect() would write."""
    spec = yaml.safe_load((FIXTURES / f"{fixture}.yaml").read_text())
    raw.mkdir(parents=True, exist_ok=True)
    for name in COMMANDS:
        entry = spec.get(name, {"rc": 1, "err": "not in fixture\n"})
        (raw / f"{name}.out").write_text(entry.get("out", ""))
        (raw / f"{name}.err").write_text(entry.get("err", ""))
        (raw / f"{name}.rc").write_text(f"{entry.get('rc', 0)}\n")
    return raw


def fixture_config(fixture: str) -> dict:
    return yaml.safe_load((FIXTURES / f"{fixture}.config.yaml").read_text()) or {}


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

    from nullbill import ci, forge
    from nullbill.render import strip_notes

    bare = strip_notes(plan)
    checks = [(label, guest, cmd, want) for label, guest, cmd, want in forge._checks(bare)] if forge.enabled(bare) else []
    if ci.enabled(bare):
        checks += [(label, "runner-dispatcher", cmd, want) for label, cmd, want in ci._service_checks(bare)]
    return {
        f"pct exec {bare['guests'][guest]['vmid']} -- bash -c {shlex.quote(cmd)}": ("nope" if label == wrong else want) + "\n"
        for label, guest, cmd, want in checks
    }


def agent_api(
    plan: dict, extra: dict[str, set] | None = None, snapshot_status: str = "403", dispatcher_extra: dict[str, set] | None = None
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

    def run(target, command):
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
