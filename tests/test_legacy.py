"""Disks kept from a previous OS: probe, plan rules (docs/adr/0007-legacy-disks.md)."""

import copy

import pytest
from conftest import fixture_config

from nullbill import legacy
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.probe import _disks
from nullbill.render import strip_notes
from nullbill.verify import run_verify

OLD = "ata-SYNTHETIC_HDD_0002"
MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"


def part(n, fstype, parttype=DATA, label=None, uuid="UUID", mounts=(), used_gib=None):
    return {
        "n": n,
        "size_mib": 1024,
        "fstype": fstype,
        "parttype": parttype,
        "label": label,
        "uuid": uuid and f"{uuid}{n}",
        "mounts": list(mounts),
        "used_bytes": None if used_gib is None else used_gib * 2**30,
    }


def with_parts(facts, *parts, disk=OLD):
    f = copy.deepcopy(facts("large"))
    d = next(d for d in f["disks"] if d["id"] == disk)
    d["partitions"] = list(parts)
    d["signatures"] = sorted({"gpt", *(p["fstype"] for p in parts if p["fstype"])})
    return f


def plan(facts_, pins):
    return build_plan(build_model({**fixture_config("large"), "disks": pins}), facts_)


def errors(facts_, pins):
    with pytest.raises(NullBillError) as exc:
        plan(facts_, pins)
    return exc.value.messages


def kept(p, disk=OLD):
    return strip_notes(p)["disks"][disk]["partitions"]


# Probe


def test_probe_reads_partitions_by_type_label_and_uuid(facts):
    old = next(d for d in facts("large")["disks"] if d["id"] == OLD)
    assert old["system"] is False
    assert [(p["n"], p["fstype"], p["parttype"], p["label"]) for p in old["partitions"]] == [
        (1, None, MSR, None),
        (2, "ntfs", DATA, "Data"),
    ]
    assert old["partitions"][1]["uuid"] == "0123456789ABCDEF"
    assert old["smart"] == {"health": "PASSED", "power_on_hours": 41000, "reallocated_sectors": 0}


def lsblk(mounts):
    return (
        '{"blockdevices": [{"name": "sdb", "size": 2000000000000, "type": "disk", "rota": true, "pttype": "gpt", "mountpoints": [null],'
        ' "children": [{"name": "sdb1", "size": 1999000000000, "type": "part", "fstype": "ntfs", "parttype": "0x7", "partn": 1,'
        f' "label": "Old", "uuid": "AB", "mountpoints": {mounts}}}]}}]}}'
    )


def test_a_disk_mounted_as_legacy_is_not_a_system_disk():
    """Once apply has mounted it, the next probe must still let it be pinned."""
    by_id = "ata-SYNTHETIC_HDD_0009 ../../sdb\n"
    (disk,) = _disks(lsblk('["/mnt/nb-legacy/old"]'), by_id)
    assert disk["system"] is False and disk["partitions"][0]["mounts"] == ["/mnt/nb-legacy/old"]
    (disk,) = _disks(lsblk('["/mnt/elsewhere"]'), by_id)
    assert disk["system"] is True


def test_smart_failure_and_unreadable_disks():
    by_id = "ata-SYNTHETIC_HDD_0009 ../../sdb\n"
    failed = '== /dev/sdb\n{"smart_status": {"passed": false}, "ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": 12}}]}}\n'
    (disk,) = _disks(lsblk("[null]"), by_id, failed)
    assert disk["smart"] == {"health": "FAILED", "power_on_hours": None, "reallocated_sectors": 12}
    (disk,) = _disks(lsblk("[null]"), by_id, "== /dev/sdb\nnot json\n")
    assert disk["smart"]["health"] == "unknown"
    (disk,) = _disks(lsblk("[null]"), by_id, None)
    assert disk["smart"] == {"health": "unknown"}


# Plan


def test_unpinned_old_disk_stays_untouched_and_plans_no_share(facts):
    p = build_plan(build_model(fixture_config("large")), facts("large"))
    assert p["disks"][OLD].value["use"] == "untouched"
    assert "files" not in p["guests"]


