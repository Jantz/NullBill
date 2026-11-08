"""nullbill upgrade against real git repositories: an origin with main and
a detached checkout like install.sh's."""

import subprocess

import pytest

from nullbill import upgrade
from nullbill.errors import NullBillError


def git(path, *args):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.internal",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.internal",
    }
    return subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True, env=env | {"PATH": "/usr/bin:/bin"}
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    origin, work, checkout = tmp_path / "origin.git", tmp_path / "work", tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    (work / "README").write_text("one\n")
    git(work, "add", "README")
    git(work, "commit", "-q", "-m", "first")
    git(work, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(origin), str(checkout)], check=True)
    git(checkout, "checkout", "-q", "--detach", "main")
    return work, checkout


def commit(work, text, message):
    (work / "README").write_text(text)
    git(work, "commit", "-q", "-am", message)
    git(work, "push", "-q", "origin", "HEAD:main")
    return git(work, "rev-parse", "HEAD")


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
    # Again: nothing to do, nothing restarted.
    assert upgrade.upgrade(checkout, tmp_path / "home", say=said.append, finish=finished.append) is None
    assert len(finished) == 1 and "newest commit" in said[-1]


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


def test_refuses_what_is_not_a_checkout(tmp_path):
    with pytest.raises(NullBillError, match="not a git checkout"):
        upgrade.upgrade(tmp_path, tmp_path, finish=pytest.fail)
