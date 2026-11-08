"""The setup page's server, over real HTTPS on localhost."""

import http.client
import http.server
import json
import os
import ssl
import stat
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import pytest
import yaml
from fixturedata import fixture_config, write_raw

from nullbill.probe import parse
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
    assert status == 200 and data["plan"] == {"text": None, "errors": [], "advice": [], "summary": None, "tailnet_policy": None}
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
    client.call("/api/choices", {"workloads": ["agents", "linux-ci", "media"], "backups": True, "owner_login": "owner-a@example.internal"})
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
    # It said nothing of why, and the page says so, at the first stage.
    assert done["progress"]["steps"][0]["status"] == "failed"
    assert done["progress"]["error"]["message"].startswith("It ended without saying why")
    assert client.call("/api/state")[1]["last_jobs"]["apply"]["state"] == "failed"
    later = client.call(f"/api/jobs/{job['id']}?offset={done['offset']}")[1]
    assert later["text"] == ""


def test_choices_saved_after_the_last_good_setup_run_are_waiting(site):
    app, client = site
    sign_in(app, client)
    choices = {"workloads": ["agents"], "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", choices)
    assert client.call("/api/state")[1]["config_changed"] is False  # nothing ran yet
    job = client.call("/api/jobs", {"action": "apply"})[1]
    assert wait(client, job["id"])["state"] == "done"
    config = app.home / "config.yaml"
    started = app.jobs[job["id"]].started
    os.utime(config, (started - 60, started - 60))
    assert client.call("/api/state")[1]["config_changed"] is False
    os.utime(config, (started + 60, started + 60))
    assert client.call("/api/state")[1]["config_changed"] is True


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
    odd = client.call("/api/agent", {"credential": "sk-ant-oat01-abc\u2026def"})
    assert odd[0] == 400 and odd[1]["error"].startswith("the pasted subscription token has HORIZONTAL ELLIPSIS at character 17 of 20")
    assert "abc" not in odd[1]["error"]
    key = "sk-ant-api03-" + "a" * 40
    assert client.call("/api/agent", {"credential": f" {key}\n"}) == (200, {"ok": True, "agent_credential": "API key"})
    assert app.codec(app.home).load()["anthropic_api_key"] == key
    token = "sk-ant-oat01-" + "b" * 40
    wrapped = f"{token[:30]}\n  {token[30:]} "
    assert client.call("/api/agent", {"credential": wrapped}) == (200, {"ok": True, "agent_credential": "subscription token"})
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


def test_tailscale_oauth_client_is_kept_and_never_shown(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["tailscale_client"] is False
    bad = client.call("/api/tailscale-client", {"client_id": "kExampleCNTRL", "secret": "tskey-auth-" + "e" * 30})
    assert bad[0] == 400 and "tskey-client-" in bad[1]["error"]
    secret = "tskey-client-kExampleCNTRL-" + "f" * 30
    answer = client.call("/api/tailscale-client", {"client_id": " kExampleCNTRL ", "secret": f"{secret}\n"})
    assert answer == (200, {"ok": True, "tailscale_client": True})
    stored = app.codec(app.home).load()
    assert (stored["tailscale_client_id"], stored["tailscale_client_secret"]) == ("kExampleCNTRL", secret)
    assert secret not in json.dumps(client.call("/api/state")[1])


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
        "answering": ["pbs:9100"],
        "security_waiting": [],
        "reboot_waiting": ["pve:9100"],
    }
    assert all(url.startswith("http://10.42.1.14:9090/api/v1/") for url in asked)

    def unreachable(url):
        raise OSError("no route to host")

    app.fetch = unreachable
    assert client.call("/api/status")[1] == {"monitoring": "unreachable", "error": "no route to host"}
    assert Client(client.port, app.home / "web" / "cert.pem").call("/api/status")[0] == 401


