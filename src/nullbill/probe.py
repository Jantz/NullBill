"""Read-only hardware probe of a PVE host over SSH, and the parser for its output.

collect() stores each command's raw output; parse() turns a raw directory into
facts. Splitting them lets the golden tests feed synthetic raw output through
the same parser the real host uses.
"""

import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from nullbill.errors import NullBillError

# name -> (command, required). Every command is read-only.
COMMANDS: dict[str, tuple[str, bool]] = {
    "pveversion": ("pveversion", True),
    "lscpu": ("lscpu -J", True),
    "meminfo": ("cat /proc/meminfo", True),
    "lsblk": ("lsblk -J -b -o NAME,SIZE,TYPE,ROTA,FSTYPE,PTTYPE,MOUNTPOINTS", True),
    "disk-by-id": ("find /dev/disk/by-id -maxdepth 1 -type l -printf '%f %l\\n'", True),
    "storage": ("pvesh get /nodes/localhost/storage --output-format json", True),
    "lspci": ("lspci -Dnn", True),
    "iommu": ("find /sys/kernel/iommu_groups -mindepth 3 -maxdepth 3 -type l", False),
    "nvidia-smi": ("nvidia-smi --query-gpu=pci.bus_id,name,memory.total --format=csv,noheader,nounits", False),
}

Runner = Callable[[str, str], tuple[int, str, str]]


