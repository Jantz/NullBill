"""What a setup run started now does to each machine, said before it runs.

Nothing here asks the server anything. A setup run changes a machine in two
ways, and both can be told from what the controller holds:

- OpenTofu creates, deletes or changes guests. The page's review compares the
  plan with the guests on the server already (web/review.py).
- apply runs a playbook on a machine unless it ran there before with the same
  inputs (applied.py). The controller wrote down what each one last ran with
  (applied.RECORD): its sources as a digest, each input as a fingerprint.
  apply.FORESEEN works the next run's inputs out from the plan, the choices
  and the saved secrets, with a Reach that refuses every call. Equal sources
  and inputs: the run leaves the playbook out on that machine.

Where a playbook runs again, `effects` reads its roles to say what that can
restart: it follows what changed (an input, a file, a task) to the tasks that
use it and from those to the handlers they notify. It errs towards naming a
restart that then does not happen; a task it cannot place taints its whole
role.

What cannot be foreseen is said, not guessed: an input only the run reads
(a certificate it signs anew, a token a guest holds) counts as unchanged,
and a machine no run has written down yet is "not known".
"""

import io
import json
import re
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Protocol

import yaml

from nullbill import applied, apply, ci, power
from nullbill.errors import NullBillError
from nullbill.home import REPO_ROOT
from nullbill.render import strip_notes

# What the page calls the PVE host's row.
HOST = "Proxmox host"
# A secret the saved secrets do not hold yet: the run makes it, so whatever
# takes it is set up again.
MADE_BY_THE_RUN = "(made by the run)"


class Unforeseen(Exception):
    """A builder asked for something only a run can do."""


class Refuses:
    """The Reach a preview runs with: no command, no playbook, no program."""

    def host(self, command: str, stdin: str | None = None, timeout: int = 900) -> tuple[int, str, str]:
        raise Unforeseen(f"it would run on the host: {command[:60]}")

    def playbook(self, playbook: str, hosts: dict, variables: dict) -> int:
        raise Unforeseen(f"it would run {playbook}")

    def local(self, argv: list[str], cwd: Path, env: dict, capture: bool = False) -> tuple[int, str]:
        raise Unforeseen(f"it would run {argv[0]}")


class Saved:
    """The saved secrets, read only. One a run would make now stands as
    MADE_BY_THE_RUN, never made."""

    def __init__(self, data: dict[str, str]):
        self.data = dict(data)

    def get(self, name: str) -> str:
        return self.data.get(name, MADE_BY_THE_RUN)

    def ensure(self, name: str, make: object = None) -> str:
        return self.data.get(name, MADE_BY_THE_RUN)

    def set(self, name: str, value: str) -> None:
        raise Unforeseen(f"it would save the secret {name}")

    def drop(self, name: str) -> None:
        raise Unforeseen(f"it would drop the secret {name}")


def context(
    plan: dict, model: dict, home: Path, secrets: dict[str, str], files: dict[str, str], lan_host: str | None = None
) -> apply.Context:
    """An apply Context that can only be read from."""
    return apply.Context(plan, model, home, "root@preview.invalid", Refuses(), Saved(secrets), files, lan_host=lan_host)  # type: ignore[arg-type]


def steps(ctx: apply.Context) -> tuple[list[apply.Step], dict[str, str]]:
    """Every playbook run a setup run of this plan makes, in apply's order,
    and per stage that could not be worked out, why not."""
    found: list[apply.Step] = []
    unknown: dict[str, str] = {}
    for stage, _ in apply.STAGES:
        foresee = apply.FORESEEN.get(stage)
        if foresee is None:
            continue
        try:
            found += foresee(ctx)
        except Unforeseen as exc:
            unknown[stage] = str(exc)
        except (NullBillError, KeyError, OSError, ValueError, TypeError) as exc:
            unknown[stage] = f"{type(exc).__name__}: {exc}"[:200]
    return found, unknown


# How one step compares with the record of its last run.
SAME, SOURCE, INPUTS, UNFINISHED, FIRST = "same", "source", "inputs", "unfinished", "first"


@dataclass
class Verdict:
    step: apply.Step
    does: str  # SAME, SOURCE, INPUTS, UNFINISHED or FIRST
    inputs: list[str] = field(default_factory=list)  # the inputs that differ, by name
    last: dict | None = None  # the record's entry

    @property
    def runs(self) -> bool:
        return self.does != SAME


