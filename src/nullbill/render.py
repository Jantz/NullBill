"""Emit nested dicts as YAML with a comment above any value wrapped in Note."""

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class Note:
    value: Any
    why: str


def _scalar(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, width=10_000).strip()
    return text.removesuffix("\n...").removesuffix("...").strip()


def _lines(node: dict, indent: int) -> list[str]:
    pad = " " * indent
    out = []
    for key, value in node.items():
        if isinstance(value, Note):
            out += [f"{pad}# {line}" for line in value.why.splitlines()]
            value = value.value
        if isinstance(value, dict) and value:
            out.append(f"{pad}{_scalar(key)}:")
            out += _lines(value, indent + 2)
        elif isinstance(value, list) and value and all(not isinstance(v, (dict, list)) for v in value):
            out.append(f"{pad}{_scalar(key)}:")
            out += [f"{pad}  - {_scalar(v)}" for v in value]
        else:
            out.append(f"{pad}{_scalar(key)}: {_scalar(value)}")
    return out


def render(doc: dict, header: str) -> str:
    lines = [f"# {line}" if line else "#" for line in header.splitlines()]
    return "\n".join(lines + _lines(doc, 0)) + "\n"


def strip_notes(node: Any) -> Any:
    if isinstance(node, Note):
        return strip_notes(node.value)
    if isinstance(node, dict):
        return {k: strip_notes(v) for k, v in node.items()}
    if isinstance(node, list):
        return [strip_notes(v) for v in node]
    return node