def ssh_run(target: str, command: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout, proc.stderr


def collect(target: str, probe_root: Path, run: Runner = ssh_run) -> Path:
    """Run every probe command and store raw output under a new timestamped dir."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = probe_root / stamp / "raw"
    raw.mkdir(parents=True, mode=0o700)
    failed = []
    for name, (command, required) in COMMANDS.items():
        rc, out, err = run(target, command)
        (raw / f"{name}.out").write_text(out)
        (raw / f"{name}.err").write_text(err)
        (raw / f"{name}.rc").write_text(f"{rc}\n")
        if rc != 0 and required:
            failed.append(f"{name}: `{command}` exited {rc}: {err.strip()[:200]}")
    if failed:
        raise NullBillError(f"probe failed, raw output kept in {raw}", *failed)
    return raw.parent


def _read(raw: Path, name: str) -> str | None:
    rc = int((raw / f"{name}.rc").read_text().strip())
    return (raw / f"{name}.out").read_text() if rc == 0 else None


def _lscpu_fields(entries: list[dict]) -> dict[str, str]:
    fields = {}
    for entry in entries:
        fields[entry["field"].rstrip(":")] = entry.get("data")
        fields.update(_lscpu_fields(entry.get("children", [])))
    return fields


def _cpu(text: str) -> dict:
    f = _lscpu_fields(json.loads(text)["lscpu"])
    return {
        "model": f.get("Model name"),
        "threads": int(f["CPU(s)"]),
        "sockets": int(f.get("Socket(s)") or 1),
    }


def _memory_mib(text: str) -> int:
    match = re.search(r"^MemTotal:\s+(\d+) kB$", text, re.MULTILINE)
    if not match:
        raise NullBillError("meminfo: no MemTotal line")
    return int(match.group(1)) // 1024


ID_PREFERENCE = ("nvme-eui.", "wwn-", "nvme-", "ata-", "scsi-")


def _disk_ids(text: str) -> dict[str, str]:
    """kernel name -> most stable by-id name."""
    candidates: dict[str, list[str]] = {}
    for line in text.splitlines():
        link, _, target = line.partition(" ")
        if "-part" in link or not target:
            continue
        candidates.setdefault(Path(target).name, []).append(link)

    def rank(link: str) -> tuple[int, str]:
        for i, prefix in enumerate(ID_PREFERENCE):
            if link.startswith(prefix):
                return i, link
        return len(ID_PREFERENCE), link

    return {name: min(links, key=rank) for name, links in candidates.items()}


def _walk(dev: dict):
    yield dev
    for child in dev.get("children", []):
        yield from _walk(child)


def _disks(lsblk_text: str, by_id_text: str) -> list[dict]:
    ids = _disk_ids(by_id_text)
    disks = []
    for dev in json.loads(lsblk_text)["blockdevices"]:
        if dev["type"] != "disk":
            continue
        nodes = list(_walk(dev))
        signatures = sorted({s for n in nodes for s in (n.get("pttype"), n.get("fstype")) if s})
        mounted = any(m for n in nodes for m in (n.get("mountpoints") or []) if m)
        # A disk holding the PVE volume group serves the host even when the
        # LVs that use it are not mounted.
        holds_pve = any(n.get("name", "").startswith("pve-") for n in nodes)
        disks.append(
            {
                "id": ids.get(dev["name"], dev["name"]),
                "stable_id": dev["name"] in ids,
                "size_gib": int(dev["size"]) // 2**30,
                "rotational": bool(dev.get("rota")),
                "system": mounted or holds_pve,
                "signatures": signatures,
            }
        )
    return sorted(disks, key=lambda d: d["id"])


def _storage(text: str) -> list[dict]:
    out = []
    for s in json.loads(text):
        if not s.get("active") or not s.get("enabled", 1):
            continue
        out.append(
            {
                "name": s["storage"],
                "type": s["type"],
                "content": sorted(s.get("content", "").split(",")),
                "avail_gib": int(s.get("avail", 0)) // 2**30,
            }
        )
    return sorted(out, key=lambda s: s["name"])


GPU_CLASSES = {"0300", "0302", "0380"}
VENDORS = {"10de": "nvidia", "1002": "amd", "8086": "intel"}
LSPCI_LINE = re.compile(r"^(?P<addr>\S+) .*?\[(?P<cls>[0-9a-f]{4})\]: (?P<name>.+) \[(?P<ven>[0-9a-f]{4}):(?P<dev>[0-9a-f]{4})\]")


def _iommu(text: str | None) -> dict[str, int]:
    groups = {}
    for line in (text or "").splitlines():
        parts = line.strip().split("/")
        # /sys/kernel/iommu_groups/<group>/devices/<addr>
        if len(parts) >= 7 and parts[-2] == "devices":
            groups[parts[-1]] = int(parts[-3])
    return groups


def _nvidia_vram(text: str | None) -> dict[str, int]:
    vram = {}
    for line in (text or "").splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) == 3 and cols[2].isdigit():
            vram[cols[0].lower()[-12:]] = int(cols[2])
    return vram


def _gpus(lspci_text: str, iommu_text: str | None, nvidia_text: str | None) -> list[dict]:
    groups = _iommu(iommu_text)
    vram = _nvidia_vram(nvidia_text)
    gpus = []
    for line in lspci_text.splitlines():
        m = LSPCI_LINE.match(line)
        if not m or m["cls"] not in GPU_CLASSES:
            continue
        addr = m["addr"]
        group = groups.get(addr)
        peers = sorted(a for a, g in groups.items() if g == group and a != addr) if group is not None else []
        gpus.append(
            {
                "pci": addr,
                "vendor": VENDORS.get(m["ven"], m["ven"]),
                # Vendor and device ID, as a PVE PCI mapping names the device.
                "id": f"{m['ven']}:{m['dev']}",
                "name": re.sub(r" \[[0-9a-f]{4}\]$", "", m["name"]),
                # Bus 00 holds integrated graphics; those are left to the host.
                "integrated": addr.split(":")[1] == "00",
                "vram_mib": vram.get(addr),
                "iommu_group": group,
                "iommu_peers": peers,
            }
        )
    return gpus


def parse(raw: Path) -> dict:
    texts = {name: _read(raw, name) for name in COMMANDS}
    missing = [n for n, (_, required) in COMMANDS.items() if required and texts[n] is None]
    if missing:
        raise NullBillError(f"{raw}: required probe output missing: {', '.join(missing)}")
    version = re.search(r"pve-manager/(\d+\.\d+(?:\.\d+)?)", texts["pveversion"])
    return {
        "pve_version": version.group(1) if version else None,
        "cpu": _cpu(texts["lscpu"]),
        "memory_mib": _memory_mib(texts["meminfo"]),
        "disks": _disks(texts["lsblk"], texts["disk-by-id"]),
        "storage": _storage(texts["storage"]),
        "gpus": _gpus(texts["lspci"], texts["iommu"], texts["nvidia-smi"]),
    }


def run_probe(target: str, home: Path, run: Runner = ssh_run) -> Path:
    probe_root = home / "probe"
    probe_root.mkdir(mode=0o700, exist_ok=True)
    run_dir = collect(target, probe_root, run)
    facts = parse(run_dir / "raw")
    (run_dir / "facts.json").write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n")
    latest = probe_root / "latest"
    latest.unlink(missing_ok=True)
    latest.symlink_to(run_dir.name)
    return run_dir