def test_services_list_what_the_plan_runs_and_reveal_only_their_passwords(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["services"] == {"items": [], "background": []}
    golden = Path(__file__).parent / "golden" / "small.plan.yaml"
    (app.home / "plan.yaml").write_text(golden.read_text())
    services = client.call("/api/state")[1]["services"]
    items = {s["name"]: s for s in services["items"]}
    assert list(items) == ["proxmox", "grafana", "forgejo", "jellyfin", "workspace", "operator", "linux-runner"]
    assert items["proxmox"]["url"] == "https://10.42.1.254:8006" and items["proxmox"]["watch"] == "pve"
    assert items["grafana"]["url"] == "http://10.42.1.14:3000"
    assert (items["grafana"]["user"], items["grafana"]["password"]) == ("admin", "grafana")
    assert (items["forgejo"]["url"], items["forgejo"]["user"]) == ("https://10.42.2.17/", "nullbill-admin")
    assert items["workspace"]["addresses"] == ["ssh root@10.42.3.19"] and "pct enter 2019" in items["workspace"]["how"]
    assert "pct enter 2016" in items["operator"]["how"]
    jellyfin = items["jellyfin"]
    assert (jellyfin["url"], jellyfin["user"], jellyfin["password"]) == ("http://10.42.7.24:8096/", "nullbill-admin", "jellyfin")
    assert jellyfin["watch"] == "jellyfin" and "Keep a disk" in jellyfin["how"]
    background = ["router", "internet proxy", "certificate authority", "PostgreSQL", "CI dispatcher", "Tailscale entry", "backup server"]
    assert services["background"] == background
    legacy_plan = yaml.safe_load((golden.parent / "large-legacy.plan.yaml").read_text())
    (app.home / "plan.yaml").write_text(yaml.safe_dump(legacy_plan))
    files = next(s for s in client.call("/api/state")[1]["services"]["items"] if s["name"] == "files")
    assert files["addresses"] == ["\\\\10.42.1.23\\data"] and files["password"] == "files"
    jellyfin = next(s for s in client.call("/api/state")[1]["services"]["items"] if s["name"] == "jellyfin")
    assert "a folder under /media/data" in jellyfin["how"]
    secrets = app.codec(app.home).data
    secrets |= {"grafana_admin_password": "grafana-pw", "forgejo_admin_password": "forgejo-pw", "page_password": "never"}
    secrets["jellyfin_admin_password"] = "jellyfin-pw"
    assert client.call("/api/password-reveal", {"name": "jellyfin"}) == (200, {"password": "jellyfin-pw"})
    assert client.call("/api/password-reveal", {"name": "grafana"}) == (200, {"password": "grafana-pw"})
    assert client.call("/api/password-reveal", {"name": "forgejo"}) == (200, {"password": "forgejo-pw"})
    for name in ("page_password", "grafana_admin_password", "tailscale_client_secret", ""):
        status, data = client.call("/api/password-reveal", {"name": name})
        assert status == 400 and "no password" in data["error"]
    anonymous = Client(client.port, app.home / "web" / "cert.pem")
    assert anonymous.call("/api/password-reveal", {"name": "grafana"})[0] == 401


def test_deleting_guests_runs_only_for_what_setup_stopped_before_deleting(site):
    """Delete and run setup: only while apply waits on the owner's word."""
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["removals"] == []
    status, data = client.call("/api/jobs", {"action": "remove-guests"})
    assert status == 400 and "not waiting to delete anything" in data["error"]
    (app.home / "removals.json").write_text('{"guests": ["windows-dev"]}\n')
    assert client.call("/api/state")[1]["removals"] == ["windows-dev"]
    status, job = client.call("/api/jobs", {"action": "remove-guests"})
    assert status == 200 and job["args"] == ["apply", "--remove-guests"]
    wait(client, job["id"])


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
    # Setup and updates wait for the owner's switch.
    assert agent.call("/api/agent/apply", {})[0] == 403
    assert agent.call("/api/agent/upgrade", {})[0] == 403


def test_a_remote_agent_gets_the_same_verify_only_api_through_the_vpn_guest(site, capfd):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/remote-agents", {"name": "laptop"})[0] == 400  # no vpn guest planned
    # The test client stands in for the vpn guest, through which the tailnet comes.
    (app.home / "plan.yaml").write_text(
        "guests:\n  vpn:\n    ip: 127.0.0.1\n  nullbill:\n    ip: 10.42.1.2\n  operator:\n    ip: 10.42.1.16\n"
    )
    assert client.call("/api/remote-agents", {"name": "Laptop!"})[0] == 400
    status, made = client.call("/api/remote-agents", {"name": "laptop"})
    assert status == 200 and made["url"] == "https://10.42.1.2:8443/" and len(made["spki"]) == 44
    assert client.call("/api/remote-agents", {"name": "laptop"})[0] == 400
    stored = (app.home / "web" / "remote-agents.json").read_text()
    assert made["token"] not in stored and made["token"] not in json.dumps(client.call("/api/state")[1])
    assert client.call("/api/state")[1]["remote_agents"] == ["laptop"]
    assert (app.home / "web" / "remote-agents.json").stat().st_mode & 0o777 == 0o600

    agent = Client(client.port, app.home / "web" / "cert.pem")
    agent.tls = client.tls
    agent.bearer = made["token"]
    status, job = agent.call("/api/agent/verify", {})
    assert status == 200 and job["args"] == ["verify"]
    while agent.call(f"/api/agent/jobs/{job['id']}")[1]["state"] == "running":
        time.sleep(0.05)
    assert json.loads((app.home / "jobs" / f"{job['id']}.json").read_text())["by"] == "remote:laptop"
    # Verify only, as for the operator.
    assert agent.call("/api/jobs", {"action": "apply"})[0] == 401
    assert agent.call("/api/agent/apply", {})[0] == 403
    assert "nullbill-web: agent remote:laptop from 127.0.0.1: POST /api/agent/verify" in capfd.readouterr().err
    # The operator's token does not work from the vpn guest, nor a remote one from elsewhere.
    (app.home / "web" / "agent-token.sha256").write_text(web.hashlib.sha256(b"agent-token").hexdigest() + "\n")
    agent.bearer = "agent-token"
    assert agent.call("/api/agent/jobs")[0] == 401
    agent.bearer = made["token"]
    (app.home / "plan.yaml").write_text("guests:\n  vpn:\n    ip: 10.42.6.11\n  nullbill:\n    ip: 10.42.1.2\n")
    assert agent.call("/api/agent/jobs")[0] == 401
    (app.home / "plan.yaml").write_text("guests:\n  vpn:\n    ip: 127.0.0.1\n  nullbill:\n    ip: 10.42.1.2\n")
    assert agent.call("/api/agent/jobs")[0] == 200
    # Revoked: refused at once.
    assert client.call("/api/remote-agents/revoke", {"name": "laptop"}) == (200, {"ok": True, "remote_agents": []})
    assert agent.call("/api/agent/jobs")[0] == 401
    assert "agent refused from 127.0.0.1: GET /api/agent/jobs" in capfd.readouterr().err


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


def probed(app, cls="large"):
    from fixturedata import write_raw

    from nullbill.probe import parse

    run = app.home / "probe" / "run1"
    (run / "facts.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "facts.json").write_text(json.dumps(parse(write_raw(run / "raw", cls))))
    (app.home / "probe" / "latest").symlink_to("run1")


OWNER = {"workloads": ["agents", "linux-ci"], "owner_login": "owner-a@example.internal"}
OLD = "ata-SYNTHETIC_HDD_0002"


def pins(app):
    return yaml.safe_load((app.home / "config.yaml").read_text()).get("disks")


def test_the_other_disks_are_listed_with_their_partitions_and_health(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    disks = {d["id"]: d for d in client.call("/api/state")[1]["disks"]}
    assert "nvme-SYNTHETIC_NVME_0001" not in disks
    old = disks[OLD]
    assert old["choice"] == "alone" and old["health"] == "PASSED" and old["power_on_hours"] == 41000 and old["confirm"] == "D_0002"
    assert [(p["n"], p["what"], p["label"], p["keepable"]) for p in old["partitions"]] == [
        (1, "partition 1 (Microsoft reserved)", None, False),
        (2, "partition 2 (ntfs)", "Data", True),
    ]


def test_keeping_a_disk_plans_the_share_and_shows_where_it_is(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep"}}})
    assert status == 200 and data["plan"]["errors"] == [] and pins(app) == {OLD: {"use": "legacy"}}
    state = client.call("/api/state")[1]
    assert {d["id"]: d["choice"] for d in state["disks"]}[OLD] == "keep"
    assert state["files"] == {"ip": "10.42.1.23", "shares": ["data"], "user": "nbshare", "password_set": False}
    files = {"name": "files"}
    assert client.call("/api/password-reveal", files) == (400, {"error": "run setup first; it makes the password"})
    app.codec(app.home).data["files_share_password"] = "a-long-share-password"
    assert client.call("/api/password-reveal", files) == (200, {"password": "a-long-share-password"})
    # Read-write, then back; keys the page does not show stay.
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep", "rw": True}}})
    assert pins(app) == {OLD: {"use": "legacy", "mode": "rw"}}
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    config["disks"][OLD]["name"] = "old"
    (app.home / "config.yaml").write_text(yaml.safe_dump(config))
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep", "rw": True}}})
    assert pins(app) == {OLD: {"use": "legacy", "mode": "rw", "name": "old"}}
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep", "rw": False}}})
    assert pins(app) == {OLD: {"use": "legacy", "name": "old"}}
    # Saving the other choices alone leaves the disks as they are.
    client.call("/api/choices", OWNER)
    assert pins(app) == {OLD: {"use": "legacy", "name": "old"}}
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "alone"}}})
    assert pins(app) is None and client.call("/api/state")[1]["files"] is None