def compare(key: str, step: apply.Step, last: dict | None) -> Verdict:
    """Would a run leave this step out? The same question apply asks the
    machine's mark, answered from the controller's record."""
    if last is None:
        return Verdict(step, FIRST)
    if not last.get("digest"):
        return Verdict(step, UNFINISHED, last=last)
    was = last.get("inputs") or {}
    now = applied.prints(key, {}, step.inputs)
    differ = {name for name, value in now.items() if was.get(name) != value}
    # An input the run reads itself is taken as it was; one the run no longer
    # passes, or passes for the first time, changes what the playbook does.
    differ |= {name for name in set(was) - set(now) if applied.whole(name) not in step.read}
    differ |= set(step.read) - set(was)
    if last.get("source") != applied.source(step.playbook):
        return Verdict(step, SOURCE, sorted(differ), last)
    return Verdict(step, INPUTS if differ else SAME, sorted(differ), last)


# --- What a playbook's roles restart -----------------------------------------


class Tree(Protocol):
    """ansible/ at one version: paths relative to it, and their text."""

    paths: list[str]

    def read(self, path: str) -> str | None: ...


class Checkout:
    """ansible/ as it is on disk."""

    def __init__(self, root: Path):
        self.root = root / "ansible"

    @cached_property
    def paths(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)

    def read(self, path: str) -> str | None:
        try:
            return (self.root / path).read_text(errors="replace")
        except OSError:
            return None


class Commit:
    """ansible/ as a commit holds it, read from git in one go."""

    def __init__(self, root: Path, commit: str):
        self.files: dict[str, bytes] = {}
        try:
            done = subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar", commit, "ansible"], capture_output=True, check=False, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if done.returncode != 0:
            return
        with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tar:
            for member in tar:
                if member.isfile() and (data := tar.extractfile(member)) is not None:
                    self.files[member.name.removeprefix("ansible/")] = data.read()

    @cached_property
    def paths(self) -> list[str]:
        return sorted(self.files)

    def read(self, path: str) -> str | None:
        data = self.files.get(path)
        return None if data is None else data.decode(errors="replace")


def _role_of(path: str) -> tuple[str, str] | None:
    """(role, path inside it) of a path under roles/."""
    parts = path.split("/")
    return (parts[1], "/".join(parts[2:])) if len(parts) > 2 and parts[0] == "roles" else None


def reach(tree: Tree, playbook: str) -> tuple[list[str], list[str]]:
    """The roles a playbook can reach and every file its run depends on, by
    applied.sources' rules, on any tree."""
    roles = {found[0] for p in tree.paths if (found := _role_of(p))}
    text = tree.read(playbook)
    if text is None:
        return [], []
    files = [playbook]
    todo = sorted(applied.named_roles(text, roles))
    seen: list[str] = []
    while todo:
        role = todo.pop()
        if role in seen:
            continue
        seen.append(role)
        for path in tree.paths:
            inside = _role_of(path)
            if inside is None or inside[0] != role or {"tests", "__pycache__"} & set(inside[1].split("/")):
                continue
            files.append(path)
            if path.endswith((".yml", ".yaml")):
                todo += sorted(applied.named_roles(tree.read(path) or "", roles) - set(seen))
    files += [p for p in tree.paths if p.startswith("connection_plugins/") and p.endswith(".py") and p.count("/") == 1]
    return seen, files


@dataclass(frozen=True)
class Restart:
    what: str  # the program, as its unit is named
    how: str  # restart, reload (no interruption) or reboot (the whole machine)


@dataclass
class Task:
    role: str
    file: str  # inside the role: tasks/main.yml
    name: str
    body: dict
    text: str  # everything a change can reach it by: its own keys and its blocks'
    tokens: set[str]
    notify: tuple[str, ...]
    handler: bool = False
    built: bool = False  # it names a file by an expression this cannot read

    @property
    def listens(self) -> tuple[str, ...]:
        heard = self.body.get("listen") or ()
        return (self.name, *([heard] if isinstance(heard, str) else heard))


WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SYSTEMCTL = re.compile(r"systemctl\s+(?:try-)?(restart|reload)(?:-or-restart)?\s+([A-Za-z0-9@_.-]+)")
BLOCKS = ("block", "rescue", "always")
ITEM = re.compile(r"\{\{\s*item\s*\}\}")
# A file looked up by a pattern, or by a name that is an expression.
LOOKED_UP = re.compile(r"fileglob|(?:lookup|query)\(\s*'(?:ansible\.builtin\.)?(?:file|template)'\s*,\s*[^'\s]")
VERB = re.compile(r"^(Restart|Reload)\s+(.*)$")


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value) if isinstance(value, list) else ()


