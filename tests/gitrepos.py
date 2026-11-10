"""Real git repositories for tests: an origin with main, a clone to commit
in, and a detached checkout like install.sh's."""

import subprocess
from pathlib import Path


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


def make(tmp_path: Path) -> tuple[Path, Path]:
    """(work, checkout): commit and push in work; checkout is the controller's."""
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


def commit(work: Path, text: str, message: str, path: str = "README") -> str:
    """Write one file, commit it and push it to origin's main."""
    target = work / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(work, "add", path)
    git(work, "commit", "-q", "-m", message)
    git(work, "push", "-q", "origin", "HEAD:main")
    return git(work, "rev-parse", "HEAD")
