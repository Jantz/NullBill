"""The KDE desktop (docs/adr/0023-desktop.md): the plan's card and USB
ports, nb-power handing the card over and back, and what apply and verify
do around it."""

# ruff: noqa: F811 (the fixture imported from test_apply is taken by name)

import json
import os
import tempfile
from pathlib import Path

import pytest
from fixturedata import desktop_config, fixture_config, write_raw
from test_apply import ctx  # noqa: F401 (a fixture)
from test_nb_power import POLICY, Host, guest

from nullbill import apply, desktop, power, probe
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

SLOT = "0000:01:00"
CARD = f"{SLOT}.0"


def plan_for(config: dict, cls: str = "mid", facts_change=None) -> dict:
    facts = probe.parse(write_raw(Path(tempfile.mkdtemp()) / "raw", cls))
    if facts_change:
        facts_change(facts)
    return build_plan(build_model(config), facts)


def test_the_desktop_borrows_the_card_the_local_models_use():
    plan = strip_notes(plan_for(desktop_config()))
    lent = plan["gpus"][CARD]
    # The host keeps it, and inference its budget.
    assert lent["owner"] == "host-shared" and lent["lent_to"] == "desktop" and lent["consumers"] == {"inference": 24564}
    assert lent["pci_id"] == "10de:0001" and lent["iommu_group"] == 14
    section = plan["desktop"]
    assert section["gpu"] == {"pci": CARD, "name": lent["name"], "vendor": "nvidia", "takes_from": {"inference": "Local AI models"}}
    # Where the probe saw a keyboard and a mouse; a disk's port is not taken.
    assert section["usb"] == ["1-2", "1-3.1"] and section["port"] == 3389 and section["user"] == "owner-a"
    assert {"kind": "vm", "zone": "desktop", "vmid": 2029, "run": "on-demand"}.items() <= plan["guests"]["desktop"].items()
    assert power.policy(plan)["gpu"] == {
        "group": "desktop",
        "slot": SLOT,
        "name": lent["name"],
        "yields": [{"name": "inference", "kind": "lxc", "vmid": 2020, "title": "Local AI models"}],
    }
    assert "Starting it stops the local AI models" in desktop.sentence(plan)


def test_without_the_desktop_nothing_changes():
    plan = strip_notes(plan_for(fixture_config("mid")))
    assert "desktop" not in plan and "lent_to" not in plan["gpus"][CARD]
    assert "gpu" not in power.policy(plan) and desktop.takes_from(plan) == {}


def test_a_virtual_screen_when_told_or_without_a_card():
    plan = strip_notes(plan_for(desktop_config(gpu=False)))
    assert plan["desktop"]["gpu"] == {} and "lent_to" not in plan["gpus"][CARD]
    assert "gpu" not in power.policy(plan)
    assert desktop.sentence(plan).startswith("It has a virtual screen")
    # The small class has no discrete card at all.
    small = fixture_config("small")
    small["intent"]["workloads"] = ["desktop"]
    assert strip_notes(plan_for(small, "small"))["desktop"]["gpu"] == {}


def refused(config: dict, cls: str = "mid", facts_change=None) -> str:
    with pytest.raises(NullBillError) as found:
        plan_for(config, cls, facts_change)
    return str(found.value)