def _flatten(role: str, file: str, items: object, outer: str = "", notify: tuple[str, ...] = (), handler: bool = False) -> list[Task]:
    out: list[Task] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        own = {k: v for k, v in item.items() if k not in BLOCKS}
        text = outer + json.dumps(own, sort_keys=True, default=str)
        told = (*notify, *_names(item.get("notify")))
        if any(k in item for k in BLOCKS):
            for k in BLOCKS:
                out += _flatten(role, file, item.get(k), text, told, handler)
            continue
        spelled, built = _sources(item)
        text += " " + " ".join(spelled)
        built = built or bool(LOOKED_UP.search(text))
        out.append(Task(role, file, str(item.get("name", "")), item, text, set(WORD.findall(text)), told, handler, built))
    return out


def _sources(item: dict) -> tuple[list[str], bool]:
    """The role files a task names through its loop ("{{ item }}.j2" over a
    list written out), spelled out; and whether it names one by an
    expression this cannot read. A path that starts at / is on the machine."""
    loop = item.get("loop")
    listed = isinstance(loop, list) and all(isinstance(v, str | int) for v in loop)
    spelled: list[str] = []
    built = False
    for args in item.values():
        src = args.get("src") if isinstance(args, dict) else None
        if not isinstance(src, str) or "{{" not in src or src.startswith("/"):
            continue
        if "{{" in ITEM.sub("", src).replace("{{ role_path }}", "") or (ITEM.search(src) and not listed):
            built = True
        elif ITEM.search(src):
            spelled += [ITEM.sub(str(v), src) for v in loop]  # type: ignore[union-attr]
    return spelled, built


def _names_file(text: str, own: str, role: str, rel: str) -> bool:
    """Does a task's or a file's text in role `own` name the file rel of `role`?"""
    if own == role:
        return rel in text
    return f"{role}/files/{rel}" in text or f"{role}/templates/{rel}" in text


def _module(task: Task, *short: str) -> object | None:
    """The arguments of the task's module when it is one of `short`."""
    for key, args in task.body.items():
        if key.rsplit(".", 1)[-1] in short:
            return args if args is not None else {}
    return None


def restarts_of(task: Task) -> list[Restart]:
    """What a task or handler restarts, reloads or reboots, if anything."""
    if _module(task, "reboot") is not None:
        return [Restart("the whole machine", "reboot")]
    named = VERB.match(task.name)
    args = _module(task, "systemd_service", "systemd", "service")
    if isinstance(args, dict) and args.get("state") in ("restarted", "reloaded"):
        how = "restart" if args["state"] == "restarted" else "reload"
        unit, loop = str(args.get("name") or ""), task.body.get("loop")
        if unit and "{{" not in unit:
            return [Restart(unit, how)]
        if "item" in WORD.findall(unit) and isinstance(loop, list) and all(isinstance(u, str) for u in loop):
            return [Restart(u, how) for u in loop]
        return [Restart(named.group(2) if named else task.name, how)]
    args = _module(task, "command", "shell")
    line = args if isinstance(args, str) else " ".join(map(str, (args or {}).get("argv", []))) or str((args or {}).get("cmd", ""))
    if args is not None and (found := SYSTEMCTL.search(line)):
        return [Restart(found.group(2) if "{{" not in found.group(2) else task.name, found.group(1))]
    if task.handler and named:
        return [Restart(named.group(2), named.group(1).lower())]
    return []


@dataclass
class Roles:
    """A playbook's roles, read for what a change in them can restart."""

    roles: list[str]
    files: list[str]
    tasks: list[Task]
    variables: dict[str, dict[str, str]]  # role -> a default or var -> its value as text
    texts: dict[tuple[str, str], str]  # (role, path under files/ or templates/) -> its text
    broken: bool = False  # a file that did not parse: nothing can be ruled out


def read_roles(tree: Tree, playbook: str) -> Roles:
    roles, files = reach(tree, playbook)
    found = Roles(roles, files, [], {r: {} for r in roles}, {})
    for path in files:
        inside = _role_of(path)
        if inside is None:
            continue
        role, rel = inside
        kind, _, rest = rel.partition("/")
        text = tree.read(path) or ""
        if kind in ("files", "templates"):
            found.texts[(role, rest)] = text
        if kind not in ("tasks", "handlers", "defaults", "vars") or not rel.endswith((".yml", ".yaml")):
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            found.broken = True
            continue
        if kind in ("tasks", "handlers"):
            found.tasks += _flatten(role, rel, data, handler=kind == "handlers")
        elif isinstance(data, dict):
            found.variables[role] |= {str(k): json.dumps(v, sort_keys=True, default=str) for k, v in data.items()}
    return found


