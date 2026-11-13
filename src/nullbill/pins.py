"""The versions this repository pins (data/pins.yaml): reading them out of
the files that hold them, writing a new one in place, and telling a small
step from one the owner is asked about (docs/adr/0022-latest-stable.md).

tools/pins moves the pins; the page reads them from two commits to say what
an update brings and whether it may install by itself (`steps`, `asks`).
"""

import re
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import yaml

from nullbill.errors import NullBillError

TABLE = "src/nullbill/data/pins.yaml"
ALL = "all"

Read = Callable[[str], str | None]


def table(text: str | None = None) -> dict[str, dict]:
    """The pins table, this checkout's or another commit's text of it."""
    if text is None:
        text = resources.files("nullbill").joinpath("data/pins.yaml").read_text()
    return yaml.safe_load(text) or {}


def checkout(root: Path) -> Read:
    def read(path: str) -> str | None:
        try:
            return (root / path).read_text()
        except OSError:
            return None

    return read


def _node(text: str, key: str) -> yaml.Node | None:
    node = yaml.compose(text)
    for part in key.split("."):
        if not isinstance(node, yaml.MappingNode):
            return None
        node = next((value for name, value in node.value if name.value == part), None)
    return node


def _shell(key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(key)}=(\S*)$", re.M)


def _is_yaml(path: str) -> bool:
    return path.endswith((".yml", ".yaml"))


def value(text: str, path: str, key: str) -> str | None:
    """What a file's text pins under key, as text."""
    if not _is_yaml(path):
        found = _shell(key).search(text)
        return found[1] if found else None
    node = _node(text, key)
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _scalar(new: object, old: yaml.ScalarNode) -> str:
    text = str(new)
    # Quoted as it was, and quoted when YAML would read text as a number
    # (26.3); a number that was one stays one (a build's).
    was_text = old.style in ('"', "'") or isinstance(yaml.safe_load(old.value), str)
    if old.style in ('"', "'") or (was_text and not isinstance(yaml.safe_load(text), str)):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def block(new: object, indent: int) -> str:
    """Nested pins as YAML lines, each mapping in the order given."""
    pad, out = " " * indent, ""
    if isinstance(new, dict):
        for name, inner in new.items():
            if isinstance(inner, (dict, list)):
                out += f"{pad}{name}:\n{block(inner, indent + 2)}"
            else:
                # Text that YAML would read as a number (26.3) stays text.
                if isinstance(inner, str) and not isinstance(yaml.safe_load(inner), str):
                    inner = f'"{inner}"'
                out += f"{pad}{name}: {inner}\n"
    else:
        for item in new:
            lines = block(item, indent + 2).splitlines()
            out += f"{pad}- {lines[0].strip()}\n" + "".join(f"{line}\n" for line in lines[1:])
    return out


def put(text: str, path: str, key: str, new: object) -> str:
    """The file's text with key set to new: a scalar in place, comments and
    all else untouched, or a list or mapping in place of the old one."""
    if not _is_yaml(path):
        if not _shell(key).search(text):
            raise NullBillError(f"{path} sets no {key}")
        return _shell(key).sub(f"{key}={new}", text, count=1)
    node = _node(text, key)
    if node is None:
        raise NullBillError(f"{path} has no {key}")
    start, end = node.start_mark.index, node.end_mark.index
    if isinstance(node, yaml.ScalarNode):
        return text[:start] + _scalar(new, node) + text[end:]
    # A block ends where the next key starts, after that line's indent.
    line = text.rfind("\n", 0, end) + 1
    if not text[line:end].strip():
        end = line
    return text[:start] + block(new, node.start_mark.column).lstrip(" ") + text[end:]


def current(read: Read, pins: dict[str, dict] | None = None) -> dict[str, str]:
    """Each pin's version as the files read through `read` hold it."""
    out = {}
    for name, pin in (table() if pins is None else pins).items():
        text = read(pin["file"])
        found = value(text, pin["file"], pin["key"]) if text is not None else None
        if found is not None:
            out[name] = found
    return out


def numbers(version: str) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", version)]


def held(pin: dict, old: str, new: str) -> bool:
    """Whether going from old to new is a step the owner is asked about."""
    hold = pin.get("hold", 0)
    if old == new:
        return False
    if hold == ALL:
        return True
    return numbers(old)[:hold] != numbers(new)[:hold]


def steps(old: Read, new: Read) -> list[dict]:
    """What differs between two trees' pins, by the newer tree's table: one
    entry per pin, with `held` for a step that waits for the owner. A pin
    the older tree did not have is no step, unless the table says what
    that tree had instead (`was`)."""
    text = new(TABLE)
    if text is None:
        return []
    pins = table(text)
    before, after = current(old, pins), current(new, pins)
    for name, pin in pins.items():
        if "was" in pin and name in after:
            before.setdefault(name, str(pin["was"]))
    out = []
    for name, pin in pins.items():
        if name in before and name in after and before[name] != after[name]:
            out.append(
                {
                    "name": name,
                    "title": pin["title"],
                    "from": before[name],
                    "to": after[name],
                    "held": held(pin, before[name], after[name]),
                    "services": pin.get("services", []),
                }
            )
    return out


def in_use(plan: dict) -> set[str]:
    """The services a plan runs, a further game server under its game's name."""
    return set(plan.get("guests", {})) | {game.get("game", name) for name, game in plan.get("games", {}).items()}


def asks(found: list[dict], plan: dict | None) -> list[dict]:
    """The held steps that touch something this server runs. Without a plan
    to say what it runs, every held step is one: a server whose last run
    kept no plan is asked, not skipped."""
    if plan is None:
        return [step for step in found if step["held"]]
    used = in_use(plan)
    return [step for step in found if step["held"] and (not step["services"] or used & set(step["services"]))]