def test_the_plan_refuses_what_cannot_work():
    def iommu_off(facts):
        for g in facts["gpus"]:
            g["iommu_group"], g["iommu_peers"] = None, []

    assert "turn on VT-d" in refused(desktop_config(), facts_change=iommu_off)

    def shared_group(facts):
        facts["gpus"][0]["iommu_peers"] = [*facts["gpus"][0]["iommu_peers"], "0000:02:00.0"]

    assert "shares its IOMMU group" in refused(desktop_config(), facts_change=shared_group)

    def read_before_the_desktop(facts):
        # The parser then counted the card's PCIe port as a peer, and knew no USB.
        shared_group(facts)
        del facts["usb"]

    def read_without_subsystems(facts):
        for g in facts["gpus"]:
            del g["subsystem_id"]

    assert "has not read NVIDIA Corporation Synthetic GPU 24G's subsystem ID" in refused(
        desktop_config(), facts_change=read_without_subsystems
    )
    stale = refused(desktop_config(), facts_change=read_before_the_desktop)
    assert "read the hardware again on Setup, Hardware" in stale and "0000:02:00.0" in stale
    assert "no discrete graphics card at 0000:09:00.0" in refused(desktop_config(card="0000:09:00.0"))
    always = {**desktop_config(), "power": {"desktop": {"run": "always"}}}
    assert "keep run: on-demand" in refused(always)
    passed = {**desktop_config(), "gpus": {CARD: {"owner": "vm:windows-dev"}}}
    assert "the card is passed to windows-dev" in refused(passed)
    many = desktop_config(usb=[f"1-{n}" for n in range(1, 16)])
    # The schema holds config.yaml to PVE's 14; the plan holds the probe's.
    assert "desktop.usb" in refused(many) and "is too long" in refused(many)


def test_usb_ports_named_in_config_and_those_unplugged():
    plan = strip_notes(plan_for(desktop_config(usb=["1-2", "3-4"])))
    assert plan["desktop"]["usb"] == ["1-2", "3-4"] and plan["desktop"]["usb_missing"] == ["3-4"]


def test_the_usb_probe_reads_keyboards_and_mice_by_port():
    text = "1-2\tf0f0:0001\tkeyboard,mouse\tCombo\n1-10\tf0f0:0002\t\tStick\nusb1\tf0f0:0003\t\t\n1-1.4\tzzzz:0001\tmouse\tBad id\n"
    found = probe._usb(text)
    assert [d["port"] for d in found] == ["1-2", "1-10"]
    assert found[0]["kinds"] == ["keyboard", "mouse"] and found[1]["kinds"] == []
    assert probe._usb(None) is None
    assert desktop.input_ports({"usb": found}) == ["1-2"]


def test_usb_changes_keep_what_is_there_and_drop_the_rest():
    config = "boot: order=scsi0\nusb0: host=1-2\nusb1: host=1-9,usb3=1\nusb3: host=f0f0:0001\n"
    delete, add = desktop.usb_changes(config, ["1-2", "1-3.1"])
    # usb3 names a device, not a port: not NullBill's, left alone.
    assert delete == [1] and add == {1: "1-3.1"}
    assert desktop.usb_changes("usb0: host=1-2\n", ["1-2"]) == ([], {})


def test_the_desktop_has_its_own_python_and_no_debian_stages():
    from nullbill import release

    plan = strip_notes(plan_for(desktop_config()))
    model = build_model(desktop_config())
    assert "desktop" not in release.candidates(plan, model)
    assert desktop.PYTHON == "/usr/bin/python3.13"


# nb-power on the host: a stand-in sysfs for the card.
DESKTOP = {
    "title": "KDE desktop",
    "memory_mib": 4096,
    "stop": "ask",
    "idle_minutes": 240,
    "guests": [{"name": "desktop", "kind": "vm", "vmid": 2029, "memory_mib": 4096}],
}


def card_policy(yields: bool = True) -> dict:
    groups = {k: v for k, v in POLICY["groups"].items() if k != "inference"} | {"desktop": DESKTOP}
    found = {"group": "desktop", "slot": SLOT, "name": "Synthetic GPU", "yields": []}
    if yields:
        found["yields"] = [{"name": "inference", "kind": "lxc", "vmid": 2020, "title": "Local AI models"}]
    return {**POLICY, "headroom_mib": 8000, "groups": groups, "gpu": found}