@dataclass
class Effects:
    restarts: list[Restart]
    # False: something changed that could not be followed, so every restart
    # the roles hold is named.
    followed: bool = True


def _all(roles: Roles) -> Effects:
    return Effects(_unique(r for t in roles.tasks for r in restarts_of(t)), followed=False)


def _unique(found: object) -> list[Restart]:
    order = {"reboot": 0, "restart": 1, "reload": 2}
    return sorted(set(found), key=lambda r: (order[r.how], r.what))  # type: ignore[call-overload]


def _includes(task: Task) -> tuple[str, str] | None:
    """(role, tasks file) a task brings in, when it is an include."""
    args = _module(task, "include_tasks", "import_tasks")
    if args is not None:
        name = args if isinstance(args, str) else str((args or {}).get("file", ""))
        return (task.role, f"tasks/{name}") if name else None
    args = _module(task, "include_role", "import_role")
    if isinstance(args, dict) and args.get("name"):
        return str(args["name"]), f"tasks/{args.get('tasks_from') or 'main'}"
    return None


def effects(now: Tree, playbook: str, inputs: set[str], changed: set[str] | None = None, was: Tree | None = None) -> Effects:
    """What a rerun of playbook can restart when `inputs` (by name) and the
    files in `changed` (paths under ansible/) differ from its last run.
    was: the tree of that last run, to tell which task or default changed;
    without it a changed file taints everything it holds. changed None: the
    sources differ and nothing says where."""
    roles = read_roles(now, playbook)
    if roles.broken or changed is None or applied.FACTS in inputs:
        return _all(roles)
    before = read_roles(was, playbook) if was is not None else None
    names = {applied.whole(name) for name in inputs}
    # Which entries of a table changed, where the record names them.
    parts: dict[str, set[str]] = {}
    for name in inputs:
        if "[" in name:
            parts.setdefault(applied.whole(name), set()).add(name.split("[", 1)[1].removesuffix("]"))
    tainted: set[int] = set()
    texts: set[tuple[str, str]] = set()

    def whole(role: str, file: str | None = None) -> None:
        tainted.update(i for i, t in enumerate(roles.tasks) if t.role == role and not t.handler and file in (None, t.file))

    for path in sorted(changed):
        inside = _role_of(path)
        if inside is None:
            if path.startswith("connection_plugins/"):
                continue  # how Ansible gets there, not what it does there
            return _all(roles)  # the playbook itself
        role, rel = inside
        if role not in roles.roles:
            continue
        kind, _, rest = rel.partition("/")
        if kind in ("files", "templates"):
            if (role, rest) not in roles.texts:
                continue  # taken away: a task that named it changed too
            texts.add((role, rest))
            users = [t for t in roles.tasks if _names_file(t.text, t.role, role, rest)]
            users += [k for k, text in roles.texts.items() if k != (role, rest) and _names_file(text, k[0], role, rest)]
            users += [k for k, value in roles.variables[role].items() if rest in value]
            # Nothing names it. Where a task of the role builds a file's
            # name or looks files up, it may be one of those; elsewhere no
            # task places it (a test's data beside the rules it tests).
            if not users and any(t.built for t in roles.tasks if t.role == role):
                whole(role)
        elif kind == "tasks":
            old = {t.text for t in before.tasks if t.role == role and t.file == rel} if before is not None else None
            if old is None:
                whole(role, rel)
            else:
                tainted.update(i for i, t in enumerate(roles.tasks) if t.role == role and t.file == rel and t.text not in old)
        elif kind == "handlers":
            continue  # a handler runs when a task tells it to, changed or not
        elif kind in ("defaults", "vars") and before is not None:
            old_values = before.variables.get(role, {})
            names |= {k for k, v in roles.variables[role].items() if old_values.get(k) != v}
        elif kind in ("defaults", "vars"):
            names |= set(roles.variables[role])
        else:
            return _all(roles)  # meta, a plugin, something this does not read
    includes = {i: target for i, t in enumerate(roles.tasks) if (target := _includes(t))}
    seeded = set(tainted)  # tainted by a changed file, not by an input
    grew = True
    while grew:
        size = (len(names), len(tainted), len(texts))
        for role, values in roles.variables.items():
            for k, v in values.items():
                if k not in names and (names & set(WORD.findall(v)) or any(r == role and rel in v for r, rel in texts)):
                    names.add(k)
        for key, text in roles.texts.items():
            if key not in texts and (names & set(WORD.findall(text)) or any(_names_file(text, key[0], r, rel) for r, rel in texts)):
                texts.add(key)
        for i, task in enumerate(roles.tasks):
            if i in tainted or task.handler:
                continue
            if names & task.tokens or any(_names_file(task.text, task.role, r, rel) for r, rel in texts):
                tainted.add(i)
        for i in list(tainted):
            task = roles.tasks[i]
            names |= set(_names(task.body.get("register")))
            for key, args in task.body.items():
                if key.rsplit(".", 1)[-1] == "set_fact" and isinstance(args, dict):
                    names |= {str(k) for k in args if k != "cacheable"}
            if i in includes:
                names |= {str(k) for k in (task.body.get("vars") or {})}
                whole(*includes[i])
        grew = size != (len(names), len(tainted), len(texts))
    fired = [roles.tasks[i] for i in tainted]
    told = {name for t in fired for name in t.notify}
    handlers = [t for t in roles.tasks if t.handler]
    while True:
        more = [h for h in handlers if h not in fired and told & set(h.listens)]
        if not more:
            break
        fired += more
        told |= {name for h in more for name in h.notify}
    return Effects(_unique(r for t in fired if not _spared(t, roles, seeded, names, parts) for r in restarts_of(t)))