def test_erasing_a_disk_needs_the_end_of_its_name(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase"}}})
    assert status == 400 and "type D_0002, the end of its name" in data["error"]
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "confirm": "0002"}}})[0] == 400
    assert not (app.home / "config.yaml").exists()
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "confirm": "D_0002"}}})
    assert status == 200 and pins(app) == {OLD: {"use": "pbs", "wipe": True}}
    # Chosen once, it stays chosen without typing the name again.
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase"}}})[0] == 200
    assert client.call("/api/choices", {**OWNER, "disks": {"ata-NOT_THERE": {"choice": "keep"}}})[0] == 400
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "format"}}})[0] == 400
    assert pins(app) == {OLD: {"use": "pbs", "wipe": True}}


def kept_on_server(app, used_bytes=100 * 2**30, storage_type="lvmthin"):
    """The old disk as apply leaves it: partition 2 mounted as data."""
    facts = app.home / "probe" / "latest" / "facts.json"
    data = json.loads(facts.read_text())
    disk = next(d for d in data["disks"] if d["id"] == OLD)
    disk["partitions"][1]["mounts"] = ["/mnt/nb-legacy/data"]
    disk["partitions"][1]["used_bytes"] = used_bytes
    for st in data["storage"]:
        if st["name"] == "local-lvm":
            st["type"] = storage_type
    facts.write_text(json.dumps(data))