def test_legacy_skips_windows_partitions_by_type_and_shares_the_data(facts):
    """A Windows system disk: EFI, MSR, C: and recovery; only C: is kept."""
    f = with_parts(facts, part(1, "vfat", EFI), part(2, None, MSR), part(3, "ntfs", label="Windows"), part(4, "ntfs", RECOVERY))
    p = plan(f, {OLD: {"use": "legacy"}})
    assert kept(p) == [
        {"n": 3, "fstype": "ntfs", "label": "Windows", "uuid": "UUID3", "mode": "ro", "mount": "/mnt/nb-legacy/windows", "share": "windows"}
    ]
    why = p["disks"][OLD].why
    assert "Not backed up by NullBill" in why
    assert "partition 1 (EFI system), partition 2 (Microsoft reserved), partition 4 (Windows recovery)" in why
    assert p["guests"]["files"]["zone"] == "mgmt" and "agent_managed" not in p["guests"]["files"]
    assert "role:owner -> files tcp/445" in p["flows"].value
    assert p["backups"].value["status"] == "ok"


def test_mbr_types_are_skipped_too(facts):
    f = with_parts(facts, part(1, "ntfs", "0x27"), part(2, "ntfs", "0x7", label="Media"))
    assert [k["n"] for k in kept(plan(f, {OLD: {"use": "legacy"}}))] == [2]


@pytest.mark.parametrize("parttype", [EFI, MSR, RECOVERY, "0x27"])
def test_naming_a_skipped_partition_is_an_error(facts, parttype):
    f = with_parts(facts, part(1, "ntfs", parttype), part(2, "ntfs"))
    (message,) = errors(f, {OLD: {"use": "legacy", "partitions": [1]}})
    assert message.startswith(f"disks.{OLD}.partitions: partition 1 is a ") and "never mounted" in message


@pytest.mark.parametrize("fstype", ["BitLocker", "crypto_LUKS", "LVM2_member", "zfs_member", "linux_raid_member", "ext4", None])
def test_unsupported_filesystems_are_named(facts, fstype):
    f = with_parts(facts, part(1, fstype), part(2, "ntfs"))
    (message,) = errors(f, {OLD: {"use": "legacy", "partitions": [1, 2]}})
    assert message == f"disks.{OLD}.partitions: partition 1 holds {fstype or 'no filesystem'}; a kept disk can hold NTFS, exFAT or FAT"
    # Not named: left alone, and the rest is kept.
    p = plan(f, {OLD: {"use": "legacy"}})
    assert [k["n"] for k in kept(p)] == [2] and "partition 1" in p["disks"][OLD].why


def test_a_disk_with_nothing_to_keep_is_an_error(facts):
    f = with_parts(facts, part(1, None, MSR), part(2, "BitLocker"))
    (message,) = errors(f, {OLD: {"use": "legacy"}})
    assert (
        message
        == f"disks.{OLD}: no partition with NTFS, exFAT or FAT to keep; found partition 1 (Microsoft reserved), partition 2 (BitLocker)"
    )


def test_legacy_with_wipe_is_an_error(facts):
    (message,) = errors(facts("large"), {OLD: {"use": "legacy", "wipe": True}})
    assert message == f"disks.{OLD}: use: legacy keeps the data; wipe: true makes no sense with it"


def test_legacy_on_the_system_disk_is_refused(facts):
    (message,) = errors(facts("large"), {"nvme-SYNTHETIC_NVME_0001": {"use": "legacy"}})
    assert message == "disks.nvme-SYNTHETIC_NVME_0001: the PVE system disk cannot be pinned"


def test_legacy_on_an_unstable_name_is_refused(facts):
    f = copy.deepcopy(facts("large"))
    d = next(d for d in f["disks"] if d["id"] == OLD)
    d["stable_id"] = False
    assert f"disks.{OLD}: no /dev/disk/by-id name; refusing to plan an unstable disk name" in errors(f, {OLD: {"use": "legacy"}})


def test_read_write_is_for_ntfs_only(facts):
    f = with_parts(facts, part(1, "exfat"), part(2, "ntfs", label="Data"))
    assert kept(plan(f, {OLD: {"use": "legacy", "mode": "rw", "partitions": [2]}}))[0]["mode"] == "rw"
    (message,) = errors(f, {OLD: {"use": "legacy", "mode": "rw"}})
    assert message.startswith(f"disks.{OLD}.mode: rw is for NTFS only; partition 1 holds exFAT")