# A condition that picks, among the entries of a registered loop that
# changed, those whose key matches a pattern: the router restarts whole only
# for a file that names its interfaces.
MATCHING = re.compile(
    r"(\w+)\.results\s*\|\s*selectattr\('changed'\)\s*\|\s*map\(attribute='item\.key'\)\s*\|\s*select\('match',\s*'([^']+)'\)"
)


def _spared(task: Task, roles: Roles, seeded: set[int], names: set[str], parts: dict[str, set[str]]) -> bool:
    """A restart that what changed cannot set off after all: its condition
    asks only for changed entries whose key matches a pattern, of a loop
    over one input, and no entry of that input that changed matches."""
    picks = dict(MATCHING.findall(" ".join(_names(task.body.get("when")))))
    # Anything else that changed and that the task, or a block around it, names sets it off as before.
    if not picks or (task.tokens & names) - set(picks):
        return False
    for register, pattern in picks.items():
        if register not in names:
            continue
        loops = [(i, t) for i, t in enumerate(roles.tasks) if register in _names(t.body.get("register"))]
        if len(loops) != 1 or loops[0][0] in seeded:
            return False
        reasons = (loops[0][1].tokens & names) - {register}
        if len(reasons) != 1 or (source := next(iter(reasons))) not in parts:
            return False
        if any(re.match(pattern, part) for part in parts[source]):
            return False
    return True


# --- What that means for each machine ----------------------------------------


