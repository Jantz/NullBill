"""Disks kept from a previous OS: probe, plan rules (docs/adr/0007-legacy-disks.md)."""

import copy

import pytest
from conftest import fixture_config

from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.probe import _disks
from nullbill.render import strip_notes

OLD = "ata-SYNTHETIC_HDD_0002"
MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
EFI = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"


def part(n, fstype, parttype=DATA, label=None, uuid="UUID", mounts=()):
    return {
        "n": n,
        "size_mib": 1024,
        "fstype": fstype,
        "parttype": parttype,
        "label": label,
        "uuid": uuid and f"{uuid}{n}",
        "mounts": list(mounts),
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
