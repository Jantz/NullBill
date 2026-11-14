"""Which NullBill this controller runs, what main holds beyond it, and which
of main's commits change what a setup run builds.

The controller is a git checkout (install.sh, nullbill/upgrade.py), so git
answers all of it. A commit changes what setup builds when it touches
SETUP_PATHS outside NOT_SETUP: the Ansible roles, the catalog, and the code
that plans and applies. Everything else (the page, its agent, the docs, the
tests) is in force as soon as the page restarted. The rule is by path, so it
errs towards asking for a setup run that then finds nothing to change.

Every function answers None, or an empty list, where git cannot say: a
NullBill that was not installed as a checkout, or a commit this clone does
not hold.
"""

import hashlib
import re
import subprocess
import time
from pathlib import Path

from nullbill.errors import NullBillError
from nullbill.upgrade import BRANCH

SETUP_PATHS = ["ansible", "src/nullbill"]
NOT_SETUP = [
    "src/nullbill/web",
    "src/nullbill/agenttools.py",
    "src/nullbill/preview.py",
    "src/nullbill/upgrade.py",
    "src/nullbill/version.py",
]
FETCH_TIMEOUT_S = 60


def _git(root: Path, *args: str, timeout: float = 30) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False, timeout=timeout, umask=0o022)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def head(root: Path) -> str | None:
    """The commit that is checked out."""
    return _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") if (root / ".git").exists() else None


def docs(root: Path, commit: str) -> str | None:
    """Where a browser reads this commit's files, so that the page can link
    a runbook it names: GitHub's view of the checkout's origin at the commit
    that runs. None for an origin that is not on GitHub."""
    url = _git(root, "remote", "get-url", "origin") or ""
    found = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url)
    return f"https://github.com/{found[1]}/{found[2]}/blob/{commit}/" if found else None


def holds(root: Path, commit: str) -> bool:
    return _git(root, "cat-file", "-e", f"{commit}^{{commit}}") is not None


def reader(root: Path, commit: str):
    """Reads a file's text as that commit holds it, None when it has none
    (nullbill/pins.py reads the pins of two commits this way)."""

    def read(path: str) -> str | None:
        return _git(root, "show", f"{commit}:{path}")

    return read


def describe(root: Path, commit: str) -> dict | None:
    """A commit as the page shows it."""
    out = _git(root, "show", "-s", "--format=%H%x00%ct%x00%s", commit)
    if not out:
        return None
    full, when, subject = out.split("\0", 2)
    return {"id": full[:12], "commit": full, "time": int(when), "subject": subject}


def _in_setup(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in SETUP_PATHS) and not any(
        path == p or path.startswith(p + "/") for p in NOT_SETUP
    )


def setup_digest(root: Path, commit: str = "HEAD") -> str | None:
    """A digest of every file at `commit` that says what setup builds. Two
    commits with the same digest build the same server."""
    listing = _git(root, "ls-tree", "-r", commit, "--", *SETUP_PATHS)
    if listing is None:
        return None
    digest = hashlib.sha256()
    for line in listing.splitlines():
        if _in_setup(line.partition("\t")[2]):
            digest.update(line.encode() + b"\n")
    return digest.hexdigest()[:16]


def changes(root: Path, old: str, new: str) -> list[dict]:
    """The commits `new` holds and `old` does not, newest first, each with
    `setup`: whether it changes what setup builds."""
    span = f"{old}..{new}"
    listed = _git(root, "log", "--format=%H%x00%ct%x00%s", span)
    excluded = [f":(exclude){p}" for p in NOT_SETUP]
    touching = _git(root, "log", "--format=%H", span, "--", *SETUP_PATHS, *excluded)
    if listed is None or touching is None:
        return []
    setup = set(touching.split())
    out = []
    for line in listed.splitlines():
        full, when, subject = line.split("\0", 2)
        out.append({"id": full[:12], "time": int(when), "subject": subject, "setup": full in setup})
    return out


def behind(root: Path, old: str, new: str) -> bool:
    """Is `old` an ancestor of `new`, and not `new` itself?"""
    if old == new:
        return False
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", old, new], capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def at(root: Path, when: float) -> str | None:
    """The commit that was checked out at a time, by git's own record of
    where HEAD has been. For a setup run from before the page wrote down
    the commit each run starts with."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime(when))
    return _git(root, "rev-parse", "--verify", "--quiet", f"HEAD@{{{stamp}}}")


def fetch(root: Path) -> str:
    """Ask origin for main's newest commit, without moving the checkout."""
    if not (root / ".git").exists():
        raise NullBillError("this NullBill was not installed with install.sh, so the page cannot look for updates")
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "fetch", "--quiet", "origin", BRANCH],
            capture_output=True,
            text=True,
            check=False,
            timeout=FETCH_TIMEOUT_S,
            umask=0o022,
        )
    except subprocess.TimeoutExpired as exc:
        raise NullBillError(f"the repository did not answer within {FETCH_TIMEOUT_S} seconds") from exc
    if done.returncode != 0:
        raise NullBillError(
            f"could not reach the repository: {done.stderr.strip().splitlines()[-1] if done.stderr.strip() else 'git fetch failed'}"
        )
    newest = _git(root, "rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}")
    if newest is None:
        raise NullBillError("the repository answered, but not with a commit")
    return newest