def changed_files(root: Path, old: str, new: str | None = None) -> set[str] | None:
    """Paths under ansible/ that differ between a commit and another, or the
    checkout as it is on disk (new None). None where git cannot say."""
    argv = ["git", "-C", str(root), "diff", "--name-only", old, *([new] if new else []), "--", "ansible"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return {line.removeprefix("ansible/") for line in done.stdout.splitlines() if line.startswith("ansible/")}


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


PARTS_NAMED = 3
# Inputs whose name in words says too little.
INPUT_WORDS = {"heap mib": "Java's share of its memory"}


def _input_words(playbook_roles: list[str], names: list[str]) -> list[str]:
    """Inputs as the owner can read them: the role's prefix off, in words,
    and of a table the entries that changed."""
    parts: dict[str, list[str]] = {}
    for name in names:
        parts.setdefault(applied.whole(name), [])
        if "[" in name:
            parts[applied.whole(name)].append(name.split("[", 1)[1].removesuffix("]"))
    out = []
    for name, inner in parts.items():
        for role in sorted(playbook_roles, key=len, reverse=True):
            if name.startswith(role + "_"):
                name = name[len(role) + 1 :]
                break
        words = name.replace("_", " ")
        words = INPUT_WORDS.get(words, words)
        if inner:
            more = f" and {len(inner) - PARTS_NAMED} more" if len(inner) > PARTS_NAMED else ""
            words += f" ({', '.join(sorted(inner)[:PARTS_NAMED])}{more})"
        out.append(words)
    return out


def _source_why(files: set[str] | None, what: str = "This version of NullBill") -> str:
    if not files:
        return f"{what} sets it up differently."
    roles = sorted({found[0].replace("_", " ") if (found := _role_of(p)) else p for p in files})
    count = f"{len(files)} file{'' if len(files) == 1 else 's'}"
    return f"{what} sets it up differently: {count} of {_and(roles)} changed."


def restart_note(found: Effects, runs: bool) -> str:
    """One sentence on what a machine's reruns can restart."""
    if not runs:
        return ""
    if not found.restarts:
        return "Nothing in it restarts: no program has to start again for what changed."
    groups = {how: [r.what for r in found.restarts if r.how == how] for how in ("reboot", "restart", "reload")}
    parts = []
    if groups["reboot"]:
        parts.append("the whole machine may restart")
    if groups["restart"]:
        parts.append(f"{_and(groups['restart'])} may restart")
    if groups["reload"]:
        parts.append(f"{_and(groups['reload'])} may reload its settings, which interrupts nothing")
    text = "; ".join(parts) + "."
    text = text[0].upper() + text[1:]
    return (
        text
        if found.followed
        else text + " What changed could not be followed to single programs, so this names every one its setup can restart."
    )


def in_short(what: str, why: list[str], found: Effects, shorts: list[str]) -> tuple[str, bool]:
    """A machine's row in a few words, and whether the run interrupts
    anything on it: the page's one line per machine, with why behind it."""
    if what == "create":
        return "New guest", False
    if what in ("delete", "change"):
        # The first reason, without its further sentences.
        return (why[0].split(". ")[0].rstrip(".") if why else ""), what == "delete"
    if what != "again":
        return "", False
    if shorts:
        return "; ".join(shorts), True
    if any(r.how == "reboot" for r in found.restarts):
        return "The whole machine may restart", True
    units = [r.what for r in found.restarts if r.how == "restart"]
    if not units:
        return "Nothing restarts", False
    first = units[0][0].upper() + units[0][1:]
    return (f"{first} may restart" if len(units) == 1 else f"{first} and {len(units) - 1} more may restart"), True


@dataclass
class Baseline:
    """What the next run is compared with."""

    key: str | None  # the secret the record's fingerprints are made with
    record: dict[str, dict]
    full: bool = False  # the last run's checks failed: every step runs
    root: Path | None = None  # the checkout, for what changed in it since
    built: str | None = None  # the commit of the last setup run that finished
    trees: dict[str, Commit] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.record) and self.key is not None

    def tree(self, commit: str | None) -> Tree | None:
        """ansible/ as a commit held it, or None where git does not say."""
        if not commit or self.root is None:
            return None
        if commit not in self.trees:
            self.trees[commit] = Commit(self.root, commit)
        return self.trees[commit] if self.trees[commit].paths else None


ALL_AGAIN = "The last run's checks failed, so this run goes through every step again."
IN_PLACE = "OpenTofu changes it in place; Proxmox may restart it for that."
# New cores or memory. The pinned bpg/proxmox provider restarts a running
# container for either and a running VM for both, short of memory hotplug,
# which NullBill does not turn on; a CI pool's image is a template that
# never runs, and its clones are made from it per job.
RESIZED = "OpenTofu gives it the new size in place; if it is running, it may restart for that."
RESIZED_SHORT = "May restart for its new size"
# OpenTofu restarts only what runs; a guest that is off takes the size when started.
ON_DEMAND_RESIZED = (
    "OpenTofu gives it the new size in place: if it is running, it may restart for that; if it is off, it has the new size when it starts."
)
ON_DEMAND_RESIZED_SHORT = "May restart for its new size if running"
POOL_RESIZED = "Each job's VM gets the new size from the next job on; jobs that run keep theirs."
POOL_RESIZED_SHORT = "New size from the next job on"
CREATED = "New: OpenTofu creates it, and every part of its setup runs for the first time."
UNKNOWN = "unknown"


def _step(base: Baseline, now: Tree | None, v: Verdict) -> dict:
    """One playbook on one machine: whether it runs, why, and what that can restart."""
    does, commit = v.does, (v.last or {}).get("commit")
    files: set[str] | None = None
    if does == FIRST and not base.known:
        # Nothing written down yet. Git still says whether this version sets
        # the machine up differently from the one the server was built with.
        does, commit = UNKNOWN, base.built
    if does in (SOURCE, UNKNOWN) and base.root and commit and now is not None:
        differ = changed_files(base.root, commit)
        files = None if differ is None else differ & set(reach(now, v.step.playbook)[1])
        if does == UNKNOWN and files:
            does = SOURCE
    words = _input_words(reach(now, v.step.playbook)[0] if now else [], v.inputs)
    why = []
    if does == SOURCE:
        why.append(_source_why(files))
    if does in (SOURCE, INPUTS) and words:
        why.append(f"Changed: {_and(words)}.")
    if does == UNFINISHED:
        why.append("Its last setup did not finish, so it runs from the start.")
    if does == FIRST:
        why.append("No setup run has set this part of it up yet.")
    if does == SAME and base.full:
        why.append(ALL_AGAIN)
    runs = does in (SOURCE, INPUTS, UNFINISHED, FIRST) or (base.full and does == SAME)
    effect = Effects([])
    if runs and now is not None:
        if does in (UNFINISHED, FIRST):
            effect = _all(read_roles(now, v.step.playbook))
        elif does == SAME:
            effect = effects(now, v.step.playbook, set(), set())
        else:
            effect = effects(now, v.step.playbook, set(v.inputs), files if does == SOURCE else set(), base.tree(commit))
    return {
        "playbook": v.step.playbook,
        "stage": v.step.stage,
        "does": "again" if runs else does,
        "why": why,
        "effect": effect,
        "last": v.last,
    }