def test_share_false_mounts_without_a_files_guest(facts):
    p = plan(facts("large"), {OLD: {"use": "legacy", "share": False}})
    assert "share" not in kept(p)[0] and "files" not in p["guests"]


def test_files_is_never_asked_for_by_intent(facts):
    config = {**fixture_config("large"), "intent": {"workloads": ["agents", "files"]}}
    with pytest.raises(NullBillError, match="files is planned by itself"):
        build_plan(build_model(config), facts("large"))


def test_names_from_the_pin_label_or_number_and_never_twice(facts):
    f = with_parts(facts, part(1, "ntfs", label="Fotos & Videos"), part(2, "exfat", label="123"))
    assert [k["share"] for k in kept(plan(f, {OLD: {"use": "legacy"}}))] == ["fotos-videos", "part-2"]
    assert [k["share"] for k in kept(plan(f, {OLD: {"use": "legacy", "name": "old"}}))] == ["old-1", "old-2"]
    f2 = with_parts(lambda _: f, part(1, "ntfs", label="Data"), disk="ata-SYNTHETIC_HDD_0003")
    f2 = with_parts(lambda _: f2, part(1, "ntfs", label="Data"))
    messages = errors(f2, {OLD: {"use": "legacy"}, "ata-SYNTHETIC_HDD_0003": {"use": "legacy"}})
    assert any("share name 'data' is also used" in m for m in messages)


def test_legacy_keys_belong_to_legacy_only(facts):
    messages = errors(facts("large"), {OLD: {"use": "pbs", "wipe": True, "mode": "rw"}})
    assert f"disks.{OLD}.mode: only for use: legacy" in messages


def test_a_partition_mounted_elsewhere_is_refused(facts):
    f = with_parts(facts, part(1, "ntfs", mounts=["/media/x"]))
    f["disks"][[d["id"] for d in f["disks"]].index(OLD)]["system"] = False
    assert f"disks.{OLD}: partition 1 is mounted at /media/x outside NullBill" in errors(f, {OLD: {"use": "legacy"}})


def test_unknown_partition_number(facts):
    (message,) = errors(facts("large"), {OLD: {"use": "legacy", "partitions": [7]}})
    assert message == f"disks.{OLD}.partitions: no partition 7; the disk has 1, 2"


def test_converting_a_mounted_legacy_disk_is_refused(facts):
    """Safety rule 2: no wipe of a kept disk until a verified copy exists."""
    f = with_parts(facts, part(2, "ntfs", label="Data", mounts=["/mnt/nb-legacy/data"]))
    (message,) = errors(f, {OLD: {"use": "pbs", "wipe": True}})
    assert message.startswith(f"disks.{OLD}: kept as legacy and mounted at /mnt/nb-legacy/data; converting it to pbs")
    # Unpinned or ignored: apply stops sharing it and leaves it alone.
    assert plan(f, {OLD: {"use": "ignore"}})["disks"][OLD].value["use"] == "ignore"
    assert plan(f, {})["disks"][OLD].value["use"] == "untouched"


def test_failing_disk_gets_a_warning(facts):
    f = copy.deepcopy(facts("large"))
    next(d for d in f["disks"] if d["id"] == OLD)["smart"] = {"health": "FAILED"}
    p = plan(f, {OLD: {"use": "legacy"}})
    assert p["disks"][OLD].value["health"] == "FAILED"
    assert "SMART says this disk is failing" in p["disks"][OLD].why


# Verify


STATUS = "Name Type Status Total Used Available %\nnb-data lvmthin active 3500000000 10 3499999990 0.00%\n"


def answers(host="fuseblk ro,noatime,user_id=0", guest="fuseblk ro,relatime", samba="SMB3\nrequired\nNever\n", storage=STATUS):
    def run(target, command):
        if command == "pvesm status --storage nb-data":
            return (0, storage, "") if storage else (2, "", "storage 'nb-data' does not exist\n")
        if command.startswith("findmnt"):
            return 0, host + "\n" if host else "", ""
        if command.startswith("pct exec 2023 -- findmnt"):
            return (0, guest + "\n", "") if guest else (1, "", "")
        if command.startswith("pct exec 2023 -- sh -c"):
            return 0, samba, ""
        raise AssertionError(command)

    return run