class CardHost(Host):
    def __init__(self, tmp_path, yields=True):
        super().__init__(tmp_path, card_policy(yields))
        self.set(
            {
                "2012": guest("step-ca", state="running"),
                "2018": guest("runner-dispatcher"),
                "2020": guest("inference", state="running"),
                "2022": guest("windows-dev", "vm", mem=4096),
                "2024": guest("jellyfin"),
                "2029": guest("desktop", "vm", mem=4096),
            }
        )
        self.pci = tmp_path / "pci"
        for driver in ("nvidia", "snd_hda_intel", "vfio-pci"):
            (self.pci / "drivers" / driver).mkdir(parents=True)
        (self.pci / "drivers_probe").write_text("")
        for fn in (".0", ".1"):
            device = self.pci / "devices" / f"{SLOT}{fn}"
            device.mkdir(parents=True)
            (device / "driver_override").write_text("(null)\n")
        self.bind("nvidia", "snd_hda_intel")
        (tmp_path / "bin" / "systemctl").write_text('#!/bin/sh\necho "$@" >> "$NB_FAKE_WORLD.systemctl"\n')
        (tmp_path / "bin" / "systemctl").chmod(0o755)

    def bind(self, *drivers):
        """As the kernel leaves the card: PVE binds vfio-pci at the VM's start."""
        for fn, driver in zip((".0", ".1"), drivers, strict=True):
            link = self.pci / "devices" / f"{SLOT}{fn}" / "driver"
            if link.is_symlink():
                link.unlink()
            link.symlink_to(self.pci / "drivers" / driver)

    def run(self, *args, check=True):
        os.environ["NB_PCI_ROOT"] = str(self.pci)
        try:
            return super().run(*args, check=check)
        finally:
            del os.environ["NB_PCI_ROOT"]


@pytest.fixture
def card(tmp_path):
    return CardHost(tmp_path)


def test_starting_the_desktop_asks_before_it_takes_the_card(card):
    answer = card.json("start", "desktop", rc=power.NO_ROOM)
    assert answer["error"] == "gpu" and answer["card"] == "Synthetic GPU"
    assert answer["stop"] == [{"name": "inference", "title": "Local AI models", "busy": []}]
    assert card.running() == ["inference"]
    card.change(2020, busy=["1 connection"])
    assert card.json("start", "desktop", rc=power.NO_ROOM)["stop"][0]["busy"] == ["1 connection"]


def test_with_a_yes_it_stops_the_local_models_and_gives_the_card_back_after(card):
    answer = card.json("start", "desktop", "--stop", "inference")
    assert answer["ok"] and "Local AI models" in answer["stopped"]
    assert card.running() == ["desktop"]
    card.bind("vfio-pci", "vfio-pci")
    found = card.status()
    assert found["gpu"]["lent"] is True and found["gpu"]["yielded"] == [{"name": "inference", "title": "Local AI models"}]
    assert card.metrics()['nb_power_guest_off{guest="inference",group="gpu"}'] == "1"
    # The idle tick leaves it while the desktop runs.
    card.minutes(5)
    assert card.running() == ["desktop"]
    card.json("stop", "desktop")
    # Back to the host's drivers: override cleared, unbound, probed again.
    device = card.pci / "devices" / CARD
    assert (device / "driver_override").read_text() == "\n"
    assert (card.pci / "drivers/vfio-pci/unbind").read_text() in (CARD, f"{SLOT}.1")
    assert (card.pci / "drivers_probe").read_text() in (CARD, f"{SLOT}.1")
    # The stand-in kernel binds nothing: the local models wait for the card.
    assert card.running() == []
    card.bind("nvidia", "snd_hda_intel")
    card.minutes(1)
    assert card.running() == ["inference"]
    assert "restart nb-nvidia-devices.service" in Path(f"{card.world}.systemctl").read_text()
    found = card.status()
    assert found["gpu"]["lent"] is False and found["gpu"]["yielded"] == []
    assert card.metrics()['nb_power_guest_off{guest="inference",group="gpu"}'] == "0"


def test_a_desktop_that_does_not_start_gives_the_card_back(card):
    card.change(2029, start_fails=True)
    done = card.run("start", "desktop", "--stop", "inference", check=False)
    assert done.returncode == 1 and "did not start" in done.stderr
    # Nothing was bound to VFIO, so the local models start again at once.
    assert card.running() == ["inference"]


