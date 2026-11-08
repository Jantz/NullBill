"""verify runs curl inside some guests; the Debian LXC template has none,
and the role checks' image does, so only this test catches a role that
forgets it."""

from pathlib import Path

import pytest
import yaml

ROLES = Path(__file__).resolve().parent.parent / "ansible" / "roles"


def apt_packages(role: str) -> set[str]:
    names = set()
    for task in yaml.safe_load((ROLES / role / "tasks" / "main.yml").read_text()):
        apt = task.get("ansible.builtin.apt") or {}
        if apt.get("state", "present") != "absent":
            name = apt.get("name", [])
            names |= {name} if isinstance(name, str) else set(name)
    return names


# Guest role -> the verify module that runs curl in that guest.
@pytest.mark.parametrize(
    "role, module", [("forgejo", "forge.py"), ("runner_dispatcher", "ci.py"), ("runner_dispatcher", "pveapi.py"), ("operator", "pveapi.py")]
)
def test_guests_verify_runs_curl_in_install_it(role, module):
    source = (ROLES.parent.parent / "src" / "nullbill" / module).read_text()
    assert "curl " in source
    assert "curl" in apt_packages(role)