def _row(name: str | None, guest: dict | None, lines: list[dict], stuck: str | None, on_demand: bool) -> dict:
    change = (guest or {}).get("change", "keep")
    again = [line for line in lines if line["does"] == "again"]
    why: list[str] = list((guest or {}).get("detail") or []) if change in ("delete", "change") else []
    pool = (guest or {}).get("kind") == "vm-pool"
    # A new size is the run's first effect on the guest, whatever runs in it after.
    resized = bool((guest or {}).get("resized"))
    if change == "delete":
        what = "delete"
    elif change == "create":
        what, lines, again = "create", [], []
        why.append(CREATED)
    elif resized or stuck:
        # A new size is a change either way; what runs in it may still be unknown.
        what = "change" if resized else UNKNOWN
        if stuck:
            again = []
            why.append(f"What its setup runs with could not be worked out without the server ({stuck}).")
    elif again:
        what = "again"
    elif change == "change":
        what = "change"
    elif any(line["does"] == UNKNOWN for line in lines):
        what = UNKNOWN
    else:
        what = "alone"
    if change == "change":
        why.append((POOL_RESIZED if pool else ON_DEMAND_RESIZED if on_demand else RESIZED) if resized else IN_PLACE)
    for line in again:
        why += [reason for reason in line["why"] if reason not in why]
    effect = Effects(_unique(r for line in again for r in line["effect"].restarts), all(line["effect"].followed for line in again))
    notes = [line["note"] for line in again if line.get("note")]
    effect_text, interrupts = in_short(what, why, effect, [line["short"] for line in again if line.get("short")])
    if resized:
        effect_text, interrupts = (
            (POOL_RESIZED_SHORT, False) if pool else (ON_DEMAND_RESIZED_SHORT, True) if on_demand else (RESIZED_SHORT, True)
        )
    timed = [line["last"] for line in again if line["last"] and line["last"].get("took_s") is not None]
    last = None
    if timed:
        last = {
            "took_s": round(sum(t["took_s"] for t in timed), 1),
            "answered_s": round(sum(t.get("answered_s") or 0 for t in timed), 1) or None,
            "at": max(t.get("at") or 0 for t in timed) or None,
        }
    return {
        "name": name or HOST,
        "host": name is None,
        "what": what,
        "effect": effect_text,
        "interrupts": interrupts,
        "why": why,
        "restarts": [{"what": r.what, "how": r.how} for r in effect.restarts],
        # Said already by a new size, which may restart the whole guest.
        "restart_note": " ".join(notes) or ("" if resized and not effect.restarts else restart_note(effect, bool(again))),
        "last": last,
        "on_demand": on_demand,
        "steps": [{k: line[k] for k in ("playbook", "stage", "does")} for line in lines if change != "delete"],
    }


WHATS = ("create", "delete", "change", "again", "alone", UNKNOWN)
DISPATCHER = "runner-dispatcher"
BAKES_SHORT = "Its image is baked again; CI jobs wait"
BAKES = "Jobs that run finish first and new ones wait in Forgejo until the image is baked."


