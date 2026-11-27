"""The owner's desktop: Fedora KDE Plasma in a VM, on a monitor, keyboard
and mouse plugged into the server and by Remote Desktop over Tailscale
(docs/adr/0023-desktop.md).

A container cannot drive a monitor, so the desktop is a VM, and a VM can
only use a graphics card that is wholly its own. So the desktop borrows
one: while it runs, the card (every function of it, its HDMI audio too)
and the USB ports the plan names are the VM's, and what uses the card on
the host (the local models) is stopped first; when the desktop stops, the
card goes back to the host and those start again. The host's nb-power
does the handing over (power.py's `gpu` rules), and nothing takes the card
without the owner's yes on the page. desktop.gpu: false keeps every card
with the host; the desktop then has a virtual screen, reached by Remote
Desktop alone.

In the guest, Fedora's own drivers (Mesa: NVK on NVIDIA, radeonsi on AMD,
iris on Intel), so a daily update never leaves the screen without a
driver; KDE's own RDP server, KRDP, which signs in with the desktop
user's password and gives a remote client a screen of its own size while
the server's monitor goes dark; Plasma Login Manager signs that user in at
boot and Plasma locks at once, so a session is there to connect to and
the monitor shows a lock screen.
"""

import json
import re
import shlex
from typing import Any

from nullbill.probe import Runner
from nullbill.render import Note, strip_notes
from nullbill.results import Result

NAME = "desktop"
PORT = 3389
# The desktop user's password: Remote Desktop's and the lock screen's.
PASSWORD = "desktop_password"
# Who signs in: the Linux account the role makes.
USER_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
DEFAULT_USER = "owner"
# The host's services that use a card and stop while the desktop has it.
GPU_USERS = ("inference",)
# Ansible's modules run under this Python in the desktop: ansible-core
# 2.19's own module code fails on Fedora 45's Python 3.15 (dataclasses has
# no _is_type), and Fedora keeps 3.13 as a package of its own. apply's
# desktop stage installs it first (PREPARE).
PYTHON = "/usr/bin/python3.13"
PREPARE = f"test -x {PYTHON} || sudo dnf -y -q install python3.13"
# PVE gives a VM at most this many USB devices (usb0 to usb13).
MAX_USB = 14
# A keyboard or a mouse: USB HID boot interfaces (class 03, protocol 1 or 2).
INPUT = {"keyboard", "mouse"}


def planned(plan: dict) -> bool:
    return NAME in plan.get("guests", {})


def section(plan: dict) -> dict:
    return plan.get("desktop") or {}


def user(model: dict) -> str:
    """The desktop's Linux account: the owner's name from people, when it is
    one Linux takes, else `owner`."""
    owners = sorted(name for name, p in model["people"].items() if p["role"] == "owner")
    name = owners[0] if owners else DEFAULT_USER
    return name if USER_PATTERN.match(name) and name not in ("root", "nullbill") else DEFAULT_USER


def card(model: dict, facts: dict, errors: list[str]) -> dict | None:
    """The discrete card the desktop borrows, or None (desktop.gpu: false,
    or the host has none)."""
    want = model["desktop"]
    if not want["gpu"]:
        return None
    discrete = [g for g in facts["gpus"] if not g["integrated"]]
    if "card" in want:
        found = [g for g in discrete if g["pci"] == want["card"]]
        if not found:
            errors.append(f"desktop.card: no discrete graphics card at {want['card']} in the probe")
            return None
        return found[0]
    return discrete[0] if discrete else None


def lend(model: dict, facts: dict, planned_services: dict, gpus: dict, errors: list[str]) -> str | None:
    """Mark the card the desktop borrows in the plan's GPU section: it stays
    the host's (its consumers keep their budgets) and is the desktop's
    while it runs. Returns its address. A VM takes all of a card's IOMMU
    group, so the group must hold that card alone; PCI bridges, which VFIO
    leaves to the host, are not counted (probe.py)."""
    if NAME not in planned_services:
        return None
    found = card(model, facts, errors)
    if found is None:
        return None
    pci = found["pci"]
    entry = gpus.get(pci)
    if entry is None:
        return None
    if entry.value["owner"].startswith("vm:"):
        errors.append(
            f"gpus.{pci}.owner: the card is passed to {entry.value['owner'][3:]}; "
            "the desktop borrows a card the host keeps (or set desktop.gpu: false)"
        )
        return None
    slot = pci.rsplit(".", 1)[0]
    foreign = [p for p in found["iommu_peers"] if p.rsplit(".", 1)[0] != slot]
    if found["iommu_group"] is None:
        errors.append(
            f"desktop: the desktop borrows {found['name']} through IOMMU, which is off or unreported; "
            "turn on VT-d (Intel) or AMD-Vi/IOMMU in the server's firmware settings, or set desktop.gpu: false"
        )
        return None
    if foreign:
        errors.append(
            f"desktop: {found['name']} shares its IOMMU group {found['iommu_group']} with {', '.join(foreign)}, "
            "which would go to the desktop with it; move the card to another slot, or set desktop.gpu: false"
        )
        return None
    value = dict(entry.value)
    value |= {"pci_id": found["id"], "iommu_group": found["iommu_group"], "lent_to": NAME}
    gpus[pci] = Note(value, entry.why + " While the desktop runs it is the desktop's, and what uses it on the host stops.")
    return pci