def test_a_kept_disk_is_copied_or_given_up_before_it_is_erased(site):
    """Safety rule 2 on the page: erasing a kept disk says how its files are kept."""
    app, client = site
    sign_in(app, client)
    probed(app)
    kept_on_server(app)
    old = {d["id"]: d for d in client.call("/api/state")[1]["disks"]}[OLD]
    assert old["kept"] and old["used_gib"] == 100 and old["copy_to"] == "local-lvm"
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "copy"}}})
    assert status == 400 and "type D_0002" in data["error"]
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "copy", "confirm": "D_0002"}}})
    assert status == 200 and data["plan"]["errors"] == []
    assert pins(app) == {OLD: {"use": "pbs", "wipe": True, "evacuate": "local-lvm"}}
    state = client.call("/api/state")[1]
    assert {d["id"]: d["choice"] for d in state["disks"]}[OLD] == "copy"
    assert state["files"]["shares"] == ["data"]
    # Erasing it instead: the owner's word that the files are elsewhere.
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "confirm": "D_0002"}}})
    assert status == 200 and data["plan"]["errors"] == []
    assert pins(app) == {OLD: {"use": "pbs", "wipe": True, "discard_data": True}}


def test_an_erased_disk_becomes_backups_or_storage(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "for": "storage", "confirm": "D_0002"}}})
    assert status == 200 and data["plan"]["errors"] == [] and pins(app) == {OLD: {"use": "storage", "wipe": True}}
    assert {d["id"]: d for d in client.call("/api/state")[1]["disks"]}[OLD]["for"] == "storage"
    # A storage disk made the backup disk loses what it holds: the name again.
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "for": "backups"}}})
    assert status == 400 and "type D_0002" in data["error"]
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "for": "storage"}}})[0] == 200
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "erase", "for": "nas"}}})[0] == 400
    # A kept disk copied, then storage: the copy moves onto it.
    kept_on_server(app)
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "copy", "for": "storage", "confirm": "D_0002"}}})
    assert status == 200 and data["plan"]["errors"] == []
    assert pins(app) == {OLD: {"use": "storage", "wipe": True, "evacuate": "local-lvm"}}


def test_a_disk_not_kept_cannot_be_copied(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "copy", "confirm": "D_0002"}}})
    assert status == 400 and "only a disk kept and mounted on the server" in data["error"]
    kept_on_server(app, storage_type="zfspool")
    assert {d["id"]: d for d in client.call("/api/state")[1]["disks"]}[OLD]["copy_to"] is None
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "copy", "confirm": "D_0002"}}})
    assert status == 400 and "not LVM or LVM-thin" in data["error"]
    assert not (app.home / "config.yaml").exists()


