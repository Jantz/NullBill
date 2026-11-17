"""tools/page-demo: the page against stand-ins, which nothing else runs.
A state that no longer comes up would only show the next time someone
wants to look at the page."""

import importlib.util
import sys
from pathlib import Path

import pytest

SERVE = Path(__file__).parents[1] / "tools" / "page-demo" / "serve.py"


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("page_demo_serve", SERVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_built_server_has_nothing_waiting_and_every_page_its_state(demo, tmp_path, monkeypatch):
    monkeypatch.setenv("NULLBILL_HOME", "")
    app = demo.build(tmp_path, "healthy")
    state = app.state(True)
    assert state["built"] and state["review"]["reasons"] == [] and state["job"]["state"] == "done"
    # The run was a real one against the stand-in host: it left its records.
    assert state["review"]["machines"]["known"] and state["checks"]["counts"]["FAIL"] == 0
    assert state["backups"]["built"] and not state["backups"]["pending"]
    assert app.status()["monitoring"] == "ok" and app.on_demand()["groups"]


def test_a_failed_first_run_stops_in_its_stage_with_the_programs_words(demo, tmp_path, monkeypatch):
    monkeypatch.setenv("NULLBILL_HOME", "")
    app = demo.build(tmp_path, "failed")
    state = app.state(True)
    assert not state["built"] and state["job"]["state"] == "failed"
    error = state["last_jobs"]["apply"]["error"]
    assert error["stage_text"] == "Set up Forgejo (Git and CI)" and "Install the packages Forgejo needs" in error["message"]


def test_every_state_is_listed_in_the_readme(demo):
    readme = (SERVE.parent / "README.md").read_text()
    assert all(f"| `{state}` |" in readme for state in demo.STATES)
