import os
import subprocess

from nullbill.home import REPO_ROOT

CHECK = REPO_ROOT / "tools" / "check"


def run(skip):
    env = {**os.environ, "CI_SKIP": skip}
    return subprocess.run([CHECK], env=env, text=True, capture_output=True)


def test_unknown_name_fails_before_any_check():
    result = run("firewall, gitleaks")
    assert result.returncode == 2
    assert "unknown check 'gitleaks'" in result.stderr
    assert result.stdout == ""
