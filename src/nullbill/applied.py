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

The controller keeps its own record of each of those runs (RECORD): which
sources and which inputs the playbook last ran with on each machine, input
by input, when, and how long it took. Nothing on the server reads it. It is
what the page's preview compares the next run with (preview.py), so it can
say which machines a run leaves alone without asking the server anything.
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import time
from functools import cache
from pathlib import Path

from nullbill.home import REPO_ROOT

ANSIBLE = REPO_ROOT / "ansible"
# Where each machine keeps the digest of each playbook's last good run.
DIR = "/var/lib/nullbill/applied"

# The controller's record of each playbook's last run on each machine, in
# $NULLBILL_HOME: {"<machine>/<playbook>": entry}. No secret is in it: the
# inputs are kept as HMACs under the same key as the marks.
RECORD = "applied.json"
# The host's name in the record, as in Ansible's inventory.
HOST = "pve"
# The input that stands for what a run read from the machine (digest's facts).
FACTS = "(read from the machine)"
# A CI pool's golden image keeps no mark: its entry ("<pool>/image") says
# when apply last found or made it baked (baked), with which of the
# settings the plan says (settings) and how long the bake took (took_s).
IMAGE = "image"

# A role named in a playbook's roles list, a role's dependencies, or
# include_role / import_role: `- name`, `- role: name`, or `name: name`.
# A role one of whose files another role places counts too (_BORROWED).
_REFERENCE = re.compile(r"^\s*(?:-\s*(?:role:\s*)?|name:\s*|role:\s*)([a-z][a-z0-9_]*)\s*$", re.M)


# A file taken from another role: "{{ role_path }}/../agent_ssh/files/nb-agent-command".
_BORROWED = re.compile(r"/\.\./([a-z][a-z0-9_]*)/(?:files|templates)/")


def _roles() -> set[str]:
    return {p.name for p in (ANSIBLE / "roles").iterdir() if p.is_dir()}


def named_roles(text: str, roles: set[str]) -> set[str]:
    """The roles a playbook or a role's YAML file names."""
    return {m for m in [*_REFERENCE.findall(text), *_BORROWED.findall(text)] if m in roles}


def _references(path: Path, roles: set[str]) -> set[str]:
    return named_roles(path.read_text(), roles)


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


def source(playbook: str) -> str:
    """A digest of everything in the repo a playbook's run depends on. It is
    no secret: the files are public."""
    return _source_digest(playbook).hex()[:32]


def prints(key: str, hostvars: dict, variables: dict, facts: str = "") -> dict[str, str]:
    """One fingerprint per input of a playbook run on one machine, by the
    input's name: the same inputs digest() covers, apart. Two runs whose
    fingerprints are equal ran with equal inputs."""
    what = {k: v for k, v in hostvars.items() if not k.startswith("ansible_")} | variables
    if facts:
        what[FACTS] = facts
    out = {}

    def add(name: str, value: object) -> None:
        data = json.dumps([name, value], sort_keys=True, default=str).encode()
        out[name] = hmac.new(key.encode(), data, "sha256").hexdigest()[:16]

    for name, value in what.items():
        add(name, value)
        # A table of files or settings also entry by entry, as name[entry],
        # so that what changed in it can be named.
        if isinstance(value, dict):
            for inner, part in value.items():
                add(f"{name}[{inner}]", part)
    return out


def whole(name: str) -> str:
    """The input a fingerprint's name belongs to: files of files[/etc/hosts]."""
    return name.split("[", 1)[0]


@cache
def commit() -> str | None:
    """The commit this checkout is at, which names the sources a run used
    for as long as nobody edits them by hand; None when it is no checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def entry(machine: str | None, playbook: str) -> str:
    return f"{machine or HOST}/{Path(playbook).name}"


def record(home: Path) -> dict[str, dict]:
    """What the controller wrote down of each playbook's last run."""
    try:
        found = json.loads((home / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def note(home: Path, machine: str | None, playbook: str, **fields: object) -> None:
    """Write fields of one machine's entry for a playbook down, keeping the
    rest of the entry. A field given as None is taken out."""
    found = record(home)
    kept = found.get(entry(machine, playbook)) or {}
    kept = {k: v for k, v in (kept | fields).items() if v is not None}
    found[entry(machine, playbook)] = kept
    path = home / RECORD
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(found, f, indent=1, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


def ran(home: Path, machine: str | None, playbook: str, stage: str, digest: str, inputs: dict[str, str], took_s: float | None) -> None:
    """A playbook's good run on a machine, or (took_s None) a run that found
    the machine's mark equal to these inputs and left the playbook out."""
    fields: dict = {"stage": stage, "source": source(playbook), "digest": digest, "inputs": inputs, "seen": int(time.time())}
    fields["commit"] = commit()
    if took_s is not None:
        fields |= {"at": int(time.time()), "took_s": round(took_s, 1), "answered_s": None}
    note(home, machine, playbook, **fields)
