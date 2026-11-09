"""The owner's own accounts on the services NullBill runs.

The owner pastes SSH public keys on the page (Access, SSH keys); they are
`people.<owner>.ssh_keys` in config.yaml. Apply puts them on the workspace
(root's authorized_keys, inside a marked block) and on the owner's own
Forgejo user, which apply makes once with a name taken from the owner's
Tailscale login and keeps in the secrets, so a later change of login does
not make a second user.
"""

import re

from nullbill.errors import NullBillError

# OpenSSH's public key types that Forgejo and Debian 12's sshd both accept.
KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
)
KEY = re.compile("(" + "|".join(re.escape(t) for t in KEY_TYPES) + r") [A-Za-z0-9+/]{40,}={0,3}( [^\r\n]{0,200})?")
MAX_KEYS = 20

# Names Forgejo keeps for itself or for its routes; the owner's user is then "owner".
RESERVED = {"admin", "api", "assets", "explore", "git", "login", "nullbill-admin", "org", "repo", "root", "user", "users"}


def parse(text: str) -> list[str]:
    """The public keys in what the owner pasted, one per line, blank lines
    and duplicates dropped. A private key or anything else is refused."""
    keys: list[str] = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = " ".join(raw.split())
        if not line:
            continue
        if "PRIVATE KEY" in line:
            raise NullBillError("that is a private key: paste the public one, the file ending in .pub")
        if not KEY.fullmatch(line):
            raise NullBillError(f"line {n} is not an SSH public key; it starts with ssh-ed25519, ssh-rsa or ecdsa-sha2-")
        if not any(k.split()[:2] == line.split()[:2] for k in keys):
            keys.append(line)
    if len(keys) > MAX_KEYS:
        raise NullBillError(f"at most {MAX_KEYS} keys")
    return keys


def person(model: dict) -> dict | None:
    return next((p for p in (model.get("people") or {}).values() if p.get("role") == "owner"), None)


def keys(model: dict) -> list[str]:
    return list((person(model) or {}).get("ssh_keys") or [])


def forgejo_user(login: str) -> str:
    """A Forgejo user name from a Tailscale login such as dev-b@example.internal."""
    name = re.sub(r"[^a-z0-9_.-]+", "-", login.split("@")[0].lower())
    name = re.sub(r"[-_.]{2,}", "-", name).strip("-_.")[:40].strip("-_.")
    return name if name and name not in RESERVED else "owner"
