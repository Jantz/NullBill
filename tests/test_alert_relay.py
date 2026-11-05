"""nb-alert-relay (ansible/roles/monitoring/files) against stand-ins for
ntfy and Healthchecks.io on localhost."""

import http.client
import importlib.machinery
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "ansible" / "roles" / "monitoring" / "files" / "nb-alert-relay"


def load(monkeypatch):
    # No __pycache__ next to the script the role copies.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader("nb_alert_relay", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def outside():
    """Records what reaches ntfy and Healthchecks.io; status 500 on /broken."""
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            got.append((self.path, {k: self.headers.get(k) for k in ("Title", "Priority", "Tags")}, body))
            self.send_response(500 if self.path == "/broken" else 200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = serve(Handler)
    yield f"http://127.0.0.1:{server.server_address[1]}", got
    server.shutdown()


@pytest.fixture
def relay(outside, monkeypatch):
    url, _ = outside
    module = load(monkeypatch)
    servers = []

    def start(ntfy="/topic", hc="/ping"):
        config = {"ntfy_url": url + ntfy if ntfy else "", "healthchecks_url": url + hc if hc else ""}
        server = serve(module.handler(config))
        servers.append(server)
        return server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()


def post(port, payload, path="/alertmanager"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=json.dumps(payload), headers={"Content-Type": "application/json"})
    return conn.getresponse().status


def alert(name, status="firing", severity="page", summary="something happened"):
    return {"status": status, "labels": {"alertname": name, "severity": severity}, "annotations": {"summary": summary}}


def test_watchdog_pings_healthchecks_and_nothing_else(relay, outside):
    _, got = outside
    port = relay()
    assert post(port, {"alerts": [alert("Watchdog", severity="none")]}) == 200
    assert post(port, {"alerts": [alert("Watchdog", status="resolved", severity="none")]}) == 200
    assert [path for path, _, _ in got] == ["/ping"]


def test_alerts_reach_ntfy_with_priority_by_severity(relay, outside):
    _, got = outside
    port = relay()
    payload = {"alerts": [alert("SecurityUpdatesPending", summary="forgejo waits"), alert("RebootRequired", severity="warn")]}
    assert post(port, payload) == 200
    assert post(port, {"alerts": [alert("SecurityUpdatesPending", status="resolved", summary="forgejo waits")]}) == 200
    assert got == [
        ("/topic", {"Title": "NullBill: SecurityUpdatesPending", "Priority": "high", "Tags": "warning"}, "forgejo waits"),
        ("/topic", {"Title": "NullBill: RebootRequired", "Priority": "default", "Tags": "warning"}, "something happened"),
        (
            "/topic",
            {"Title": "NullBill: SecurityUpdatesPending resolved", "Priority": "low", "Tags": "white_check_mark"},
            "Resolved: forgejo waits",
        ),
    ]


def test_a_failed_send_answers_502_so_alertmanager_retries(relay):
    assert post(relay(ntfy="/broken"), {"alerts": [alert("DiskAlmostFull")]}) == 502


def test_unset_channels_stay_silent_and_other_paths_are_refused(relay, outside):
    _, got = outside
    port = relay(ntfy="", hc="")
    assert post(port, {"alerts": [alert("Watchdog"), alert("DiskAlmostFull")]}) == 200
    assert got == []
    assert post(port, {}, path="/other") == 404
