"""The agent's root key on the host: the authorized_keys edit, run by a real
shell on a stand-in file, and verify's report of it."""

import json
import subprocess
from pathlib import Path

import pytest

from nullbill import hostroot
from nullbill.errors import NullBillError
from nullbill.verify import _agent_root

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIagent"
OWNER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIowner nullbill@nullbill\n"


def edit(path, grant, operator=None):
    flag = path.parent / "flag/agent-root-on"
    command = hostroot.command(grant, operator).replace(hostroot.KEYS, str(path)).replace(hostroot.FLAG, str(flag))
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)


def test_the_switch_adds_one_line_and_takes_it_out_again(tmp_path):
    keys = tmp_path / "authorized_keys"
    keys.write_text(OWNER)
    line = hostroot.line(KEY, "10.42.1.16")
    # restrict: no port, agent or X11 forwarding and no pty, so nothing
    # reaches the host without nb-agent-command recording it.
    assert line == f'restrict,from="10.42.1.16",command="{hostroot.RECORDER}" {KEY} {hostroot.COMMENT}'
    flag = tmp_path / "flag/agent-root-on"
    assert edit(keys, line).returncode == 0
    assert edit(keys, line).returncode == 0
    assert keys.read_text() == OWNER + line + "\n" and hostroot.granted(keys.read_text())
    # The flag nb-agent-command asks for as root comes after the line, root's alone.
    assert flag.exists() and flag.stat().st_mode & 0o777 == 0o600
    # A new operator address replaces the line rather than adding a second.
    moved = hostroot.line(KEY, "10.42.1.17")
    assert edit(keys, moved).returncode == 0
    assert keys.read_text() == OWNER + moved + "\n"
    assert edit(keys, None, "10.42.1.16").returncode == 0
    assert keys.read_text() == OWNER and not hostroot.granted(OWNER) and not flag.exists()


def test_the_file_is_rewritten_in_place_as_pve_links_it(tmp_path):
    real = tmp_path / "priv-authorized_keys"
    real.write_text(OWNER)
    (tmp_path / "authorized_keys").symlink_to(real)
    assert edit(tmp_path / "authorized_keys", hostroot.line(KEY, "10.42.1.16")).returncode == 0
    assert (tmp_path / "authorized_keys").is_symlink() and hostroot.granted(real.read_text())


def test_it_never_leaves_the_host_without_another_key(tmp_path):
    keys = tmp_path / "authorized_keys"
    keys.write_text(hostroot.line(KEY, "10.42.1.16") + "\n")
    flag = tmp_path / "flag/agent-root-on"
    flag.parent.mkdir()
    flag.write_text("")
    done = edit(keys, None, "10.42.1.16")
    assert done.returncode == 1 and "no other key left" in done.stderr
    assert hostroot.granted(keys.read_text())
    # The flag went first all the same: nb-agent-command runs nothing as root.
    assert not flag.exists()


def test_off_ends_the_operators_sessions_and_no_one_elses():
    off = hostroot.command(None, "10.42.1.16")
    assert off.startswith(f"rm -f {hostroot.FLAG} || exit 1; ")
    assert "( sport = :22 and dst 10.42.1.16 )" in off and "RemoteHost" in off
    # After the edit, whatever it did, and with its exit code.
    assert off.index("rc=$?") < off.index("ss -Htnp") and off.endswith("exit $rc")
    # Without a usable address the key and flag still go, and nothing else is killed.
    for operator in (None, "10.42.1.16; reboot"):
        assert "ss -Htnp" not in hostroot.command(None, operator) and hostroot.FLAG in hostroot.command(None, operator)
    with pytest.raises(NullBillError):
        hostroot.line(KEY, '10.42.1.16" ,command="sh')


def test_the_operator_check_runs_the_switchs_own_commands():
    """ansible/roles/operator/tests/check.sh runs these on a stand-in host,
    forwarding, ControlMaster and the owner's session included."""
    path = Path(__file__).parents[1] / "ansible/roles/operator/tests/hostroot.json"
    data = json.loads(path.read_text())
    key = data["key"]
    assert data["on"] == hostroot.command(hostroot.line(key, "127.0.0.1"))
    assert data["off"] == hostroot.command(None, "127.0.0.1")


def test_only_an_ed25519_key_goes_in(tmp_path):
    with pytest.raises(NullBillError):
        hostroot.line('ssh-ed25519 AAAA" ,command="sh', "10.42.1.16")
    (tmp_path / hostroot.PUBLIC_KEY).parent.mkdir()
    (tmp_path / hostroot.PUBLIC_KEY).write_text(f"{KEY} {hostroot.COMMENT}\n")
    assert hostroot.public_key(tmp_path) == KEY
    assert not hostroot.enabled(tmp_path)


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        ((0, OWNER, ""), "PASS"),
        ((0, OWNER + hostroot.line(KEY, "10.42.1.16") + "\n", ""), "DEGRADED"),
        # The agent's key without NullBill's comment, or in other options.
        ((0, OWNER + KEY + "\n", ""), "DEGRADED"),
        ((0, OWNER + f'command="/bin/sh" {KEY} someone\n', ""), "DEGRADED"),
        # The host's flag left behind.
        ((0, OWNER + hostroot.FLAG_SAID + "\n", ""), "DEGRADED"),
        ((1, "", "denied"), "FAIL"),
    ],
)
def test_verify_reports_the_agents_root_while_it_lasts(answer, status):
    asked = []
    result = _agent_root("root@pve.internal", lambda target, command: asked.append(command) or answer, KEY)
    assert result.status == status and result.name == "the agent has no root on the PVE host"
    assert asked == [hostroot.LOOK]


def test_verifys_look_reads_both_key_files_and_the_flag(tmp_path):
    keys, flag = tmp_path / "authorized_keys", tmp_path / "agent-root-on"
    keys.write_text(OWNER)
    (tmp_path / "authorized_keys2").write_text(KEY + "\n")
    look = hostroot.LOOK.replace(hostroot.KEYS2, str(tmp_path / "authorized_keys2")).replace(hostroot.KEYS, str(keys))
    look = look.replace(hostroot.FLAG, str(flag))
    out = subprocess.run(["bash", "-c", look], capture_output=True, text=True, check=True).stdout
    assert hostroot.granted(out, KEY) and not hostroot.granted(out)
    (tmp_path / "authorized_keys2").unlink()
    flag.write_text("")
    out = subprocess.run(["bash", "-c", look], capture_output=True, text=True, check=True).stdout
    assert out == OWNER + hostroot.FLAG_SAID + "\n" and hostroot.granted(out)
