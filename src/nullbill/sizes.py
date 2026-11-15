"""Each guest's processor cores, memory and disk, as the owner sets them.

config.yaml's `overrides.<service>.cores`, `.memory_mib` and `.disk_gib`
are the one place a size is pinned; plan._size applies them. This reads
the page's form (Setup, What to run: Size under each row, and Core
servers) and the agent's set_choices into them. An empty value, or the
service's floor, is automatic: nothing is written down and the plan gives
the floor. A pool's size is each clone's. The nullbill container is
install.sh's, which OpenTofu does not manage, so it has no size here.

A size the form changes is checked on its own (whole cores from the floor
to the host's threads, memory from the floor to the host's memory in
quarter GiB, disk in whole GiB from the floor, or from what the guest has
on the server, to what the storage has free), every problem a message of
its own; a size the form sends as it is saved passes untouched, so a value
saved before a rule or the hardware changed blocks no other save. A save
that gives guests more memory than before and so no longer fits, when it
fitted before, is refused with how much is missing and what would fit
(`too_much`); disks likewise against the storage (`disk_too_much`). A tick
that does not fit is saved as before and the plan says so.

A disk only grows. apply grows each guest's disk to its planned size and
never shrinks one (apply.grow_disks), and records what each guest has in
`$NULLBILL_HOME/guest-disks.json` (`DISKS`); the form refuses less.
"""

import copy
import json
from pathlib import Path

from nullbill import custom
from nullbill import plan as planning
from nullbill.errors import NullBillError, Refused

KEYS = ("cores", "memory_mib", "disk_gib")
STEP_MIB = 256
# Without a probe of the host: the most the schema of the owner's own takes
# for cores and disk, and for memory 64 GiB, what the game form took before sizes.
MAX_CORES, MAX_MIB, MAX_GIB = custom.SIZE_LIMITS["cores"][1], 65536, custom.SIZE_LIMITS["disk_gib"][1]
# What apply measured of each guest's disk, in GiB, by guest name.
DISKS = "guest-disks.json"


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
    steps, and what the guests' storage has free; without a probe, the most
    config.yaml takes."""
    if not facts:
        return {"cores": MAX_CORES, "memory_mib": MAX_MIB, "disk_gib": MAX_GIB}
    storage = planning.guest_storage(facts)
    return {
        "cores": facts["cpu"]["threads"],
        "memory_mib": facts["memory_mib"] // STEP_MIB * STEP_MIB,
        "disk_gib": min(MAX_GIB, storage["avail_gib"]) if storage else MAX_GIB,
    }


def measured(home: Path | None) -> dict[str, int]:
    """Each guest's disk in GiB as apply last measured it; empty when no
    run has."""
    try:
        found = json.loads((home / DISKS).read_text()) if home else {}
    except (OSError, ValueError):
        return {}
    disks = found.get("disks") if isinstance(found, dict) else None
    return {str(k): v for k, v in (disks or {}).items() if isinstance(v, int) and not isinstance(v, bool)}


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
    if isinstance(value, str) and value.strip().isdecimal():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _read(model: dict, name: str, key: str, value: object, most: dict, probed: bool, has: int | None) -> int | None:
    """One value from a form; None for automatic. Raises what is wrong.
    has: the guest's disk on the server, which a disk set may not go below.
    Automatic stays automatic: apply keeps a larger disk as it is."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    floor, top = least(model, name)[key], most[key]
    asked = _whole(value)
    if key == "cores":
        if asked is None or not floor <= asked <= max(floor, top):
            room = ", as many as the server has threads" if probed else ""
            raise NullBillError(f"{label(model, name)}: its cores are a whole number from {floor} to {max(floor, top)}{room}")
    elif key == "disk_gib":
        low = max(floor, has or 0)
        if asked is not None and floor <= asked < low:
            raise NullBillError(f"{label(model, name)}: its disk has {low} GiB on the server and a disk only grows, so {low} GiB or more")
        if asked is None or not low <= asked <= max(low, top):
            room = ", what the server's storage has free" if probed else ""
            raise NullBillError(f"{label(model, name)}: its disk is a whole number of GiB from {low} to {max(low, top)}{room}")
    elif asked is None or not floor <= asked <= max(floor, top) or (asked != floor and asked % STEP_MIB):
        raise NullBillError(
            f"{label(model, name)}: its memory is from {gib(floor)} to {gib(max(floor, top))} in steps of "
            f"{gib(STEP_MIB)} ({floor} to {max(floor, top)} MiB)"
        )
    return None if asked == floor else asked


