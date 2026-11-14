"""Moving the guests of a server built on an earlier Debian to the release
guests run now (catalog.yaml's `images.release`), in place
(docs/adr/0022-latest-stable.md, docs/runbooks/debian-upgrade.md).

A guest keeps the image it was made from (tofu.py ignores a newer one), so
a new image pin alone never changes a guest that exists. apply's release
stage does, one guest at a time: a copy first (a PVE snapshot, or a backup
where PVE cannot snapshot), then `data/nb-release-upgrade` inside the guest
as a unit of its own, a restart, and a look at what the guest then says it
runs. A guest whose upgrade fails is put back from its snapshot and the run
stops. `$NULLBILL_HOME/releases.json` records what each guest runs, so the
page can say what the next run upgrades without asking the server.

Left alone: Windows, a CI pool's image (the ci stage makes it again from
the new image), PBS (built on the new release from its first day) and the
services of the owner's own, whose programs NullBill does not know. The
container NullBill itself runs in goes last and by itself, when the run
has ended (apply.controller_release, tools/upgrade-controller).
"""

import json
import time
from importlib import resources
from pathlib import Path

from nullbill import custom

RECORD = "releases.json"
UNIT = "nb-release-upgrade"
SCRIPT = "/usr/local/sbin/nb-release-upgrade"
RESULT = "/var/lib/nullbill/release-upgrade.result"
LOG = "/var/log/nb-release-upgrade.log"
# How long one guest may take, and how often apply looks.
LIMIT_S = 90 * 60
POLL_S = 20
# A snapshot is kept this long after its guest's upgrade, then deleted by
# the next full setup run: it grows with every block the guest changes.
KEEP_COPY_S = 7 * 86400
# The result line of a run that changed nothing (too little room).
REFUSED = "refused: "
# A snapshot keeps every block the upgrade replaces: what is downloaded,
# and the new packages unpacked, which come to three to four times their
# archives. So much room, times what a guest downloads, its snapshot may
# take on the guests' storage.
SNAPSHOT_TIMES = 5


def room(text: str) -> tuple[int, int, int] | None:
    """`nb-release-upgrade FROM TO room`'s answer: MiB needed in the guest,
    MiB free there, MiB it downloads. None when it gave none."""
    for line in reversed(text.splitlines()):
        words = line.split()
        if len(words) == 4 and words[0] == "room" and all(word.isdigit() for word in words[1:]):
            return int(words[1]), int(words[2]), int(words[3])
    return None


# Guests the others need to download anything: first, so a problem there
# stops the run before the rest is touched.
FIRST = ("router", "egress-proxy")
PBS = "pbs"


def script() -> str:
    return resources.files("nullbill").joinpath("data/nb-release-upgrade").read_text()


# Puts what it is sent into the guest as the script. It reads the stream it
# was given and never opens /dev/stdin: in an unprivileged container that
# is a pipe of the host's root, which the container's root may read from
# but not open again ("Permission denied", on the first real run).
PLACE = f"cat > {SCRIPT}.new && chmod 0755 {SCRIPT}.new && mv {SCRIPT}.new {SCRIPT}"


def snapshot(release: dict) -> str:
    return f"nb-before-debian-{release['version']}"


def candidates(plan: dict, model: dict) -> list[str]:
    """The planned guests the release stage keeps on the current release:
    every Linux guest NullBill sets up itself that lives on."""
    own = set(custom.names(model))
    names = [
        name
        for name, guest in plan["guests"].items()
        if guest["kind"] in ("lxc", "vm")
        and guest["zone"] != "windows"
        and name not in own
        and name != PBS
        # install.sh's container, where apply itself runs: last, by itself.
        and not guest.get("installed")
    ]
    return sorted(names, key=lambda n: (FIRST.index(n) if n in FIRST else len(FIRST), names.index(n)))


def read(home: Path) -> dict[str, dict]:
    try:
        found = json.loads((home / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def write(home: Path, record: dict[str, dict]) -> None:
    path = home / RECORD
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


def note(home: Path, name: str, codename: str, **more: object) -> None:
    record = read(home)
    record[name] = record.get(name, {}) | {"release": codename, "at": time.time()} | more
    write(home, record)


def unknown(home: Path, plan: dict, model: dict, existing: list[str] | None) -> list[str]:
    """The candidates that exist (`existing`: the guests the last full run
    recorded; None when no run did) and that no run has found on the
    current release: a guest made by this version starts on it, so these
    are the ones made before, until the stage has looked."""
    if existing is None:
        return []
    record, codename = read(home), model["images"]["release"]["codename"]
    return [n for n in candidates(plan, model) if n in existing and record.get(n, {}).get("release") != codename]


def expired(home: Path, now: float) -> dict[str, dict]:
    """Guests whose snapshot from before the upgrade has had its time."""
    return {
        name: entry
        for name, entry in read(home).items()
        if (entry.get("copy") or {}).get("kind") == "snapshot" and now - entry.get("upgraded", now) >= KEEP_COPY_S
    }


def result(text: str) -> tuple[str, str]:
    """What the result file's last line says: (`done`, codename),
    (`failed`, why), (`refused`, why: the script changed nothing) or
    (`running`, the step it is at)."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "running", ""
    last = lines[-1]
    if last.startswith("done "):
        return "done", last.removeprefix("done ").strip()
    if last.startswith("failed: "):
        return "failed", last.removeprefix("failed: ").strip()
    if last.startswith(REFUSED):
        return "refused", last.removeprefix(REFUSED).strip()
    return "running", last
