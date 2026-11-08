"""The agent's root key on the host: the authorized_keys edit, run by a real
shell on a stand-in file, and verify's report of it."""

import subprocess

import pytest

from nullbill import hostroot
from nullbill.errors import NullBillError
from nullbill.verify import _agent_root

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIagent"
OWNER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIowner nullbill@nullbill\n"


def edit(path, grant):
    command = hostroot.command(grant).replace(hostroot.KEYS, str(path))
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)


def test_the_switch_adds_one_line_and_takes_it_out_again(tmp_path):
    keys = tmp_path / "authorized_keys"
    keys.write_text(OWNER)
    line = hostroot.line(KEY, "10.42.1.16")
    assert line == f'from="10.42.1.16",command="{hostroot.RECORDER}",no-agent-forwarding,no-X11-forwarding {KEY} {hostroot.COMMENT}'
    assert edit(keys, line).returncode == 0
    assert edit(keys, line).returncode == 0
    assert keys.read_text() == OWNER + line + "\n" and hostroot.granted(keys.read_text())
    # A new operator address replaces the line rather than adding a second.
    moved = hostroot.line(KEY, "10.42.1.17")
    assert edit(keys, moved).returncode == 0
    assert keys.read_text() == OWNER + moved + "\n"
    assert edit(keys, None).returncode == 0
    assert keys.read_text() == OWNER and not hostroot.granted(OWNER)


def test_the_file_is_rewritten_in_place_as_pve_links_it(tmp_path):
    real = tmp_path / "priv-authorized_keys"
    real.write_text(OWNER)
    (tmp_path / "authorized_keys").symlink_to(real)
    assert edit(tmp_path / "authorized_keys", hostroot.line(KEY, "10.42.1.16")).returncode == 0
    assert (tmp_path / "authorized_keys").is_symlink() and hostroot.granted(real.read_text())


def test_it_never_leaves_the_host_without_another_key(tmp_path):
    keys = tmp_path / "authorized_keys"
    keys.write_text(hostroot.line(KEY, "10.42.1.16") + "\n")
    done = edit(keys, None)
    assert done.returncode == 1 and "no other key left" in done.stderr
    assert hostroot.granted(keys.read_text())


def test_only_an_ed25519_key_goes_in(tmp_path):
    with pytest.raises(NullBillError):
        hostroot.line('ssh-ed25519 AAAA" ,command="sh', "10.42.1.16")
    (tmp_path / hostroot.PUBLIC_KEY).parent.mkdir()
    (tmp_path / hostroot.PUBLIC_KEY).write_text(f"{KEY} {hostroot.COMMENT}\n")
    assert hostroot.public_key(tmp_path) == KEY
    assert not hostroot.enabled(tmp_path)


@pytest.mark.parametrize(
    ("answer", "status"),
    [((0, OWNER, ""), "PASS"), ((0, OWNER + hostroot.line(KEY, "10.42.1.16") + "\n", ""), "DEGRADED"), ((1, "", "denied"), "FAIL")],
)
def test_verify_reports_the_agents_root_while_it_lasts(answer, status):
    result = _agent_root("root@pve.internal", lambda target, command: answer)
    assert result.status == status and result.name == "the agent has no root on the PVE host"
