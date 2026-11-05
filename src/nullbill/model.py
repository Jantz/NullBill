"""Load catalog.yaml and config.yaml, validate both, merge them."""

import copy
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from nullbill.errors import NullBillError


def _data(name: str) -> Any:
    return yaml.safe_load(resources.files("nullbill.data").joinpath(name).read_text())


def _validator(definition: str) -> Draft202012Validator:
    schema = _data("schema.yaml")
    return Draft202012Validator({**schema, "$ref": f"#/$defs/{definition}"})


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


def build_model(config: dict) -> dict:
    _check("config", config, "config.yaml")
    catalog = _data("catalog.yaml")
    _check("model", catalog, "catalog.yaml")
    model = merge(catalog, config)
    _check("model", model, "catalog.yaml + config.yaml")
    _agent_ssh(model)
    _node_exporter(model)
    return model
