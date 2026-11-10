"""Load catalog.yaml and config.yaml, validate both, merge them."""

import copy
import functools
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from nullbill.errors import NullBillError


@functools.cache
def _parsed(name: str) -> Any:
    return yaml.safe_load(resources.files("nullbill.data").joinpath(name).read_text())


def _data(name: str) -> Any:
    """A data file, parsed once: the page builds the model several times
    for each request, and parsing the catalog took most of that time. A
    copy, since the merged model is changed after it is built."""
    return copy.deepcopy(_parsed(name))


@functools.cache
def _validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator({**_parsed("schema.yaml"), "$ref": f"#/$defs/{definition}"})


def shipped(section: str) -> frozenset[str]:
    """The names catalog.yaml itself has under a section, such as services:
    what config.yaml adds beside them is the owner's own (custom.py)."""
    return frozenset(_parsed("catalog.yaml")[section])


def _check(definition: str, doc: Any, source: str) -> None:
    errors = sorted(_validator(definition).iter_errors(doc), key=lambda e: list(e.absolute_path))
    if errors:
        raise NullBillError(*(f"{source}: {'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors))


def merge(base: Any, over: Any) -> Any:
    """Mappings merge key by key; any other value in `over` replaces `base`."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, value in over.items():
            out[key] = merge(base[key], value) if key in base else copy.deepcopy(value)
        return out
    return copy.deepcopy(over)


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise NullBillError(f"{path} not found")
    return yaml.safe_load(path.read_text()) or {}


# The operator reaches agent-managed guests by SSH, with a short-lived
# certificate that is valid on those guests only. Windows guests have no SSH.
AGENT_SSH = {"port": 22, "proto": "tcp", "from": ["service:operator"]}


def _agent_ssh(model: dict) -> None:
    if "operator" not in model["services"]:
        return
    for svc in model["services"].values():
        if svc.get("agent_managed") and svc["zone"] != "windows":
            svc["ports"] = [*svc.get("ports", []), copy.deepcopy(AGENT_SSH)]


# Monitoring scrapes node_exporter (update and disk metrics) on every Linux
# guest that lives on. Pool clones are short-lived; Windows has none.
NODE_EXPORTER = {"port": 9100, "proto": "tcp", "from": ["service:monitoring"]}


def node_exporter_guest(svc: dict) -> bool:
    return svc["kind"] != "vm-pool" and svc["zone"] != "windows"


def _node_exporter(model: dict) -> None:
    if "monitoring" not in model["services"]:
        return
    for svc in model["services"].values():
        if node_exporter_guest(svc):
            svc["ports"] = [*svc.get("ports", []), copy.deepcopy(NODE_EXPORTER)]


# apply reaches a Linux VM over SSH through the host (apply.connection).
# The catalog's VMs say so in their ports; one config.yaml adds gets it here.
VM_SSH = {"port": 22, "proto": "tcp", "from": ["host"]}


def _own_vm_ssh(model: dict) -> None:
    for name in model["services"].keys() - shipped("services"):
        svc = model["services"][name]
        reached = any(p["port"] == 22 and p["proto"] == "tcp" and "host" in p["from"] for p in svc.get("ports", []))
        if svc["kind"] == "vm" and svc["zone"] != "windows" and not reached:
            svc["ports"] = [*svc.get("ports", []), copy.deepcopy(VM_SSH)]


# One more server of a game the catalog has (docs/adr/0016-more-game-
# servers.md): what the game's own entry says, with this server's own
# title, settings and admins.
OWN_PER_SERVER = ("title", "settings", "admins")


def _more_games(model: dict, catalog: dict, config: dict) -> None:
    """Each games entry under a name of its own becomes a game server like
    the catalog's: a service, a workload and a rule for when it runs, all
    of that name, made from the game's. Its address (`id`) is its own; the
    port is the game's, since every guest has its own address."""
    problems = []
    for name, entry in model["games"].items():
        if name in catalog["games"]:
            continue
        kind = entry["game"]
        if name in catalog["services"] or name in (config.get("services") or {}):
            problems.append(f"config.yaml: games.{name}: a service has this name already; give the server another")
            continue
        if "eula" in entry:
            problems.append(f"config.yaml: games.{name}.eula: the agreement is given once, as games.{kind}.eula, for all its servers")
            continue
        own = {key: value for key, value in entry.items() if key not in ("game", "id")}
        try:
            _check(f"game_{kind}", own, f"config.yaml: games.{name}")
        except NullBillError as exc:
            problems.extend(exc.messages)
            continue
        base = model["games"][kind]
        title = entry.get("title") or f"{base['title']} ({name})"
        full = {key: copy.deepcopy(value) for key, value in base.items() if key not in OWN_PER_SERVER}
        full |= {"settings": {}} | ({"admins": []} if "admins" in base else {})
        model["games"][name] = merge(full, entry) | {"title": title}
        svc = copy.deepcopy(catalog["services"][kind])
        svc |= {"id": entry["id"], "workload": name, "description": f"{title}: one more {base['title']} server. Not agent-managed."}
        if "share" in svc:
            svc["share"]["title"] = title
        model["services"][name] = svc
        rule = catalog["power"].get(kind, {})
        model["power"][name] = {**rule, "title": title, **(config.get("power") or {}).get(name, {})}
    if problems:
        raise NullBillError(*problems)


# A workload config.yaml puts on demand without a rule of its own in the
# catalog's `power`: it is named as the workload and never stops by itself.
POWER_DEFAULTS = {"run": "always", "stop": "ask", "idle_minutes": 120}


def _power_defaults(model: dict) -> None:
    for name, entry in model.get("power", {}).items():
        if isinstance(entry, dict):
            model["power"][name] = {"title": name, **POWER_DEFAULTS, **entry}


def build_model(config: dict) -> dict:
    _check("config", config, "config.yaml")
    catalog = _data("catalog.yaml")
    _check("model", catalog, "catalog.yaml")
    model = merge(catalog, config)
    _more_games(model, catalog, config)
    _power_defaults(model)
    _check("model", model, "catalog.yaml + config.yaml")
    _own_vm_ssh(model)
    _agent_ssh(model)
    _node_exporter(model)
    return model
