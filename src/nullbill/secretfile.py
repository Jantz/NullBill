"""Owner secrets in $NULLBILL_HOME/secrets.sops.yaml (docs/adr/0005-apply.md).

sops encrypts each value with the age key in $NULLBILL_HOME/age.key and
leaves the names readable. Plaintext only passes through pipes: sops reads
it from stdin when saving and writes it to stdout when loading.

Each secret is saved as soon as it is made, so a run that fails later still
finds the value it may already have given a guest.
"""

import json
import os
import secrets as random
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from nullbill.errors import NullBillError

FILE = "secrets.sops.yaml"
KEY = "age.key"


class Codec(Protocol):
    def load(self) -> dict[str, str]: ...

    def save(self, data: dict[str, str]) -> None: ...


class Sops:
    """The real codec: the sops and age binaries on PATH."""

    def __init__(self, home: Path):
        self.path = home / FILE
        self.key = home / KEY

    def _run(self, argv: list[str], stdin: str | None = None) -> str:
        env = os.environ | {"SOPS_AGE_KEY_FILE": str(self.key)}
        try:
            proc = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=60)
        except FileNotFoundError as exc:
            raise NullBillError(f"{argv[0]} not found; apply needs sops and age (docs/runbooks/controller.md)") from exc
        if proc.returncode != 0:
            # sops never prints plaintext on failure; its error names the cause.
            raise NullBillError(f"{argv[0]} failed on {self.path}: {proc.stderr.strip()[:300]}")
        return proc.stdout

    def recipient(self) -> str:
        if not self.key.exists():
            self._run(["age-keygen", "-o", str(self.key)])
            print(f"nullbill: made {self.key}; back it up next to the PBS encryption key")
        for line in self.key.read_text().splitlines():
            if line.startswith("# public key: "):
                return line.removeprefix("# public key: ").strip()
        raise NullBillError(f"{self.key}: no '# public key:' line; it is not an age-keygen key file")

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self._run(["sops", "decrypt", "--output-type", "json", str(self.path)]))

    def save(self, data: dict[str, str]) -> None:
        argv = ["sops", "encrypt", "--age", self.recipient(), "--input-type", "json", "--output-type", "yaml", "/dev/stdin"]
        text = self._run(argv, json.dumps(data, sort_keys=True))
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        tmp.replace(self.path)


def password() -> str:
    """32 random bytes, URL-safe: no quoting needed in YAML, ini or a shell."""
    return random.token_urlsafe(32)


class Secrets:
    def __init__(self, codec: Codec):
        self.codec = codec
        self.data = codec.load()

    def get(self, name: str) -> str:
        if name not in self.data:
            raise NullBillError(f"secret {name!r} is missing from {FILE}; add it with `sops edit {FILE}` in $NULLBILL_HOME")
        return self.data[name]

    def ensure(self, name: str, make: Callable[[], str] = password) -> str:
        """The stored value, or a new one from `make`, saved before it is used."""
        if name not in self.data:
            self.set(name, make())
        return self.data[name]

    def set(self, name: str, value: str) -> None:
        if self.data.get(name) == value:
            return
        self.data[name] = value
        self.codec.save(self.data)
