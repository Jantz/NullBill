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
    storage = {"server": "10.42.1.15", "datastore": backups.DATASTORE, "username": backups.TOKEN_ID, "fingerprint": "aa:bb"}

    def run(target, command):
        if command.startswith(f"pvesh get /nodes/localhost/storage/{backups.STORAGE}/content"):
            return 0, json.dumps(content), ""
        if command == f"pvesh get /storage/{backups.STORAGE} --output-format json":
            return 0, json.dumps(storage), ""
        if "proxmox-backup-client restore" in command:
            vmid = int(re.search(r"ct/(\d+)/", command)[1])
            return 0, (hostname or by_vmid[vmid]) + "\n", ""
        raise KeyError(command)

    return run
