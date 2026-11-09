"""nullbill upgrade: move this controller's NullBill to main's newest commit.

install.sh put a git checkout at /opt/nullbill and set it up with
tools/setup-controller. upgrade fetches main from the same origin, moves the
checkout there, and has systemd run tools/finish-upgrade from the new commit:
setup-controller again, the page's unit file, and a restart of the page.

That last part runs as a transient systemd unit, outside the page's own
sandbox (ProtectSystem=full keeps /usr and /etc read-only for the page and
the jobs it starts), and outside its cgroup, so restarting the page does not
end it. A running apply or verify is not affected: the page refuses to start
a job while another runs.

Only main is followed: what the page installs is what the public repository
holds, never a commit made on this machine.
"""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from nullbill.errors import NullBillError

BRANCH = "main"
FINISH = ["systemd-run", "--quiet", "--wait", "--pipe", "--collect", "--unit", "nullbill-upgrade"]


def _git(root: Path, *args: str) -> str:
    # The page runs with UMask=0077, and a checkout under it left every file
    # it changed root-only: the page's agent (nbagent) could no longer import
    # its own tools or read the docs. The checkout is the public repository.
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False, umask=0o022)
    if done.returncode != 0:
        raise NullBillError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def _finish(argv: Sequence[str]) -> None:
    if subprocess.run(list(argv), check=False).returncode != 0:
        raise NullBillError("tools/finish-upgrade failed; its output is above. Press Update NullBill again once it is fixed")


def upgrade(root: Path, home: Path, say: Callable[[str], None] = print, finish: Callable[[Sequence[str]], None] = _finish) -> str | None:
    """The commit NullBill moved to, or None when it already was there."""
    if not (root / ".git").exists():
        raise NullBillError(f"{root} is not a git checkout; install NullBill with install.sh to update it this way")
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise NullBillError(f"{root} has local changes; NullBill updates only a clean checkout of {BRANCH}")
    old = _git(root, "rev-parse", "HEAD")
    _git(root, "fetch", "--quiet", "origin", BRANCH)
    new = _git(root, "rev-parse", "FETCH_HEAD")
    if new == old:
        say(f"NullBill is at {BRANCH}'s newest commit, {old[:12]}")
        return None
    ahead = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", new, old], check=False).returncode == 0
    if ahead:
        raise NullBillError(f"{root} is at {old[:12]}, newer than {BRANCH} ({new[:12]}); NullBill does not go back by itself")
    say(f"Updating NullBill from {old[:12]} to {new[:12]}:")
    say(_git(root, "log", "--format=  %h %s", f"{old}..{new}") or f"  (from {old[:12]}, which {BRANCH} no longer holds)")
    _git(root, "checkout", "--quiet", "--detach", new)
    finish([*FINISH, "-p", f"Environment=NULLBILL_HOME={home}", "-p", "Environment=HOME=/root", str(root / "tools" / "finish-upgrade")])
    say(f"NullBill is at {new[:12]}; the page restarted with it")
    return new
