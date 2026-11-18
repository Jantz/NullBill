"""tools/check-authors: a pull request's commits carry only the owner's
noreply identity, GitHub's (a web edit) or Dependabot's."""

import os
import subprocess

import yaml

from nullbill.home import REPO_ROOT

OWNER = "owner-a <1000+owner-a@users.noreply.github.com>"


def commit(repo, author, committer, message):
    (repo / "f").write_text(message)
    name, _, email = author.partition(" <")
    cname, _, cemail = committer.partition(" <")
    env = os.environ | {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email.rstrip(">"),
        "GIT_COMMITTER_NAME": cname,
        "GIT_COMMITTER_EMAIL": cemail.rstrip(">"),
    }
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], check=True, env=env)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def check(repo, base, head):
    return subprocess.run(
        [str(REPO_ROOT / "tools" / "check-authors"), base, head],
        cwd=repo,
        capture_output=True,
        text=True,
        env=os.environ | {"NB_OWNER": OWNER},
    )


def test_only_the_owner_github_and_dependabot_may_write_a_commit(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    base = commit(tmp_path, OWNER, OWNER, "base")
    commit(tmp_path, OWNER, OWNER, "own")
    commit(tmp_path, OWNER, "GitHub <noreply@github.com>", "web edit")
    bot = "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"
    good = commit(tmp_path, bot, OWNER, "bump")
    assert check(tmp_path, base, good).returncode == 0
    # A real name or address, as author or committer, fails, and the log does not repeat it.
    real = "Real Name <someone@example.internal>"
    bad = commit(tmp_path, real, OWNER, "leak")
    done = check(tmp_path, base, bad)
    assert done.returncode == 1 and bad[:12] in done.stdout and "Real Name" not in done.stdout + done.stderr
    worse = commit(tmp_path, OWNER, real, "leak again")
    assert "committer" in check(tmp_path, bad, worse).stdout
    # GitHub commits for an author; it is never one.
    github = commit(tmp_path, "GitHub <noreply@github.com>", "GitHub <noreply@github.com>", "no author")
    assert check(tmp_path, worse, github).returncode == 1


def test_ci_checks_pull_requests_against_the_owners_noreply_identity():
    steps = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "check.yml").read_text())["jobs"]["check"]["steps"]
    step = next(s for s in steps if s.get("run", "").startswith("tools/check-authors"))
    owner = "${{ github.repository_owner }}"
    assert "pull_request" in step["if"]
    assert step["env"]["NB_OWNER"] == f"{owner} <${{{{ github.repository_owner_id }}}}+{owner}@users.noreply.github.com>"
