"""Fixture helpers without pytest, for tools/apply-check/drive.py too."""

from pathlib import Path

import yaml

from nullbill.probe import COMMANDS

FIXTURES = Path(__file__).parent / "fixtures"
CLASSES = ["small", "mid", "large"]


def write_raw(raw: Path, fixture: str) -> Path:
    """Materialise a fixture as the raw directory probe.collect() would write."""
    spec = yaml.safe_load((FIXTURES / f"{fixture}.yaml").read_text())
    raw.mkdir(parents=True, exist_ok=True)
    for name in COMMANDS:
        entry = spec.get(name, {"rc": 1, "err": "not in fixture\n"})
        (raw / f"{name}.out").write_text(entry.get("out", ""))
        (raw / f"{name}.err").write_text(entry.get("err", ""))
        (raw / f"{name}.rc").write_text(f"{entry.get('rc', 0)}\n")
    return raw


def fixture_config(fixture: str) -> dict:
    return yaml.safe_load((FIXTURES / f"{fixture}.config.yaml").read_text()) or {}
