import os
import subprocess

import yaml

from nullbill.home import REPO_ROOT

CHECK = REPO_ROOT / "tools" / "check"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "check.yml"


def run(*parts, skip=""):
    env = {**os.environ, "CI_SKIP": skip}
    return subprocess.run([CHECK, *parts], env=env, text=True, capture_output=True)


def test_unknown_name_fails_before_any_check():
    result = run(skip="firewall, gitleaks")
    assert result.returncode == 2
    assert "unknown check 'gitleaks'" in result.stderr
    assert result.stdout == ""


def test_unknown_part_fails_before_any_check():
    result = run("roles", "lint")
    assert result.returncode == 2
    assert "unknown part 'lint'" in result.stderr
    assert result.stdout == ""


def test_a_skipped_part_says_so_and_runs_nothing_else():
    result = run("tofu", skip="tofu")
    assert result.returncode == 0
    assert result.stdout == "SKIP: check-tofu (CI_SKIP)\n"


def test_ci_jobs_run_every_part_and_every_role_once():
    matrix = yaml.safe_load(WORKFLOW.read_text())["jobs"]["check"]["strategy"]["matrix"]["include"]
    parts = [part for job in matrix for part in job["parts"].split()]
    once = ["static", "apply", "install", "firewall", "guests", "host", "release", "tofu"]
    assert sorted(parts) == sorted(once + ["roles"] * parts.count("roles"))
    sharded = [role for job in matrix if job["parts"] == "roles" for role in job["roles"].split()]
    roles = sorted(p.parent.parent.name for p in (REPO_ROOT / "ansible" / "roles").glob("*/tests/check.sh"))
    assert sorted(sharded) == roles
    assert len({job["name"] for job in matrix}) == len(matrix)