def _images(ctx: apply.Context, base: Baseline, at: float) -> dict[str, dict]:
    """A line per CI pool for its golden image, which keeps no mark: apply
    bakes it again once it is ci.REBAKE_AFTER_S old or the plan bakes it with
    other settings (apply.bake). And one for the dispatcher, which stops
    taking jobs meanwhile. The two roots an image trusts are read when it
    bakes, so a new root is not foreseen."""
    lines: dict[str, dict] = {}
    if not apply.planned(ctx, DISPATCHER):
        return lines
    for name in apply.linux_pools(ctx):
        last = base.record.get(applied.entry(name, applied.IMAGE)) if base.known else None
        line = {"playbook": applied.IMAGE, "stage": "ci", "does": SAME, "why": [], "effect": Effects([]), "last": None}
        if not last or last.get("baked") is None:
            line["does"] = UNKNOWN
        elif last.get("settings") != apply.image_print(ctx, name):
            line |= {"does": "again", "why": ["Its image is baked with other settings now, so the run bakes it again."]}
        elif at - last["baked"] >= ci.REBAKE_AFTER_S:
            days = ci.REBAKE_AFTER_S // 86400
            line |= {"does": "again", "why": [f"Its image is over {days} days old, so the run bakes it again."]}
        if line["does"] == "again":
            line |= {"note": BAKES, "short": BAKES_SHORT, "last": {"took_s": last.get("took_s"), "at": last.get("baked")}}
        lines[name] = line
    baked = [name for name, line in lines.items() if line["does"] == "again"]
    if baked:
        lines[DISPATCHER] = {
            "playbook": applied.IMAGE,
            "stage": "ci",
            "does": "again",
            "why": [f"It stops taking jobs while the image of {_and(baked)} is baked again."],
            "effect": Effects([Restart("nb-dispatcher", "restart")]),
            "last": None,
        }
    return lines


def machines(ctx: apply.Context, base: Baseline, guests: list[dict], at: float | None = None) -> dict:
    """A row for the host and one per guest: what a setup run started now
    (at) does to it. guests: review.guests' rows (create, delete, change, keep)."""
    found, unknown = steps(ctx)
    demand = power.on_demand(strip_notes(ctx.plan))
    # The sources apply itself reads; base.root is asked only what git knows.
    now = Checkout(REPO_ROOT)
    lines: dict[str | None, list[dict]] = {}
    for one in found:
        last = base.record.get(applied.entry(one.machine, one.playbook)) if base.known else None
        lines.setdefault(one.machine, []).append(_step(base, now, compare(base.key or "", one, last)))
    if "ci" not in unknown:
        for name, line in _images(ctx, base, time.time() if at is None else at).items():
            lines.setdefault(name, []).append(line)
    # A stage that could not be worked out: the machines it last ran on are not known.
    stuck = {entry.split("/", 1)[0]: unknown[last["stage"]] for entry, last in base.record.items() if last.get("stage") in unknown}
    rows = [_row(None, None, lines.get(None, []), stuck.get(applied.HOST), False)]
    rows += [_row(g["name"], g, lines.get(g["name"], []), stuck.get(g["name"]), g["name"] in demand) for g in guests]
    counts = {what: sum(1 for r in rows if r["what"] == what) for what in WHATS}
    return {"known": base.known, "full": base.full, "machines": rows, "counts": counts}


def update(root: Path, head: str, newest: str, ran: list[tuple[str | None, str]], record: dict[str, dict]) -> dict | None:
    """What a waiting update changes for each machine, from the files the
    two commits hold: nothing of the new version runs to say this. ran: each
    (machine, playbook) setup runs here. code: the update also changes how
    NullBill works out what a machine is set up with, which only the review
    after updating can weigh."""
    differ = changed_files(root, head, newest)
    if differ is None:
        return None
    old, new = Commit(root, head), Commit(root, newest)
    by_machine: dict[str | None, list[dict]] = {}
    reached: dict[str, set[str]] = {}
    for machine, playbook in ran:
        if playbook not in reached:
            reached[playbook] = differ & (set(reach(new, playbook)[1]) | set(reach(old, playbook)[1]))
        files = reached[playbook]
        line: dict = {"playbook": playbook, "does": "again" if files else SAME, "why": [], "effect": Effects([]), "stage": ""}
        if files:
            line["why"] = [_source_why(files, "The update")]
            line["effect"] = effects(new, playbook, set(), files, old) if new.read(playbook) is not None else Effects([])
        line["last"] = record.get(applied.entry(machine, playbook))
        by_machine.setdefault(machine, []).append(line)
    rows = [_row(machine, None, lines, None, False) for machine, lines in by_machine.items()]
    rows.sort(key=lambda r: (r["what"] != "again", not r["host"]))
    code = _code(root, head, newest)
    return {"machines": rows, "code": code, "counts": {what: sum(1 for r in rows if r["what"] == what) for what in ("again", "alone")}}


def _code(root: Path, head: str, newest: str) -> bool:
    """Does the update change the code that plans and applies?"""
    from nullbill import version

    try:
        done = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", head, newest, "--", "src/nullbill"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    return done.returncode != 0 or any(version._in_setup(line) for line in done.stdout.splitlines())
