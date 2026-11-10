"""The Services page's block for a game server: whether it answers and who
plays, its log and console, and its worlds (docs/adr/0019-game-worlds.md).

Everything here is asked of `nb-game` in the server's guest, through the
host (`pct exec`): the guest holds the worlds and is the only one that
moves them. This module turns what the page sends into `nb-game`'s
arguments and what `nb-game` answers into what the page shows.
"""

import json

from nullbill import games
from nullbill.errors import NullBillError

ACTIONS = ("restart", "console", "new", "use", "rename", "delete")
# What a world's own options are called, for the line under its name.
ORIGIN = {"setup": "Made by the first setup run", "new": "Made here", "upload": "Uploaded"}


def summary(game: str, options: dict) -> list[str]:
    """A world's options in the page's words: "Expert", "Seed nullbill"."""
    known = {o["key"]: o for o in games.NEW_WORLD[game]}
    out = []
    for key, value in options.items():
        option = known.get(key)
        if option is None:
            continue
        if "choices" in option:
            label = next((text for choice, text in option["choices"] if str(choice) == str(value)), str(value))
            out.append(label)
        elif isinstance(option["default"], bool):
            name = option["label"].split(":")[0]
            out.append(name if str(value).lower() == "true" else f"No {name[0].lower()}{name[1:]}")
        else:
            out.append(f"Seed {value}")
    return out


def view(model: dict, name: str, report: dict | None, why: str | None = None, off: bool = False) -> dict:
    """What the page shows of a game server. report: `nb-game report`, or
    None with `why` when the guest did not answer (off: because it runs on
    demand and is off)."""
    game, of = model["games"][name], games.kind(model, name)
    out: dict = {
        "name": name,
        "title": game["title"],
        "kind": of,
        "word": games.WORLD[of],
        "upload": games.WORLD_FILE[of],
        "max_upload": games.MAX_WORLD_BYTES,
        "say": games.SAY[of],
        "new_world": games.new_world_options(model, name),
        "live": report is not None,
        "off": off,
        "why": why,
    }
    if report is None:
        return out
    out |= {key: report.get(key) for key in ("running", "players", "names", "active", "free_bytes", "most")}
    # Which server runs the game and its plugin or mod files (Minecraft);
    # a guest from before NullBill asked says nothing, and runs Mojang's.
    out |= {"server": str(report.get("server") or "vanilla"), "mods": [str(f) for f in report.get("mods") or []]}
    out["log"] = [str(line) for line in report.get("log") or []]
    out["worlds"] = [
        {
            "id": world["id"],
            "title": world["title"],
            "active": bool(world.get("active")),
            "bytes": int(world.get("bytes") or 0),
            "made": world.get("made") if world.get("origin") != "setup" else None,
            "origin": ORIGIN.get(world.get("origin"), ""),
            "options": summary(of, world.get("options") or {}),
            # Minecraft: one Paper has opened, which only Paper opens now.
            **({"paper": True} if world.get("paper") else {}),
        }
        for world in report.get("worlds") or []
    ]
    return out


def world_id(value: object) -> str:
    ident = str(value or "")
    if not games.WORLD_ID.fullmatch(ident):
        raise NullBillError("name the world by its id, as the list gives it")
    return ident


def arguments(model: dict, name: str, body: dict) -> tuple[list[str], str, bool]:
    """(nb-game's arguments, what is happening in the page's words, whether
    it may take minutes) for an action the page or the agent asks."""
    action = body.get("action")
    title = model["games"][name]["title"]
    word = games.WORLD[games.kind(model, name)]
    kick = ["--kick"] if body.get("kick") is True else []
    if action == "restart":
        return ["service", "restart"], f"restarting {title}", True
    if action == "console":
        return ["console", games.read_console(body.get("line"))], "typing on the console", False
    if action == "new":
        made = games.read_world_title(body.get("title"))
        return ["world", "new", made, *games.read_new_world(model, name, body.get("options")), *kick], f"making the {word} {made}", True
    if action == "use":
        return ["world", "use", world_id(body.get("id")), *kick], f"changing the {word} that runs", True
    if action == "rename":
        return ["world", "rename", world_id(body.get("id")), games.read_world_title(body.get("title"))], "renaming", False
    if action == "delete":
        return ["world", "delete", world_id(body.get("id"))], f"deleting a {word}", False
    raise NullBillError(f"send action: {', '.join(ACTIONS)}")


def answer(out: str, err: str, rc: int) -> dict:
    """nb-game world's one JSON line; anything else is an error in its own words."""
    try:
        found = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except ValueError:
        found = None
    if not isinstance(found, dict) or "ok" not in found:
        said = (err.strip() or out.strip()).removeprefix("nb-game: ")
        raise NullBillError(said[:300] or f"the game server's guest did not answer (exit {rc})")
    return found