def read(
    model: dict, overrides: dict, form: object, facts: dict | None = None, strict: bool = True, has: dict[str, int] | None = None
) -> tuple[dict, list[str]]:
    """overrides with the sizes form put into it, and every problem, a
    message each. form: {guest: {cores, memory_mib, disk_gib}}; a key the
    form leaves out stays as it is. strict False (the page's preview) keeps
    the saved value for what cannot be read, so the meter follows what can.
    has: each guest's disk on the server (`measured`)."""
    out = copy.deepcopy(overrides)
    if form is None:
        return out, []
    if not isinstance(form, dict):
        return out, [SHAPE] if strict else []
    most = limits(facts)
    problems: list[str] = []
    for name, asked in form.items():
        if not isinstance(asked, dict):
            if strict:
                said = label(model, str(name)) if str(name) in model["services"] else str(name)[:40]
                problems.append(f"{said}: {GUEST_SHAPE}")
            continue
        if not sizable(model, str(name)):
            if strict:
                why = "install.sh made it; change its size in Proxmox" if str(name) in model["services"] else "no guest has this name"
                problems.append(f"{str(name)[:40]}: {why}")
            continue
        pin = dict(out.get(name) or {})
        if strict:
            problems += [f"{label(model, name)}: {str(key)[:40]} {NOT_A_SIZE}" for key in asked if key not in KEYS]
        for key in KEYS:
            if key not in asked:
                continue
            if key in pin and _whole(asked[key]) == pin[key]:
                # As it is saved: kept without a check, which a rule or the
                # hardware may have moved since.
                continue
            try:
                value = _read(model, name, key, asked[key], most, bool(facts), (has or {}).get(name))
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


SHAPE = (
    "sizes: give each guest's name with its cores, memory_mib and disk_gib, "
    'as in {"forgejo": {"cores": 2, "memory_mib": 2048, "disk_gib": 64}}'
)
GUEST_SHAPE = 'its size is cores, memory_mib and disk_gib, as in {"cores": 2, "memory_mib": 2048, "disk_gib": 64}'
NOT_A_SIZE = "is not a size; a size is cores, memory_mib and disk_gib"


def raise_problems(problems: list[str], headline: str) -> None:
    """Several problems as a headline with a list, one as itself."""
    if len(problems) > 1:
        raise Refused(headline, problems)
    if problems:
        raise NullBillError(*problems)


def _memory(model: dict, name: str) -> int:
    return model.get("overrides", {}).get(name, {}).get("memory_mib", least(model, name)["memory_mib"])


def _disk(model: dict, name: str) -> int:
    return model.get("overrides", {}).get(name, {}).get("disk_gib", least(model, name)["disk_gib"])


def too_much(before: dict, after: dict, facts: dict | None) -> list[str]:
    """Why a save that gives guests more memory than before does not fit:
    a message per such guest, with what is missing and the most that fits
    beside the rest. Empty when it fits, when no guest got more, or when it
    does not fit with those guests as they were either (a tick or an older
    size: the plan says so, as before). So what is missing is all this
    change's. before, after: merged models."""
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
    if planning.memory_over(back, facts) > 0:
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


def disk_too_much(before: dict, after: dict, facts: dict | None) -> list[str]:
    """As too_much, for disks against the storage the guests are on: a
    message per guest given a larger disk, with the most that fits beside
    the rest. Disk adds up, so the most is what is asked less the shortfall
    the guest makes alone."""
    if not facts:
        return []
    over = planning.disk_over(after, facts)
    if over <= 0:
        return []
    planned = planning.planned_services(after, facts)

    def was(name: str) -> int:
        return _disk(before, name) if name in before["services"] else least(after, name)["disk_gib"]

    more = [n for n in planned if _disk(after, n) > was(n)]
    if not more:
        return []
    back = copy.deepcopy(after)
    for name in more:
        back.setdefault("overrides", {}).setdefault(name, {})["disk_gib"] = was(name)
    left = -planning.disk_over(back, facts)
    if left < 0:
        return []
    storage = planning.guest_storage(facts)["name"]
    out = []
    for name in more:
        # A pool's disk counts once for its golden image and once per job VM.
        times = planning.disk_copies(after, name)
        best = was(name) + left // times
        said = f"{label(after, name)}: a {_disk(after, name)} GiB disk does not fit; storage {storage} would be {over} GiB short."
        tail = " as it was" if len(more) > 1 else ""
        said += f" At most {best} GiB fits beside the rest{tail}."
        out.append(said)
    return out
