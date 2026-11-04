"""Write everything apply would place on the host into one build directory."""

import shutil
from pathlib import Path

from nullbill.firewall import render_firewall


def render_all(plan: dict, model: dict) -> dict[str, str]:
    files = {f"firewall/{name}": text for name, text in render_firewall(plan, model).items()}
    return dict(sorted(files.items()))


def write_build(files: dict[str, str], build: Path) -> None:
    """Replace `build` with exactly these files, so stale output cannot linger."""
    if build.exists():
        shutil.rmtree(build)
    for rel, text in files.items():
        path = build / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text)
