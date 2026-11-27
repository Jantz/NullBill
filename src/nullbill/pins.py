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

# A vendor's version, file name, hash or address as it may stand in a pin:
# Ansible templates every role default and catalog value, so a vendor
# answer like `1.0{{ lookup('pipe', ...) }}` must never reach a file.
SAFE = re.compile(r"[A-Za-z0-9._+~:/@=-]+")
VERSION = r"[0-9A-Za-z][0-9A-Za-z.+~:_-]{0,79}"
FILE = r"[A-Za-z0-9._+-]{1,200}"
INT = "int"
LIBRARIES = "libraries"


def _hex(n: int) -> str:
    return rf"[0-9a-f]{{{n}}}"


def _url(*prefixes: str) -> str:
    return "(?:" + "|".join(re.escape(p) for p in prefixes) + r")[A-Za-z0-9._/+~-]{1,300}"


CATALOG = "src/nullbill/data/catalog.yaml"
# Where Fabric's libraries may come from: Fabric's Maven and Maven Central.
FABRIC_MAVEN = ("https://maven.fabricmc.net/", "https://repo1.maven.org/maven2/")


def _defaults(role: str) -> str:
    return f"ansible/roles/{role}/defaults/main.yml"


# Everything tools/pins bump writes, by file and key, with what a value
# there may look like. A range of main's commits that changes nothing but
# these values and the files in WHOLE is pin data: the nightly installs it
# by itself (nullbill/version.py), and pins.yml gives main nothing else.
WRITES: dict[str, dict[str, str]] = {
    _defaults("forgejo"): {"forgejo_version": VERSION, "forgejo_sha256": _hex(64)},
    _defaults("ci_runner"): {"ci_runner_version": VERSION, "ci_runner_sha256": _hex(64)},
    _defaults("monitoring"): {"monitoring_grafana_version": VERSION, "monitoring_loki_version": VERSION},
    _defaults("step_ca"): {"step_ca_version": VERSION, "step_cli_version": VERSION},
    _defaults("operator"): {"step_cli_version": VERSION, "operator_claude_code_version": VERSION, "operator_claude_code_sha512": _hex(128)},
    _defaults("tls_cert"): {"tls_cert_step_cli_version": VERSION},
    _defaults("vpn"): {"vpn_tailscale_version": VERSION},
    _defaults("jellyfin"): {
        "jellyfin_version": VERSION,
        "jellyfin_ffmpeg_package": r"jellyfin-ffmpeg\d{1,3}",
        "jellyfin_ffmpeg_version": VERSION,
    },
    _defaults("qbittorrent"): {"qbittorrent_version": VERSION, "qbittorrent_commit": _hex(40)},
    _defaults("inference"): {"inference_llama_tag": r"b\d{1,8}", "inference_llama_commit": _hex(40)},
    _defaults("model_gateway"): {"model_gateway_version": VERSION},
    CATALOG: {
        "games.minecraft.version": VERSION,
        "games.minecraft.url": _url("https://piston-data.mojang.com/"),
        "games.minecraft.sha256": _hex(64),
        "games.minecraft.java": INT,
        "games.minecraft.servers.paper.version": VERSION,
        "games.minecraft.servers.paper.build": INT,
        "games.minecraft.servers.paper.url": _url("https://fill-data.papermc.io/"),
        "games.minecraft.servers.paper.sha256": _hex(64),
        "games.minecraft.servers.fabric.version": VERSION,
        "games.minecraft.servers.fabric.loader": VERSION,
        "games.minecraft.servers.fabric.libraries": LIBRARIES,
        "games.terraria.version": VERSION,
        "games.terraria.url": _url("https://terraria.org/api/download/pc-dedicated-server/"),
        "games.terraria.sha256": _hex(64),
        "games.factorio.version": VERSION,
        "games.factorio.url": _url("https://factorio.com/get-download/"),
        "games.factorio.sha256": _hex(64),
        # Plain HTTP, the one way Proxmox serves it: tools/pins takes the
        # checksum from the index Proxmox signs, and apply checks the file by it.
        "images.debian-lxc.url": _url("http://download.proxmox.com/images/system/"),
        "images.debian-lxc.sha512": _hex(128),
        "images.debian-cloud.url": _url("https://cloud.debian.org/images/cloud/"),
        "images.debian-cloud.sha512": _hex(128),
        "images.fedora-cloud.url": _url("https://dl.fedoraproject.org/pub/fedora/linux/releases/"),
        "images.fedora-cloud.sha256": _hex(64),
        "images.fedora-cloud.release": VERSION,
    },
}
LIBRARY = {"file": FILE, "url": _url(*FABRIC_MAVEN), "sha256": _hex(64)}
# Files that change whole with the pins: LiteLLM's lock, which tools/pins
# makes, and what pins.yml's UPDATE_GOLDEN=1 run writes (the tests' plans
# and renders, the game roles' check variables). Each is a regex on the
# path, with the regex each of its lines must match, or None for a file
# only the tests read.
REQUIREMENT = (
    r"[A-Za-z0-9._-]+(\[[a-z0-9,_-]+\])?==[A-Za-z0-9.+!-]+( ; [A-Za-z0-9_.<>=!'\" ]+)? \\"
    r"|    --hash=sha256:[0-9a-f]{64}( \\)?|    #( .*)?|"
)
WHOLE: dict[str, str | None] = {
    r"ansible/roles/model_gateway/files/requirements\.in": rf"litellm\[proxy\]=={VERSION}|",
    r"ansible/roles/model_gateway/files/requirements\.txt": REQUIREMENT,
    r"tests/golden/[A-Za-z0-9._-]+\.(yaml|txt)": None,
    r"ansible/roles/game_[a-z]+/tests/vars\.json": None,
    r"ansible/roles/game_minecraft/tests/(paper|fabric)\.json": None,
}


