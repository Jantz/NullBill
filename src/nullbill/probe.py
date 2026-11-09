"""Read-only hardware probe of a PVE host over SSH, and the parser for its output.

collect() stores each command's raw output; parse() turns a raw directory into
facts. Splitting them lets the golden tests feed synthetic raw output through
the same parser the real host uses.
"""

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from nullbill.errors import NullBillError

# One NTFS partition, $d, as Windows left it. Each tool only reads, also
# while the partition is mounted read-only (tools/host-check compares the
# image before and after): what ntfs-3g would say to a read-write mount,
# the volume's flags, and a fingerprint of the journal's two restart pages,
# which name the Windows session that wrote them.
NTFS_CHECK = (
    'echo "== $d"; ntfs-3g.probe --readwrite "$d" 2>&1; echo "probe=$?"; '
    "ntfsinfo -f -m \"$d\" 2>/dev/null | grep -i 'volume flags'; "
    'echo "journal=j$(ntfscat -f -i 2 "$d" 2>/dev/null | head -c 8192 | sha256sum | cut -c1-12)"'
)

# name -> (command, required). Every command is read-only.
COMMANDS: dict[str, tuple[str, bool]] = {
    "pveversion": ("pveversion", True),
    "lscpu": ("lscpu -J", True),
    "meminfo": ("cat /proc/meminfo", True),
    "lsblk": ("lsblk -J -b -o NAME,SIZE,TYPE,ROTA,FSTYPE,PTTYPE,PARTTYPE,PARTN,LABEL,UUID,FSUSED,MOUNTPOINTS", True),
    "disk-by-id": ("find /dev/disk/by-id -maxdepth 1 -type l -printf '%f %l\\n'", True),
    "storage": ("pvesh get /nodes/localhost/storage --output-format json", True),
    "lspci": ("lspci -Dnn", True),
    "iommu": ("find /sys/kernel/iommu_groups -mindepth 3 -maxdepth 3 -type l", False),
    "nvidia-smi": ("nvidia-smi --query-gpu=pci.bus_id,name,memory.total --format=csv,noheader,nounits", False),
    # SMART health and attributes of every disk, read only; smartctl is part
    # of PVE. A disk that answers nothing is reported as unknown.
    "smart": (
        "command -v smartctl >/dev/null || exit 127; "
        'for d in $(lsblk -dnpo NAME,TYPE | awk \'$2 == "disk" {print $1}\'); do echo "== $d"; smartctl -j -H -A "$d"; done; true',
        False,
    ),
    # Copies apply made of kept disks before converting them
    # (nb-legacy-copy); none until then.
    "copies": ("cat /var/lib/nullbill/kept-copies 2>/dev/null; true", False),
    # Which LVM volume group each physical volume is in, so a disk apply
    # made storage or the PBS disk is known as such, not as a disk with
    # signatures.
    "pvs": ("pvs --reportformat json -o pv_name,vg_name", False),
    # Whether Windows left each NTFS partition of a real disk (one with a
    # by-id name) fit to be changed; none until apply has installed ntfs-3g
    # for a kept NTFS disk.
    "ntfs": (
        "command -v ntfs-3g.probe >/dev/null || exit 127; "
        'for p in /dev/disk/by-id/*-part*; do d=$(readlink -f "$p"); '
        'case " $seen " in *" $d "*) continue ;; esac; seen="$seen $d"; '
        '[ "$(blkid -p -o value -s TYPE "$d" 2>/dev/null)" = ntfs ] || continue; '
        f"{NTFS_CHECK}; done; true",
        False,
    ),
}

Runner = Callable[[str, str], tuple[int, str, str]]


SSH_TIMEOUT_S = 120


