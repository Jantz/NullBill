"""Which server runs a Minecraft game server, and its plugins or mods
(docs/adr/0021-minecraft-mods.md).

`games.<name>.server` is vanilla (Mojang's own file), paper (takes plugins)
or fabric (takes mods); the catalog pins Paper's build and Fabric's loader
with its libraries by SHA-256, beside Mojang's file, which both start.
`games.<name>.mods` lists plugins or mods from Modrinth, each one file
pinned by the SHA-512 Modrinth gave when the owner added it. The page asks
Modrinth (search, a project's versions, what a version needs) and writes
the list; the server's guest downloads exactly those files through the
egress proxy and takes none whose hash differs.

Nothing here reaches the server: this module reads Modrinth and edits
config.yaml. The role (ansible/roles/game_minecraft) installs what the list
says with the next run of setup's games stage.
"""

import copy
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from nullbill.errors import NullBillError

API = "https://api.modrinth.com/v2/"
SITE = "https://modrinth.com"
TIMEOUT_S = 20
KINDS = ("vanilla", "paper", "fabric")
# Modrinth's loader names a server of each kind runs files for. Paper runs
# plugins written for Spigot and Bukkit, which it grew out of.
LOADERS = {"paper": ("paper", "spigot", "bukkit"), "fabric": ("fabric",)}
# What Modrinth calls such a project, and the page such a file.
PROJECT = {"paper": "plugin", "fabric": "mod"}
# A project may be both (Chunky is): what counts is a file for the kind.
WORD = PROJECT
LABEL = {"vanilla": "Vanilla", "paper": "Paper", "fabric": "Fabric"}
WHAT = {
    "vanilla": "Mojang's own server, as it comes. No plugins or mods.",
    "paper": "Takes plugins. Players join with the ordinary game, nothing to install.",
    "fabric": "Takes mods. Some mods must also be in each player's own game; the list says which.",
}
MAX_MODS = 60
MAX_QUERY = 80
HITS = 20
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,120}[.]jar")
URL = re.compile(r"https://cdn[.]modrinth[.]com/data/[A-Za-z0-9]+/versions/[A-Za-z0-9]+/[A-Za-z0-9%._+-]+[.]jar")
PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._!@$()`+,-]{0,63}")
# Newest first within what its authors call finished.
CHANNELS = ("release", "beta", "alpha")

Fetch = Callable[[str, dict], object]


def fetch(path: str, params: dict | None = None) -> object:
    """One GET of Modrinth's API. NULLBILL_MODRINTH_API points it at a
    stand-in. The client names itself and nothing of the owner."""
    base = os.environ.get("NULLBILL_MODRINTH_API", API)
    query = urllib.parse.urlencode({key: value if isinstance(value, str) else json.dumps(value) for key, value in (params or {}).items()})
    request = urllib.request.Request(base + path + (f"?{query}" if query else ""), headers={"User-Agent": "nullbill"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NullBillError("Modrinth has nothing of that name") from None
        raise NullBillError(f"Modrinth answered {error.code}; try again in a moment") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise NullBillError(f"Modrinth did not answer ({str(getattr(error, 'reason', error))[:120]})") from None


def takes(model: dict, name: str) -> bool:
    """Whether a game server can run plugins or mods at all: Minecraft's."""
    return "server" in model["games"].get(name, {})


def server(model: dict, name: str) -> str:
    return model["games"][name].get("server", "vanilla")


def files(model: dict, name: str) -> list[dict]:
    """What the server's kind is made of besides Mojang's file, each
    {file, url, sha256}: Paper's one file, Fabric's loader and libraries."""
    game, kind = model["games"][name], server(model, name)
    if kind == "paper":
        pin = game["servers"]["paper"]
        return [{"file": f"paper-{pin['version']}-{pin['build']}.jar", "url": pin["url"], "sha256": pin["sha256"]}]
    if kind == "fabric":
        return [dict(lib) for lib in game["servers"]["fabric"]["libraries"]]
    return []


def release(model: dict, name: str) -> str:
    """The kind's own release, for the page: Paper's build, Fabric's loader."""
    game, kind = model["games"][name], server(model, name)
    if kind == "paper":
        return f"build {game['servers']['paper']['build']}"
    if kind == "fabric":
        return f"loader {game['servers']['fabric']['loader']}"
    return ""


def problems(model: dict, name: str) -> list[str]:
    """What plan refuses about a server's kind and its plugins or mods."""
    game, kind = model["games"][name], server(model, name)
    out: list[str] = []
    listed = game.get("mods") or []
    if kind == "vanilla":
        if listed:
            out.append(
                f"games.{name}.mods: Mojang's own server takes no plugins or mods; "
                f"choose Paper or Fabric for {game['title']} on the Services page, or remove them"
            )
        return out
    pin = game["servers"][kind]
    if pin["version"] != game["version"]:
        out.append(
            f"games.{name}.servers.{kind}: it is pinned for Minecraft {pin['version']} and the server is {game['version']}; "
            "move both pins together"
        )
    seen_files: set[str] = set()
    seen_projects: set[str] = set()
    for mod in listed:
        where, word = f"games.{name}.mods: {mod['title']} {mod['version']}", WORD[kind]
        if not set(mod["loaders"]) & set(LOADERS[kind]):
            made_for = ", ".join(mod["loaders"]) or "no server"
            out.append(f"{where} is made for {made_for}, and {game['title']} runs {LABEL[kind]}; remove it on the Services page")
        elif game["version"] not in mod["game_versions"]:
            out.append(
                f"{where} is not for Minecraft {game['version']}; on the Services page, Check for updates takes a newer "
                f"{word} where there is one, or remove it"
            )
        if mod["file"].lower() in seen_files or mod["project"] in seen_projects:
            out.append(f"{where} is listed twice")
        seen_files.add(mod["file"].lower())
        seen_projects.add(mod["project"])
    have = {mod["project"] for mod in listed}
    for mod in listed:
        for need in mod.get("needs", []):
            if need not in have:
                out.append(f"games.{name}.mods: {mod['title']} needs {need}, which is not in the list; add it on the Services page")
    return out


def role_mods(model: dict, name: str) -> list[dict]:
    """What the role downloads: each file, where from and its SHA-512."""
    return [{"file": mod["file"], "url": mod["url"], "sha512": mod["sha512"]} for mod in model["games"][name].get("mods") or []]


# Modrinth, read.


def _facets(kind: str, version: str) -> list[list[str]]:
    out = [[f"categories:{loader}" for loader in LOADERS[kind]], [f"versions:{version}"], [f"project_type:{PROJECT[kind]}"]]
    if kind == "fabric":
        # Not what only a player's own game runs (shaders' helpers, maps).
        out.append(["server_side:required", "server_side:optional"])
    return out


def read_query(value: object) -> str:
    query = " ".join(str(value or "").split())
    if len(query) > MAX_QUERY or re.search(r"[\x00-\x1f\x7f]", query):
        raise NullBillError(f"search for at most {MAX_QUERY} characters")
    return query


def search(ask: Fetch, model: dict, name: str, query: object) -> list[dict]:
    """Plugins or mods for this server's kind and Minecraft, the most used
    first; an empty query lists the most used of all."""
    kind = _kind_that_takes(model, name)
    version, have = model["games"][name]["version"], {mod["project"] for mod in model["games"][name].get("mods") or []}
    params = {"query": read_query(query), "limit": str(HITS), "index": "relevance" if read_query(query) else "downloads"}
    found = ask("search", params | {"facets": _facets(kind, version)})
    hits = found.get("hits") if isinstance(found, dict) else None
    if not isinstance(hits, list):
        raise NullBillError("Modrinth's answer was not a list of projects")
    out = []
    for hit in hits:
        if not isinstance(hit, dict) or not PROJECT_NAME.fullmatch(str(hit.get("slug") or "")):
            continue
        out.append(
            {
                "project": hit["slug"],
                "title": _line(hit.get("title"), 80) or hit["slug"],
                "what": _line(hit.get("description"), 200),
                "by": _line(hit.get("author"), 40),
                "downloads": int(hit.get("downloads") or 0),
                "players_need": hit.get("client_side") == "required",
                "added": hit["slug"] in have,
                "link": f"{SITE}/project/{hit['slug']}",
            }
        )
    return out


def _line(value: object, most: int) -> str:
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).split())[:most]


def _kind_that_takes(model: dict, name: str) -> str:
    if name not in model["games"] or not takes(model, name):
        raise NullBillError("only a Minecraft server takes plugins or mods")
    kind = server(model, name)
    if kind == "vanilla":
        raise NullBillError(
            f"{model['games'][name]['title']} runs Mojang's own server, which takes none; choose Paper (plugins) or Fabric (mods) first"
        )
    return kind


def _newest(ask: Fetch, kind: str, version: str, project: str) -> dict | None:
    """The newest version of a project for this kind and Minecraft: a
    release before a beta before an alpha."""
    listed = ask(f"project/{urllib.parse.quote(project, safe='')}/version", {"loaders": list(LOADERS[kind]), "game_versions": [version]})
    if not isinstance(listed, list):
        raise NullBillError("Modrinth's answer was not a list of versions")
    usable = [v for v in listed if isinstance(v, dict) and v.get("status", "listed") == "listed" and v.get("files")]
    for channel in CHANNELS:
        of = sorted((v for v in usable if v.get("version_type") == channel), key=lambda v: str(v.get("date_published")), reverse=True)
        if of:
            return of[0]
    return None


def _entry(kind: str, version: str, project: dict, found: dict) -> dict:
    """config.yaml's entry for one version of a project."""
    title = _line(project.get("title"), 80) or str(project.get("slug"))
    jars = [f for f in found["files"] if isinstance(f, dict) and str(f.get("filename", "")).endswith(".jar")]
    file = next((f for f in jars if f.get("primary")), jars[0] if jars else None)
    if file is None:
        raise NullBillError(f"{title} has no .jar file for {LABEL[kind]} on Minecraft {version}")
    name, url, sha512 = str(file.get("filename")), str(file.get("url")), str((file.get("hashes") or {}).get("sha512"))
    if not FILE.fullmatch(name) or not URL.fullmatch(url) or not re.fullmatch(r"[0-9a-f]{128}", sha512):
        raise NullBillError(f"{title}: its file is not one NullBill takes (a .jar from Modrinth's own store with a SHA-512)")
    number = _line(found.get("version_number"), 50) or "?"
    channel = found.get("version_type")
    entry = {
        "project": str(project["slug"]),
        "id": str(project["id"]),
        "title": title,
        "version": number if channel == "release" else f"{number} ({channel})",
        "file": name,
        "url": url,
        "sha512": sha512,
        "loaders": [str(loader) for loader in found.get("loaders") or []][:30],
        "game_versions": [str(v) for v in found.get("game_versions") or []][:200],
    }
    if project.get("client_side") == "required":
        entry["players_need"] = True
    return entry


def resolve(ask: Fetch, model: dict, name: str, project: object, replace: bool = False) -> list[dict]:
    """One project as entries for a server's list: its newest version for
    the server's kind and Minecraft, then everything that version needs
    and the list lacks, each marked as there for others. replace: the
    project is in the list and its newest version is wanted."""
    kind = _kind_that_takes(model, name)
    game = model["games"][name]
    version, word = game["version"], WORD[kind]
    asked = str(project or "").strip()
    if not PROJECT_NAME.fullmatch(asked):
        raise NullBillError(f"name the {word} as Modrinth does in its address, such as lithium")
    have = {mod["project"]: mod for mod in game.get("mods") or []}
    ids = {mod["id"]: mod for mod in have.values()}
    out: list[dict] = []
    queue: list[tuple[str, bool]] = [(asked, False)]
    while queue:
        wanted, for_others = queue.pop(0)
        info = ask(f"project/{urllib.parse.quote(wanted, safe='')}")
        if not isinstance(info, dict) or not PROJECT_NAME.fullmatch(str(info.get("slug") or "")) or not info.get("id"):
            raise NullBillError(f"Modrinth has no {word} called {wanted[:40]!r}")
        title = _line(info.get("title"), 80) or info["slug"]
        if info["slug"] in {e["project"] for e in out}:
            continue
        if str(info["id"]) in ids and not (replace and not for_others):
            if not for_others:
                raise NullBillError(f"{title} is in {game['title']}'s list already")
            continue
        if info.get("server_side") == "unsupported":
            raise NullBillError(f"{title} is for a player's own game only; a server does not run it")
        found = _newest(ask, kind, version, str(info["id"]))
        if found is None:
            raise NullBillError(f"{title} has no version for {LABEL[kind]} on Minecraft {version}")
        entry = _entry(kind, version, info, found) | ({"for_others": True} if for_others else {})
        needs = []
        for dep in found.get("dependencies") or []:
            other = str((dep or {}).get("project_id") or "")
            if not other:
                continue
            if dep.get("dependency_type") == "incompatible" and other in ids:
                raise NullBillError(f"{title} does not work together with {ids[other]['title']}, which is in the list")
            if dep.get("dependency_type") == "required":
                needs.append(other)
        entry["_needs"] = needs
        out.append(entry)
        queue += [(other, True) for other in needs if other not in ids]
        if len(out) + len(have) > MAX_MODS:
            raise NullBillError(f"a server takes at most {MAX_MODS} {word}s")
    # What each needs, by the names the list goes by.
    slug = {e["id"]: e["project"] for e in out} | {mod["id"]: mod["project"] for mod in have.values()}
    for entry in out:
        needs = [slug[other] for other in entry.pop("_needs") if other in slug]
        if needs:
            entry["needs"] = needs
    return out


def newer(ask: Fetch, model: dict, name: str) -> list[dict]:
    """Entries for every plugin or mod with another version now, and for
    what those need that the list lacks."""
    game = model["games"][name]
    out: list[dict] = []
    for mod in game.get("mods") or []:
        if mod["id"] in {e["id"] for e in out}:
            continue
        try:
            # By its id: a project may have been renamed since.
            found = resolve(ask, model, name, mod["id"], replace=True)
        except NullBillError:
            # Gone from Modrinth, or nothing for this Minecraft: it stays as it is.
            continue
        if found[0]["sha512"] == mod["sha512"]:
            continue
        found[0] |= {key: mod[key] for key in ("for_others",) if key in mod}
        out += [e for e in found if e["id"] not in {o["id"] for o in out}]
    return out


# config.yaml, written.


def _own(config: dict, model: dict, name: str) -> tuple[dict, dict]:
    """(a copy of config.yaml, this server's own entry in it)."""
    if name not in model["games"] or not takes(model, name):
        raise NullBillError("only a Minecraft server takes plugins or mods")
    out = copy.deepcopy(config)
    return out, out.setdefault("games", {}).setdefault(name, {})


def _tidy(config: dict, name: str) -> dict:
    entry = config["games"][name]
    if entry.get("server") == "vanilla":
        del entry["server"]
    if not entry.get("mods"):
        entry.pop("mods", None)
    if not entry:
        del config["games"][name]
    if not config["games"]:
        del config["games"]
    return config


def set_server(config: dict, model: dict, name: str, kind: object) -> tuple[dict, list[str]]:
    """config.yaml with another kind of server for a game server, and the
    titles of the plugins or mods that leave with the old kind: a plugin
    is no mod, and Mojang's server takes neither."""
    if kind not in KINDS:
        raise NullBillError(f"a Minecraft server is one of {', '.join(KINDS)}")
    out, entry = _own(config, model, name)
    listed = model["games"][name].get("mods") or []
    gone = [mod["title"] for mod in listed] if kind != server(model, name) else []
    entry["server"] = kind
    if gone:
        entry.pop("mods", None)
    return _tidy(out, name), gone


def consequences(model: dict, name: str, kind: str, gone: list[str]) -> list[str]:
    """What the owner should know before a server's kind changes; nothing
    between Mojang's server and Fabric without a list, which share worlds."""
    now = server(model, name)
    if kind == now:
        return []
    out = []
    if gone:
        word, one = WORD[now], len(gone) == 1
        out.append(
            f"{', '.join(gone)} {'leaves' if one else 'leave'} the list: {LABEL[kind]} cannot run "
            f"{'that ' + word if one else 'those ' + word + 's'}. "
            f"What only a {word} added to the world may be gone when it is opened without."
        )
    if kind == "paper":
        out.append(
            "Paper stores a world its own way the first time it opens it. From then on only Paper opens that world: "
            "Fabric and Mojang's own server cannot."
        )
        out.append("Download the world first (Worlds, Download) if you may want it as it is now.")
    if now == "paper":
        other = "Mojang's own server" if kind == "vanilla" else LABEL[kind]
        out.append(
            f"A world Paper has opened does not open under {other}. "
            "The one that runs is kept in the list, for Paper, and the server starts with a new world."
        )
    return out


def put(config: dict, model: dict, name: str, entries: list[dict]) -> dict:
    """config.yaml with these entries in a server's list, each in the
    place of the one of its project where there is one."""
    out, entry = _own(config, model, name)
    listed = [dict(mod) for mod in model["games"][name].get("mods") or []]
    for new in entries:
        at = next((i for i, mod in enumerate(listed) if mod["id"] == new["id"]), None)
        if at is None:
            listed.append(new)
        else:
            # Asked for by the owner once, it stays the owner's.
            listed[at] = new if listed[at].get("for_others") else {k: v for k, v in new.items() if k != "for_others"}
    if len(listed) > MAX_MODS:
        raise NullBillError(f"a server takes at most {MAX_MODS} {WORD.get(server(model, name), 'mod')}s")
    entry["mods"] = listed
    return _tidy(out, name)


def drop(config: dict, model: dict, name: str, project: object) -> tuple[dict, list[str]]:
    """config.yaml without one plugin or mod, and the titles of all that
    left: with it goes what was only there because it needed it."""
    out, entry = _own(config, model, name)
    listed = [dict(mod) for mod in model["games"][name].get("mods") or []]
    mod = next((m for m in listed if m["project"] == project), None)
    if mod is None:
        raise NullBillError(f"{str(project)[:40]!r} is not in {model['games'][name]['title']}'s list")
    users = [m["title"] for m in listed if mod["project"] in m.get("needs", [])]
    if users:
        raise NullBillError(
            f"{', '.join(users)} need{'s' if len(users) == 1 else ''} {mod['title']}; remove {'it' if len(users) == 1 else 'them'} first"
        )
    gone = [mod]
    listed.remove(mod)
    while True:
        needed = {need for m in listed for need in m.get("needs", [])}
        spare = [m for m in listed if m.get("for_others") and m["project"] not in needed]
        if not spare:
            break
        gone += spare
        listed = [m for m in listed if m not in spare]
    entry["mods"] = listed
    return _tidy(out, name), [m["title"] for m in gone]


def view(model: dict, name: str) -> dict:
    """A server's kind and list as the page shows them."""
    game = model["games"][name]
    if not takes(model, name):
        return {"name": name, "takes": False}
    kind = server(model, name)
    return {
        "name": name,
        "takes": True,
        "title": game["title"],
        "minecraft": game["version"],
        "server": kind,
        "release": release(model, name),
        "servers": [{"id": k, "label": LABEL[k], "what": WHAT[k]} for k in KINDS],
        "word": WORD.get(kind),
        "most": MAX_MODS,
        "mods": [
            {
                "project": mod["project"],
                "title": mod["title"],
                "version": mod["version"],
                "file": mod["file"],
                "players_need": bool(mod.get("players_need")),
                "for_others": bool(mod.get("for_others")),
                "needs": list(mod.get("needs", [])),
                "link": f"{SITE}/project/{mod['project']}",
            }
            for mod in game.get("mods") or []
        ],
    }


def describe(entries: list[dict]) -> list[str]:
    """Entries a line each, for the agent's request and the page's answer."""
    out = []
    for entry in entries:
        line = f"{entry['title']} {entry['version']} ({entry['file']})"
        if entry.get("for_others"):
            line += ", which another needs"
        if entry.get("players_need"):
            line += "; every player needs it in their own game too"
        out.append(line)
    return out