def verdicts(p, run):
    return [(r.status, r.name, r.detail) for r in legacy.check(p, "root@pve.internal", run)]


def test_verify_passes_a_kept_disk_mounted_and_shared_as_planned(facts):
    p = plan(facts("large"), {OLD: {"use": "legacy"}})
    assert verdicts(p, answers()) == [
        ("PASS", "kept disk data is mounted ro on the host", ""),
        ("PASS", "files guest sees kept disk data ro", ""),
        ("PASS", "files guest's Samba takes SMB3 with encryption only, and no guests", ""),
    ]


def test_verify_fails_a_missing_or_wrongly_mounted_kept_disk(facts):
    p = plan(facts("large"), {OLD: {"use": "legacy"}})
    missing = verdicts(p, answers(host="", guest=""))
    assert missing[0][0] == "FAIL" and f"is partition 2 of {OLD} connected" in missing[0][2]
    assert missing[1] == ("FAIL", "files guest sees kept disk data ro", "nothing mounted at /srv/share/data")
    # Asked for read-write, ntfs-3g fell back to read-only.
    rw = plan(facts("large"), {OLD: {"use": "legacy", "mode": "rw"}})
    assert [v[:1] + v[2:] for v in verdicts(rw, answers())[:2]] == [("FAIL", "fuseblk ro"), ("FAIL", "/srv/share/data is ro")]
    assert verdicts(p, answers(samba="SMB2\nrequired\nBad User\n"))[2][2] == "server min protocol = SMB2; map to guest = Bad"


def test_verify_without_a_share_checks_the_host_only(facts):
    p = plan(facts("large"), {OLD: {"use": "legacy", "share": False}})
    assert [v[1] for v in verdicts(p, answers())] == ["kept disk data is mounted ro on the host"]


def test_verify_without_a_target_skips_the_kept_disk(facts):
    config = {**fixture_config("large"), "disks": {OLD: {"use": "legacy"}}}
    model = build_model(config)
    results = run_verify(build_plan(model, facts("large")), model, None, None)
    skipped = [r.name for r in results if r.status == "SKIP" and "kept disk" in r.name]
    assert skipped == ["kept disk data is mounted ro on the host", "files guest sees kept disk data ro"]


# Converting a kept disk (Phase 2)


def mounted(facts, used_gib=100, **kw):
    """The old disk as apply left it: partition 2 kept and mounted as data."""
    return with_parts(facts, part(1, None, MSR), part(2, "ntfs", label="Data", mounts=["/mnt/nb-legacy/data"], used_gib=used_gib), **kw)