def ssh_run(target: str, command: str) -> tuple[int, str, str]:
    """A command that outlives SSH_TIMEOUT_S is a failed command (exit 124,
    as timeout(1) says), not a traceback that ends verify without a word."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command],
            capture_output=True,
            text=True,
            timeout=SSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        return 124, out, f"no answer within {SSH_TIMEOUT_S} s"
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


# Where apply mounts a disk kept from a previous OS (legacy.py). Such a
# mount does not make the disk a system disk.
LEGACY_ROOT = "/mnt/nb-legacy"


def _legacy_mount(path: str) -> bool:
    return path.startswith(LEGACY_ROOT + "/")


def _partitions(dev: dict, ntfs: dict[str, tuple[str | None, str | None]]) -> list[dict]:
    parts = []
    for child in dev.get("children", []):
        if child.get("type") != "part":
            continue
        mounts = [m for m in child.get("mountpoints") or [] if m]
        state, journal = ntfs.get(child.get("name"), (None, None))
        parts.append(
            {
                "n": child.get("partn"),
                "size_mib": int(child["size"]) // 2**20,
                "fstype": child.get("fstype"),
                # A GPT type GUID, or an MBR type code such as 0x7.
                "parttype": (child.get("parttype") or "").lower() or None,
                "label": child.get("label"),
                "uuid": child.get("uuid"),
                "mounts": mounts,
                # Bytes in use; known only while mounted.
                "used_bytes": int(child["fsused"]) if child.get("fsused") is not None else None,
                # NTFS only: whether Windows left it fit to be changed
                # (NTFS_STATES; None when not read), and its journal's
                # fingerprint, which a pin to discard that journal names.
                "rw_state": state,
                "journal": journal,
            }
        )
    return sorted(parts, key=lambda p: p["n"] or 0)


# What a read-write mount of an NTFS partition would meet. Only "clean"
# mounts read-write; "unclean" does once the owner discards the journal
# (legacy.py); the others need Windows.
NTFS_STATES = ("clean", "unclean", "hibernated", "chkdsk", "damaged")
NO_JOURNAL = "j" + hashlib.sha256(b"").hexdigest()[:12]


def ntfs_state(text: str) -> tuple[str | None, str | None]:
    """NTFS_CHECK's output for one partition: its state, or None when the
    tools could not tell, and the journal's fingerprint.

    ntfs-3g.probe exits 14 for a hibernated Windows on the volume itself
    (hiberfil.sys) and for a journal Windows keeps open (Fast Startup,
    hibernation with the system on another disk, a pulled cable; the journal
    is version 2.0 then), and 15 for an older journal left unclean. It passes
    a volume Windows marked for chkdsk, which the volume's dirty flag tells."""
    probe = re.search(r"^probe=(\d+)$", text, re.MULTILINE)
    flags = re.search(r"volume flags:\s*0x([0-9a-f]+)", text, re.IGNORECASE)
    journal = re.search(r"^journal=(j[0-9a-f]{12})$", text, re.MULTILINE)
    rc = int(probe.group(1)) if probe else None
    state = None
    if rc == 13:
        state = "damaged"
    elif flags and int(flags.group(1), 16) & 1:
        state = "chkdsk"
    elif rc == 14 and "Windows is hibernated" in text:
        state = "hibernated"
    elif rc in (14, 15):
        state = "unclean"
    elif rc == 0 and flags:
        state = "clean"
    # The hash of nothing names no journal: ntfscat could not read it.
    return state, journal.group(1) if journal and journal.group(1) != NO_JOURNAL else None


def _ntfs(text: str | None) -> dict[str, tuple[str | None, str | None]]:
    """kernel name (sda2) -> state and journal fingerprint."""
    parts = re.split(r"^== (/dev/\S+)$", text or "", flags=re.MULTILINE)
    return {Path(device).name: ntfs_state(body) for device, body in zip(parts[1::2], parts[2::2], strict=True)}


def _copies(text: str | None) -> list[dict]:
    """nb-legacy-copy's records: NAME VOLUME SIZE_GIB FILES BYTES DISK per line."""
    out = []
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) != 6 or line.startswith("#"):
            continue
        name, volume, size_gib, files, size, disk = fields
        out.append({"name": name, "volume": volume, "size_gib": int(size_gib), "files": int(files), "bytes": int(size), "disk": disk})
    return sorted(out, key=lambda c: c["name"])


def _smart(text: str | None, ids: dict[str, str]) -> dict[str, dict]:
    """by-id name -> health (PASSED, FAILED or unknown), power-on hours and
    reallocated sectors, from `smartctl -j -H -A` per disk."""
    out: dict[str, dict] = {}
    parts = re.split(r"^== (/dev/\S+)$", text or "", flags=re.MULTILINE)
    for device, body in zip(parts[1::2], parts[2::2], strict=True):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        passed = (data.get("smart_status") or {}).get("passed")
        attributes = {a.get("id"): a for a in (data.get("ata_smart_attributes") or {}).get("table", [])}
        reallocated = (attributes.get(5) or {}).get("raw", {}).get("value")
        name = Path(device.strip()).name
        out[ids.get(name, name)] = {
            "health": "unknown" if passed is None else "PASSED" if passed else "FAILED",
            "power_on_hours": (data.get("power_on_time") or {}).get("hours"),
            "reallocated_sectors": reallocated,
        }
    return out


def _volume_groups(text: str | None) -> dict[str, str]:
    """pvs: kernel device name (sdb, nvme0n1p3) -> volume group."""
    out = {}
    for report in json.loads(text).get("report", []) if text else []:
        for pv in report.get("pv", []):
            if pv.get("vg_name"):
                out[Path(pv["pv_name"]).name] = pv["vg_name"]
    return out


def _disks(
    lsblk_text: str, by_id_text: str, smart_text: str | None = None, pvs_text: str | None = None, ntfs_text: str | None = None
) -> list[dict]:
    ids = _disk_ids(by_id_text)
    ntfs = _ntfs(ntfs_text)
    smart = _smart(smart_text, ids)
    groups = _volume_groups(pvs_text)
    disks = []
    for dev in json.loads(lsblk_text)["blockdevices"]:
        if dev["type"] != "disk":
            continue
        nodes = list(_walk(dev))
        signatures = sorted({s for n in nodes for s in (n.get("pttype"), n.get("fstype")) if s})
        mounted = any(m and not _legacy_mount(m) for n in nodes for m in (n.get("mountpoints") or []))
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
                "partitions": _partitions(dev, ntfs),
                "vgs": sorted({groups[n["name"]] for n in nodes if n.get("name") in groups}),
                "smart": smart.get(ids.get(dev["name"], dev["name"]), {"health": "unknown"}),
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
        "disks": _disks(texts["lsblk"], texts["disk-by-id"], texts["smart"], texts["pvs"], texts["ntfs"]),
        "storage": _storage(texts["storage"]),
        "gpus": _gpus(texts["lspci"], texts["iommu"], texts["nvidia-smi"]),
        "copies": _copies(texts["copies"]),
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
