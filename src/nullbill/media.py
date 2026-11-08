"""The media server: what plan, apply and verify need for Jellyfin.

Jellyfin runs alone in the media zone (docs/adr/0009-media-server.md). Its
library is the disks kept from a previous OS (legacy.py): each shared
partition is bind-mounted read-only at MEDIA_ROOT/<name>, the same host
mount the files guest shares over SMB. It transcodes on the host's
integrated Intel graphics when the probe finds them, through the render
node passed in as a device, and on the CPU otherwise.
"""

import re
import shlex

from nullbill import legacy
from nullbill.probe import Runner
from nullbill.render import Note, strip_notes
from nullbill.results import Result

JELLYFIN = "jellyfin"
PORT = 8096
MEDIA_ROOT = "/media"
# The kept disks' files belong to uid 1000 in an unprivileged container
# (legacy.SHARE_UID); Jellyfin runs as that uid and group, and the render
# node is given to the group.
UID = legacy.SHARE_UID
ADMIN_USER = "nullbill-admin"
# Where the jellyfin role keeps what apply and verify read (root only).
PASSWORD_FILE = "/etc/nullbill/jellyfin-admin-password"
API_KEY_FILE = "/etc/nullbill/jellyfin-api-key"
VAINFO = "/usr/lib/jellyfin-ffmpeg/vainfo"
DEV_LINE = re.compile(r"^dev(\d+): (.+)$")


def enabled(plan: dict) -> bool:
    return JELLYFIN in plan["guests"]


def plan_section(model: dict, facts: dict, planned: dict, shares: list[str], errors: list[str]) -> Note | dict:
    """How Jellyfin transcodes, and the kept disks (their share names) it plays."""
    pin = model.get("media", {}).get("transcode", "auto")
    if JELLYFIN not in planned:
        if "transcode" in model.get("media", {}):
            errors.append("media.transcode: the media workload is not planned")
        return Note({}, "Not planned.")
    igpu = next((g for g in facts["gpus"] if g["integrated"] and g["vendor"] == "intel"), None)
    if pin == "cpu":
        transcode = Note("cpu", "Pinned in config.yaml.")
    elif igpu is None:
        transcode = Note("cpu", "No integrated Intel graphics in the probe; switch them on in the firmware to transcode there.")
    else:
        transcode = Note(
            {"gpu": igpu["pci"], "name": igpu["name"]},
            "The host's integrated Intel graphics, with Quick Sync; apply passes their render node to Jellyfin.",
        )
    if shares:
        why = "Kept disks, read-only. Add each folder as a library in Jellyfin."
    else:
        why = "No kept disk to play yet (disks.<id>.use: legacy); Jellyfin starts with an empty library."
    return {"transcode": transcode, "library": Note([f"{MEDIA_ROOT}/{n}" for n in shares], why)}


def library(plan: dict) -> list[str]:
    """The kept disks' share names: one folder each under MEDIA_ROOT."""
    return [m["share"] for m in legacy.shares(plan)]


def igpu(plan: dict) -> str | None:
    """The integrated GPU's PCI address when Jellyfin transcodes there."""
    transcode = (plan.get("media") or {}).get("transcode")
    return transcode["gpu"] if isinstance(transcode, dict) else None


def mount_point(m: dict) -> str:
    """A kept partition as a bind mount point of the Jellyfin guest. Always
    read-only, even where the files guest may write."""
    return f"{m['mount']},mp={MEDIA_ROOT}/{m['share']},backup=0,ro=1"


def render_node(pci: str) -> str:
    """Host command printing the render node of the GPU at pci, by its fixed
    by-path name; renderD numbers follow probe order."""
    return f"readlink -e /dev/dri/by-path/pci-{pci}-render || true"


def device(node: str) -> str:
    return f"{node},gid={UID},mode=0660"


def current_devices(pct_config: str) -> dict[int, str]:
    found = {}
    for line in pct_config.splitlines():
        m = DEV_LINE.match(line.strip())
        if m:
            found[int(m[1])] = m[2]
    return found


# Jellyfin answers in camelCase unless asked for the names its API documents.
ACCEPT = 'Accept: application/json; profile="PascalCase"'


def _api(path: str) -> str:
    key = f'"Authorization: MediaBrowser Token=\\"$(cat {API_KEY_FILE})\\""'
    return f"curl -sS --max-time 10 -H '{ACCEPT}' -H {key} http://127.0.0.1:{PORT}{path}"


def _checks(plan: dict) -> list[tuple[str, str, str]]:
    """(label, command in the Jellyfin guest, expected output)."""
    info = f"curl -sS --max-time 10 -H '{ACCEPT}' http://127.0.0.1:{PORT}/System/Info/Public"
    field = "python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])'"
    out = [
        ("jellyfin answers with its first-run setup done", f"{info} | {field} StartupWizardCompleted", "True"),
        (
            "jellyfin shows nothing without sign-in",
            f"code=$(curl -sS --max-time 10 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{PORT}/Users); "
            'case $code in 401 | 403) echo refused ;; *) echo "$code" ;; esac',
            "refused",
        ),
    ]
    for name in library(plan):
        path = shlex.quote(f"{MEDIA_ROOT}/{name}")
        # access(2) answers EROFS for a read-only mount: nothing is written.
        test = f"test -r {path} && test -x {path} && ! test -w {path} && echo read-only"
        as_jellyfin = f"runuser -u {JELLYFIN} -- sh -c {shlex.quote(test)}"
        out.append((f"jellyfin reads {MEDIA_ROOT}/{name} and cannot write to it", as_jellyfin, "read-only"))
    if igpu(plan):
        encoding = f"{_api('/System/Configuration/encoding')} | {field}"
        out.append(("jellyfin transcodes with Quick Sync", f"{encoding} HardwareAccelerationType", "qsv"))
        vainfo = f'{VAINFO} --display drm --device "$({encoding} VaapiDevice)" 2>&1'
        probe = f"if runuser -u {JELLYFIN} -- {vainfo} | grep -q VAEntrypointEncSlice; then echo encodes; else echo none; fi"
        out.append(("jellyfin's transcoder opens the integrated graphics", probe, "encodes"))
    return out


def commands(plan: dict) -> list[tuple[str, str, str]]:
    vmid = plan["guests"][JELLYFIN]["vmid"]
    return [(label, f"pct exec {vmid} -- bash -c {shlex.quote(command)}", want) for label, command, want in _checks(plan)]


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    return [Result("SKIP", label, reason) for label, _, _ in commands(plan)] if enabled(plan) else []


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if not enabled(plan):
        return []
    results = []
    for label, command, want in commands(plan):
        rc, out, err = run(target, command)
        got = out.strip()
        results.append(Result("PASS", label) if got == want else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:120]))
    return results
