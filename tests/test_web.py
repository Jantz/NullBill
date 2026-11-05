"""The setup page's server, over real HTTPS on localhost."""

import http.client
import http.server
import json
import ssl
import stat
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest
import yaml
from fixturedata import fixture_config

from nullbill.web import server as web

FAKE_PROBE = """
import json, os, pathlib, sys
sys.path.insert(0, {tests!r})
from fixturedata import write_raw
from nullbill.probe import parse
home = pathlib.Path(os.environ["NULLBILL_HOME"])
if sys.argv[1] == "probe":
    run = home / "probe" / "run1"
    facts = parse(write_raw(run / "raw", "small"))
    (run / "facts.json").write_text(json.dumps(facts))
    (home / "probe" / "latest").symlink_to("run1")
    print("probe stored")
else:
    print("ran", " ".join(sys.argv[1:]))
    sys.exit(3 if "--stage" in sys.argv else 0)
"""


class MemoryCodec:
    def __init__(self):
        self.data = {}

    def load(self):
        return dict(self.data)

    def save(self, data):
        self.data = dict(data)


class Sent(list):
    """Stands in for the network: records each send, fails on demand."""

    fail = False

    def __call__(self, url, body="", headers=None):
        if self.fail:
            raise OSError("network unreachable")
        self.append((url, body, headers))


class Client:
    def __init__(self, port: int, cert):
        self.port = port
        self.tls = ssl.create_default_context(cafile=str(cert))
        self.tls.check_hostname = False
        self.cookie = None
        self.bearer = None

    def call(self, path, body=None, header=True):
        conn = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.tls, timeout=30)
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-NullBill"] = "1"
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        if body is None:
            conn.request("GET", path, headers=headers)
        else:
            conn.request("POST", path, body=json.dumps(body), headers=headers)
        response = conn.getresponse()
        data = response.read()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";")[0]
        self.headers = dict(response.getheaders())
        kind = response.getheader("Content-Type") or ""
        return response.status, json.loads(data) if kind.startswith("application/json") else data.decode()


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "install.json").write_text(json.dumps({"host": "192.0.2.10", "node": "pve", "vmid": 2002}))
    script = tmp_path / "fake_nullbill.py"
    script.write_text(FAKE_PROBE.format(tests=str(Path(__file__).parent)))
    codec = MemoryCodec()
    app = web.App(home, command=[sys.executable, str(script)], codec=lambda _: codec, send=Sent())
    app.ensure_setup_code()
    cert, key = web.ensure_certificate(home)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    yield app, Client(server.server_address[1], cert)
    server.shutdown()


def sign_in(app, client, password="correct horse battery"):
    code = app.code_file.read_text().strip()
    assert client.call("/api/login", {"secret": code.lower()})[0] == 200
    assert client.call("/api/password", {"password": password}) == (200, {"ok": True})