def test_setup_takes_the_card_only_when_nobody_uses_it(card):
    card.change(2020, busy=["1 connection"])
    done = card.run("ensure", "desktop", check=False)
    assert done.returncode == power.GPU_BUSY and "it is in use" in done.stderr
    assert card.running() == ["inference"]
    card.change(2020, busy=[])
    done = card.run("ensure", "desktop")
    assert done.stdout.splitlines() == ["yielded Local AI models", "started desktop"]
    assert card.running() == ["desktop"]
    card.bind("vfio-pci", "vfio-pci")
    card.run("stop", "desktop", "--by", "setup")
    card.bind("nvidia", "snd_hda_intel")
    card.run("setup", "end")
    assert card.running() == ["inference"]


def test_with_nothing_on_the_host_using_it_the_card_stays_vfios(tmp_path):
    host = CardHost(tmp_path, yields=False)
    host.json("start", "desktop")
    host.bind("vfio-pci", "vfio-pci")
    host.json("stop", "desktop")
    assert (host.pci / "devices" / CARD / "driver_override").read_text() == "(null)\n"
    assert host.status()["gpu"]["lent"] is False


# verify's look inside the desktop.
def report(**values) -> str:
    body = {"session": True, "locked": True, "krdp": True, "listening": True, "drivers": {"card0": "nouveau"}} | values
    return json.dumps({"exitcode": 0, "out-data": json.dumps(body)})


def test_verify_reads_the_desktop_through_its_guest_agent():
    plan = plan_for(desktop_config())
    status = {"groups": [], "gpu": {"yielded": [{"name": "inference", "title": "Local AI models"}]}}
    calls = []

    def run(target, command):
        calls.append(command)
        return 0, report(), ""

    results = desktop.check(plan, "root@pve", run, {}, status)
    assert [r.status for r in results] == ["PASS"] * 4
    assert calls == ["qm guest exec 2029 --timeout 30 -- /usr/local/sbin/nb-desktop report owner-a"]
    bad = desktop.check(
        plan,
        "root@pve",
        lambda t, c: (0, report(session=False, krdp=False, drivers={"card0": "virtio_gpu"}), ""),
        {},
        {"groups": [], "gpu": {"yielded": []}},
    )
    assert [r.status for r in bad] == ["FAIL"] * 4
    assert "expected nouveau" in bad[2].detail and "Local AI models is not stopped" in bad[3].detail
    off = desktop.check(plan, "root@pve", run, {"desktop": "KDE desktop"}, status)
    assert {r.status for r in off} == {"SKIP"} and "start it on the page" in off[0].detail
    assert len(calls) == 1


# What apply does around the desktop.
def desktop_ctx(ctx, **desktop_section):
    c = ctx("mid", config=desktop_config(**desktop_section))
    c.secrets.set("ntfy_topic", "nullbill-topic")
    return c


def lent(c):
    """nb-power status while the desktop has the card."""
    c.reach.power_status = {"groups": [], "gpu": {"lent": True, "yields": [{"name": "inference", "title": "Local AI models"}]}}


def said(capsys) -> str:
    return capsys.readouterr().out


def test_setup_starts_the_desktop_and_stops_it_again_after(ctx, capsys):
    c = desktop_ctx(ctx)
    apply.stage_desktop(c)
    reach = c.reach
    assert reach.power_stopped == [["desktop", "--by", "setup"]] and "desktop" not in reach.power_on
    playbooks = [call for call in reach.calls if call[0] == "playbook"]
    assert [p[1] for p in playbooks] == ["desktop.yml"]
    variables = playbooks[0][3]
    assert variables["desktop_user"] == "owner-a" and variables["desktop_gpu_vendor"] == "nvidia"
    assert variables["desktop_password"] == c.secrets.data["desktop_password"] and len(variables["desktop_password"]) >= 12
    assert variables["desktop_cert"] and variables["desktop_key"]
    # Ansible's modules run under Fedora's python3.13, installed first over SSH.
    assert playbooks[0][2]["desktop"]["ansible_python_interpreter"] == desktop.PYTHON
    # Without a terminal, so Fedora's session markers stay out of the modules' answers.
    assert playbooks[0][2]["desktop"]["ansible_ssh_use_tty"] is False
    ssh = [call[1] for call in reach.calls if call[0] == "local" and call[1][0] == "ssh"]
    assert ssh and ssh[0][-2:] == ["nullbill@10.42.11.29", desktop.PREPARE]
    out = said(capsys)
    assert "stopped the desktop again" in out and "Remote Desktop to desktop." in out