def test_the_owner_gives_the_gpu_memory_the_probe_could_not_read(site):
    """A fresh Proxmox host has no NVIDIA driver, so the probe reads no VRAM;
    the page asks for it in GB and pins it for that GPU only."""
    app, client = site
    sign_in(app, client)
    run = app.home / "probe" / "run1"
    facts = parse(write_raw(run / "raw", "mid"))
    facts["gpus"][0]["vram_mib"] = None
    (run / "facts.json").write_text(json.dumps(facts))
    (app.home / "probe" / "latest").symlink_to("run1")
    gpus = client.call("/api/state")[1]["gpus"]
    assert [(g["pci"], g["probed_mib"], g["vram_mib"]) for g in gpus] == [("0000:01:00.0", None, None)]
    choices = {"workloads": ["agents", "inference"], "backups": False, "owner_login": "owner-a@example.internal"}
    bad = client.call("/api/choices", {**choices, "gpus": {"0000:01:00.0": "twelve"}})
    assert bad[0] == 400 and "number of GB" in bad[1]["error"]
    assert client.call("/api/choices", {**choices, "gpus": {"0000:01:00.0": "12", "0000:09:00.0": "8"}})[0] == 200
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    assert config["gpus"] == {"0000:01:00.0": {"vram_mib": 12288}}
    state = client.call("/api/state")[1]
    assert state["gpus"][0]["vram_mib"] == 12288
    # The model gateway's card: the planned model, where it answers, its key once setup made one.
    name = "model-gateway.nullbill.internal"
    want = {"url": f"https://{name}:4000/v1", "name": name, "ip": "10.42.4.21", "model": "qwen3.5-9b", "key_set": False}
    assert state["gateway"] == want
    gateway = {"name": "model-gateway"}
    assert client.call("/api/password-reveal", gateway) == (400, {"error": "run setup first; it makes the password"})
    app.codec(app.home).data["model_gateway_key"] = "sk-gateway-key"
    assert client.call("/api/password-reveal", gateway) == (200, {"password": "sk-gateway-key"})
    item = next(s for s in state["services"]["items"] if s["name"] == "model-gateway")
    assert item["addresses"] == [want["url"], f"Hosts line for your device: 10.42.4.21 {name}"]
    assert (item["password"], item["watch"]) == ("model-gateway", "model-gateway") and "qwen3.5-9b" in item["what"]
    # Cleared again, the pin goes.
    client.call("/api/choices", {**choices, "gpus": {"0000:01:00.0": ""}})
    assert "gpus" not in yaml.safe_load((app.home / "config.yaml").read_text())


AGENT_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIagent"