def input_ports(facts: dict) -> list[str]:
    """The USB ports where the probe saw a keyboard or a mouse."""
    return sorted({d["port"] for d in facts.get("usb") or [] if INPUT & set(d["kinds"])}, key=_port_key)


def _port_key(port: str) -> list[int]:
    return [int(n) for n in re.split(r"[-.]", port)]


def plan_section(model: dict, facts: dict, planned_services: dict, gpus: dict, lent: str | None, errors: list[str]) -> Any:
    """The plan's `desktop`: who signs in, which card and USB ports it
    borrows, what stops while it runs."""
    if NAME not in planned_services:
        return Note({}, "Not planned.")
    want = model["desktop"]
    rule = model["power"][NAME]
    takes = {s: model["power"][planned_services[s]["workload"]]["title"] for s in GPU_USERS if s in planned_services} if lent else {}
    if takes and rule["run"] != "on-demand":
        errors.append(
            f"power.{NAME}.run: the desktop borrows the graphics card {', '.join(takes.values())} uses, so it starts on the page and "
            "runs on demand; keep run: on-demand, or set desktop.gpu: false"
        )
    if lent and model["host"]["console"]["enabled"] and not any(g["integrated"] for g in facts["gpus"]):
        others = [p for p, e in gpus.items() if p != lent and e.value["owner"] == "host-shared"]
        if not others:
            errors.append(
                "host.console: the on-site screen needs a graphics card the host keeps, and the desktop borrows the only one; "
                "the desktop is the server's screen while it runs: turn host.console off, or set desktop.gpu: false"
            )
    usb = list(want["usb"]) if "usb" in want else input_ports(facts)
    if len(usb) > MAX_USB:
        errors.append(f"desktop.usb: a VM takes at most {MAX_USB} USB devices; {len(usb)} ports are named")
    found = {d["port"] for d in facts.get("usb") or []}
    out: dict[str, Any] = {
        "user": Note(user(model), "Signs in on the server's screen and by Remote Desktop, with the password on the page (Services)."),
        "port": Note(PORT, "KDE's own Remote Desktop server, for the owner over Tailscale."),
    }
    if lent:
        entry = gpus[lent].value
        out["gpu"] = Note(
            {"pci": lent, "name": entry["name"], "vendor": entry.get("vendor") or _vendor(facts, lent), "takes_from": takes},
            "Borrowed while the desktop runs: plug the monitor into this card."
            + (f" {', '.join(takes.values())} stops while the desktop has it and starts again after." if takes else ""),
        )
    else:
        why = "desktop.gpu is false" if not want["gpu"] else "the server has no discrete graphics card"
        out["gpu"] = Note({}, f"No graphics card ({why}): a virtual screen, reached by Remote Desktop alone.")
    out["usb"] = Note(
        usb,
        "Whatever is plugged into these USB ports is the desktop's while it runs, the host's otherwise."
        if usb
        else "No USB ports: plug a keyboard and mouse into the server and look at the hardware again, or name ports in desktop.usb.",
    )
    gone = [p for p in usb if p not in found]
    if gone and facts.get("usb") is not None:
        out["usb_missing"] = Note(gone, "Nothing is plugged into these now; they are the desktop's all the same.")
    return out


def _vendor(facts: dict, pci: str) -> str:
    return next((g["vendor"] for g in facts["gpus"] if g["pci"] == pci), "")


def lent_card(plan: dict) -> str | None:
    """The PCI address of the card the desktop borrows, or None."""
    return next((pci for pci, g in plan.get("gpus", {}).items() if g.get("lent_to") == NAME), None)


def takes_from(plan: dict) -> dict[str, str]:
    """The host's services that stop while the desktop has the card, with
    their workloads' titles."""
    return dict((section(plan).get("gpu") or {}).get("takes_from", {}))


def sentence(plan: dict) -> str:
    """What the desktop does to the graphics card, for the page and the agent."""
    gpu = section(plan).get("gpu") or {}
    if not gpu:
        return "It has a virtual screen and no graphics card of its own; you reach it by Remote Desktop."
    text = f"While it runs it has the {gpu['name']} to itself: its monitor outputs are the desktop's screen."
    if gpu.get("takes_from"):
        text += " Starting it stops the local AI models, and stopping it starts them again."
    return text


