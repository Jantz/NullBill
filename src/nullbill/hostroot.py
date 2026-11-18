"""Root on the PVE host for the runtime agent, while the owner allows it.

The owner chose this (docs/adr/0003-agent-access.md): the agent on the
operator may diagnose and change anything on the host, so it can update
NullBill, the host and the services without the owner at a shell.

The operator role makes the agent's key (ansible/roles/operator); apply's
agents stage keeps its public half in $NULLBILL_HOME. The page's switch puts
one line for it in the host's /root/.ssh/authorized_keys, or takes it out.
That line accepts the key from the operator's address only, with `restrict`
(no forwarding of any kind, no pty), and runs every command through
nb-agent-command, which records it in the journal (shipped to Loki as
`{job="commands", host="pve"}`) before it runs, or refuses it. As root it
also refuses unless the host's FLAG exists: on writes it after the line,
off removes it first, so a connection the agent kept open (ssh's
ControlMaster) runs nothing more once the switch is off. Off then ends every
SSH session from the operator's address.
"""

import json
import re
import shlex
from pathlib import Path

from nullbill.errors import NullBillError

COMMENT = "nullbill-agent-root"
# Where the page's switch is kept, and the agent's public key.
SWITCH = "web/agent-powers.json"
PUBLIC_KEY = "web/agent-host-key.pub"
KEYS = "/root/.ssh/authorized_keys"
# sshd's default AuthorizedKeysFile reads this one too.
KEYS2 = "/root/.ssh/authorized_keys2"
# ansible/roles/host_baseline installs it from ansible/roles/agent_ssh.
RECORDER = "/usr/local/sbin/nb-agent-command"
# nb-agent-command runs nothing as root without it (root-only, 0600).
FLAG = "/etc/nullbill/agent-root-on"
# What verify's look at the host prints when FLAG is there.
FLAG_SAID = "# nullbill: agent-root-on"
KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,3}")
IPV4 = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")


def enabled(home: Path) -> bool:
    path = home / SWITCH
    return path.exists() and json.loads(path.read_text()).get("enabled") is True


def public_key(home: Path) -> str | None:
    path = home / PUBLIC_KEY
    if not path.exists():
        return None
    match = KEY.search(path.read_text())
    return match[0] if match else None


def line(key: str, operator_ip: str) -> str:
    if not KEY.fullmatch(key):
        raise NullBillError("the agent's host key is not an ed25519 public key")
    if not IPV4.fullmatch(operator_ip):
        raise NullBillError("the operator's address is not an IPv4 address")
    return f'restrict,from="{operator_ip}",command="{RECORDER}" {key} {COMMENT}'


def _end_sessions(operator_ip: str) -> str:
    """Shell that ends every SSH session from the operator's address: its
    logind sessions (which take the commands they started along), then each
    sshd process still holding a connection from there and every process
    under it. Only the operator's address: the page and apply reach the host
    from the nullbill container."""
    if not IPV4.fullmatch(operator_ip):
        raise NullBillError("the operator's address is not an IPv4 address")
    return (
        "if command -v loginctl >/dev/null; then "
        "for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do "
        f'if [ "$(loginctl show-session "$s" -p RemoteHost --value 2>/dev/null)" = {operator_ip} ]; then '
        'loginctl terminate-session "$s" || true; fi; '
        "done; fi; "
        f"roots=$(ss -Htnp state established '( sport = :22 and dst {operator_ip} )' 2>/dev/null "
        "| grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u); "
        'if [ -n "$roots" ]; then '
        'kill -TERM $(ps -e -o pid=,ppid= | awk -v roots="$roots" '
        "'BEGIN { n = split(roots, r); for (i = 1; i <= n; i++) k[r[i]] = 1 } { up[$1] = $2 } "
        "END { do { c = 0; for (x in up) if (!(x in k) && (up[x] in k)) { k[x] = 1; c = 1 } } while (c); for (x in k) print x }') "
        "2>/dev/null || true; fi"
    )


def command(grant: str | None, operator_ip: str | None = None) -> str:
    """A host shell command that drops every agent line from authorized_keys
    and, with grant, adds that one, then writes FLAG. Without grant, FLAG goes
    first and, given the operator's address, every session from there ends
    after the edit, even when the edit failed. On PVE the file is a link into
    /etc/pve/priv, so it is rewritten in place, not replaced."""
    add = f"printf '%s\\n' {shlex.quote(grant)} >>\"$t\"; " if grant else ""
    edit = (
        f"f={KEYS}; t=$(mktemp); "
        f'grep -v \' {COMMENT}$\' "$f" >"$t" || true; '
        f"{add}"
        # Never leave the host without the keys NullBill and the owner use.
        f"grep -q '^ssh-' \"$t\" || {{ rm -f \"$t\"; echo 'no other key left; not changing {KEYS}' >&2; exit 1; }}; "
        f'cat "$t" >"$f"; rm -f "$t"'
    )
    if grant:
        return f'set -e; {edit}; d={FLAG}; mkdir -p "${{d%/*}}"; ( umask 077; : >"$d" )'
    end = f"{_end_sessions(operator_ip)}; " if operator_ip and IPV4.fullmatch(operator_ip) else ""
    return f"rm -f {FLAG} || exit 1; ( set -e; {edit} ); rc=$?; {end}exit $rc"


# verify's look: both files sshd reads, and whether FLAG is there.
LOOK = f"set -e; cat {KEYS}; cat {KEYS2} 2>/dev/null || true; if [ -e {FLAG} ]; then echo {shlex.quote(FLAG_SAID)}; fi"


def granted(authorized_keys: str, key: str | None = None) -> bool:
    """Whether any line gives the agent's key root: by its comment, by its
    key material wherever it appears, or the host's FLAG as LOOK prints it."""
    blob = key.split()[1] if key and len(key.split()) > 1 else None
    for entry in authorized_keys.splitlines():
        if entry.rstrip().endswith(" " + COMMENT) or entry.strip() == FLAG_SAID:
            return True
        if blob and blob in entry.split():
            return True
    return False