def test_the_owners_switch_gives_the_agent_root_on_the_host_and_setup_through_the_page(site):
    app, client = site
    sign_in(app, client)
    host = []
    app.host_run = lambda target, command: host.append((target, command)) or (0, "", "")
    agent = Client(client.port, app.home / "web" / "cert.pem")
    agent.tls, agent.bearer = client.tls, "agent-token"
    (app.home / "web" / "agent-token.sha256").write_text(web.hashlib.sha256(b"agent-token").hexdigest() + "\n")
    (app.home / "config.yaml").write_text("target:\n  ssh: root@192.0.2.10\n")
    (app.home / "plan.yaml").write_text("zones:\n  mgmt:\n    host_ip: 10.42.1.254\nguests:\n  operator:\n    ip: 127.0.0.1\n")
    # No key until setup made the operator.
    assert client.call("/api/agent-powers", {"enabled": True})[0] == 400 and host == []
    (app.home / web.hostroot.PUBLIC_KEY).write_text(AGENT_KEY + " nullbill-agent-root\n")
    assert client.call("/api/agent-powers", {"enabled": "yes"})[0] == 400
    assert client.call("/api/agent-powers", {"enabled": True}) == (200, {"ok": True, "agent_powers": True})
    assert host == [("root@192.0.2.10", web.hostroot.command(web.hostroot.line(AGENT_KEY, "127.0.0.1")))]
    assert client.call("/api/state")[1]["agent_powers"] is True

    # The agent runs one stage; the owner hears of it before it starts, and how it ended.
    assert agent.call("/api/agent/apply", {"stage": "nope"})[0] == 400
    status, job = agent.call("/api/agent/apply", {"stage": "ci"})
    assert status == 200 and job["args"] == ["apply", "--stage", "ci"]
    url, body, headers = app.send[-1]
    assert url.startswith(web.NTFY) and body == "The agent started: Set up the server (stage ci)" and headers["Tags"] == "robot"
    seen = wait(client, job["id"])
    assert agent.call(f"/api/agent/jobs/{job['id']}")[1]["text"] == seen["text"] == "ran apply --stage ci\n"
    app.tell_agent_jobs()
    app.tell_agent_jobs()
    assert [b for _, b, _ in app.send[1:]] == ["Set up the server, started by the agent, failed (exit 3)."]
    assert json.loads((app.home / "jobs" / f"{job['id']}.json").read_text())["by"] == "operator"
    # It reads the owner's jobs too while the switch is on.
    owner = client.call("/api/jobs", {"action": "probe"})[1]
    wait(client, owner["id"])
    assert agent.call(f"/api/agent/jobs/{owner['id']}")[0] == 200
    # When the owner cannot be told, nothing starts.
    app.send.fail = True
    assert agent.call("/api/agent/upgrade", {})[0] == 409
    app.send.fail = False
    status, job = agent.call("/api/agent/upgrade", {})
    assert status == 200 and job["args"] == ["upgrade"]
    wait(client, job["id"])

    # Off: the key comes out of the host first, and the API closes again.
    assert client.call("/api/agent-powers", {"enabled": False}) == (200, {"ok": True, "agent_powers": False})
    assert host[-1] == ("root@192.0.2.10", web.hostroot.command(None))
    assert agent.call("/api/agent/apply", {})[0] == 403
    assert agent.call(f"/api/agent/jobs/{owner['id']}")[0] == 404
    # Off even when the host cannot be reached, and the owner is told what to do.
    app.host_run = lambda target, command: (255, "", "ssh: connect to host 192.0.2.10 port 22: No route to host")
    assert client.call("/api/agent-powers", {"enabled": True})[0] == 400
    assert not app.agent_powers()
    (app.home / web.hostroot.SWITCH).write_text('{"enabled": true}\n')
    status, data = client.call("/api/agent-powers", {"enabled": False})
    assert status == 400 and "stop the operator container" in data["error"] and not app.agent_powers()


def test_a_remote_agent_never_gets_setup_even_with_the_switch_on(site):
    app, client = site
    sign_in(app, client)
    (app.home / "plan.yaml").write_text("guests:\n  vpn:\n    ip: 127.0.0.1\n  nullbill:\n    ip: 10.42.1.2\n")
    made = client.call("/api/remote-agents", {"name": "laptop"})[1]
    (app.home / web.hostroot.SWITCH).write_text('{"enabled": true}\n')
    agent = Client(client.port, app.home / "web" / "cert.pem")
    agent.tls, agent.bearer = client.tls, made["token"]
    assert agent.call("/api/agent/apply", {})[0] == 403
    assert agent.call("/api/agent/upgrade", {})[0] == 403


def test_the_agents_github_token_is_checked_with_github_and_never_shown(site):
    app, client = site
    sign_in(app, client)
    asked = []

    def github(url, token):
        asked.append((url, token))
        if token.endswith("bad"):
            raise OSError("HTTP Error 401: Unauthorized")
        if url.endswith("/user"):
            return {"login": "owner-a", "id": 1000}
        return {"permissions": {"push": not token.endswith("read")}}

    app.github = github
    good = "github_pat_" + "A" * 40
    assert client.call("/api/agent-github", {"token": "hunter2"})[0] == 400
    assert client.call("/api/agent-github", {"token": good[:-3] + "bad"})[0] == 400
    status, data = client.call("/api/agent-github", {"token": good[:-4] + "read"})
    assert status == 400 and "cannot push" in data["error"]
    assert client.call("/api/agent-github", {"token": f" {good}\n"}) == (200, {"ok": True, "agent_github": "owner-a"})
    assert asked[-1] == (web.GITHUB_API + "repos/" + web.GITHUB_REPO, good)
    stored = app.codec(app.home).data
    assert (stored["agent_github_token"], stored["agent_github_id"]) == (good, "1000")
    state = json.dumps(client.call("/api/state")[1])
    assert good not in state and '"agent_github": "owner-a"' in state
    # Removed: every agent_github_ value goes.
    assert client.call("/api/agent-github", {"token": ""}) == (200, {"ok": True, "agent_github": None})
    assert not [k for k in app.codec(app.home).data if k.startswith("agent_github")]