def variables(plan: dict, password: str) -> dict:
    """The desktop role's inputs, but for its certificate (desktop_cert and
    desktop_key: the browser CA's, which the run signs)."""
    gpu = section(plan).get("gpu") or {}
    return {
        "desktop_user": section(plan)["user"],
        "desktop_password": password,
        "desktop_port": PORT,
        "desktop_gpu_vendor": gpu.get("vendor", ""),
    }


# The run signs these; a role's input all the same (apply.BROWSER_CERT).
CERT_INPUTS = ("desktop_cert", "desktop_key")
USB_LINE = re.compile(r"^usb(\d+):\s*host=([0-9]+-[0-9.]+)\s*(?:,.*)?$")


def usb_now(config: str) -> dict[int, str]:
    """`qm config`'s USB passthrough: usbN -> host port."""
    found = {}
    for line in config.splitlines():
        m = USB_LINE.match(line.strip())
        if m:
            found[int(m.group(1))] = m.group(2)
    return found


def usb_changes(config: str, ports: list[str]) -> tuple[list[int], dict[int, str]]:
    """What `qm set` changes so the VM has the USB ports named, and no
    other: the usbN entries to delete and those to set. A port that is
    already there keeps its entry."""
    now = usb_now(config)
    keep = {n: p for n, p in now.items() if p in ports}
    delete = sorted(n for n in now if n not in keep)
    free = [n for n in range(MAX_USB) if n not in keep]
    want = [p for p in ports if p not in keep.values()]
    return delete, dict(zip(free, want, strict=False))


# The kernel driver Fedora's Mesa sits on, by the card's vendor; "" is
# the virtual screen's.
DRIVERS = {"nvidia": ("nouveau",), "amd": ("amdgpu", "radeon"), "intel": ("i915", "xe"), "": ("virtio_gpu",)}
REPORT = "/usr/local/sbin/nb-desktop report"


def _names(plan: dict) -> list[str]:
    gpu = section(plan).get("gpu") or {}
    names = [
        "desktop: a session is signed in, to connect to",
        f"desktop: KDE's Remote Desktop server listens on {PORT}",
        f"desktop: Fedora's own driver drives {gpu['name']}" if gpu else "desktop: the virtual screen has its driver",
    ]
    if gpu.get("takes_from"):
        names.append(f"desktop: {', '.join(gpu['takes_from'].values())} stops while the desktop has the card")
    return names


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    return [Result("SKIP", name, reason) for name in _names(plan)] if planned(plan) else []


def check(plan_with_notes: dict, target: str, run: Runner, off: dict[str, str], found: dict | None) -> list[Result]:
    """verify's look inside the desktop, through the guest agent, when it
    runs; verify never starts it. found: nb-power's status."""
    plan = strip_notes(plan_with_notes)
    if not planned(plan):
        return []
    if NAME in off:
        return skipped(plan, f"{off[NAME]} is off (it runs on demand); start it on the page, under Services, to check it")
    names = _names(plan)
    vmid = plan["guests"][NAME]["vmid"]
    user = section(plan)["user"]
    rc, out, err = run(target, f"qm guest exec {vmid} --timeout 30 -- {REPORT} {shlex.quote(user)}")
    try:
        report = json.loads(json.loads(out)["out-data"]) if rc == 0 else None
    except (ValueError, KeyError, TypeError):
        report = None
    if not isinstance(report, dict):
        why = f"its guest agent gave no report ({(err.strip() or out.strip() or f'exit {rc}')[:100]})"
        return [Result("FAIL", name, why) for name in names]
    results = [
        Result("PASS", names[0])
        if report.get("session")
        else Result("FAIL", names[0], f"{user} is not signed in; the login manager signs them in at boot"),
        Result("PASS", names[1])
        if report.get("krdp") and report.get("listening")
        else Result(
            "FAIL", names[1], "krdpserver " + ("runs but does not listen" if report.get("krdp") else "does not run in the session")
        ),
    ]
    gpu = section(plan).get("gpu") or {}
    want = DRIVERS.get(gpu.get("vendor", "") if gpu else "", ())
    drivers = sorted(set((report.get("drivers") or {}).values()) - {""})
    if any(d in want for d in drivers):
        results.append(Result("PASS", names[2]))
    else:
        results.append(
            Result("FAIL", names[2], f"the guest's graphics drivers: {', '.join(drivers) or 'none'}; expected {' or '.join(want)}")
        )
    if gpu.get("takes_from"):
        yielded = {y["name"] for y in ((found or {}).get("gpu") or {}).get("yielded", [])}
        on = [title for name, title in gpu["takes_from"].items() if name not in yielded]
        if found is None:
            results.append(Result("SKIP", names[3], "nb-power did not answer"))
        elif on:
            results.append(Result("FAIL", names[3], f"{', '.join(on)} is not stopped while the desktop has the card"))
        else:
            results.append(Result("PASS", names[3]))
    return results
