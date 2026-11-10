"""nullbill.version against real git repositories."""

import time

import pytest
from gitrepos import commit, git, make

from nullbill import version
from nullbill.errors import NullBillError


@pytest.fixture
def repos(tmp_path):
    return make(tmp_path)


def test_says_which_commit_runs_and_what_main_holds_beyond_it(repos):
    work, checkout = repos
    first = version.head(checkout)
    assert first == git(checkout, "rev-parse", "HEAD")
    assert version.describe(checkout, first) == {
        "id": first[:12],
        "commit": first,
        "time": int(git(work, "log", "-1", "--format=%ct")),
        "subject": "first",
    }
    page = commit(work, "body {}\n", "Page: a new colour", "src/nullbill/web/static/style.css")
    role = commit(work, "- name: install\n", "Forgejo: a newer release", "ansible/roles/forgejo/tasks/main.yml")
    # Fetching looks; it does not move the checkout.
    assert version.fetch(checkout) == role
    assert version.head(checkout) == first
    assert version.behind(checkout, first, role) and not version.behind(checkout, role, first) and not version.behind(checkout, role, role)
    found = version.changes(checkout, first, role)
    assert [(c["id"], c["subject"], c["setup"]) for c in found] == [
        (role[:12], "Forgejo: a newer release", True),
        (page[:12], "Page: a new colour", False),
    ]
    assert version.changes(checkout, role, role) == []


def test_only_what_setup_builds_from_changes_the_setup_digest(repos):
    work, checkout = repos
    seen = []

    def digest(text, message, path):
        commit(work, text, message, path)
        git(checkout, "fetch", "-q", "origin", "main")
        seen.append(version.setup_digest(checkout, "FETCH_HEAD"))
        return seen[-1]

    base = digest("services: {}\n", "catalog", "src/nullbill/data/catalog.yaml")
    assert base and len(base) == 16
    # The page, its agent's tools, the updater, the docs and the tests build nothing.
    assert digest("x = 1\n", "page", "src/nullbill/web/server.py") == base
    assert digest("x = 1\n", "tools", "src/nullbill/agenttools.py") == base
    assert digest("x = 1\n", "updater", "src/nullbill/upgrade.py") == base
    assert digest("words\n", "docs", "docs/runbooks/everyday-use.md") == base
    assert digest("def test(): pass\n", "tests", "tests/test_x.py") == base
    # A role, the catalog and the code that plans and applies do.
    role = digest("- name: install\n", "role", "ansible/roles/forgejo/tasks/main.yml")
    apply = digest("x = 1\n", "apply", "src/nullbill/apply.py")
    catalog = digest("services: {a: {}}\n", "catalog again", "src/nullbill/data/catalog.yaml")
    assert len({base, role, apply, catalog}) == 4
    # The checkout itself has not moved.
    assert version.setup_digest(checkout) != catalog


def test_finds_the_commit_that_was_checked_out_at_a_time(repos):
    work, checkout = repos
    first = version.head(checkout)
    time.sleep(1.1)
    before = time.time()
    time.sleep(1.1)
    newest = commit(work, "two\n", "second")
    git(checkout, "fetch", "-q", "origin", "main")
    git(checkout, "checkout", "-q", "--detach", "FETCH_HEAD")
    assert version.at(checkout, before) == first
    assert version.at(checkout, time.time() + 1) == newest


def test_answers_nothing_where_git_cannot_say(tmp_path, repos):
    _, checkout = repos
    assert version.head(tmp_path) is None
    assert version.setup_digest(tmp_path) is None
    assert version.describe(checkout, "0" * 40) is None
    assert not version.holds(checkout, "0" * 40) and version.holds(checkout, version.head(checkout))
    assert version.changes(checkout, "0" * 40, "HEAD") == []
    with pytest.raises(NullBillError, match="not installed with install.sh"):
        version.fetch(tmp_path)
    git(checkout, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(NullBillError, match="could not reach the repository"):
        version.fetch(checkout)
