"""Each guest's processor cores and memory, as the owner sets them.

config.yaml's `overrides.<service>.cores` and `.memory_mib` are the one
place a size is pinned; plan._size applies them. This reads the page's form
(Setup, What to run: Size under each row, and Core servers) and the agent's
set_choices into them. An empty value, or the service's floor, is
automatic: nothing is written down and the plan gives the floor. A pool's
size is each clone's. The nullbill container is install.sh's, which
OpenTofu does not manage, so it has no size here.

A size is checked on its own (whole cores from the floor to the host's
threads, memory from the floor to the host's memory in quarter GiB), every
problem a message of its own. A save that gives guests more memory than
before and so no longer fits is refused with how much is missing and what
would fit (`too_much`); a tick that does not fit is saved as before and the
plan says so.
"""

import copy

from nullbill import custom
from nullbill import plan as planning
from nullbill.errors import NullBillError, Refused

KEYS = ("cores", "memory_mib")
STEP_MIB = 256
# Without a probe of the host: the most the schema of the owner's own takes.
MAX_CORES, MAX_MIB = custom.SIZE_LIMITS["cores"][1], custom.SIZE_LIMITS["memory_mib"][1]


def sizable(model: dict, name: str) -> bool:
    svc = model["services"].get(name)
    return svc is not None and not svc.get("installed")


def label(model: dict, name: str) -> str:
    """What a refusal calls a guest: a game server's title, a service of
    the owner's own as the page shows it, else the guest's name."""
    if name in model.get("games", {}):
        return model["games"][name]["title"]
    if name in custom.names(model):
        return custom.title(model, name)
    return name


def least(model: dict, name: str) -> dict:
    floor = model["services"][name]["floor"]
    return {key: floor[key] for key in KEYS}


def limits(facts: dict | None) -> dict:
    """The most a guest may have: the host's threads and its memory, in
    steps; without a probe, the most config.yaml takes."""
    if not facts:
        return {"cores": MAX_CORES, "memory_mib": MAX_MIB}
    return {"cores": facts["cpu"]["threads"], "memory_mib": facts["memory_mib"] // STEP_MIB * STEP_MIB}


def gib(mib: int) -> str:
    return f"{round(mib / 1024, 2):g} GiB"


def amount(mib: int) -> str:
    """A shortfall: under a GiB in MiB, which 0.02 GiB says badly."""
    return f"{mib} MiB" if mib < 1024 else gib(mib)


def _whole(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _read(model: dict, name: str, key: str, value: object, most: dict, probed: bool) -> int | None:
    """One value from a form; None for automatic. Raises what is wrong."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    floor, top = least(model, name)[key], most[key]
    asked = _whole(value)
    if key == "cores":
        if asked is None or not floor <= asked <= max(floor, top):
            room = ", as many as the server has threads" if probed else ""
            raise NullBillError(f"{label(model, name)}: its cores are a whole number from {floor} to {max(floor, top)}{room}")
    elif asked is None or not floor <= asked <= max(floor, top) or (asked != floor and asked % STEP_MIB):
        raise NullBillError(
            f"{label(model, name)}: its memory is from {gib(floor)} to {gib(max(floor, top))} in steps of "
            f"{gib(STEP_MIB)} ({floor} to {max(floor, top)} MiB)"
        )
    return None if asked == floor else asked


def read(model: dict, overrides: dict, form: object, facts: dict | None = None, strict: bool = True) -> tuple[dict, list[str]]:
    """overrides with the sizes form put into it, and every problem, a
    message each. form: {guest: {cores, memory_mib}}; a key the form leaves
    out stays as it is. strict False (the page's preview) keeps the saved
    value for what cannot be read, so the meter follows what can."""
    out = copy.deepcopy(overrides)
    if not isinstance(form, dict):
        return out, []
    most = limits(facts)
    problems: list[str] = []
    for name, asked in form.items():
        if not isinstance(asked, dict):
            continue
        if not sizable(model, str(name)):
            if strict:
                why = "install.sh made it; change its size in Proxmox" if str(name) in model["services"] else "no guest has this name"
                problems.append(f"{str(name)[:40]}: {why}")
            continue
        pin = dict(out.get(name) or {})
        for key in KEYS:
            if key not in asked:
                continue
            try:
                value = _read(model, name, key, asked[key], most, bool(facts))
            except NullBillError as exc:
                if strict:
                    problems.extend(exc.messages)
                continue
            if value is None:
                pin.pop(key, None)
            else:
                pin[key] = value
        if pin:
            out[name] = pin
        else:
            out.pop(name, None)
    return out, problems


def raise_problems(problems: list[str], headline: str) -> None:
    """Several problems as a headline with a list, one as itself."""
    if len(problems) > 1:
        raise Refused(headline, problems)
    if problems:
        raise NullBillError(*problems)


def _memory(model: dict, name: str) -> int:
    return model.get("overrides", {}).get(name, {}).get("memory_mib", least(model, name)["memory_mib"])


def too_much(before: dict, after: dict, facts: dict | None) -> list[str]:
    """Why a save that gives guests more memory than before does not fit:
    a message per such guest, with what is missing and the most that fits
    beside the rest. Empty when it fits, when no guest got more, or when it
    did not fit before either for other reasons than those guests.
    before, after: merged models."""
    if not facts:
        return []
    over = planning.memory_over(after, facts)
    if over <= 0:
        return []
    planned = planning.planned_services(after, facts)

    def was(name: str) -> int:
        return _memory(before, name) if name in before["services"] else least(after, name)["memory_mib"]

    more = [n for n in planned if _memory(after, n) > was(n)]
    if not more:
        return []
    back = copy.deepcopy(after)
    for name in more:
        back.setdefault("overrides", {}).setdefault(name, {})["memory_mib"] = was(name)
    if planning.memory_over(back, facts) >= over:
        return []
    out = []
    for name in more:
        # Each on its own: the others given more are taken as they were before.
        trial = copy.deepcopy(back)
        pin = trial["overrides"][name]
        # The most that fits, of the values a form takes: the floor, or a step.
        floor = least(after, name)["memory_mib"]
        values = sorted({floor, *range(-(-floor // STEP_MIB) * STEP_MIB, _memory(after, name), STEP_MIB)})
        low, high, best = 0, len(values) - 1, None
        while low <= high:
            mid = (low + high) // 2
            pin["memory_mib"] = values[mid]
            if planning.memory_over(trial, facts) <= 0:
                best, low = values[mid], mid + 1
            else:
                high = mid - 1
        said = f"{label(after, name)}: {gib(_memory(after, name))} of memory does not fit; the server would be {amount(over)} short."
        if best is None:
            said += " Even its least does not fit beside the rest: give something else less, or turn it off."
        elif len(more) > 1:
            said += f" At most {gib(best)} fits beside the rest as it was."
        else:
            said += f" At most {gib(best)} fits beside the rest."
        out.append(said)
    return out
