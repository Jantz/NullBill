"""Root on the PVE host for the runtime agent, while the owner allows it.

The owner chose this (docs/adr/0003-agent-access.md): the agent on the
operator may diagnose and change anything on the host, so it can update
NullBill, the host and the services without the owner at a shell.

The operator role makes the agent's key (ansible/roles/operator); apply's
agents stage keeps its public half in $NULLBILL_HOME. The page's switch puts
one line for it in the host's /root/.ssh/authorized_keys, or takes it out.
That line accepts the key from the operator's address only, and runs every
command through nb-agent-command, which records it in the journal (shipped
to Loki as `{job="commands", host="pve"}`) before it runs, or refuses it.
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
# ansible/roles/host_baseline installs it from ansible/roles/agent_ssh.
RECORDER = "/usr/local/sbin/nb-agent-command"
KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]+={0,3}")


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
    return f'from="{operator_ip}",command="{RECORDER}",no-agent-forwarding,no-X11-forwarding {key} {COMMENT}'


def command(grant: str | None) -> str:
    """A host shell command that drops every agent line from authorized_keys
    and, with grant, adds that one. On PVE the file is a link into
    /etc/pve/priv, so it is rewritten in place, not replaced."""
    add = f"printf '%s\\n' {shlex.quote(grant)} >>\"$t\"; " if grant else ""
    return (
        f"set -e; f={KEYS}; t=$(mktemp); "
        f'grep -v \' {COMMENT}$\' "$f" >"$t" || true; '
        f"{add}"
        # Never leave the host without the keys NullBill and the owner use.
        f"grep -q '^ssh-' \"$t\" || {{ rm -f \"$t\"; echo 'no other key left; not changing {KEYS}' >&2; exit 1; }}; "
        f'cat "$t" >"$f"; rm -f "$t"'
    )


def granted(authorized_keys: str) -> bool:
    return any(entry.rstrip().endswith(" " + COMMENT) for entry in authorized_keys.splitlines())
