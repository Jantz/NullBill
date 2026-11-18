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


def test_a_merge_is_not_what_a_version_is_called_nor_a_change_of_its_own(repos):
    """main moves by fast-forward to a branch that merged main into itself."""
    work, checkout = repos
    first = version.head(checkout)
    git(work, "checkout", "-q", "-b", "work-branch")
    (work / "page.css").write_text("body {}\n")
    git(work, "add", "page.css")
    git(work, "commit", "-q", "-m", "Page: a new colour")
    change = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "main")
    other = commit(work, "two\n", "Docs: a word")
    git(work, "checkout", "-q", "work-branch")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge remote-tracking branch 'origin/main' into work-branch", "main")
    merge = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", "HEAD:main")
    assert version.fetch(checkout) == merge
    about = version.describe(checkout, merge)
    assert about["id"] == merge[:12] and about["subject"] in ("Page: a new colour", "Docs: a word")
    assert {c["id"] for c in version.changes(checkout, first, merge)} == {change[:12], other[:12]}
    # A span that holds a merge and nothing else still reads as one change.
    git(checkout, "checkout", "-q", "--detach", other)
    git(checkout, "merge", "-q", "--no-ff", "-m", "Merge the page", change)
    only = git(checkout, "rev-parse", "HEAD")
    assert [c["subject"] for c in version.changes(checkout, merge, only)] == ["Merge the page"]


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
    assert version.changes(checkout, "0" * 40, "HEAD") == []
    with pytest.raises(NullBillError, match="not installed with install.sh"):
        version.fetch(tmp_path)
    git(checkout, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    with pytest.raises(NullBillError, match="could not reach the repository"):
        version.fetch(checkout)


def test_links_runbooks_to_the_commit_that_runs_only_on_github(tmp_path, repos):
    _, checkout = repos
    head = version.head(checkout)
    # The test's origin is a local path: nothing to link to.
    assert version.docs(checkout, head) is None
    for url in ("https://github.com/Example/NullBill", "https://github.com/Example/NullBill.git", "git@github.com:Example/NullBill.git"):
        git(checkout, "remote", "set-url", "origin", url)
        assert version.docs(checkout, head) == f"https://github.com/Example/NullBill/blob/{head}/"
    git(checkout, "remote", "set-url", "origin", "https://example.internal/Example/NullBill")
    assert version.docs(checkout, head) is None
    assert version.docs(tmp_path, head) is None


FORGEJO = "ansible/roles/forgejo/defaults/main.yml"
CATALOG = "src/nullbill/data/catalog.yaml"
LOCK = "ansible/roles/model_gateway/files/requirements.txt"
SHA = "a" * 64


def test_pin_data_is_only_what_tools_pins_writes_and_only_as_a_pin_may_hold_it(repos):
    work, checkout = repos
    commit(work, f"# Forgejo\nforgejo_version: 15.0.11\nforgejo_sha256: {SHA}\nforgejo_port: 3000\n", "Forgejo", FORGEJO)
    commit(work, 'games:\n  minecraft:\n    version: "26.3"\n    eula: false\n', "Catalog", CATALOG)
    commit(work, f"x==1.0 \\\n    --hash=sha256:{SHA}\n", "Lock", LOCK)
    base = commit(work, "plan: 1\n", "Golden", "tests/golden/small.plan.yaml")

    def moved(text, path, *more):
        """One change on top of base, with more (path, text) beside it."""
        git(work, "reset", "-q", "--hard", base)
        for name, content in ((path, text), *more):
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(content)
            git(work, "add", name)
        git(work, "commit", "-q", "-m", "move")
        git(work, "push", "-q", "-f", "origin", "HEAD:main")
        version.fetch(checkout)
        return version.not_pins(checkout, base, git(work, "rev-parse", "HEAD"))

    # A version, its checksum, the tests' regenerated files and LiteLLM's lock move by themselves.
    lock = (LOCK, f"x==1.1 \\\n    --hash=sha256:{'c' * 64}\n")
    golden = ("tests/golden/small.plan.yaml", "plan: 2\n")
    assert moved(f"# Forgejo 15\nforgejo_version: 15.0.12\nforgejo_sha256: {'b' * 64}\nforgejo_port: 3000\n", FORGEJO, lock, golden) == []
    # Any other key, a value no pin holds, a line no lock has, or any other file is code.
    assert "more than its pins" in moved(f"forgejo_version: 15.0.12\nforgejo_sha256: {SHA}\nforgejo_port: 22\n", FORGEJO)[-1]
    injected = "\"15.0.13{{ lookup('pipe', 'id') }}\""
    assert "forgejo_version cannot be" in moved(f"forgejo_version: {injected}\nforgejo_sha256: {SHA}\nforgejo_port: 3000\n", FORGEJO)[0]
    assert moved(f"--index-url https://example.internal/\nx==1.1 \\\n    --hash=sha256:{SHA}\n", LOCK)
    assert moved('games:\n  minecraft:\n    version: "26.4"\n    eula: true\n', CATALOG)
    assert moved("- name: x\n", "ansible/roles/forgejo/tasks/main.yml")
    assert moved(f"forgejo_version: 15.0.12\nforgejo_sha256: {SHA}\nforgejo_port: 3000\n", "src/nullbill/data/pins.yaml")


def test_a_pin_file_turned_into_a_link_is_no_pin_data(repos, tmp_path):
    work, checkout = repos
    base = commit(work, f"x==1.0 \\\n    --hash=sha256:{SHA}\n", "Lock", LOCK)
    (work / LOCK).unlink()
    (work / LOCK).symlink_to("/etc/shadow")
    git(work, "add", LOCK)
    git(work, "commit", "-q", "-m", "link")
    git(work, "push", "-q", "origin", "HEAD:main")
    version.fetch(checkout)
    assert "not a plain file" in version.not_pins(checkout, base, git(work, "rev-parse", "HEAD"))[0]
    assert version.not_pins(checkout, base, "0" * 40)
