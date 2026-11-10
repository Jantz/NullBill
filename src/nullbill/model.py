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
    _power_defaults(model)
    _check("model", model, "catalog.yaml + config.yaml")
    _own_vm_ssh(model)
    _agent_ssh(model)
    _node_exporter(model)
    return model