def paths() -> list[str]:
    """Every path pin data may change, as anchored regexes: what pins.yml's
    PIN_PATHS holds (a test keeps the two equal)."""
    return [f"^{re.escape(path)}$" for path in WRITES] + [f"^{path}$" for path in WHOLE]


def check(path: str, key: str, new: object) -> None:
    """Refuse a value tools/pins was not made to write there."""
    rule = WRITES.get(path, {}).get(key)
    if rule is None:
        raise NullBillError(f"{path}: {key} is no pin tools/pins writes")
    if rule == INT:
        if not (isinstance(new, int) and not isinstance(new, bool) and 0 < new < 10**9):
            raise NullBillError(f"{path}: {key} must be a whole number, not {str(new)[:80]!r}")
        return
    if rule == LIBRARIES:
        if not (isinstance(new, list) and new and all(isinstance(lib, dict) and set(lib) == set(LIBRARY) for lib in new)):
            raise NullBillError(f"{path}: {key} must be a list of file, url and sha256")
        for lib in new:
            for name, pattern in LIBRARY.items():
                _match(path, f"{key}.{name}", pattern, lib[name])
        return
    _match(path, key, rule, new)


def _match(path: str, key: str, pattern: str, new: object) -> None:
    if not isinstance(new, str) or not re.fullmatch(pattern, new) or ".." in new:
        raise NullBillError(f"{path}: {key} cannot be {str(new)[:80]!r}")


def _at(doc: object, key: str) -> object:
    for part in key.split("."):
        if not isinstance(doc, dict) or part not in doc:
            return None
        doc = doc[part]
    return doc


def _set(doc: dict, key: str, new: object) -> None:
    *parents, last = key.split(".")
    for part in parents:
        doc = doc[part]
    doc[last] = new


def only_pins(path: str, old: str | None, new: str | None) -> str | None:
    """Why changing one file from old to new text is more than pin data,
    or None when it is pin data alone."""
    if old is None or new is None:
        return f"{path}: added or removed"
    for pattern, lines in WHOLE.items():
        if re.fullmatch(pattern, path):
            if lines is not None and not all(re.fullmatch(lines, line) for line in new.split("\n")):
                return f"{path}: holds a line tools/pins does not write"
            return None
    keys = WRITES.get(path)
    if keys is None:
        return f"{path}: not pin data"
    try:
        before, after = yaml.safe_load(old), yaml.safe_load(new)
    except yaml.YAMLError:
        return f"{path}: not YAML"
    if not isinstance(before, dict) or not isinstance(after, dict):
        return f"{path}: not a YAML mapping"
    for key in keys:
        found, was = _at(after, key), _at(before, key)
        if found == was:
            continue
        if was is None or found is None:
            return f"{path}: {key} added or removed"
        try:
            check(path, key, found)
        except NullBillError as exc:
            return str(exc)
        _set(before, key, found)
    if before != after:
        return f"{path}: changes more than its pins"
    return None


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


def _safe(new: object) -> str:
    """A value as text, refused when it holds what no version, hash, file
    name or address does: braces or percent signs (Jinja), quotes, `#`,
    spaces or line breaks (YAML)."""
    text = str(new)
    if isinstance(new, bool) or not SAFE.fullmatch(text):
        raise NullBillError(f"refusing to write {text[:80]!r} as a pin")
    return text


def _scalar(new: object, old: yaml.ScalarNode) -> str:
    text = _safe(new)
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
            _safe(name)
            if isinstance(inner, (dict, list)):
                out += f"{pad}{name}:\n{block(inner, indent + 2)}"
            else:
                _safe(inner)
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
        return _shell(key).sub(f"{key}={_safe(new)}", text, count=1)
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
