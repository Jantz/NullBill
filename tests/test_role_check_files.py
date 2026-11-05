"""Role checks place the small class's rendered files; keep their copies
in ansible/roles/<role>/tests/files equal to what render_all makes. Set
UPDATE_GOLDEN=1 to rewrite them and review the diff."""

import os
from pathlib import Path

import pytest
from fixturedata import fixture_config

from nullbill.artifacts import render_all
from nullbill.model import build_model
from nullbill.plan import build_plan

ROLES = Path(__file__).parent.parent / "ansible" / "roles"


@pytest.mark.parametrize(("role", "machine"), [("router", "router"), ("egress_proxy", "egress-proxy")])
def test_role_check_files_match_render(facts, role, machine):
    files = ROLES / role / "tests" / "files"
    model = build_model(fixture_config("small"))
    rendered = render_all(build_plan(model, facts("small")), model)
    prefix = f"{machine}/"
    want = {name.removeprefix(prefix): text for name, text in rendered.items() if name.startswith(prefix)}
    if os.environ.get("UPDATE_GOLDEN"):
        for name, text in want.items():
            (files / name).parent.mkdir(parents=True, exist_ok=True)
            (files / name).write_text(text)
    have = {str(p.relative_to(files)): p.read_text() for p in sorted(files.rglob("*")) if p.is_file()}
    assert have == want
