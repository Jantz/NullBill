import subprocess

import pytest

from nullbill.home import REPO_ROOT

SCAN = REPO_ROOT / "tools" / "denylist-scan"


def scan(home, text, env_home=True):
    env = {"PATH": "/usr/bin:/bin"}
    if env_home:
        env["NULLBILL_HOME"] = str(home)
    return subprocess.run([SCAN], input=text, text=True, capture_output=True, env=env)


def test_missing_home_fails(tmp_path):
    assert scan(tmp_path, "x", env_home=False).returncode == 1


def test_missing_list_fails(tmp_path):
    result = scan(tmp_path, "x")
    assert result.returncode == 1 and "missing" in result.stderr


@pytest.mark.parametrize("content", ["", "# only a comment\n\n"])
def test_empty_list_fails(tmp_path, content):
    (tmp_path / "denylist.txt").write_text(content)
    result = scan(tmp_path, "x")
    assert result.returncode == 1 and "no entries" in result.stderr


def test_match_fails_without_echoing_entry(tmp_path):
    (tmp_path / "denylist.txt").write_text("# comment\nSecretHost\n")
    result = scan(tmp_path, "clean line\nssh to secrethost.example.internal\n")
    assert result.returncode == 1
    assert "lines 2" in result.stderr and "secrethost" not in result.stderr.lower()


def test_clean_input_passes(tmp_path):
    (tmp_path / "denylist.txt").write_text("SecretHost\n")
    assert scan(tmp_path, "nothing to see\n").returncode == 0
