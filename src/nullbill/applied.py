"""What a playbook run depends on, so a rerun can leave out the runs whose
inputs have not changed.

After a playbook succeeds on a machine, apply leaves a digest of its inputs
there: the playbook, every role it reaches, the connection plugin, and the
variables it ran with. On the next run apply reads that digest back first
and skips the playbook when it is the same. A guest OpenTofu made afresh has
none, so it gets the whole playbook; so does every machine after a run whose
checks failed (apply.FULL_NEXT), or with `apply --full`.

The digest is an HMAC under a key kept in secrets, since the variables hold
secrets and the digest is left on the guests.
"""

import hashlib
import hmac
import json
import re
from functools import cache
from pathlib import Path

from nullbill.home import REPO_ROOT

ANSIBLE = REPO_ROOT / "ansible"
# Where each machine keeps the digest of each playbook's last good run.
DIR = "/var/lib/nullbill/applied"

# A role named in a playbook's roles list, a role's dependencies, or
# include_role / import_role: `- name`, `- role: name`, or `name: name`.
_REFERENCE = re.compile(r"^\s*(?:-\s*(?:role:\s*)?|name:\s*|role:\s*)([a-z][a-z0-9_]*)\s*$", re.M)


def _roles() -> set[str]:
    return {p.name for p in (ANSIBLE / "roles").iterdir() if p.is_dir()}


def _references(path: Path, roles: set[str]) -> set[str]:
    return {m for m in _REFERENCE.findall(path.read_text()) if m in roles}


@cache
def sources(playbook: str) -> tuple[Path, ...]:
    """The playbook, each file of every role it can reach, and the
    connection plugins: everything in the repo its run depends on. A word
    that only looks like a role reference adds a role, which costs a rerun,
    never a skip."""
    roles = _roles()
    path = ANSIBLE / playbook
    if not path.exists():
        return ()  # ansible-playbook reports it
    files = [path]
    todo, seen = sorted(_references(path, roles)), set()
    while todo:
        role = todo.pop()
        if role in seen:
            continue
        seen.add(role)
        base = ANSIBLE / "roles" / role
        for path in sorted(base.rglob("*")):
            # tests/ holds the role's container check, not what it runs.
            if path.is_file() and not {"tests", "__pycache__"} & set(path.relative_to(base).parts):
                files.append(path)
                if path.suffix in (".yml", ".yaml"):
                    todo += sorted(_references(path, roles) - seen)
    files += sorted(p for p in (ANSIBLE / "connection_plugins").glob("*.py"))
    return tuple(files)


@cache
def _source_digest(playbook: str) -> bytes:
    h = hashlib.sha256()
    for path in sources(playbook):
        name = path.relative_to(ANSIBLE) if path.is_relative_to(ANSIBLE) else path
        h.update(str(name).encode() + b"\0" + path.read_bytes() + b"\0")
    return h.digest()


def digest(key: str, playbook: str, hostvars: dict, variables: dict, facts: str = "") -> str:
    """The inputs of one playbook run on one machine. facts: what the run
    also depends on that is read from the machine, such as its kernel."""
    # How Ansible gets there is not what it does there: the host's address
    # changes when the home network closes.
    what = {k: v for k, v in hostvars.items() if not k.startswith("ansible_")}
    data = json.dumps([playbook, what, variables, facts], sort_keys=True, default=str).encode()
    return hmac.new(key.encode(), _source_digest(playbook) + data, "sha256").hexdigest()
