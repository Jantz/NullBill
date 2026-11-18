"""nullbill upgrade against real git repositories: an origin with main and
a detached checkout like install.sh's."""

import json
import os

import pytest
from gitrepos import commit, git, make

from nullbill import upgrade
from nullbill.errors import NullBillError


@pytest.fixture
def repos(tmp_path):
    return make(tmp_path)


def test_moves_to_mains_newest_commit_then_finishes_outside_the_pages_sandbox(repos, tmp_path):
    work, checkout = repos
    commit(work, "two\n", "second")
    newest = commit(work, "three\n", "third")
    said, finished = [], []
    assert upgrade.upgrade(checkout, tmp_path / "home", say=said.append, finish=finished.append) == newest
    assert git(checkout, "rev-parse", "HEAD") == newest and (checkout / "README").read_text() == "three\n"
    assert "third" in said[1] and "second" in said[1]
    [argv] = finished
    assert argv[: len(upgrade.FINISH)] == upgrade.FINISH
    assert f"Environment=NULLBILL_HOME={tmp_path / 'home'}" in argv and argv[-1] == str(checkout / "tools" / "finish-upgrade")
    # The restarted page reads what it was updated from.
    first = git(work, "rev-parse", "HEAD~2")
    moved = json.loads((tmp_path / "home" / upgrade.LAST).read_text())
    assert (moved["from"], moved["to"]) == (first, newest) and moved["at"] > 0
    # Again: nothing to do, nothing restarted, and the record stays.
    assert upgrade.upgrade(checkout, tmp_path / "home", say=said.append, finish=finished.append) is None
    assert len(finished) == 1 and "newest commit" in said[-1]
    assert json.loads((tmp_path / "home" / upgrade.LAST).read_text()) == moved


def test_an_update_whose_second_half_failed_is_finished_by_the_next_upgrade(repos, tmp_path):
    work, checkout = repos
    newest = commit(work, "two\n", "second")
    home, said, finished = tmp_path / "home", [], []

    def fails(argv):
        raise NullBillError("tools/finish-upgrade failed")

    with pytest.raises(NullBillError, match="finish-upgrade failed"):
        upgrade.upgrade(checkout, home, say=said.append, finish=fails)
    # The checkout has moved, so nothing newer is found from now on; the
    # record is what says the programs around it are the old version's.
    assert git(checkout, "rev-parse", "HEAD") == newest
    assert upgrade.unfinished(home, newest) and not upgrade.unfinished(home, None)
    with pytest.raises(NullBillError):
        upgrade.upgrade(checkout, home, say=said.append, finish=fails)
    assert upgrade.unfinished(home, newest)
    assert upgrade.upgrade(checkout, home, say=said.append, finish=finished.append) == newest
    assert len(finished) == 1 and finished[0][-1] == str(checkout / "tools" / "finish-upgrade")
    assert "did not finish last time" in said[-2] and not upgrade.unfinished(home, newest)
    moved = json.loads((home / upgrade.LAST).read_text())
    assert moved["finished"] is True and moved["to"] == newest
    assert upgrade.upgrade(checkout, home, say=said.append, finish=finished.append) is None and len(finished) == 1
    # A record from before `finished` was written counts as finished.
    (home / upgrade.LAST).write_text(json.dumps({"from": moved["from"], "to": newest, "at": 1.0}))
    assert not upgrade.unfinished(home, newest)


def test_files_it_checks_out_stay_readable_under_the_pages_umask(repos, tmp_path):
    # The page's UMask=0077 made changed files root-only, and nbagent could
    # not start its tools (python -m nullbill agent-tools).
    work, checkout = repos
    commit(work, "new\n", "second", "docs/new.md")
    old = os.umask(0o077)
    try:
        upgrade.upgrade(checkout, tmp_path / "home", say=lambda _: None, finish=lambda _: None)
    finally:
        os.umask(old)
    for path in (checkout / "README", checkout / "docs", checkout / "docs" / "new.md"):
        assert path.stat().st_mode & 0o004, path


def test_refuses_local_changes_and_going_back(repos, tmp_path):
    work, checkout = repos
    commit(work, "two\n", "second")
    (checkout / "README").write_text("edited here\n")
    with pytest.raises(NullBillError, match="local changes"):
        upgrade.upgrade(checkout, tmp_path, finish=pytest.fail)
    git(checkout, "checkout", "-q", "README")
    # A checkout ahead of main (a commit made on this machine) stays where it is.
    git(checkout, "fetch", "-q", "origin", "main")
    git(checkout, "checkout", "-q", "--detach", "FETCH_HEAD")
    (checkout / "README").write_text("mine\n")
    git(checkout, "commit", "-q", "-am", "made here")
    with pytest.raises(NullBillError, match="newer than main"):
        upgrade.upgrade(checkout, tmp_path, finish=pytest.fail)


def test_the_nightlys_update_moves_only_pins_and_no_further_than_it_looked(repos, tmp_path):
    work, checkout = repos
    path = "ansible/roles/forgejo/defaults/main.yml"
    base = commit(work, "forgejo_version: 15.0.11\n", "Forgejo", path)
    upgrade.upgrade(checkout, tmp_path, say=lambda _: None, finish=lambda _: None)
    pin = commit(work, "forgejo_version: 15.0.12\n", "Forgejo 15.0.12", path)
    # main moved on with code after the night looked: the night stops at what it looked at.
    code = commit(work, "- name: x\n", "Forgejo: tasks", "ansible/roles/forgejo/tasks/main.yml")
    assert upgrade.upgrade(checkout, tmp_path, say=lambda _: None, finish=lambda _: None, only_pins=True, to=pin) == pin
    assert git(checkout, "rev-parse", "HEAD") == pin != base
    with pytest.raises(NullBillError, match="waits for Update NullBill"):
        upgrade.upgrade(checkout, tmp_path, finish=pytest.fail, only_pins=True)
    assert git(checkout, "rev-parse", "HEAD") == pin
    with pytest.raises(NullBillError, match="not a commit of main"):
        upgrade.upgrade(checkout, tmp_path, finish=pytest.fail, to="0" * 40)
    # The owner's own press installs it.
    assert upgrade.upgrade(checkout, tmp_path, say=lambda _: None, finish=lambda _: None) == code


def test_refuses_what_is_not_a_checkout(tmp_path):
    with pytest.raises(NullBillError, match="not a git checkout"):
        upgrade.upgrade(tmp_path, tmp_path, finish=pytest.fail)