def test_setup_leaves_the_desktop_while_the_card_is_in_use(ctx, capsys):
    c = desktop_ctx(ctx)
    c.reach.card_busy = True
    apply.stage_desktop(c)
    assert not [call for call in c.reach.calls if call[0] == "playbook"]
    assert c.reach.power_stopped == []
    assert "left the desktop as it is: the graphics card is in use" in said(capsys)


def test_a_desktop_already_on_stays_on(ctx):
    c = desktop_ctx(ctx)
    c.reach.power_on.add("desktop")
    apply.stage_desktop(c)
    assert c.reach.power_stopped == [] and "desktop" in c.reach.power_on


def test_while_the_desktop_has_the_card_setup_leaves_the_local_models_alone(ctx, capsys):
    c = desktop_ctx(ctx)
    lent(c)
    assert apply.card_away(c) == {"inference": "Local AI models"}
    apply.stage_inference(c)
    assert not [call for call in c.reach.calls if call[0] == "playbook"]
    assert "left inference as it is" in said(capsys)
    # Read once a run.
    asked = [call for call in c.reach.calls if call[0] == "host" and "status --json" in call[1]]
    apply.card_away(c)
    assert len([call for call in c.reach.calls if call[0] == "host" and "status --json" in call[1]]) == len(asked)


def test_without_a_card_to_lend_nothing_asks_nb_power(ctx):
    c = desktop_ctx(ctx, gpu=False)
    lent(c)
    assert apply.card_away(c) == {}
    assert not [call for call in c.reach.calls if "status --json" in str(call)]


def test_the_desktop_gets_its_usb_ports_and_loses_the_others(ctx, capsys):
    c = desktop_ctx(ctx)
    apply.give_usb(c)
    assert c.reach.usb_set == [["--usb0", "host=1-2", "--usb1", "host=1-3.1"]]
    assert "the USB ports 1-2, 1-3.1" in said(capsys)


def test_the_host_loads_vfio_only_when_a_card_is_lent(ctx):
    assert apply.desktop_host_inputs(desktop_ctx(ctx)) == {"host_baseline_vfio": True, "host_baseline_nouveau_off": True}
    assert apply.desktop_host_inputs(desktop_ctx(ctx, gpu=False)) == {}


def test_the_pcie_port_a_card_sits_behind_stays_with_the_host():
    lspci = (
        "0000:00:01.0 PCI bridge [0604]: Example Corp PCI Express Graphics Port [8086:0001] (rev 01)\n"
        "0000:00:02.0 VGA compatible controller [0300]: Example Corp Integrated Graphics [8086:0002] (rev 04)\n"
        "0000:01:00.0 VGA compatible controller [0300]: NVIDIA Corporation Synthetic GPU 24G [10de:0001] (rev a1)\n"
        "0000:01:00.1 Audio device [0403]: NVIDIA Corporation Synthetic GPU Audio [10de:0002] (rev a1)\n"
        "0000:02:00.0 Ethernet controller [0200]: Example Corp Ethernet [8086:0003]\n"
    )
    groups = "".join(f"/sys/kernel/iommu_groups/2/devices/{a}\n" for a in ("0000:00:01.0", "0000:01:00.0", "0000:01:00.1"))
    found = {g["pci"]: g for g in probe._gpus(lspci, groups, None, "0000:00:02.0 0x8086 0x7777\n0000:01:00.0 0x1569 0xf302\n")}
    assert found[CARD]["iommu_group"] == 2 and found[CARD]["iommu_peers"] == ["0000:01:00.1"]
    assert found[CARD]["subsystem_id"] == "1569:f302"
    # Anything else in the group is a peer, and would go to the desktop with the card.
    shared = groups + "/sys/kernel/iommu_groups/2/devices/0000:02:00.0\n"
    assert probe._gpus(lspci, shared, None)[1]["iommu_peers"] == ["0000:01:00.1", "0000:02:00.0"]