def wait(client, job_id):
    for _ in range(200):
        status, job = client.call(f"/api/jobs/{job_id}")
        assert status == 200
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_page_and_headers(site):
    _, client = site
    status, page = client.call("/")
    assert status == 200 and "<title>NullBill</title>" in page
    assert client.headers["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert client.headers["Cache-Control"] == "no-store"
    assert client.call("/app.js")[0] == 200


def test_setup_code_then_password(site):
    app, client = site
    assert stat.S_IMODE(app.code_file.stat().st_mode) == 0o600
    assert client.call("/api/state") == (200, {"auth": False, "has_password": False})
    assert client.call("/api/state")[1].get("install") is None
    assert client.call("/api/login", {"secret": "WRONG-CODE"})[0] == 401
    code = app.code_file.read_text().strip()
    assert client.call("/api/login", {"secret": code}, header=False)[0] == 403
    status, _ = client.call("/api/login", {"secret": code})
    assert status == 200
    assert "Secure" in client.headers["Set-Cookie"] and "HttpOnly" in client.headers["Set-Cookie"]
    assert "SameSite=Strict" in client.headers["Set-Cookie"]
    assert client.call("/api/password", {"password": "short"})[0] == 400
    assert client.call("/api/password", {"password": "correct horse battery"})[0] == 200
    assert not app.code_file.exists()
    stored = json.loads(app.owner_file.read_text())
    assert "correct horse" not in json.dumps(stored)
    other = Client(client.port, app.home / "web" / "cert.pem")
    assert other.call("/api/login", {"secret": code})[0] == 401
    assert other.call("/api/login", {"secret": "correct horse battery"})[0] == 200


def test_everything_but_state_and_login_needs_a_session(site):
    _, client = site
    assert client.call("/api/choices", {"owner_login": "a@example.internal"})[0] == 401
    assert client.call("/api/jobs", {"action": "probe"})[0] == 401
    assert client.call("/api/jobs/20260101T000000-probe")[0] == 401


def test_lockout_after_repeated_failures(site):
    app, client = site
    code = app.code_file.read_text().strip()
    for _ in range(web.LOCKOUT_FAILURES):
        assert client.call("/api/login", {"secret": "nope"})[0] == 401
    assert client.call("/api/login", {"secret": code})[0] == 401


def test_choices_write_config_and_probe_gives_a_plan(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/choices", {"workloads": ["agents"], "owner_login": "not an email"})[0] == 400
    choices = {"workloads": ["agents", "linux-ci", "bogus"], "backups": False, "owner_login": "owner-a@example.internal"}
    status, data = client.call("/api/choices", choices)
    assert status == 200 and data["plan"] == {"text": None, "errors": [], "tailnet_policy": None}
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    assert config["target"] == {"ssh": "root@192.0.2.10"}
    assert config["intent"] == {"workloads": ["agents", "linux-ci"]}
    assert config["people"] == {"owner": {"role": "owner", "login": "owner-a@example.internal"}}
    assert config["services"] == {"pbs": {"enabled": False}}
    status, job = client.call("/api/jobs", {"action": "probe"})
    assert status == 200 and job["args"] == ["probe"]
    done = wait(client, job["id"])
    assert done["state"] == "done" and "probe stored" in (app.home / "jobs" / f"{job['id']}.log").read_text()
    state = client.call("/api/state")[1]
    assert state["probed"] and state["plan"]["errors"] == []
    assert "class: small" in state["plan"]["text"] and "status: disabled" in state["plan"]["text"]
    # The policy to paste into Tailscale names the owner and the vpn guest's tag.
    policy = json.loads(state["plan"]["tailnet_policy"])
    assert policy["groups"] == {"group:nb-owner": ["owner-a@example.internal"]} and "tag:nb-vpn" in policy["tagOwners"]
    # Backups back on: the services key goes away again.
    client.call("/api/choices", {"workloads": ["agents", "linux-ci"], "backups": True, "owner_login": "owner-a@example.internal"})
    assert "services" not in yaml.safe_load((app.home / "config.yaml").read_text())
    assert fixture_config("small")["intent"] == yaml.safe_load((app.home / "config.yaml").read_text())["intent"]
    # The on-site screen: on, then off again leaves no host key behind.
    on = {"workloads": ["agents", "linux-ci"], "console": True, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", on)
    assert yaml.safe_load((app.home / "config.yaml").read_text())["host"] == {"console": {"enabled": True}}
    assert client.call("/api/state")[1]["choices"]["console"] is True
    assert "console:\n    enabled: true" in client.call("/api/state")[1]["plan"]["text"]
    client.call("/api/choices", {**on, "console": False})
    assert "host" not in yaml.safe_load((app.home / "config.yaml").read_text())


def test_jobs_report_failure_and_offsets(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/jobs", {"action": "rm -rf"})[0] == 400
    job = client.call("/api/jobs", {"action": "apply", "stage": "host"})[1]
    done = wait(client, job["id"])
    assert done["state"] == "failed" and done["rc"] == 3
    assert done["text"] == "ran apply --stage host\n"
    later = client.call(f"/api/jobs/{job['id']}?offset={done['offset']}")[1]
    assert later["text"] == ""


def test_one_job_at_a_time(site, tmp_path):
    app, client = site
    sign_in(app, client)
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5)\n")
    app.command = [sys.executable, str(slow)]
    job = client.call("/api/jobs", {"action": "verify"})[1]
    assert client.call("/api/jobs", {"action": "verify"}) == (400, {"error": "another job is still running"})
    app.jobs[job["id"]].process.kill()


def test_alerts_topic_is_made_with_the_password_and_tested(site):
    app, client = site
    sign_in(app, client)
    alerts = client.call("/api/state")[1]["alerts"]
    assert alerts["ntfy_url"].startswith("https://ntfy.sh/nullbill-") and len(alerts["ntfy_url"]) > 40
    assert alerts["healthchecks_url"] == ""
    bad = client.call("/api/alerts", {"healthchecks_url": "https://example.com/ping"})
    assert bad[0] == 400 and "hc-ping.com" in bad[1]["error"]
    ping = "https://hc-ping.com/0f4b3c9e-0000-4000-8000-000000000000"
    status, data = client.call("/api/alerts", {"healthchecks_url": ping})
    assert status == 200 and data["alerts"] == {"ntfy_url": alerts["ntfy_url"], "healthchecks_url": ping}
    assert client.call("/api/alerts/test", {}) == (200, {"ok": True, "sent": ["ntfy", "Healthchecks.io"]})
    (ntfy, body, headers), (hc, _, _) = app.send
    assert (ntfy, hc) == (alerts["ntfy_url"], ping)
    assert headers["Title"] == "NullBill: test alert" and "reach this device" in body
    # Clearing the URL stops the ping; the topic stays.
    client.call("/api/alerts", {"healthchecks_url": ""})
    assert client.call("/api/state")[1]["alerts"] == {"ntfy_url": alerts["ntfy_url"], "healthchecks_url": ""}


def test_agent_credential_is_kept_by_kind_and_never_shown(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["agent_credential"] is None
    bad = client.call("/api/agent", {"credential": "hunter2"})
    assert bad[0] == 400 and "claude setup-token" in bad[1]["error"]
    key = "sk-ant-api03-" + "a" * 40
    assert client.call("/api/agent", {"credential": f" {key}\n"}) == (200, {"ok": True, "agent_credential": "API key"})
    assert app.codec(app.home).load()["anthropic_api_key"] == key
    token = "sk-ant-oat01-" + "b" * 40
    assert client.call("/api/agent", {"credential": token}) == (200, {"ok": True, "agent_credential": "subscription token"})
    stored = app.codec(app.home).load()
    assert stored["claude_oauth_token"] == token and "anthropic_api_key" not in stored
    state = json.dumps(client.call("/api/state")[1])
    assert token not in state and '"agent_credential": "subscription token"' in state


def test_tailscale_key_waits_for_setup_and_is_never_shown(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["tailscale_key"] is False
    bad = client.call("/api/vpn", {"key": "tskey-api-" + "c" * 30})
    assert bad[0] == 400 and "tskey-auth-" in bad[1]["error"]
    key = "tskey-auth-kExample1CNTRL-" + "d" * 30
    assert client.call("/api/vpn", {"key": f" {key}\n"}) == (200, {"ok": True, "tailscale_key": True})
    assert app.codec(app.home).load()["tailscale_auth_key"] == key
    assert key not in json.dumps(client.call("/api/state")[1])


def test_a_failed_test_alert_says_so(site):
    app, client = site
    sign_in(app, client)
    app.send.fail = True
    status, data = client.call("/api/alerts/test", {})
    assert status == 400 and "network unreachable" in data["error"]


def test_alert_settings_wait_for_a_running_job(site, tmp_path):
    app, client = site
    sign_in(app, client)
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5)\n")
    app.command = [sys.executable, str(slow)]
    job = client.call("/api/jobs", {"action": "verify"})[1]
    ping = "https://hc-ping.com/0f4b3c9e-0000-4000-8000-000000000000"
    assert client.call("/api/alerts", {"healthchecks_url": ping}) == (400, {"error": "wait until the running job finishes"})
    app.jobs[job["id"]].process.kill()


def test_status_reads_prometheus_on_the_monitoring_guest(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/status") == (200, {"monitoring": "not planned"})
    (app.home / "plan.yaml").write_text("guests:\n  monitoring:\n    ip: 10.42.1.14\n")
    asked = []

    def fetch(url):
        asked.append(url)
        if url.endswith("/alerts"):
            alerts = [
                {"state": "firing", "labels": {"alertname": "Watchdog", "severity": "none"}},
                {
                    "state": "firing",
                    "labels": {"alertname": "RebootRequired", "severity": "warn"},
                    "annotations": {"summary": "pve needs a reboot"},
                },
                {"state": "pending", "labels": {"alertname": "DiskAlmostFull", "severity": "page"}},
                {
                    "state": "firing",
                    "labels": {"alertname": "TargetDown", "severity": "page"},
                    "annotations": {"summary": "forgejo is down"},
                },
            ]
            return {"data": {"alerts": alerts}}
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
        results = {
            "up": [{"metric": {"instance": "forgejo:9100"}, "value": [0, "0"]}, {"metric": {"instance": "pbs:9100"}, "value": [0, "1"]}],
            "node_reboot_required == 1": [{"metric": {"instance": "pve:9100"}, "value": [0, "1"]}],
        }
        return {"data": {"result": results.get(query, [])}}

    app.fetch = fetch
    status, data = client.call("/api/status")
    assert status == 200 and data == {
        "monitoring": "ok",
        "alerts": [
            {"name": "TargetDown", "severity": "page", "summary": "forgejo is down"},
            {"name": "RebootRequired", "severity": "warn", "summary": "pve needs a reboot"},
        ],
        "targets": 2,
        "down": ["forgejo:9100"],
        "security_waiting": [],
        "reboot_waiting": ["pve:9100"],
    }
    assert all(url.startswith("http://10.42.1.14:9090/api/v1/") for url in asked)

    def unreachable(url):
        raise OSError("no route to host")

    app.fetch = unreachable
    assert client.call("/api/status")[1] == {"monitoring": "unreachable", "error": "no route to host"}
    assert Client(client.port, app.home / "web" / "cert.pem").call("/api/status")[0] == 401


def test_closing_the_lan_needs_the_page_over_tailscale_and_then_shuts_the_lan_out(site, monkeypatch):
    from nullbill import lan

    app, client = site
    sign_in(app, client)
    vpn = {"address": "10.42.6.11"}
    monkeypatch.setattr(app, "network", lambda: ("10.42.0.0/16", vpn["address"]))
    state = client.call("/api/state")[1]["lan"]
    assert state == {"state": "open", "via_vpn": False, "minutes": 15}
    status, job = client.call("/api/jobs", {"action": "close-lan"})
    assert status == 200 and job["args"] == ["apply", "--close-lan"]
    assert wait(client, job["id"])["state"] == "done"
    # Keep it closed counts only from the vpn guest's address.
    lan.write(app.home, lan.CLOSING, 0)
    status, data = client.call("/api/jobs", {"action": "keep-lan-closed"})
    assert status == 400 and "opened over Tailscale" in data["error"]
    vpn["address"] = "127.0.0.1"  # the test client stands in for the vpn guest
    assert client.call("/api/state")[1]["lan"]["via_vpn"] is True
    status, job = client.call("/api/jobs", {"action": "keep-lan-closed"})
    assert status == 200 and job["args"] == ["apply", "--confirm-lan"]
    wait(client, job["id"])
    # Closed: a client outside the NullBill network gets nothing, not even the page.
    lan.write(app.home, lan.CLOSED)
    status, body = client.call("/")
    assert status == 403 and "open it over Tailscale" in body
    assert client.call("/api/login", {"secret": "x"})[0] == 403
    monkeypatch.setattr(app, "network", lambda: ("127.0.0.0/8", "127.0.0.1"))
    assert client.call("/api/state")[1]["lan"]["state"] == "closed"


def test_the_ci_images_are_baked_again_each_sunday_night(site, tmp_path):
    app, _ = site
    sunday = time.mktime((2026, 11, 8, 3, 15, 0, 0, 0, -1))
    # Nothing to bake before a first apply made a Linux pool.
    assert app.scheduled(sunday) is None
    (app.home / "tofu").mkdir()
    (app.home / "plan.yaml").write_text(yaml.safe_dump({"guests": {"linux-runner": {"kind": "vm-pool", "zone": "build"}}}))
    assert app.scheduled(sunday - 86400) is None
    assert app.scheduled(sunday - 3600) is None
    job = app.scheduled(sunday)
    assert job.action == "rebake-ci" and job.args == ["apply", "--stage", "ci"]
    job.process.wait()
    # Once a week, and not while another job runs.
    assert app.scheduled(sunday + 600) is None
    next_week = sunday + 7 * 86400
    command, slow = app.command, tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5)\n")
    app.command = [sys.executable, str(slow)]
    busy = app.start("verify")
    assert app.scheduled(next_week) is None
    busy.process.kill()
    busy.process.wait()
    app.command = command
    assert app.scheduled(next_week + 1800).args == ["apply", "--stage", "ci"]


def test_the_runtime_agent_may_run_verify_and_read_its_output_only(site):
    app, client = site
    sign_in(app, client)
    status, owner_job = client.call("/api/jobs", {"action": "probe"})
    wait(client, owner_job["id"])
    agent = Client(client.port, app.home / "web" / "cert.pem")
    agent.tls = client.tls
    # Nothing until apply has given the operator its token.
    agent.bearer = "agent-token"
    assert agent.call("/api/agent/jobs")[0] == 401
    (app.home / "web" / "agent-token.sha256").write_text(web.hashlib.sha256(b"agent-token").hexdigest() + "\n")
    # From anywhere but the operator, the token is refused.
    (app.home / "plan.yaml").write_text("guests:\n  operator:\n    ip: 10.42.1.16\n")
    assert agent.call("/api/agent/jobs")[0] == 401
    (app.home / "plan.yaml").write_text("guests:\n  operator:\n    ip: 127.0.0.1\n")
    agent.bearer = "wrong"
    assert agent.call("/api/agent/verify", {})[0] == 401
    agent.bearer = "agent-token"
    status, job = agent.call("/api/agent/verify", {})
    assert status == 200 and job["action"] == "verify" and job["args"] == ["verify"]
    for _ in range(200):
        status, seen = agent.call(f"/api/agent/jobs/{job['id']}")
        if seen["state"] != "running":
            break
        time.sleep(0.05)
    assert status == 200 and seen["rc"] == 0 and "ran verify" in seen["text"]
    listed = agent.call("/api/agent/jobs")[1]
    assert [j["action"] for j in listed] == ["verify", "probe"]
    # Other jobs' logs, the owner's API and other actions stay closed.
    assert agent.call(f"/api/agent/jobs/{owner_job['id']}")[0] == 404
    assert agent.call("/api/state")[1] == {"auth": False, "has_password": True}
    assert agent.call("/api/jobs", {"action": "apply"})[0] == 401
    assert agent.call("/api/agent/apply", {})[0] == 401


def test_a_job_outlives_a_restart_of_the_page_and_shows_again(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    slow = [sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[-1]) if sys.argv[-1][0].isdigit() else 0); print('ran')"]
    first = web.App(home, command=slow)
    done = first.start("verify")
    while done.state() == "running":
        time.sleep(0.05)
    running = first.start("apply", stage="2")
    # A new server, as after a restart: it finds both jobs from their files.
    again = web.App(home, command=slow)
    again.load_jobs()
    assert again.jobs[done.id].state() == "done" and again.jobs[done.id].args == ["verify"]
    assert again.jobs[running.id].state() == "running"
    with pytest.raises(web.NullBillError, match="still running"):
        again.start("verify")
    while again.jobs[running.id].state() == "running":
        time.sleep(0.05)
    assert again.jobs[running.id].rc == 0 and "ran" in again.job_view(again.jobs[running.id])["text"]
    running.process.wait()
    # A job whose process is gone without a recorded exit code shows as failed.
    (home / "jobs" / f"{done.id}.rc").unlink()
    lost = web.App(home, command=slow)
    lost.load_jobs()
    assert lost.jobs[done.id].state() == "failed"
