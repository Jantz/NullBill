"""The controller's Python packages come from one hash-locked file, and
nothing it installs is fetched unchecked."""

import re
import tomllib

from nullbill.home import REPO_ROOT

LOCK = REPO_ROOT / "requirements-controller.txt"


def locked() -> dict[str, tuple[str, int]]:
    """Each locked package: its version and how many hashes it carries."""
    out: dict[str, tuple[str, int]] = {}
    name = None
    for line in LOCK.read_text().splitlines():
        found = re.match(r"([A-Za-z0-9._-]+)==([^ ;]+)", line)
        if found:
            name = found[1].lower().replace("_", "-")
            out[name] = (found[2], 0)
        elif "--hash=sha256:" in line and name:
            out[name] = (out[name][0], out[name][1] + 1)
    return out


def test_the_lock_holds_what_apply_and_nullbill_need_each_with_its_hashes():
    lock = locked()
    assert lock and all(count > 0 for _, count in lock.values())
    # requirements-apply.txt's pins, at the same versions.
    for line in (REPO_ROOT / "requirements-apply.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            name, version = line.split("==")
            assert lock[name.lower()][0] == version, name
    # pyproject.toml's dependencies and its build backend.
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    for requirement in project["project"]["dependencies"] + project["build-system"]["requires"]:
        assert re.match(r"[A-Za-z0-9._-]+", requirement)[0].lower() in lock, requirement


def test_the_controller_installs_only_hash_checked_files():
    deps = (REPO_ROOT / "tools" / "install-apply-deps").read_text()
    assert "--require-hashes --only-binary=:all: -r requirements-controller.txt" in deps
    assert "collection install --force --no-deps" in deps
    setup = (REPO_ROOT / "tools" / "setup-controller").read_text()
    installs = [line.strip() for line in setup.splitlines() if re.match(r"\s*(python3 -m )?pip install", line)]
    assert installs == ['pip install -q --no-deps --no-build-isolation -e "$root"']
    assert setup.index("tools/install-apply-deps") < setup.index("pip install")
