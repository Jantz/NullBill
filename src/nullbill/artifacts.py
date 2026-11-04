"""Write everything apply would place on the host into one build directory."""

import shutil
from pathlib import Path

from nullbill.firewall import render_firewall
from nullbill.proxy import render_proxy
from nullbill.router import render_router
from nullbill.tailnet import render_tailnet
from nullbill.tofu import render_tofu

# build/<part>/ holds what apply places on that machine, at its path there.
PARTS = {"firewall": render_firewall, "router": render_router, "egress-proxy": render_proxy, "tailnet": render_tailnet, "tofu": render_tofu}


def render_all(plan: dict, model: dict) -> dict[str, str]:
    files = {f"{part}/{name}": text for part, render in PARTS.items() for name, text in render(plan, model).items()}
    return dict(sorted(files.items()))


def write_build(files: dict[str, str], build: Path) -> None:
    """Replace `build` with exactly these files, so stale output cannot linger."""
    if build.exists():
        shutil.rmtree(build)
    for rel, text in files.items():
        path = build / rel
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(text)
