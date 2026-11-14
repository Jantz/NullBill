"""Setup, Review: what a setup run would change, from two plans."""

import pytest
from fixturedata import fixture_config, legacy_config, write_raw

from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.probe import parse
from nullbill.render import strip_notes
from nullbill.web import review


@pytest.fixture
def facts(tmp_path):
    return parse(write_raw(tmp_path / "raw", "large"))


def plan(config, facts):
    return strip_notes(build_plan(build_model(config), facts))


def by_change(rows):
    out = {}
    for row in rows:
        out.setdefault(row["change"], []).append(row["name"])
    return out


def test_a_first_run_creates_every_guest_of_the_plan(facts):
    now = plan(fixture_config("large"), facts)
    rows = review.guests(now, None, None, {})
    assert by_change(rows) == {"create": list(now["guests"])}
    router = rows[0]
    assert (router["name"], router["kind"], router["zone"], router["cores"], router["memory_mib"]) == ("router", "vm", "mgmt", 1, 512)
    # Nothing to compare with: no section is called changed, and there is no difference to show.
    assert review.sections(now, None) == [] and review.difference(None, "guests: {}\n") is None


def test_later_runs_say_what_is_created_deleted_changed_and_kept(facts):
    was = plan(fixture_config("large"), facts)
    config = legacy_config()
    config["intent"]["workloads"] = [w for w in config["intent"]["workloads"] if w != "windows-dev"]
    config["overrides"] = {"forgejo": {"memory_mib": 8192, "cores": 4}}
    now = plan(config, facts)
    rows = review.guests(now, was, set(was["guests"]), {"windows-dev": "Deletes the VM and its disk."})
    found = by_change(rows)
    assert found["create"] == ["files", "jellyfin"] and found["delete"] == ["windows-dev"]
    # The memory the Windows desktop held goes to more Windows runners.
    assert set(found["change"]) == {"router", "forgejo", "windows-runner"}
    detail = {row["name"]: row["detail"] for row in rows}
    assert detail["forgejo"] == ["memory 1.0 GiB to 8.0 GiB", "cores 1 to 4"]
    # A new size is told apart: OpenTofu sets it in place, and preview.py says what that does.
    resized = {row["name"] for row in rows if row["resized"]}
    assert "forgejo" in resized and "router" not in resized and "windows-dev" not in resized
    # The router gets a leg in the media zone.
    assert detail["router"] == ["its network legs change"]
    assert detail["windows-dev"] == ["Deletes the VM and its disk."]
    assert "postgres" in found["keep"] and detail["postgres"] == []
    assert rows[-1]["name"] == "windows-dev" and rows[-1]["kind"] == "vm"
    assert {"network zones", "firewall rules", "the media server"} <= set(review.sections(now, was))
    assert "people and what each may open" not in review.sections(now, was)


def test_what_apply_recorded_on_the_server_says_which_guests_exist(facts):
    # A run that stopped half way: the plan it started with names guests it never made.
    now = plan(fixture_config("large"), facts)
    rows = review.guests(now, now, {"router", "nullbill", "gone-guest"}, {})
    found = by_change(rows)
    assert found["keep"] == ["router", "nullbill"] and "forgejo" in found["create"]
    assert found["delete"] == ["gone-guest"]
    assert rows[-1]["detail"] == ["Deletes the guest and everything on its disk."]


def test_a_guest_put_on_demand_and_a_pool_given_more_runners_read_as_such():
    was = {"guests": {"jellyfin": {"kind": "lxc", "memory_mib": 2048}, "linux-runner": {"kind": "vm-pool", "count": 1}}}
    now = {"guests": {"jellyfin": {"kind": "lxc", "memory_mib": 2048, "run": "on-demand"}, "linux-runner": {"kind": "vm-pool", "count": 3}}}
    rows = review.guests(now, was, None, {})
    detail = {row["name"]: row["detail"] for row in rows}
    assert detail == {"jellyfin": ["runs on demand from now on"], "linux-runner": ["3 at a time, 1 before"]}
    # More runners at a time is no new size of each.
    assert not any(row["resized"] for row in rows)
    back = {row["name"]: row["detail"] for row in review.guests(was, now, None, {})}
    assert back["jellyfin"] == ["always runs from now on"]


def test_disks_say_what_is_erased_kept_and_already_done(facts):
    blank, ntfs = "ata-SYNTHETIC_HDD_0003", "ata-SYNTHETIC_HDD_0002"
    first = plan(fixture_config("large"), facts)
    # First run: the blank disk becomes the backup disk; the system disk and the untouched one are not named.
    assert review.disks(first, None, facts) == [
        {"id": blank, "size_gib": 3726, "text": "Blank; it becomes the backup disk.", "erases": False}
    ]
    # Once apply made it, nothing happens to it any more, with or without a record of the plan.
    made = {**facts, "disks": [{**d, "vgs": ["nb-pbs-disk"]} if d["id"] == blank else d for d in facts["disks"]]}
    assert review.disks(plan(fixture_config("large"), made), None, made) == []
    # Kept from now on, read-only: nothing on it is erased.
    kept = plan(legacy_config(), facts)
    [row] = review.disks(kept, first, facts)
    assert row["id"] == ntfs and not row["erases"]
    assert row["text"] == "Kept from now on: its files are shared, read-only. Nothing on it is erased."
    # Unchanged since the server was built: not named again.
    assert review.disks(kept, kept, facts) == []
    # A disk with a file system pinned for erasing.
    config = fixture_config("large")
    config["disks"] = {ntfs: {"use": "storage", "wipe": True}, blank: {"use": "pbs"}}
    erased = plan(config, facts)
    [row] = review.disks(erased, first, facts)
    assert row == {
        "id": ntfs,
        "size_gib": 3726,
        "text": "It is erased and becomes the storage nb-data. Everything on it is lost.",
        "erases": True,
    }
    # No longer kept: left alone, its files stay.
    [row] = review.disks(first, kept, facts)
    assert row["text"] == "Left alone from now on. What is on it stays."


def test_the_difference_is_every_changed_line_and_stops_at_a_length():
    assert review.difference("a: 1\nb: 2\n", "a: 1\nb: 2\n") is None
    text = review.difference("a: 1\nb: 2\n", "a: 1\nb: 3\n")
    assert "-b: 2" in text and "+b: 3" in text and text.startswith("--- built\n+++ planned")
    long = review.difference("", "".join(f"k{i}: 1\n" for i in range(review.DIFF_LINES * 2)))
    assert long.endswith("more lines") and len(long.splitlines()) == review.DIFF_LINES + 1