def test_converting_needs_wipe_and_a_copy_or_the_owners_word(facts):
    f = mounted(facts)
    (message,) = errors(f, {OLD: {"use": "pbs", "wipe": True}})
    assert "converting it to pbs erases it: set evacuate: <PVE storage>" in message and "discard_data: true" in message
    messages = errors(f, {OLD: {"use": "pbs", "evacuate": "local-lvm"}})
    assert f"disks.{OLD}: kept as legacy and mounted at /mnt/nb-legacy/data; converting it to pbs needs wipe: true" in messages
    (message,) = errors(f, {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm", "discard_data": True}})
    assert message == f"disks.{OLD}: evacuate or discard_data, not both"


def test_discarding_says_how_much_is_lost(facts):
    p = plan(mounted(facts, used_gib=300), {OLD: {"use": "pbs", "wipe": True, "discard_data": True}})
    disk = p["disks"][OLD]
    assert disk.value["converts"] == {"from": "legacy", "discard_data": True, "lost_gib": 300}
    assert "its files (300 GiB) are not copied and are lost" in disk.why
    assert p["backups"].value["status"] == "ok" and "files" not in p["guests"]


def test_evacuating_plans_a_copy_shared_from_the_files_guest(facts):
    p = plan(mounted(facts), {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    disk = p["disks"][OLD]
    assert disk.value["converts"] == {
        "from": "legacy",
        "evacuate": "local-lvm",
        "copies": [{"name": "data", "partition": 2, "used_gib": 100, "size_gib": 106}],
    }
    assert "copies its files (data) to local-lvm, checks the copy" in disk.why and "only then is the disk wiped" in disk.why
    assert "files" in p["guests"] and "role:owner -> files tcp/445" in p["flows"].value
    assert legacy.copies(strip_notes(p)) == [{"name": "data", "storage": "local-lvm", "size_gib": 106, "disk": OLD, "made": False}]
    assert legacy.shares(strip_notes(p)) == []


def test_evacuating_needs_room_on_a_storage_for_containers(facts):
    f = mounted(facts, used_gib=1500)
    messages = errors(f, {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    need = "the copy needs 1576 GiB (the files and 5 %, plus 1 GiB each)"
    assert f"disks.{OLD}.evacuate: {need}, local-lvm has 1152 GiB free after the guests' disks" in messages
    assert "disk: guests need 512 GiB, storage local-lvm has 88 GiB free besides 1576 GiB for copies of kept disks" in messages
    # A copy that fits shrinks the CI pools instead: it stays on the storage for good.
    p = plan(mounted(facts, used_gib=600), {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    assert p["disk"]["copies_gib"].value == 631 and p["disk"]["needed_gib"].value <= 1664 - 631
    (message,) = errors(mounted(facts), {OLD: {"use": "pbs", "wipe": True, "evacuate": "local"}})
    assert message == f"disks.{OLD}.evacuate: local cannot hold container volumes (content images and rootdir)"
    (message,) = errors(mounted(facts), {OLD: {"use": "pbs", "wipe": True, "evacuate": "tank"}})
    assert message == f"disks.{OLD}.evacuate: no active PVE storage named 'tank'"
    f = mounted(facts)
    f["storage"].append({**next(s for s in f["storage"] if s["name"] == "local-lvm"), "name": "tank", "type": "zfspool"})
    (message,) = errors(f, {OLD: {"use": "pbs", "wipe": True, "evacuate": "tank"}})
    assert message == f"disks.{OLD}.evacuate: tank is zfspool; a copy goes on LVM or LVM-thin storage (lvmthin, lvm)"
    (message,) = errors(mounted(facts, used_gib=None), {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    assert message == f"disks.{OLD}: how much partition 2 holds is unknown; probe again"


def test_convert_keys_belong_to_a_conversion(facts):
    (message,) = errors(facts("large"), {OLD: {"use": "legacy", "evacuate": "local-lvm"}})
    assert message == f"disks.{OLD}.evacuate: only for converting a kept disk to pbs or storage"
    # Once converted, the disk is no longer kept: the pin may keep its key.
    p = plan(facts("large"), {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    assert "converts" not in p["disks"][OLD].value and "files" not in p["guests"]


def test_a_storage_disk_is_lvm_thin_storage_of_its_own(facts):
    f = with_parts(facts, part(1, "ext4"))
    (message,) = errors(f, {OLD: {"use": "storage"}})
    assert message == f"disks.{OLD}: carries ext4, gpt; set wipe: true to let apply format it"
    p = plan(f, {OLD: {"use": "storage", "wipe": True}})
    disk = p["disks"][OLD]
    assert disk.value["use"] == "storage" and disk.value["storage"] == "nb-data" and "converts" not in disk.value
    assert "LVM-thin PVE storage nb-data, 3362 GiB for volumes" in disk.why and "apply will wipe its signatures" in disk.why
    # The guests stay on the system storage, which keeps its size.
    assert p["disk"]["storage"] == "local-lvm"
    assert plan(f, {OLD: {"use": "storage", "wipe": True, "storage": "nb-photos"}})["disks"][OLD].value["storage"] == "nb-photos"
    (message,) = errors(f, {OLD: {"use": "storage", "wipe": True, "storage": "nb-pbs-disk"}})
    assert message == f"disks.{OLD}.storage: nb-pbs-disk is taken by another PVE storage; choose another name"
    (message,) = errors(f, {OLD: {"use": "pbs", "wipe": True, "storage": "nb-data"}})
    assert message == f"disks.{OLD}.storage: only for use: storage"


def test_a_kept_disk_converted_to_storage_takes_its_copies(facts):
    p = plan(mounted(facts), {OLD: {"use": "storage", "wipe": True, "evacuate": "local-lvm"}})
    converts = p["disks"][OLD].value["converts"]
    assert converts["evacuate"] == "local-lvm" and converts["moves_to"] == "nb-data"
    assert "the copies move onto it, checked again" in p["disks"][OLD].why
    assert [g for g in p["guests"] if g == "files"] == ["files"]
    # Without a copy it is erased like a backup disk would be.
    p = plan(mounted(facts), {OLD: {"use": "storage", "wipe": True, "discard_data": True}})
    assert p["disks"][OLD].value["converts"]["discard_data"] is True
    (message,) = errors(mounted(facts), {OLD: {"use": "storage", "wipe": True}})
    assert "converting it to storage erases it" in message
    # The copies must fit on the disk once it is storage.
    small = mounted(facts, used_gib=100)
    next(d for d in small["disks"] if d["id"] == OLD)["size_gib"] = 100
    (message,) = errors(small, {OLD: {"use": "storage", "wipe": True, "evacuate": "local-lvm"}})
    assert message == f"disks.{OLD}: the copies need 106 GiB, more than the 90 GiB the disk holds as storage nb-data"


COPY = {"name": "data", "volume": "local-lvm:vm-2023-disk-1", "size_gib": 106, "files": 1200, "bytes": 107374182400, "disk": OLD}


def test_a_copy_keeps_the_files_guest_and_its_name(facts):
    f = copy.deepcopy(facts("large"))
    f["copies"] = [COPY]
    p = plan(f, {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    assert p["copies"].value == [COPY] and "files" in p["guests"]
    assert legacy.copies(strip_notes(p)) == [{**COPY, "made": True}]
    f2 = with_parts(lambda _: f, part(1, "ntfs", label="Data"), disk="ata-SYNTHETIC_HDD_0003")
    messages = errors(f2, {"ata-SYNTHETIC_HDD_0003": {"use": "legacy"}})
    taken = "share name 'data' is also used on the copy local-lvm:vm-2023-disk-1; set disks.<id>.name"
    assert f"disks.ata-SYNTHETIC_HDD_0003: {taken}" in messages


def test_probe_reads_used_bytes_and_copies():
    from nullbill.probe import _copies

    by_id = "ata-SYNTHETIC_HDD_0009 ../../sdb\n"
    text = lsblk('["/mnt/nb-legacy/old"]').replace('"uuid": "AB",', '"uuid": "AB", "fsused": 5368709120,')
    (disk,) = _disks(text, by_id)
    assert disk["partitions"][0]["used_bytes"] == 5 * 2**30
    assert _copies("data local-lvm:vm-2023-disk-1 106 1200 107374182400 ata-X\n# note\nbad line\n") == [
        {"name": "data", "volume": "local-lvm:vm-2023-disk-1", "size_gib": 106, "files": 1200, "bytes": 107374182400, "disk": "ata-X"}
    ]


def test_verify_checks_a_copy_in_the_files_guest(facts):
    f = copy.deepcopy(facts("large"))
    f["copies"] = [COPY]
    p = plan(f, {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}})
    assert verdicts(p, answers(guest="ext4 rw,relatime")) == [
        ("PASS", "files guest sees the copy data rw", ""),
        ("PASS", "files guest's Samba takes SMB3 with encryption only, and no guests", ""),
    ]
    assert verdicts(p, answers(guest=""))[0] == ("FAIL", "files guest sees the copy data rw", "nothing mounted at /srv/share/data")


def test_verify_checks_a_storage_disk(facts):
    p = plan(with_parts(facts, part(1, "ext4")), {OLD: {"use": "storage", "wipe": True}})
    label = "storage disk nb-data is active LVM-thin storage"
    assert verdicts(p, answers()) == [("PASS", label, "")]
    assert verdicts(p, answers(storage="")) == [("FAIL", label, "storage 'nb-data' does not exist")]
    assert verdicts(p, answers(storage=STATUS.replace("active", "inactive"))) == [("FAIL", label, "lvmthin inactive")]
