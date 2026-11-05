"""The router role's check places the small class's rendered router files;
keep its copy in ansible/roles/router/tests/files equal to what render_all
makes. Set UPDATE_GOLDEN=1 to rewrite it and review the diff."""

import os
from pathlib import Path

from fixturedata import fixture_config

from nullbill.artifacts import render_all
from nullbill.model import build_model
from nullbill.plan import build_plan

FILES = Path(__file__).parent.parent / "ansible" / "roles" / "router" / "tests" / "files"


def test_router_check_files_match_render(facts):
    model = build_model(fixture_config("small"))
    rendered = render_all(build_plan(model, facts("small")), model)
    want = {name.removeprefix("router/"): text for name, text in rendered.items() if name.startswith("router/")}
    if os.environ.get("UPDATE_GOLDEN"):
        for name, text in want.items():
            (FILES / name).parent.mkdir(parents=True, exist_ok=True)
            (FILES / name).write_text(text)
    have = {str(p.relative_to(FILES)): p.read_text() for p in sorted(FILES.rglob("*")) if p.is_file()}
    assert have == want
