"""The setup page's server, over real HTTPS on localhost."""

import http.client
import http.server
import json
import os
import re
import socket
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
from gitrepos import commit, git, make
from pinned import MINECRAFT

from nullbill import applied, apply, browserca, direct, preview, release
from nullbill.errors import NullBillError, Refused
from nullbill.model import build_model
from nullbill.probe import parse
from nullbill.web import progress
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
elif sys.argv[1] == "upgrade" and "NB_TEST_CHECKOUT" in os.environ:
    from nullbill import upgrade
    upgrade.upgrade(pathlib.Path(os.environ["NB_TEST_CHECKOUT"]), home, finish=lambda argv: None)
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
        return response.status, json.loads(data) if kind.startswith("application/json") else data.decode(errors="replace")


def start_site(tmp_path, monkeypatch, **more):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "install.json").write_text(json.dumps({"host": "192.0.2.10", "node": "pve", "vmid": 2002}))
    script = tmp_path / "fake_nullbill.py"
    script.write_text(FAKE_PROBE.format(tests=str(Path(__file__).parent)))
    codec = MemoryCodec()
    app = web.App(home, command=[sys.executable, str(script)], codec=lambda _: codec, send=Sent(), **more)
    app.ensure_setup_code()
    cert, key = web.ensure_certificate(home)
    server, _ = web.make_server(app, ("127.0.0.1", 0), cert, key)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    return app, Client(server.server_address[1], cert), server


@pytest.fixture
def site(tmp_path, monkeypatch):
    app, client, server = start_site(tmp_path, monkeypatch)
    yield app, client
    server.shutdown()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A page that runs from a checkout of its own origin, as install.sh
    leaves it: (app, client, work), where commits pushed from work are
    what main holds next."""
    work, checkout = make(tmp_path)
    monkeypatch.setenv("NB_TEST_CHECKOUT", str(checkout))
    app, client, server = start_site(tmp_path, monkeypatch, repo=checkout)
    yield app, client, work
    server.shutdown()


def restarted(app):
    """The page as Update NullBill's restart leaves it: a new server over
    the same home, serving the same clients."""
    app.jobs.clear()
    app.sessions.clear()
    app.version_seen = None
    app.load_jobs()
    app.load_sessions()


real_sleep = time.sleep


def sign_in(app, client, password="correct horse battery"):
    code = app.code_file.read_text().strip()
    assert client.call("/api/login", {"secret": code.lower()})[0] == 200
    assert client.call("/api/password", {"password": password}) == (200, {"ok": True})


def setup(client, action="apply", **more):
    """Start a setup run as the page does: with the id of the review it showed."""
    review = client.call("/api/state")[1]["review"]["id"]
    return client.call("/api/jobs", {"action": action, "review": review, **more})


def wait(client, job_id):
    """The job once it has ended. By the clock: the site fixture stubs
    time.sleep, and 200 requests without a pause between them ended before
    the job's process did on a busy machine."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status, job = client.call(f"/api/jobs/{job_id}")
        assert status == 200
        if job["state"] != "running":
            return job
        real_sleep(0.05)
    raise AssertionError("job did not finish")


def test_page_and_headers(site):
    _, client = site
    status, page = client.call("/")
    assert status == 200 and "<title>NullBill</title>" in page
    assert client.headers["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'none'"
    assert client.headers["Cache-Control"] == "no-store"
    assert client.call("/app.js")[0] == 200


def test_every_answer_names_the_page_version_the_page_was_served_with(site):
    # An open page compares the two and reloads itself after an update.
    app, client = site
    _, page = client.call("/")
    served = re.search(r'<meta name="nb-shell" content="([0-9a-f]{16})">', page).group(1)
    assert served == web.shell()
    assert client.call("/api/state")[0] == 200 and client.headers["X-NullBill-Shell"] == served
    assert client.call("/api/choices", {})[0] == 401 and client.headers["X-NullBill-Shell"] == served
    sign_in(app, client)
    assert client.call("/api/state")[1]["auth"] and client.headers["X-NullBill-Shell"] == served
    # The digest is of the files as they are in the repository, not of the served page.
    raw = (Path(web.__file__).parent / "static" / "index.html").read_text()
    assert '<meta name="nb-shell" content="">' in raw


def test_the_root_certificate_downloads_without_signing_in_once_setup_made_it(site):
    app, client = site
    conn = http.client.HTTPSConnection("127.0.0.1", client.port, context=client.tls, timeout=30)
    conn.request("GET", "/nullbill-root.crt")
    response = conn.getresponse()
    assert response.status == 404 and b"run setup first" in response.read()

    root = browserca.root(app.home, "nullbill.internal", "10.42.0.0/16")
    conn.request("GET", "/nullbill-root.crt")
    response = conn.getresponse()
    assert response.status == 200 and response.read() == browserca.der(root)
    assert response.getheader("Content-Type") == "application/x-x509-ca-cert"
    assert response.getheader("Content-Disposition") == 'attachment; filename="nullbill-root.crt"'

    sign_in(app, client)
    assert client.call("/api/state")[1]["trust"] == {"ready": True, "fingerprint": browserca.fingerprint(root)}


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
    # The hardware shown is what was read then, and the page says when.
    assert time.time() - state["hardware"]["read_at"] < 60
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


def test_a_games_agreement_and_admins_are_saved_with_the_choices(site):
    app, client = site
    sign_in(app, client)
    saved = lambda: yaml.safe_load((app.home / "config.yaml").read_text())  # noqa: E731
    choices = {"workloads": ["agents", "minecraft", "factorio"], "owner_login": "owner-a@example.internal"}
    # Ticked without the agreement: saved, and the plan says what is missing and where to give it.
    form = {"minecraft": {"eula": False, "admins": ""}, "terraria": {}, "factorio": {"admins": ""}}
    assert client.call("/api/choices", {**choices, "games": form})[0] == 200
    assert "games" not in saved() and saved()["intent"] == {"workloads": ["agents", "minecraft", "factorio"]}
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    plan = client.call("/api/state")[1]["plan"]
    assert any(e.startswith("games.minecraft.eula") for e in plan["errors"])
    assert plan["advice"] == [
        "Minecraft's server runs only once you agree to its EULA (https://aka.ms/MinecraftEULA): "
        "tick that you agree beside Minecraft under Setup, What to run, or untick Minecraft."
    ]
    # The problem and what to do about it travel together, for the page to show them so.
    (problem,) = [x for x in plan["problems"] if x["text"].startswith("games.minecraft.eula")]
    assert problem["advice"] == plan["advice"][0]
    preview = client.call("/api/choices/preview", {**choices, "games": form})[1]
    assert preview["advice"] == plan["advice"] and preview["problems"] == plan["problems"]
    # Agreed, with admins typed as people type them.
    form = {"minecraft": {"eula": True, "admins": " owner_a,  Friend_B owner_a "}, "factorio": {"admins": "owner-a"}}
    status, data = client.call("/api/choices", {**choices, "games": form})
    assert status == 200 and data["plan"]["errors"] == []
    assert saved()["games"] == {"minecraft": {"eula": True, "admins": ["owner_a", "Friend_B"]}, "factorio": {"admins": ["owner-a"]}}
    options = {i["name"]: i for i in client.call("/api/state")[1]["options"]["items"]}
    assert options["minecraft"]["game"]["eula"]["agreed"] is True and options["minecraft"]["game"]["admins"] == ["owner_a", "Friend_B"]
    # Choices saved without the games' form (the agent's) leave the agreement and the admins alone.
    assert client.call("/api/choices", choices)[0] == 200
    assert saved()["games"]["minecraft"] == {"eula": True, "admins": ["owner_a", "Friend_B"]}
    # Not a player name: nothing is saved.
    bad = {"minecraft": {"eula": True, "admins": "owner a; rm"}}
    status, data = client.call("/api/choices", {**choices, "games": bad})
    assert status == 400 and "admins are player names" in data["error"]
    status, data = client.call("/api/choices", {**choices, "games": {"minecraft": {"eula": True, "admins": "ab"}}})
    assert status == 400 and "games.minecraft.admins" in data["error"]
    assert saved()["games"]["minecraft"]["admins"] == ["owner_a", "Friend_B"]
    # Settings the owner wrote into config.yaml stay through a save from the page.
    config = saved()
    config["games"]["minecraft"]["settings"] = {"difficulty": "hard"}
    (app.home / "config.yaml").write_text(yaml.safe_dump(config))
    client.call("/api/choices", {**choices, "games": {"minecraft": {"eula": False, "admins": ""}, "factorio": {"admins": ""}}})
    assert saved()["games"] == {"minecraft": {"settings": {"difficulty": "hard"}}}


def test_services_show_each_game_with_its_address_and_how_to_join(site):
    app, client = site
    sign_in(app, client)
    golden = Path(__file__).parent / "golden" / "large-games.plan.yaml"
    (app.home / "plan.yaml").write_text(golden.read_text())
    items = {s["name"]: s for s in client.call("/api/state")[1]["services"]["items"]}
    assert [n for n in items if n in ("minecraft", "terraria", "factorio")] == ["minecraft", "terraria", "factorio"]
    minecraft = items["minecraft"]
    assert minecraft["title"] == "Minecraft" and minecraft["watch"] == "minecraft"
    assert minecraft["addresses"] == ["In the game: 10.42.8.25:25565"]
    assert f"Players need Minecraft: Java Edition {MINECRAFT}." in minecraft["what"]
    steps = minecraft["steps"]
    assert "Multiplayer, Add Server" in steps[0]["text"]
    assert (steps[1]["href"], steps[1]["link"]) == ("#access/people", "Access, People")
    assert steps[2]["done"] is True and steps[2]["href"] == "#setup/choices"
    # The console and the worlds are the page's own block for a game, not a shell line.
    assert minecraft["game"] == "minecraft" and not any("code" in step for step in steps)
    assert not any(st.get("href") == "#setup/choices" for st in items["terraria"]["steps"])
    assert items["factorio"]["addresses"] == ["In the game: 10.42.8.27:34197"]
    assert any("nobody needs the Space Age expansion" in st["text"] for st in items["factorio"]["steps"])
    from nullbill import names

    names.record(app.home, "nullbill.internal", "10.42.6.1")
    items = {s["name"]: s for s in client.call("/api/state")[1]["services"]["items"]}
    assert items["terraria"]["addresses"] == ["In the game: terraria.nullbill.internal:7777", "By address: 10.42.8.26:7777"]


ED25519 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwnerKeyForTestsOnlyNotARealKey00000000"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCownerkeyfortestsonlynotarealkey0000000000"


WG_KEY, WG_PEER = "A" * 43 + "=", "B" * 43 + "="
WG_FILE = f"[Interface]\nPrivateKey = {WG_KEY}\nAddress = 10.2.0.2/32\nDNS = 10.2.0.1\n\n[Peer]\nPublicKey = {WG_PEER}\n"
WG_FILE += "AllowedIPs = 0.0.0.0/0, ::/0\nEndpoint = 203.0.113.44:51820\n"


def test_the_download_clients_way_out_and_vpn_file_are_saved_with_the_choices(site):
    app, client = site
    sign_in(app, client)
    saved = lambda: yaml.safe_load((app.home / "config.yaml").read_text())  # noqa: E731
    secrets = lambda: web.Secrets(app.codec(app.home)).data  # noqa: E731
    choices = {"workloads": ["agents", "torrents"], "owner_login": "owner-a@example.internal"}
    row = lambda data: next(i for i in data["items"] if i["name"] == "torrents")["torrents"]  # noqa: E731
    # Ticked and nothing chosen: saved, and no way out is picked for the owner.
    form = {"route": "", "share": "", "folder": "Downloads", "keep_free_gib": 25}
    assert client.call("/api/choices", {**choices, "torrents": form})[0] == 200
    assert "torrents" not in saved() and row(client.call("/api/state")[1]["options"])["route"] is None
    # A pasted file counts in the preview, and nothing is written by it.
    preview = client.call("/api/choices/preview", {**choices, "torrents": {**form, "route": "vpn", "conf": WG_FILE}})[1]
    assert row(preview)["endpoint"] == "203.0.113.44:51820" and "torrents" not in saved() and "torrents_vpn_conf" not in secrets()
    # A file that cannot be used: every reason, and nothing saved.
    bad = WG_FILE.replace(f"PrivateKey = {WG_KEY}\n", "").replace("DNS = 10.2.0.1\n", "")
    status, data = client.call("/api/choices", {**choices, "torrents": {**form, "route": "vpn", "conf": bad}})
    assert status == 400 and data["headline"] == "This WireGuard file cannot be used" and len(data["errors"]) == 2
    assert "torrents" not in saved() and "torrents_vpn_conf" not in secrets()
    # Saved: the file with the secrets, reduced to what NullBill uses; its server in config.yaml.
    status, data = client.call("/api/choices", {**choices, "torrents": {**form, "route": "vpn", "conf": WG_FILE, "keep_free_gib": 40}})
    assert status == 200
    assert saved()["torrents"] == {"route": "vpn", "keep_free_gib": 40, "vpn": {"endpoint": "203.0.113.44:51820"}}
    kept = secrets()["torrents_vpn_conf"]
    assert f"PrivateKey = {WG_KEY}" in kept and "::/0" not in kept
    state = client.call("/api/state")[1]
    assert row(state["options"])["endpoint"] == "203.0.113.44:51820"
    # The key never comes back to the page, and it is no password the page reveals.
    assert WG_KEY not in json.dumps(state) and client.call("/api/password-reveal", {"name": "torrents_vpn_conf"})[0] == 400
    # Saving again without a file keeps the one there is; settings are refused together.
    assert client.call("/api/choices", {**choices, "torrents": {**form, "route": "home"}})[0] == 200
    assert saved()["torrents"] == {"route": "home", "vpn": {"endpoint": "203.0.113.44:51820"}} and "torrents_vpn_conf" in secrets()
    # From home the server asks the home router for the client's port unless the owner unticks it.
    assert row(client.call("/api/state")[1]["options"])["port_mapping"] is True
    assert client.call("/api/choices", {**choices, "torrents": {**form, "route": "home", "port_mapping": False}})[0] == 200
    assert saved()["torrents"]["port_mapping"] is False and row(client.call("/api/state")[1]["options"])["port"] == 41643
    assert client.call("/api/choices", {**choices, "torrents": {**form, "route": "home", "port_mapping": True}})[0] == 200
    status, data = client.call("/api/choices", {**choices, "torrents": {"route": "tor", "folder": "../etc", "keep_free_gib": 0}})
    assert status == 400 and len(data["errors"]) == 3 and saved()["torrents"]["route"] == "home"
    # Forgotten: the file and the server the firewall would open to.
    assert client.call("/api/torrents/forget-vpn", {})[0] == 200
    assert saved()["torrents"] == {"route": "home"} and "torrents_vpn_conf" not in secrets()


def test_ssh_keys_go_into_the_owners_person_and_survive_saving_choices(site):
    app, client = site
    sign_in(app, client)
    status, data = client.call("/api/ssh-keys", {"keys": ED25519})
    assert status == 400 and "save your choices" in data["error"]
    choices = {"workloads": ["agents", "linux-ci"], "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", choices)
    for bad, why in (("ssh-ed25519 short", "line 1 is not an SSH public key"), ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key")):
        status, data = client.call("/api/ssh-keys", {"keys": bad})
        assert status == 400 and why in data["error"], data
    # Blank lines, spaces and a duplicate (same key, other comment) are dropped.
    pasted = f"\n  {ED25519}   laptop  \n\n{RSA}\n{ED25519} again\n"
    status, data = client.call("/api/ssh-keys", {"keys": pasted})
    assert status == 200 and data["ssh_keys"] == [f"{ED25519} laptop", RSA]
    person = yaml.safe_load((app.home / "config.yaml").read_text())["people"]["owner"]
    assert person == {"role": "owner", "login": "owner-a@example.internal", "ssh_keys": [f"{ED25519} laptop", RSA]}
    client.call("/api/choices", {**choices, "workloads": ["agents"]})
    assert client.call("/api/state")[1]["choices"]["ssh_keys"] == [f"{ED25519} laptop", RSA]
    # The steps that need a key are done once one is saved.
    golden = Path(__file__).parent / "golden" / "small.plan.yaml"
    (app.home / "plan.yaml").write_text(golden.read_text())
    items = {s["name"]: s for s in client.call("/api/state")[1]["services"]["items"]}
    assert [st["done"] for st in items["workspace"]["steps"] if st.get("href") == "#access/ssh"] == [True]
    # Forgejo leads with nullbill-admin until setup made the owner's own user.
    assert (items["forgejo"]["user"], items["forgejo"]["password"]) == ("nullbill-admin", "forgejo")
    secrets = app.codec(app.home).data
    secrets |= {"forgejo_owner_user": "owner-a", "forgejo_owner_password": "owner-pw"}
    forgejo = next(s for s in client.call("/api/state")[1]["services"]["items"] if s["name"] == "forgejo")
    assert (forgejo["user"], forgejo["password"]) == ("owner-a", "forgejo-owner")
    assert any(st.get("code") == "git clone git@10.42.2.17:owner-a/<repository>.git" for st in forgejo["steps"])
    assert client.call("/api/password-reveal", {"name": "forgejo-owner"}) == (200, {"password": "owner-pw"})
    # Empty takes them all off again.
    assert client.call("/api/ssh-keys", {"keys": " \n"}) == (200, {"ok": True, "ssh_keys": []})
    assert "ssh_keys" not in yaml.safe_load((app.home / "config.yaml").read_text())["people"]["owner"]


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
    job = setup(client)[1]
    assert wait(client, job["id"])["state"] == "done"
    config = app.home / "config.yaml"
    started = app.jobs[job["id"]].started
    os.utime(config, (started - 60, started - 60))
    assert client.call("/api/state")[1]["config_changed"] is False
    os.utime(config, (started + 60, started + 60))
    assert client.call("/api/state")[1]["config_changed"] is True


def test_what_to_run_says_what_each_choice_takes_and_previews_the_form_unsaved(site):
    app, client = site
    assert client.call("/api/choices/preview", {"workloads": ["agents"]})[0] == 401
    sign_in(app, client)
    # Before any choice is saved, the page's defaults are ticked.
    options = client.call("/api/state")[1]["options"]
    assert [i["name"] for i in options["items"]] == [*web.WORKLOADS, "backups", "console"]
    assert [i["name"] for i in options["items"] if i["chosen"]] == ["agents", "linux-ci", "backups"]
    assert options["memory"] is None and options["adds"] is None  # no probe, no setup run yet
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    saved = {"workloads": ["agents", "linux-ci"], "backups": False, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", saved)
    options = client.call("/api/state")[1]["options"]
    by_name = {i["name"]: i for i in options["items"]}
    assert options["errors"] == [] and options["memory"]["free_mib"] > 0
    # A container counts at half its limit, a VM in full; a pool also keeps its image on disk.
    assert by_name["agents"]["memory_mib"] == 512 and by_name["agents"]["disk_gib"] == 32
    assert by_name["linux-ci"]["memory_mib"] == 128 + 2048 and by_name["linux-ci"]["disk_gib"] == 4 + 2 * 32
    assert "at a time" in by_name["linux-ci"]["note"]
    assert by_name["console"]["guests"] == [] and by_name["console"]["memory_mib"] == 512
    # The small class has no NVIDIA card, and Windows CI is not finished.
    assert "no NVIDIA" in by_name["inference"]["blocked"] and "Not finished" in by_name["windows-ci"]["blocked"]
    assert by_name["windows-dev"]["blocked"] is None
    # The form as ticked, without saving it and without a login typed yet.
    before = (app.home / "config.yaml").read_text()
    status, view = client.call("/api/choices/preview", {"workloads": ["agents", "linux-ci", "media", "windows-dev"], "backups": True})
    assert status == 200 and (app.home / "config.yaml").read_text() == before
    assert [i["name"] for i in view["items"] if i["chosen"]] == ["agents", "linux-ci", "media", "windows-dev", "backups"]
    memory = view["memory"]
    assert memory["free_mib"] < 0 and len(view["errors"]) == 1 and view["advice"]
    assert f"planned guests need {memory['base_mib'] + memory['chosen_mib']} MiB" in view["errors"][0]
    assert memory["host_mib"] - memory["reserve_mib"] - memory["base_mib"] - memory["chosen_mib"] == memory["free_mib"]


def test_each_guests_size_is_set_on_the_page_and_only_what_differs_from_the_least_is_saved(site):
    app, client = site
    sign_in(app, client)
    probed(app, "small")  # 4 threads and 16 GiB, as the owner's server has
    assert client.call("/api/choices", OWNER | {"backups": False})[0] == 200

    def config():
        return yaml.safe_load((app.home / "config.yaml").read_text())

    def guest(view, name):
        return next(g for g in [*(g for i in view["items"] for g in i["guests"]), *view["core"]] if g["name"] == name)

    options = client.call("/api/state")[1]["options"]
    # Every guest has its size and its least; core servers are listed apart, install.sh's container not at all.
    assert {"router", "vpn", "forgejo", "monitoring"} <= {g["name"] for g in options["core"]}
    assert "nullbill" not in {g["name"] for g in options["core"]}
    assert options["limits"] == {"cores": 4, "memory_mib": 15616, "step_mib": 256, "probed": True}
    workspace = guest(options, "workspace")
    assert (workspace["cores"], workspace["memory_mib"], workspace["set"]) == (1, 1024, {})
    assert workspace["least"] == {"cores": 1, "memory_mib": 1024}

    # The preview follows a size typed and not saved, as it follows a tick.
    status, view = client.call("/api/choices/preview", OWNER | {"backups": False, "sizes": {"workspace": {"memory_mib": 4096}}})
    agents = next(i for i in view["items"] if i["name"] == "agents")
    assert status == 200 and agents["memory_mib"] == 2048 and guest(view, "workspace")["set"] == {"memory_mib": 4096}
    assert view["memory"]["chosen_mib"] == options["memory"]["chosen_mib"] + 1536 and "overrides" not in config()
    # What cannot be read yet is left out of the preview, not refused.
    status, view = client.call(
        "/api/choices/preview", OWNER | {"backups": False, "sizes": {"workspace": {"cores": "x", "memory_mib": 4096}}}
    )
    assert status == 200 and guest(view, "workspace")["set"] == {"memory_mib": 4096}

    # Saved: only what differs from the least. Empty, null and the least are automatic.
    sizes = {
        "workspace": {"cores": 2, "memory_mib": 4096},
        "router": {"cores": "", "memory_mib": 512},
        "forgejo": {"cores": None, "memory_mib": None},
        "linux-runner": {"memory_mib": 3072},
    }
    assert client.call("/api/choices", OWNER | {"backups": False, "sizes": sizes})[0] == 200
    assert config()["overrides"] == {"workspace": {"cores": 2, "memory_mib": 4096}, "linux-runner": {"memory_mib": 3072}}
    plan = yaml.safe_load((app.home / "plan.yaml").read_text())
    assert (plan["guests"]["workspace"]["cores"], plan["guests"]["workspace"]["memory_mib"]) == (2, 4096)
    assert plan["guests"]["linux-runner"]["memory_mib"] == 3072  # each job's VM
    options = client.call("/api/state")[1]["options"]
    assert guest(options, "workspace")["set"] == {"cores": 2, "memory_mib": 4096}
    # Saving the form as the page shows it changes nothing; a guest the form leaves out keeps its size.
    assert client.call("/api/choices", OWNER | {"backups": False, "sizes": {"workspace": {"cores": "2", "memory_mib": 4096}}})[0] == 200
    assert config()["overrides"]["workspace"] == {"cores": 2, "memory_mib": 4096}

    # Every problem at once, a line each, and nothing saved.
    before = config()
    bad = {"workspace": {"cores": 9}, "router": {"memory_mib": 100}, "nullbill": {"cores": 2}, "forgejo": {"memory_mib": 1100}}
    status, answer = client.call("/api/choices", OWNER | {"backups": False, "sizes": bad})
    assert status == 400 and config() == before
    assert answer["headline"] == "Some sizes cannot be used, so nothing was saved"
    assert answer["errors"] == [
        "workspace: its cores are a whole number from 1 to 4, as many as the server has threads",
        "router: its memory is from 0.5 GiB to 15.25 GiB in steps of 0.25 GiB (512 to 15616 MiB)",
        "nullbill: install.sh made it; change its size in Proxmox",
        "forgejo: its memory is from 1 GiB to 15.25 GiB in steps of 0.25 GiB (1024 to 15616 MiB)",
    ]
    status, answer = client.call("/api/choices", OWNER | {"backups": False, "sizes": {"workspace": {"cores": 0}}})
    assert status == 400 and answer == {"error": "workspace: its cores are a whole number from 1 to 4, as many as the server has threads"}

    # More memory than the server has is refused, with what is missing and what fits.
    status, answer = client.call("/api/choices", OWNER | {"backups": False, "sizes": {"workspace": {"memory_mib": 15616}}})
    assert status == 400 and config() == before
    assert re.fullmatch(
        r"workspace: 15\.25 GiB of memory does not fit; the server would be [\d.]+ (GiB|MiB) short\."
        r" At most [\d.]+ GiB fits beside the rest\.",
        answer["error"],
    )
    most = float(re.search(r"At most ([\d.]+) GiB", answer["error"]).group(1))
    assert client.call("/api/choices", OWNER | {"backups": False, "sizes": {"workspace": {"memory_mib": int(most * 1024)}}})[0] == 200
    assert client.call("/api/state")[1]["plan"]["errors"] == []
    status, answer = client.call("/api/choices", OWNER | {"backups": False, "sizes": {"workspace": {"memory_mib": int(most * 1024) + 256}}})
    assert status == 400 and "does not fit" in answer["error"]
    # Two guests given more at once: a line for each, under one headline.
    status, answer = client.call(
        "/api/choices", OWNER | {"backups": False, "sizes": {"workspace": {"memory_mib": 15616}, "linux-runner": {"memory_mib": 8192}}}
    )
    assert status == 400 and answer["headline"] == "These sizes do not fit in the server's memory, so nothing was saved"
    assert [line.split(":")[0] for line in answer["errors"]] == ["linux-runner", "workspace"]

    # A tick that then does not fit is saved, and the plan names the size set with what to do.
    assert client.call("/api/choices", OWNER | {"workloads": [*OWNER["workloads"], "windows-dev"], "backups": False})[0] == 200
    plan = client.call("/api/state")[1]["plan"]
    assert plan["errors"][0].endswith(". More memory than the least is set for linux-runner, workspace")
    assert plan["problems"][0]["advice"].startswith("Some guests are given more memory than the least")
    # On the page the guests are a list apart, the problem's own line without them.
    assert "More memory" not in plan["problems"][0]["text"]
    items = plan["problems"][0]["items"]
    assert items[0].startswith("linux-runner: 3 GiB, its least ") and items[1:] == [f"workspace: {most:g} GiB, its least 1 GiB"]
    # Raising a guest while the choices do not fit anyway is no refusal: the plan says so, as before.
    status, _ = client.call(
        "/api/choices",
        OWNER | {"workloads": [*OWNER["workloads"], "windows-dev"], "backups": False, "sizes": {"forgejo": {"memory_mib": 2048}}},
    )
    assert status == 200 and config()["overrides"]["forgejo"] == {"memory_mib": 2048}
    assert client.call("/api/state")[1]["plan"]["errors"]
    # Turned off, a guest keeps the size it was given, for when it is on again; the plan still fits.
    assert client.call("/api/choices", OWNER | {"workloads": ["linux-ci"], "backups": False})[0] == 200
    assert "workspace" in config()["overrides"] and client.call("/api/state")[1]["plan"]["errors"] == []

    # A size saved by hand, off the page's steps, survives a save of the form as the page shows it.
    (app.home / "config.yaml").write_text(yaml.safe_dump(config() | {"overrides": {"forgejo": {"memory_mib": 3000}}}))
    options = client.call("/api/state")[1]["options"]
    shown = {
        g["name"]: {"cores": g["set"].get("cores"), "memory_mib": g["set"].get("memory_mib")}
        for g in [*(g for i in options["items"] for g in i["guests"]), *options["core"]]
    }
    assert shown["forgejo"] == {"cores": None, "memory_mib": 3000}
    assert client.call("/api/choices", OWNER | {"workloads": ["linux-ci"], "backups": False, "sizes": shown})[0] == 200
    assert config()["overrides"] == {"forgejo": {"memory_mib": 3000}}
    # A digit that is no decimal one is a refusal, not a crash.
    status, answer = client.call(
        "/api/choices", OWNER | {"workloads": ["linux-ci"], "backups": False, "sizes": {"forgejo": {"cores": "\u00b2"}}}
    )
    assert status == 400 and answer["error"].startswith("forgejo: its cores are a whole number")


def test_a_choice_turned_off_after_setup_built_it_waits_for_the_owner_before_it_is_deleted(site):
    app, client = site
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    saved = {"workloads": ["agents", "linux-ci"], "backups": False, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", saved)
    state = client.call("/api/state")[1]
    assert state["removals"] == [] and state["options"]["removes"] is None  # nothing is built yet
    job = setup(client)[1]
    assert wait(client, job["id"])["state"] == "done"
    config = app.home / "config.yaml"
    started = app.jobs[job["id"]].started
    os.utime(config, (started - 60, started - 60))
    # A server built before apply kept guests.json follows its saved choices.
    by_name = {i["name"]: i for i in client.call("/api/state")[1]["options"]["items"]}
    assert by_name["linux-ci"]["on_server"] == ["runner-dispatcher", "linux-runner"] and by_name["media"]["on_server"] == []
    assert not (app.home / "guests.json").exists()
    # Ticked off on the page, not saved: the preview says what would go and what would come.
    view = client.call("/api/choices/preview", {**saved, "workloads": ["agents", "media"]})[1]
    assert view["removes"] == ["linux-runner", "runner-dispatcher"] and view["adds"] == ["jellyfin"]
    assert client.call("/api/state")[1]["removals"] == []
    # Saved: what the server has is written down before the choices change.
    client.call("/api/choices", {**saved, "workloads": ["agents"]})
    assert "linux-runner" in json.loads((app.home / "guests.json").read_text())["guests"]
    state = client.call("/api/state")[1]
    assert state["removals"] == ["linux-runner", "runner-dispatcher"] and state["removals_stopped"] is False
    assert all("nothing of yours is on them" in note for note in state["removal_notes"].values())
    # Delete and run setup names exactly those to apply, and only if the owner was shown them all.
    for seen in ({}, {"guests": ["linux-runner"]}):
        status, data = setup(client, "remove-guests", **seen)
        assert status == 400 and "is now linux-runner, runner-dispatcher; look at it again" in data["error"]
    assert not (app.home / "removals.json").exists()
    status, job = setup(client, "remove-guests", guests=state["removals"])
    assert status == 200 and job["args"] == ["apply", "--remove-guests"]
    wait(client, job["id"])
    assert json.loads((app.home / "removals.json").read_text()) == {
        "guests": ["linux-runner", "runner-dispatcher"],
        "gone": ["linux-runner", "runner-dispatcher"],
    }
    # Turned on again and saved: nothing waits any more.
    client.call("/api/choices", saved)
    assert client.call("/api/state")[1]["removals"] == []


def test_activity_lists_runs_newest_first_and_built_waits_for_a_good_setup_run(site):
    app, client = site
    sign_in(app, client)
    st = client.call("/api/state")[1]
    assert st["runs"] == [] and st["built"] is False
    failed = client.call("/api/jobs", {"action": "apply", "stage": "host"})[1]
    wait(client, failed["id"])
    assert client.call("/api/state")[1]["built"] is False  # a failed run built nothing
    good = setup(client)[1]
    wait(client, good["id"])
    st = client.call("/api/state")[1]
    assert st["built"] is True
    assert [r["id"] for r in st["runs"]] == [good["id"], failed["id"]]
    assert st["runs"][1]["state"] == "failed" and st["runs"][1]["stopped"] is False and st["runs"][0]["by"] == "owner"
    assert all(r["seconds"] >= 0 for r in st["runs"])
    # The list says where a failed run stopped; a finished one has nothing to say.
    assert st["runs"][0]["where"] is None and st["runs"][1]["where"] == 'At "Read your choices and make the plan"'


def test_each_plan_problem_carries_what_to_do_about_it():
    """A problem with its advice beside it, and each advice line once."""
    view = web.problems({"errors": ["intent asks for inference, but no host-shared GPU budgets VRAM to it", "something new"]}, "Other.")
    assert [x["advice"] for x in view["problems"]] == [view["advice"][0], None] and view["advice"][1] == "Other."
    assert view["advice"][0].startswith("Untick Local AI models")


def test_one_job_at_a_time(site, tmp_path):
    app, client = site
    sign_in(app, client)
    slow = tmp_path / "slow.py"
    slow.write_text("import time; time.sleep(5)\n")
    app.command = [sys.executable, str(slow)]
    job = client.call("/api/jobs", {"action": "verify"})[1]
    assert client.call("/api/jobs", {"action": "verify"}) == (400, {"error": "another job is still running"})
    app.jobs[job["id"]].process.kill()


def test_a_hung_run_is_stopped_with_everything_under_it(site, tmp_path, monkeypatch):
    app, client = site
    sign_in(app, client)
    # As apply runs ansible-playbook and ssh: a child with a grandchild, and
    # one of them ignores SIGTERM, as a stuck download can.
    hung = tmp_path / "hung.py"
    pids = tmp_path / "pids"
    hung.write_text(
        "import os, signal, subprocess, sys, time\n"
        "print('--- stage media, 2026-11-09 00:30:25', flush=True)\n"
        "deaf = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)'\n"
        "child = subprocess.Popen([sys.executable, '-c', deaf])\n"
        f"open({str(pids)!r}, 'w').write(str(child.pid))\n"
        "print('nullbill: a message from the way out', flush=True)\n"
        "time.sleep(600)\n"
    )
    app.command = [sys.executable, str(hung)]
    monkeypatch.setattr(web, "STOP_WAIT_S", 1)
    job = setup(client)[1]
    while not pids.exists() or not pids.read_text():
        time.sleep(0.05)
    grandchild = int(pids.read_text())
    status, view = client.call(f"/api/jobs/{job['id']}/stop", {})
    assert status == 200
    done = wait(client, job["id"])
    assert done["state"] == "failed"
    assert not Path(f"/proc/{grandchild}").exists() or Path(f"/proc/{grandchild}/stat").read_text().split(")")[1].split()[0] == "Z"
    error = done["progress"]["error"]
    assert error["message"] == "You stopped this run from the page." and error["stage"] == "media"
    assert error["advice"] == progress.STOPPED_ADVICE and error["items"] == []
    # The page tells a deliberate stop from a failure.
    st = client.call("/api/state")[1]
    assert done["stopped"] is True and st["last_jobs"]["apply"]["stopped"] is True
    assert st["runs"][0]["stopped"] is True
    # Stopped already; and a job that ended cannot be stopped.
    assert client.call(f"/api/jobs/{job['id']}/stop", {}) == (400, {"error": "that job is not running any more"})
    # The next run starts.
    app.command = [sys.executable, "-c", "print('ran')"]
    assert wait(client, client.call("/api/jobs", {"action": "verify"})[1]["id"])["state"] == "done"


def test_stopping_needs_a_session(site):
    _, client = site
    assert client.call("/api/jobs/20260101T000000-apply/stop", {})[0] == 401


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
    assert status == 200 and data["alerts"] == {"ntfy_url": alerts["ntfy_url"], "healthchecks_url": ping, "heard": None}
    assert client.call("/api/alerts/test", {}) == (200, {"ok": True, "sent": ["ntfy", "Healthchecks.io"]})
    (ntfy, body, headers), (hc, _, _) = app.send
    assert (ntfy, hc) == (alerts["ntfy_url"], ping)
    assert headers["Title"] == "NullBill: test alert" and "reach this device" in body
    # Clearing the URL stops the ping; the topic stays.
    client.call("/api/alerts", {"healthchecks_url": ""})
    assert client.call("/api/state")[1]["alerts"] == {"ntfy_url": alerts["ntfy_url"], "healthchecks_url": "", "heard": None}


def test_a_test_alert_opened_on_a_device_tells_the_guide(site):
    """The server cannot ask ntfy whether a phone subscribed: the test alert
    links to the page with a one-time word, and opening it says it arrived."""
    app, client = site
    sign_in(app, client)
    # Without the page's origin there is no link to open.
    client.call("/api/alerts/test", {})
    assert "Click" not in app.send[-1][2]
    for origin in ("http://10.42.1.2:8443", "https://evil.example/x", "https://a\r\nX: y"):
        client.call("/api/alerts/test", {"origin": origin})
        assert "Click" not in app.send[-1][2]
    assert client.call("/api/alerts/test", {"origin": "https://10.42.1.2:8443"})[0] == 200
    _, body, headers = app.send[-1]
    link = headers["Click"]
    assert link.startswith("https://10.42.1.2:8443/#alerts/heard/") and "Tap" in body
    word = link.rsplit("/", 1)[1]
    assert client.call("/api/alerts/heard", {"word": "nope"}) == (
        400,
        {"error": "that link is from an older test alert: send another from Alerts"},
    )
    assert client.call("/api/state")[1]["alerts"]["heard"] is None
    before = time.time()
    status, data = client.call("/api/alerts/heard", {"word": word})
    assert status == 200 and data["alerts"]["heard"] >= before
    assert word not in (app.home / "web" / "alerts-heard.json").read_text()
    # A new topic has to be heard again.
    store = web.Secrets(app.codec(app.home))
    store.set("ntfy_topic", "nullbill-another-topic-0000000000000000")
    assert client.call("/api/state")[1]["alerts"]["heard"] is None
    assert client.call("/api/alerts/heard", {"word": word})[0] == 400


def test_hearing_a_test_alert_needs_a_session(site):
    _, client = site
    assert client.call("/api/alerts/heard", {"word": "x"})[0] == 401


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
    status, data = client.call("/api/agent", {"credential": f" {key}\n"})
    assert (status, data["agent_credential"]) == (200, "API key")
    assert app.codec(app.home).load()["anthropic_api_key"] == key
    token = "sk-ant-oat01-" + "b" * 40
    wrapped = f"{token[:30]}\n  {token[30:]} "
    status, data = client.call("/api/agent", {"credential": wrapped})
    assert (status, data["agent_credential"]) == (200, "subscription token")
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


def test_the_page_is_told_how_devices_on_mobile_data_reach_the_server(site):
    app, client = site
    sign_in(app, client)
    # Before the vpn guest is on the tailnet nothing is known.
    assert client.call("/api/state")[1]["direct"] is None
    why = "the home router did not open UDP 41642"
    direct.record(app.home, direct.Finding(direct.CLOSED, why, 41642, "192.168.1.57"))
    found = client.call("/api/state")[1]["direct"]
    # With when it was read, and what it comes to: nothing measured is no problem found.
    assert time.time() - found.pop("at") < 60
    assert found == {
        "kind": "closed",
        "detail": why,
        "port": 41642,
        "address": "192.168.1.57",
        "method": None,
        "devices": [],
        "verdict": "unmeasured",
        "seen": None,
    }
    # Nothing built yet: the page gets the last reading and is told it is not a fresh one.
    assert client.call("/api/path")[1]["direct"]["live"] is False
    direct.record(app.home, None)
    assert client.call("/api/state")[1]["direct"] is None and client.call("/api/path") == (200, {"direct": None})


def on_the_server(app, client, monkeypatch, peers, pongs=None, **state):
    """A server built from the small plan whose host answers direct.py's
    commands: (what the host was asked, the plan)."""
    from conftest import direct_answers, portmap_state

    from nullbill.render import strip_notes

    probed(app, "small")
    client.call("/api/choices", {"workloads": [], "backups": False, "owner_login": "owner-a@example.internal"})
    built(app, client)
    plan = strip_notes(app.saved_plan())
    answers = direct_answers(plan, portmap_state(**state), peers=peers, pongs=pongs)
    asked = []

    def host(target, command):
        asked.append(command)
        return (0, answers[command], "") if command in answers else (1, "", "unexpected")

    monkeypatch.setattr(app, "host_run", host)
    return asked, plan


def test_the_path_is_measured_on_the_host_while_the_page_asks(site, monkeypatch):
    from conftest import tailscale_peer

    app, client = site
    sign_in(app, client)
    closed = {"method": None, "external": None, "why": {"UPnP": "no answer"}}
    # A PC at home in use, and a phone on mobile data that is idle until the server pings it.
    peers = [tailscale_peer("desktop", "192.168.1.30:41641"), tailscale_peer("phone")]
    asked, plan = on_the_server(app, client, monkeypatch, peers, {"phone": "203.0.113.80:41641"}, **closed)
    found = client.call("/api/path")[1]["direct"]
    assert (found["verdict"], found["live"], found["kind"]) == ("measured", True, "closed")
    assert found["devices"] == [
        {"name": "desktop", "path": "direct", "where": "home"},
        {"name": "phone", "path": "direct", "where": "away"},
    ]
    assert found["seen"]["names"] == ["phone"] and "203.0.113.80" not in json.dumps(found)
    assert asked == [direct.vpn_command(plan), direct.ping_command(plan, ["100.64.0.3"]), direct.router_command(plan)]
    # Kept as apply and verify keep it, and not read again within seconds.
    assert direct.recorded(app.home)["verdict"] == "measured" and client.call("/api/state")[1]["direct"]["verdict"] == "measured"
    client.call("/api/path")
    assert len(asked) == 3
    # While a job is at work on the host the page shows the last reading.
    app.path_seen = None
    slow = app.home.parent / "slow.py"
    slow.write_text("import time; time.sleep(5)\n")
    app.command = [sys.executable, str(slow)]
    job = client.call("/api/jobs", {"action": "verify"})[1]
    found = client.call("/api/path")[1]["direct"]
    assert (found["verdict"], found["live"]) == ("measured", False) and len(asked) == 3
    app.jobs[job["id"]].process.kill()


def test_the_page_hears_that_something_changed_on_the_server(site):
    app, client = site
    sign_in(app, client)
    first = client.call("/api/state")[1]
    assert first["stamp"] == client.call("/api/status")[1]["stamp"] and time.time() - first["at"] < 60
    # Another device saves choices, a run ends, verify records something: each moves the stamp.
    client.call("/api/choices", {"workloads": [], "owner_login": "owner-a@example.internal"})
    saved = client.call("/api/status")[1]["stamp"]
    assert saved != first["stamp"] and client.call("/api/state")[1]["stamp"] == saved
    job = client.call("/api/jobs", {"action": "verify"})[1]
    wait(client, job["id"])
    ran = client.call("/api/status")[1]["stamp"]
    assert ran != saved
    (app.home / "names.json").write_text("{}\n")
    assert client.call("/api/status")[1]["stamp"] != ran
    # Reading the path writes its own record, which must not make the page load everything again.
    now = client.call("/api/status")[1]["stamp"]
    direct.record(app.home, direct.Finding(direct.WAITING, "the vpn guest is not on the tailnet yet"))
    assert client.call("/api/status")[1]["stamp"] == now


CHECKS = """import sys, time
print("--- stage verify, " + time.strftime("%Y-%m-%d %H:%M:%S"))
print("PASS     lan: 8006 closed")
print("DEGRADED backups: switched off; nothing is backed up")
print("SKIP     agent: no credential saved")
print("PASS 1, FAIL 0, SKIP 1, DEGRADED 1")
"""


def test_the_checks_shown_are_the_latest_runs_with_what_did_not_pass(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["checks"] is None
    probed(app, "small")
    client.call("/api/choices", {"workloads": [], "backups": False, "owner_login": "owner-a@example.internal"})
    script = app.home.parent / "checks.py"
    script.write_text(CHECKS)
    app.command = [sys.executable, str(script)]
    job = wait(client, setup(client)[1]["id"])
    # No check failed, and the run still says what is degraded and what did not run.
    assert job["progress"]["verify"]["skipped"] == [{"name": "agent", "detail": "no credential saved"}]
    checks = client.call("/api/state")[1]["checks"]
    assert time.time() - checks.pop("at") < 60
    assert checks == {
        "id": job["id"],
        "action": "apply",
        "only": False,
        "counts": {"PASS": 1, "FAIL": 0, "SKIP": 1, "DEGRADED": 1},
        "degraded": ["backups"],
    }
    # A later run of one other stage checked nothing: the setup run's checks stay the latest.
    script.write_text("print('ran')\n")
    staged = wait(client, client.call("/api/jobs", {"action": "apply", "stage": "router"})[1]["id"])
    assert client.call("/api/state")[1]["checks"]["id"] == job["id"]
    # And that run is told apart from a setup run wherever it is listed.
    assert staged["only"] == {"name": "router", "text": "Set up the router"} and job["only"] is None
    assert [s["name"] for s in staged["progress"]["steps"]] == [progress.PREPARE, "router"]
    runs = client.call("/api/state")[1]["runs"]
    assert [r["only"] and r["only"]["name"] for r in runs[:2]] == ["router", None]


def test_the_owner_switches_off_asking_the_home_router_on_the_page(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["port_mapping"] is True
    status, data = client.call("/api/port-mapping", {"enabled": False})
    assert status == 400 and "save your choices" in data["error"]
    choices = {"workloads": ["agents"], "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", choices)
    assert client.call("/api/port-mapping", {"enabled": False}) == (200, {"ok": True, "port_mapping": False})
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    assert config["tailnet"] == {"port_mapping": False} and build_model(config)["tailnet"]["port_mapping"] is False
    # Saving the choices again keeps it.
    client.call("/api/choices", choices)
    assert client.call("/api/state")[1]["port_mapping"] is False
    # On is the catalog's default: nothing stays in config.yaml.
    assert client.call("/api/port-mapping", {"enabled": True}) == (200, {"ok": True, "port_mapping": True})
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    assert "tailnet" not in config and build_model(config)["tailnet"]["port_mapping"] is True


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
    status, data = client.call("/api/status")
    assert status == 200 and data.pop("stamp") and data == {"monitoring": "not planned"}
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
    # Every answer says when Prometheus was read.
    assert time.time() - data.pop("at") < 60 and data.pop("stamp")
    assert status == 200 and data == {
        "monitoring": "ok",
        "alerts": [
            {"name": "TargetDown", "severity": "page", "summary": "forgejo is down"},
            {"name": "RebootRequired", "severity": "warn", "summary": "pve needs a reboot"},
        ],
        "targets": 2,
        "down": ["forgejo:9100"],
        "answering": ["pbs:9100"],
        # Nothing runs on demand in this plan.
        "off": [],
        "asks": [],
        "security_waiting": [],
        "reboot_waiting": ["pve:9100"],
    }
    assert all(url.startswith("http://10.42.1.14:9090/api/v1/") for url in asked)

    def unreachable(url):
        raise OSError("no route to host")

    app.fetch = unreachable
    data = client.call("/api/status")[1]
    assert time.time() - data.pop("at") < 60 and data.pop("stamp")
    assert data == {"monitoring": "unreachable", "error": "no route to host"}
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
    assert items["grafana"]["url"] == "https://10.42.1.14:3000"
    assert (items["grafana"]["user"], items["grafana"]["password"]) == ("admin", "grafana")
    assert (items["forgejo"]["url"], items["forgejo"]["user"]) == ("https://10.42.2.17/", "nullbill-admin")
    steps = {name: [st.get("code", "") + " " + st["text"] for st in s["steps"]] for name, s in items.items()}
    assert items["workspace"]["addresses"] == ["ssh root@10.42.3.19"] and any("pct enter 2019" in st for st in steps["workspace"])
    assert any(st.startswith("pct enter 2016\nsu - agent") for st in steps["operator"])
    # Until a key is saved, the steps that need one are not done and link to where it goes.
    (key_step,) = [st for st in items["workspace"]["steps"] if st.get("href") == "#access/ssh"]
    assert key_step["done"] is False and key_step["link"] == "Access, SSH keys"
    assert any("runs-on: linux" in st for st in steps["linux-runner"]) and items["linux-runner"]["watch"] == "runner-dispatcher"
    assert any("Two Factor" in st for st in steps["proxmox"])
    jellyfin = items["jellyfin"]
    assert (jellyfin["url"], jellyfin["user"], jellyfin["password"]) == ("http://10.42.7.24:8096/", "nullbill-admin", "jellyfin")
    assert jellyfin["watch"] == "jellyfin" and any("Keep a disk" in st["text"] for st in jellyfin["steps"])
    background = ["router", "internet proxy", "certificate authority", "PostgreSQL", "CI dispatcher", "Tailscale entry", "backup server"]
    assert services["background"] == background
    # Once setup has the tailnet resolving names, each leads with its name
    # and keeps the address as a fallback.
    from nullbill import names

    names.record(app.home, "nullbill.internal", "10.42.6.1")
    items = {s["name"]: s for s in client.call("/api/state")[1]["services"]["items"]}
    assert items["proxmox"]["url"] == "https://pve.nullbill.internal:8006"
    assert items["proxmox"]["addresses"] == ["By address: https://10.42.1.254:8006"]
    assert items["grafana"]["url"] == "https://monitoring.nullbill.internal:3000"
    assert items["forgejo"]["url"] == "https://forgejo.nullbill.internal/"
    assert items["forgejo"]["addresses"] == ["Git over SSH: git@forgejo.nullbill.internal", "By address: https://10.42.2.17/"]
    assert items["jellyfin"]["url"] == "http://jellyfin.nullbill.internal:8096/"
    assert items["workspace"]["addresses"] == ["ssh root@workspace.nullbill.internal", "By address: ssh root@10.42.3.19"]
    names.forget(app.home)
    legacy_plan = yaml.safe_load((golden.parent / "large-legacy.plan.yaml").read_text())
    (app.home / "plan.yaml").write_text(yaml.safe_dump(legacy_plan))
    files = next(s for s in client.call("/api/state")[1]["services"]["items"] if s["name"] == "files")
    assert files["addresses"] == ["\\\\10.42.1.23\\data"] and files["password"] == "files"
    jellyfin = next(s for s in client.call("/api/state")[1]["services"]["items"] if s["name"] == "jellyfin")
    assert any("library for each disk you kept (/media/data)" in st["text"] for st in jellyfin["steps"])
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
    status, data = setup(client, "remove-guests")
    assert status == 400 and "not waiting to delete anything" in data["error"]
    (app.home / "removals.json").write_text('{"guests": ["windows-dev"]}\n')
    assert client.call("/api/state")[1]["removals"] == ["windows-dev"]
    status, job = setup(client, "remove-guests")
    assert status == 200 and job["args"] == ["apply", "--remove-guests"]
    wait(client, job["id"])
    # Ticked again and saved: the plan has it, so nothing waits on the owner.
    (app.home / "removals.json").write_text('{"guests": ["jellyfin", "forgejo"], "gone": ["jellyfin"]}\n')
    (app.home / "plan.yaml").write_text("guests:\n  jellyfin: {kind: lxc}\n  forgejo: {kind: lxc}\n")
    assert client.call("/api/state")[1]["removals"] == ["forgejo"]  # forgejo must be made again: still asks
    (app.home / "plan.yaml").write_text("guests:\n  forgejo: {kind: lxc}\n")
    assert client.call("/api/state")[1]["removals"] == ["jellyfin", "forgejo"]


def test_closing_the_lan_needs_the_page_over_tailscale_and_then_shuts_the_lan_out(site, monkeypatch):
    from nullbill import lan

    app, client = site
    sign_in(app, client)
    vpn = {"address": "10.42.6.11"}
    monkeypatch.setattr(app, "network", lambda: ("10.42.0.0/16", vpn["address"]))
    state = client.call("/api/state")[1]["lan"]
    assert state == {"state": "open", "via_vpn": False, "minutes": 15, "left_s": None, "lapsed": None}
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


def test_a_close_nobody_confirmed_is_shown_open_again_once_the_host_says_so(site, monkeypatch):
    from nullbill import lan

    app, client = site
    sign_in(app, client)
    (app.home / "config.yaml").write_text("target:\n  ssh: root@192.0.2.10\n")
    (app.home / "plan.yaml").write_text("zones:\n  mgmt:\n    host_ip: 10.42.1.254\nguests: {}\n")
    timer = ["armed"]
    asked = []
    monkeypatch.setattr(app, "host_run", lambda target, command: asked.append(command) or (0, timer[0] + "\n", ""))
    # While the time runs the page counts it down and asks nobody.
    lan.write(app.home, lan.CLOSING, time.time() + 600)
    view = client.call("/api/state")[1]["lan"]
    assert view["state"] == "closing" and 590 < view["left_s"] <= 600 and asked == []
    # Past the deadline only the host knows. Its timer still there: closing, with no time left.
    ran_out = time.time() - 60
    lan.write(app.home, lan.CLOSING, ran_out)
    view = client.call("/api/state")[1]["lan"]
    assert (view["state"], view["left_s"]) == ("closing", 0) and asked == [lan.ARMED]
    # Not asked again at every load of the page.
    client.call("/api/state")
    assert asked == [lan.ARMED]
    # The timer ran and is gone: the home network is open again, and the page says since when.
    timer[0], app.lan_asked = "gone", 0.0
    view = client.call("/api/state")[1]["lan"]
    assert (view["state"], view["left_s"], view["lapsed"]) == ("open", None, ran_out)
    assert lan.read(app.home) == {"state": "open", "lapsed": ran_out}


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


def probed_ntfs(app, state, journal="jb95e30db0b42"):
    """The large class, read again, with how Windows left the old disk's NTFS."""
    path = app.home / "probe" / "latest" / "facts.json"
    facts = json.loads(path.read_text())
    part = next(d for d in facts["disks"] if d["id"] == OLD)["partitions"][1]
    part["rw_state"], part["journal"] = state, journal
    path.write_text(json.dumps(facts))


def write_of(client):
    return {d["id"]: d for d in client.call("/api/state")[1]["disks"]}[OLD]["write"]


def test_changing_files_waits_for_a_disk_windows_shut_down_fully(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    keep = {"choice": "keep", "rw": True}
    # Not read yet (no ntfs-3g on the host before the first setup): allowed; setup checks.
    assert write_of(client) == {"state": None, "discard": False, "can_discard": False}
    probed_ntfs(app, "clean")
    assert write_of(client)["state"] == "clean"
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: keep}})[0] == 200
    assert pins(app) == {OLD: {"use": "legacy", "mode": "rw"}}
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep"}}})
    # States only Windows mends: refused beside the control, with what to do.
    for state, said in (("hibernated", "Start that Windows"), ("chkdsk", "run chkdsk /f"), ("damaged", "Copy what you need off it")):
        probed_ntfs(app, state)
        assert write_of(client) == {"state": state, "discard": False, "can_discard": False}
        status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: keep | {"discard": True, "discard_confirm": "D_0002"}}})
        assert status == 400 and "its files cannot be changed yet" in data["error"] and said in data["error"]
        assert pins(app) == {OLD: {"use": "legacy"}}


def test_what_windows_left_unfinished_is_discarded_only_on_the_owners_typed_word(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    probed_ntfs(app, "unclean")
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep"}}})
    assert write_of(client) == {"state": "unclean", "discard": False, "can_discard": True}
    keep = {"choice": "keep", "rw": True}
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: keep}})
    assert status == 400 and "Windows did not shut it down fully" in data["error"]
    assert "or tick Discard what Windows left unfinished" in data["error"]
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: keep | {"discard": True, "discard_confirm": "0002"}}})
    assert (status, data["error"]) == (400, f"{OLD}: type D_0002, the end of its name, to discard what Windows left unfinished on it")
    assert pins(app) == {OLD: {"use": "legacy"}}
    # The unsaved form is planned before the word is typed, and writes nothing.
    status, data = client.call("/api/choices/preview", {**OWNER, "disks": {OLD: keep | {"discard": True}}})
    assert status == 200 and data["errors"] == [] and pins(app) == {OLD: {"use": "legacy"}}
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: keep | {"discard": True, "discard_confirm": " D_0002 "}}})
    assert status == 200 and data["plan"]["errors"] == []
    assert pins(app) == {OLD: {"use": "legacy", "mode": "rw", "discard_journal": ["jb95e30db0b42"]}}
    assert write_of(client) == {"state": "unclean", "discard": True, "can_discard": True}
    # Saved as it is again, nothing to type; the pin stays.
    assert client.call("/api/choices", {**OWNER, "disks": {OLD: keep | {"discard": True}}})[0] == 200
    assert pins(app)[OLD]["discard_journal"] == ["jb95e30db0b42"]
    # Windows used the disk since: another journal, which nobody confirmed.
    probed_ntfs(app, "unclean", "j1f63daad72c7")
    assert write_of(client)["discard"] is False
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: keep | {"discard": True}}})
    assert status == 400 and "type D_0002" in data["error"]
    # Setup made it read-write: the page says so before the hardware is read again,
    probed_ntfs(app, "unclean")
    mount = {"disk": OLD, "n": 2, "mode": "rw", "name": "data", "mount": "/mnt/nb-legacy/data"}
    (app.home / "disks.json").write_text(json.dumps({"mounts": [mount], "unconverted": []}))
    assert write_of(client) == {"state": "editable", "discard": False, "can_discard": False}
    # and once it is, the spent confirmation goes with the next change.
    (app.home / "disks.json").unlink()
    probed_ntfs(app, "clean", "j7d2c7ac4888b")
    client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep"}}})
    assert pins(app) == {OLD: {"use": "legacy"}}
    # A journal nobody could read cannot be named.
    probed_ntfs(app, "unclean", None)
    assert write_of(client) == {"state": "unclean", "discard": False, "can_discard": False}
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: keep | {"discard": True, "discard_confirm": "D_0002"}}})
    assert status == 400 and "its journal could not be read" in data["error"]


def test_only_ntfs_can_be_changed(site):
    app, client = site
    sign_in(app, client)
    probed(app)
    path = app.home / "probe" / "latest" / "facts.json"
    facts = json.loads(path.read_text())
    next(d for d in facts["disks"] if d["id"] == OLD)["partitions"][1]["fstype"] = "exfat"
    path.write_text(json.dumps(facts))
    assert write_of(client) == {"state": "other", "why": "partition 2 holds exFAT", "discard": False, "can_discard": False}
    status, data = client.call("/api/choices", {**OWNER, "disks": {OLD: {"choice": "keep", "rw": True}}})
    assert (status, data["error"]) == (400, f"{OLD}: its files cannot be changed: partition 2 holds exFAT, and only NTFS can be changed")


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
    want = {"url": f"https://{name}:4000/v1", "name": name, "ip": "10.42.4.21", "model": "bonsai-2-27b", "key_set": False}
    assert state["gateway"] == want
    gateway = {"name": "model-gateway"}
    assert client.call("/api/password-reveal", gateway) == (400, {"error": "run setup first; it makes the password"})
    app.codec(app.home).data["model_gateway_key"] = "sk-gateway-key"
    assert client.call("/api/password-reveal", gateway) == (200, {"password": "sk-gateway-key"})
    item = next(s for s in state["services"]["items"] if s["name"] == "model-gateway")
    assert item["addresses"] == ["From your devices: https://10.42.4.21:4000/v1", f"From the workspace: {want['url']}"]
    assert (item["password"], item["watch"]) == ("model-gateway", "model-gateway") and "bonsai-2-27b" in item["what"]
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


def test_the_app_files_are_served_with_their_types(site):
    _, client = site
    for path, (_name, kind) in web.STATIC.items():
        status, _ = client.call(path)
        assert status == 200, path
        assert client.headers["Content-Type"].startswith(kind), path
        assert ("charset" in client.headers["Content-Type"]) == (not kind.endswith("png")), path
    manifest = json.loads((Path(web.__file__).parent / "static" / "manifest.webmanifest").read_text())
    for icon in manifest["icons"]:
        assert icon["src"] in web.STATIC
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])
    sw = (Path(web.__file__).parent / "static" / "sw.js").read_text()
    # The worker stores the page's own files only, and knows the versioned
    # ones by the names the server gives them.
    plain = json.loads(sw.split("const PLAIN = ")[1].split(";")[0])
    assert plain and set(plain) <= set(web.STATIC) and not any(p.startswith("/api/") for p in plain)
    for name in web.VERSIONED:
        assert name.replace(".", "\\.") in sw.split("const VERSIONED = ")[1].split("\n")[0], name


def test_the_page_holds_every_element_and_icon_its_script_asks_for():
    """A control taken off the page while the script still reaches for it
    stops the whole page at load, and no other check opens a browser."""
    static = Path(web.__file__).parent / "static"
    page, script = (static / "index.html").read_text(), (static / "app.js").read_text()
    ids = set(re.findall(r'\bid="([^"]+)"', page))
    # The rows of What to run are made by the script (`opt-<name>`).
    asked = {i for i in re.findall(r'\$\("([^"]+)"\)', script) if not i.startswith("opt-")}
    assert asked - ids == set()
    icons = {i for i in ids if i.startswith("i-")}
    used = set(re.findall(r'href="#(i-[a-z-]+)"', page)) | {f"i-{n}" for n in re.findall(r'\bicon(?:\(|: )"([a-z-]+)"', script)}
    assert used - icons == set()


def test_connections_stay_open_and_close_without_a_reset(site):
    """Phones on mobile data reach the page through Tailscale's userspace
    stack, which turns a reset into a lost page: one connection carries
    several requests, a refused POST leaves nothing behind, and a closing
    connection reads what the client still sends."""
    app, client = site
    conn = http.client.HTTPSConnection("127.0.0.1", client.port, context=client.tls, timeout=10)
    for _ in range(3):
        conn.request("GET", "/api/state")
        response = conn.getresponse()
        assert response.status == 200 and response.version == 11
        response.read()
    conn.request("POST", "/api/login", body=b'{"secret": "x"}', headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    assert response.status == 403
    response.read()
    conn.request("GET", "/api/state")
    assert conn.getresponse().status == 200
    conn.close()
    raw = socket.create_connection(("127.0.0.1", client.port), timeout=10)
    tls = client.tls.wrap_socket(raw, server_hostname="nullbill")
    tls.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    # The page has answered and closed its side; the client still sends
    # (as a browser's TLS alert would) before it reads. time.sleep is
    # stubbed in this fixture.
    threading.Event().wait(0.3)
    tls.sendall(b"x" * 5000)
    threading.Event().wait(0.3)
    data = b""
    while chunk := tls.recv(65536):
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = int(re.search(rb"Content-Length: (\d+)", head)[1])
    assert len(body) == length
    tls.close()


# People besides the owner (Access, People).

FRIEND = {"name": "alex", "login": "alex@example.internal", "services": ["forgejo"]}


def with_tailscale(app, client):
    """Signed in, planned and built, with a stand-in for Tailscale's API."""
    from conftest import FakeTailnet

    sign_in(app, client)
    probed(app)
    assert client.call("/api/choices", {**OWNER, "workloads": ["agents", "linux-ci", "media"]})[0] == 200
    api = FakeTailnet()
    app.tailnet_client = lambda _id, _secret: api
    secret = "tskey-client-kExampleCNTRL-" + "f" * 30
    assert client.call("/api/tailscale-client", {"client_id": "kExampleCNTRL", "secret": secret})[0] == 200
    return api


def built(app, client):
    job = setup(client)[1]
    assert wait(client, job["id"])["state"] == "done"


def people_of(app):
    return yaml.safe_load((app.home / "config.yaml").read_text())["people"]


def test_a_person_added_before_the_first_setup_run_waits_for_it(site):
    app, client = site
    assert client.call("/api/people")[0] == 401 and client.call("/api/people", FRIEND)[0] == 401
    api = with_tailscale(app, client)
    view = client.call("/api/people")[1]
    assert [p["name"] for p in view["people"]] == ["owner"] and view["limit"] == {"used": 1, "max": 6}
    assert view["tailscale"] == {"waits": "People you add now get their access with the first setup run.", "in_place": None, "error": None}
    assert {"forgejo", "model-gateway"} <= {s["name"] for s in view["shareable"]}
    status, saved = client.call("/api/people", FRIEND)
    assert status == 200 and saved["added"] and saved["message"] == "alex added. It takes effect with the next setup run."
    assert api.pushes == []
    assert people_of(app)["alex"] == {"role": "friend", "login": "alex@example.internal", "services": ["forgejo"]}
    alex = saved["people"][1]
    assert alex["services"] == ["forgejo"] and alex["devices"] is None
    assert "sign in as alex@example.internal" in alex["instructions"] and "https://10.42.2.17/" in alex["instructions"]
    # The root to send along exists once setup's ca stage made it.
    assert alex["root"] is False and "nullbill-root.crt" not in alex["instructions"]
    browserca.root(app.home, "nullbill.internal", "10.42.0.0/16")
    alex = client.call("/api/people")[1]["people"][1]
    assert alex["root"] is True and "nullbill-root.crt" in alex["instructions"]


def test_a_person_added_after_setup_is_in_force_at_once(site):
    app, client = site
    api = with_tailscale(app, client)
    built(app, client)
    config = app.home / "config.yaml"
    started = max(job.started for job in app.jobs.values())
    os.utime(config, (started - 60, started - 60))
    status, saved = client.call("/api/people", FRIEND)
    assert status == 200 and saved["message"] == "alex added. In force now."
    assert saved["tailscale"] == {"waits": None, "in_place": True, "error": None}
    # Tailscale has it, so it is no choice that waits for a setup run.
    assert client.call("/api/state")[1]["config_changed"] is False
    (policy,) = api.pushes
    grant = next(g for g in policy["grants"] if g["src"] == ["alex@example.internal"])
    assert sorted(grant["ip"]) == ["tcp:22", "tcp:443"]
    assert any(t["src"] == "alex@example.internal" and t["deny"] for t in policy["tests"])
    # Their devices show as soon as they join; a login nobody added is named.
    api.devices_list += [
        {
            "user": "Alex@example.internal",
            "hostname": "alex-phone",
            "os": "iOS",
            "connectedToControl": True,
            "lastSeen": "2026-11-09T10:00:00Z",
        },
        {"user": "who@example.internal", "hostname": "laptop", "os": "linux", "lastSeen": "2026-11-08T10:00:00Z"},
    ]
    view = client.call("/api/people?fresh=1")[1]
    assert view["people"][1]["devices"] == [{"name": "alex-phone", "os": "iOS", "seen": 1794218400.0, "connected": True}]
    assert view["strangers"] == ["who@example.internal"]
    # More services, same person: one more push, nothing else.
    status, saved = client.call("/api/people", {**FRIEND, "services": ["jellyfin", "forgejo"]})
    assert status == 200 and saved["added"] is False and saved["message"] == "alex saved. In force now."
    assert people_of(app)["alex"]["services"] == ["forgejo", "jellyfin"] and len(api.pushes) == 2
    grants = [[g["ip"] for g in push["grants"] if g["src"] == ["alex@example.internal"]] for push in api.pushes]
    assert grants == [[["tcp:443", "tcp:22"]], [["tcp:443", "tcp:22"], ["tcp:8096"]]]


def test_a_refusal_with_several_reasons_lists_them():
    one = web.refusal(NullBillError("name the person"))
    assert one == {"error": "name the person"}
    several = web.refusal(Refused("Nothing was saved", ["a", "b"]))
    assert several == {"error": "Nothing was saved: a; b", "headline": "Nothing was saved", "errors": ["a", "b"]}
    # Reasons that come without a headline still get one, since a list needs it.
    assert web.refusal(NullBillError("a", "b")) == {"error": "a; b", "headline": "That did not work", "errors": ["a", "b"]}


def test_what_tailscale_refuses_is_not_saved(site):
    app, client = site
    api = with_tailscale(app, client)
    built(app, client)
    api.refuse = "test(s) failed"
    status, answer = client.call("/api/people", FRIEND)
    assert status == 400 and answer == {"error": "Tailscale did not take it, so nothing changed: test(s) failed"}
    assert list(people_of(app)) == ["owner"] and api.pushes == []
    # Several reasons come as a list under what did not happen, for the
    # page to show as one, and still in one line for a reader of `error`.
    tests = [f"a policy test failed for alex@example.internal: address 10.42.2.17:{port}: want: Accept, got: Drop" for port in (443, 22)]
    api.refuse = tests
    status, answer = client.call("/api/people", FRIEND)
    assert status == 400 and answer == {
        "error": f"Tailscale did not take it, so nothing changed: {'; '.join(tests)}",
        "headline": "Tailscale did not take it, so nothing changed",
        "errors": tests,
    }
    assert list(people_of(app)) == ["owner"] and api.pushes == []
    api.refuse = None
    for bad, why in (
        ({**FRIEND, "name": "Al ex"}, "lowercase letters"),
        ({**FRIEND, "login": "alex"}, "email address"),
        ({**FRIEND, "login": "OWNER-A@example.internal"}, "is the login of you already"),
        ({**FRIEND, "services": ["monitoring"]}, "monitoring cannot be given to a person"),
        ({**FRIEND, "name": "owner"}, "owner is you"),
    ):
        status, answer = client.call("/api/people", bad)
        assert status == 400 and why in answer["error"], bad
    assert list(people_of(app)) == ["owner"] and api.pushes == []
    # The free plan's six people, the owner among them.
    for n in range(5):
        assert client.call("/api/people", {"name": f"friend-{n}", "login": f"f{n}@example.internal"})[0] == 200
    status, answer = client.call("/api/people", FRIEND)
    assert status == 400 and "6" in answer["error"] and "alex" not in people_of(app)
    assert client.call("/api/people")[1]["limit"] == {"used": 6, "max": 6}


def test_a_removed_person_loses_access_and_their_tailscale_account(site):
    app, client = site
    api = with_tailscale(app, client)
    built(app, client)
    assert client.call("/api/people", FRIEND)[0] == 200
    api.users_list = [
        {"id": "u1", "loginName": "owner-a@example.internal", "role": "owner"},
        {"id": "u2", "loginName": "alex@example.internal", "role": "member"},
    ]
    assert client.call("/api/people/remove", {"name": "owner"})[0] == 400
    assert client.call("/api/people/remove", {"name": "nobody"})[0] == 400
    status, gone = client.call("/api/people/remove", {"name": "alex"})
    assert status == 200 and [p["name"] for p in gone["people"]] == ["owner"]
    assert gone["message"] == "alex can open nothing any more. Their account and devices are off your Tailscale network."
    assert api.deleted == ["u2"] and "alex@example.internal" not in json.dumps(api.pushes[-1])
    # Never joined: nothing to delete. No Users scope: the owner is told where to do it.
    assert client.call("/api/people", FRIEND)[0] == 200
    assert "They had not joined" in client.call("/api/people/remove", {"name": "alex"})[1]["message"]
    assert client.call("/api/people", FRIEND)[0] == 200
    api.users_scope = False
    message = client.call("/api/people/remove", {"name": "alex"})[1]["message"]
    assert "Remove alex@example.internal on Tailscale's Users page too" in message and "Users, write" in message
    assert list(people_of(app)) == ["owner"] and api.deleted == ["u2"]


def test_a_policy_changed_in_tailscale_is_seen_and_put_back(site):
    app, client = site
    api = with_tailscale(app, client)
    assert client.call("/api/people/sync", {})[0] == 400  # the first setup run does it
    built(app, client)
    assert client.call("/api/people?fresh=1")[1]["tailscale"]["in_place"] is False
    status, answer = client.call("/api/people/sync", {})
    assert status == 200 and answer["message"] == "Tailscale has the policy again." and answer["tailscale"]["in_place"] is True
    assert len(api.pushes) == 1
    api.live = {"acls": []}
    assert client.call("/api/people")[1]["tailscale"]["in_place"] is True  # asked at most twice a minute
    assert client.call("/api/people?fresh=1")[1]["tailscale"]["in_place"] is False
    # Tailscale out of reach: the page says so and still lists the people.
    api.devices = lambda: (_ for _ in ()).throw(web.TailscaleError("Tailscale did not answer"))
    view = client.call("/api/people?fresh=1")[1]
    assert view["tailscale"]["error"] == "Tailscale did not answer" and view["people"][0]["devices"] is None


def test_people_and_jobs_wait_for_each_other(site, tmp_path):
    app, client = site
    with_tailscale(app, client)
    app.people_busy = True
    status, answer = setup(client)
    assert status == 400 and "Access, People" in answer["error"]
    assert client.call("/api/people", FRIEND)[0] == 400
    app.people_busy = False
    release = tmp_path / "release"
    app.command = [sys.executable, "-c", f"import os,time\nwhile not os.path.exists({str(release)!r}): time.sleep(0.02)"]
    job = setup(client)[1]
    status, answer = client.call("/api/people", FRIEND)
    assert status == 400 and answer["error"] == "wait until the running job finishes"
    release.write_text("")
    wait(client, job["id"])
    assert client.call("/api/people", FRIEND)[0] == 200


# On demand (nullbill/power.py): When it runs under What to run, and the
# Services page's switches, which go to nb-power on the host.
NO_BACKUPS = {"owner_login": "owner-a@example.internal", "backups": False}


def power_of(app):
    return yaml.safe_load((app.home / "config.yaml").read_text()).get("power")


def test_when_it_runs_saves_only_what_differs_from_the_catalog_and_the_meter_follows(site):
    app, client = site
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    choices = {**NO_BACKUPS, "workloads": ["agents", "linux-ci", "media"]}
    client.call("/api/choices", choices)
    options = client.call("/api/state")[1]["options"]
    rows = {w["name"]: w for w in options["power"]}
    assert list(rows) == ["agents", "linux-ci", "media"] and client.call("/api/state")[1]["on_demand"] is None
    assert rows["media"] == {
        "name": "media",
        "title": "Jellyfin",
        "run": "always",
        "stop": "auto",
        "idle_minutes": 30,
        "guests": ["jellyfin"],
        "memory_mib": 1024,
        "blocked": None,
        "auto": True,
        "auto_why": None,
    }
    always = options["memory"]
    assert always["demand_mib"] == 0 and always["together"] is True
    # The form, not saved: Jellyfin on demand leaves its memory to share.
    form = {
        "media": {"run": "on-demand", "stop": "auto", "idle_minutes": 30},
        "agents": {"run": "always", "stop": "ask", "idle_minutes": 120},
    }
    view = client.call("/api/choices/preview", {**choices, "power": form})[1]
    memory = view["memory"]
    assert memory["chosen_mib"] == always["chosen_mib"] - 1024 and memory["free_mib"] == always["free_mib"] + 1024
    assert (memory["demand_mib"], memory["demand_largest_mib"], memory["together"]) == (1024, 1024, True)
    assert power_of(app) is None
    # Saved: config.yaml gets the one thing that is not the catalog's.
    assert client.call("/api/choices", {**choices, "power": form})[0] == 200
    assert power_of(app) == {"media": {"run": "on-demand"}}
    state = client.call("/api/state")[1]
    assert state["on_demand"]["groups"] == [
        {"name": "media", "title": "Jellyfin", "stop": "auto", "idle_minutes": 30, "memory_mib": 1024, "guests": ["jellyfin"]}
    ]
    assert state["on_demand"]["together"] is True and "run: on-demand" in state["plan"]["text"]
    # Minutes: a number in range when saving; a half-typed one does not break the preview.
    odd = {**choices, "power": {"media": {"run": "on-demand", "stop": "ask", "idle_minutes": "3"}}}
    status, answer = client.call("/api/choices", odd)
    assert status == 400 and "Jellyfin: the time without use is a number of minutes from 5 to 10080" in answer["error"]
    assert client.call("/api/choices/preview", odd)[1]["errors"] == []
    # What the page does not set stays; back at the catalog's rule, the key goes.
    config = yaml.safe_load((app.home / "config.yaml").read_text())
    config["power"]["media"]["busy"] = {"cpu_pct": 20}
    (app.home / "config.yaml").write_text(yaml.safe_dump(config))
    client.call("/api/choices", {**choices, "power": {"media": {"run": "on-demand", "stop": "manual", "idle_minutes": 45}}})
    assert power_of(app) == {"media": {"busy": {"cpu_pct": 20}, "run": "on-demand", "stop": "manual", "idle_minutes": 45}}
    client.call(
        "/api/choices",
        {**choices, "power": {"media": {"run": "always", "stop": "auto", "idle_minutes": 30}, "bogus": {"run": "on-demand"}}},
    )
    assert power_of(app) == {"media": {"busy": {"cpu_pct": 20}}}
    # A workload the form does not show keeps its rule.
    client.call("/api/choices", {**choices, "power": {"agents": {"run": "on-demand", "stop": "ask", "idle_minutes": 120}}})
    assert power_of(app) == {"media": {"busy": {"cpu_pct": 20}}, "agents": {"run": "on-demand"}}


def test_a_vm_alone_never_stops_by_itself_and_a_pool_alone_cannot_run_on_demand(site):
    app, client = site
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    view = client.call("/api/choices/preview", {**NO_BACKUPS, "workloads": ["windows-dev", "windows-ci"]})[1]
    rows = {w["name"]: w for w in view["power"]}
    assert rows["windows-dev"]["auto"] is False and "busy.cpu_pct" in rows["windows-dev"]["auto_why"]
    assert rows["windows-dev"]["stop"] == "ask" and rows["windows-dev"]["blocked"] is None
    assert rows["windows-ci"]["blocked"] and rows["windows-ci"]["title"] == "Windows CI runners"


class PowerHost:
    """Stands in for nb-power on the host."""

    def __init__(self):
        self.calls = []
        self.answers = []
        self.groups = [
            self.group(
                "media", "Jellyfin", "auto", 1024, "on", busy=["playing on 1 device"], idle_s=0, keep=False, asks=False, setup=False
            ),
            self.group("agents", "Workspace", "ask", 512, "off"),
        ]

    @staticmethod
    def group(name, title, stop, mib, state, **more):
        guest = {"media": "jellyfin", "agents": "workspace"}[name]
        running = "running" if state == "on" else "stopped"
        entry = {
            "name": name,
            "title": title,
            "stop": stop,
            "idle_minutes": 30 if stop == "auto" else 120,
            "memory_mib": mib,
            "state": state,
        }
        return {**entry, "guests": [{"name": guest, "kind": "lxc", "vmid": 2030, "state": running}], **more}

    def status(self):
        used = sum(g["memory_mib"] for g in self.groups if g["state"] == "on")
        events = [{"at": 1700000000, "group": "media", "what": "started", "why": "by the page"}]
        return {
            "now": 1700000100,
            "ticked": 1700000090,
            "headroom_mib": 1280,
            "used_mib": used,
            "free_mib": 1280 - used,
            "setup": False,
        } | {
            "groups": self.groups,
            "events": events,
        }

    def __call__(self, target, command):
        assert target == "root@192.0.2.10"
        self.calls.append(command)
        if command == "/usr/local/sbin/nb-power status --json":
            return 0, json.dumps(self.status()), ""
        rc, answer = self.answers.pop(0)
        return rc, json.dumps(answer), ""

    def acts(self):
        return [c.removeprefix("/usr/local/sbin/nb-power ") for c in self.calls if " status " not in c]


def on_demand_site(app, client):
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    rules = {"media": {"run": "on-demand"}, "agents": {"run": "on-demand"}}
    assert client.call("/api/choices", {**NO_BACKUPS, "workloads": ["agents", "linux-ci", "media"], "power": rules})[0] == 200
    assert client.call("/api/state")[1]["plan"]["errors"] == []
    host = PowerHost()
    app.host_run = app.host_act = host
    return host


def test_the_services_page_shows_what_runs_on_demand_and_asks_the_host_only_once_built(site):
    app, client = site
    assert client.call("/api/on-demand")[0] == 401
    host = on_demand_site(app, client)
    view = client.call("/api/on-demand")[1]["view"]
    assert view["live"] is False and "run setup" in view["why"] and host.calls == []
    assert [(g["name"], g["state"]) for g in view["groups"]] == [("agents", "unknown"), ("media", "unknown")]
    built(app, client)
    host.groups[1]["idle_minutes"] = 60
    view = client.call("/api/on-demand")[1]["view"]
    assert view["live"] is True and view["why"] is None and (view["headroom_mib"], view["used_mib"]) == (1280, 1024)
    media, agents = view["groups"]
    assert (
        media["state"] == "on"
        and media["busy"] == ["playing on 1 device"]
        and media["guests"] == [{"name": "jellyfin", "state": "running"}]
    )
    # The host's rule for the workspace is older than the saved one: the next setup run brings it.
    assert agents["state"] == "off" and "next" not in media and agents["next"] == "rules"
    assert view["events"] == [{"at": 1700000000, "title": "Jellyfin", "what": "started", "why": "by the page"}]
    # Asked again within seconds, the host is not.
    client.call("/api/on-demand")
    assert len(host.calls) == 1
    # A workload the host does not know yet waits for the next setup run.
    host.groups.pop()
    app.power_seen = None
    pending = client.call("/api/on-demand")[1]["view"]["groups"][1]
    assert (pending["name"], pending["state"], pending["next"]) == ("agents", "pending", "on-demand")
    # The host does not answer.
    app.host_run = lambda target, command: (255, "", "ssh: connect to host 192.0.2.10 port 22: No route to host")
    app.power_seen = None
    view = client.call("/api/on-demand")[1]["view"]
    assert view["live"] is False and "does not say what is on" in view["why"] and view["groups"][1]["state"] == "unknown"


def test_start_and_stop_go_to_the_host_and_ask_before_stopping_anything_in_use(site):
    app, client = site
    host = on_demand_site(app, client)
    assert client.call("/api/on-demand", {"action": "start", "name": "agents"})[0] == 400  # nothing is on the server yet
    built(app, client)
    for bad in (
        {"action": "restart", "name": "media"},
        {"action": "start", "name": "forgejo"},
        {"action": "start", "name": "media; reboot"},
    ):
        assert client.call("/api/on-demand", bad)[0] == 400
    assert client.call("/api/on-demand", {"action": "keep", "name": "media"})[0] == 400
    assert client.call("/api/on-demand", {"action": "start", "name": "agents", "stop": ["forgejo"]})[0] == 400
    assert host.acts() == []
    # No room: nothing changes, and the answer says what would have to stop.
    refusal = {"ok": False, "error": "no-room", "name": "agents", "title": "Workspace", "need_mib": 512, "free_mib": 256, "enough": True}
    refusal["stop"] = [
        {"name": "media", "title": "Jellyfin", "memory_mib": 1024, "busy": ["playing on 1 device"], "idle_s": 0, "auto": False}
    ]
    host.answers.append((3, refusal))
    status, answer = client.call("/api/on-demand", {"action": "start", "name": "agents"})
    assert status == 200 and answer["ok"] is False and answer["no_room"] == refusal and answer["view"]["live"]
    # Agreed to: the same start, naming what to stop.
    host.answers.append((0, {"ok": True, "name": "agents", "already": False, "stopped": ["Jellyfin"], "warnings": []}))
    status, answer = client.call("/api/on-demand", {"action": "start", "name": "agents", "stop": ["media"], "keep": True})
    assert status == 200 and answer["ok"] is True and answer["stopped"] == ["Jellyfin"] and answer["title"] == "Workspace"
    assert host.acts() == ["start agents --by 'the page' --json", "start agents --by 'the page' --stop media --keep --json"]
    assert app.power_acting == {}
    # Stopping what is in use asks first, with the reasons.
    status, answer = client.call("/api/on-demand", {"action": "stop", "name": "media"})
    assert status == 200 and answer["ok"] is False and answer["busy"] == ["playing on 1 device"] and len(host.acts()) == 2
    host.answers.append((0, {"ok": True, "name": "media", "already": False}))
    assert client.call("/api/on-demand", {"action": "stop", "name": "media", "force": True})[1]["ok"] is True
    host.answers.append((0, {"ok": True, "name": "media", "keep": True}))
    assert client.call("/api/on-demand", {"action": "keep", "name": "media", "on": True})[1]["keep"] is True
    assert host.acts()[2:] == ["stop media --by 'the page' --json", "keep media on --json"]
    # What nb-power refuses is said in its words.
    host.answers.append((1, {"ok": False, "error": "jellyfin did not start: unable to open file"}))
    status, answer = client.call("/api/on-demand", {"action": "start", "name": "media"})
    assert status == 400 and answer["error"] == "jellyfin did not start: unable to open file"
    # A setup run that started it stops it again itself.
    host.groups[0]["setup"], calls = True, len(host.acts())
    status, answer = client.call("/api/on-demand", {"action": "stop", "name": "media", "force": True})
    assert status == 400 and "a setup run is using Jellyfin" in answer["error"] and len(host.acts()) == calls


def test_a_guest_that_is_off_on_demand_is_not_down_and_an_idle_one_asks(site):
    app, client = site
    on_demand_site(app, client)

    def fetch(url):
        if url.endswith("/alerts"):
            return {"data": {"alerts": []}}
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
        up = [{"metric": {"instance": n}, "value": [0, v]} for n, v in (("jellyfin", "0"), ("forgejo", "0"), ("workspace", "1"))]
        results = {
            "up": up,
            "nb_power_guest_off == 1": [{"metric": {"guest": "jellyfin", "group": "media"}, "value": [0, "1"]}],
            "nb_power_group_idle_seconds and on (group) (nb_power_group_asks == 1)": [
                {"metric": {"group": "agents", "title": "Workspace"}, "value": [0, "7260"]}
            ],
        }
        return {"data": {"result": results.get(query, [])}}

    app.fetch = fetch
    data = client.call("/api/status")[1]
    assert data["down"] == ["forgejo"] and data["off"] == ["jellyfin"] and data["answering"] == ["workspace"]
    assert data["asks"] == [{"name": "agents", "title": "Workspace", "idle_s": 7260}]


def test_the_page_names_its_script_and_styles_by_version(site):
    # A stale copy of app.js, in a browser's or the service worker's cache,
    # cannot answer for the one the updated page asks for.
    _, client = site
    page = client.call("/")[1]
    for name in ("app.js", "style.css", "theme.js"):
        assert f'"/{name}?v={web.shell()}"' in page and f'"/{name}"' not in page
        assert client.call(f"/{name}?v={web.shell()}")[0] == 200
    assert f'<meta name="nb-shell" content="{web.shell()}">' in page


def test_a_restart_of_the_page_signs_nobody_out(site):
    app, client = site
    sign_in(app, client)
    token = client.cookie.split("=", 1)[1]
    saved = (app.home / "web" / "sessions.json").read_text()
    assert token not in saved and stat.S_IMODE((app.home / "web" / "sessions.json").stat().st_mode) == 0o600
    restarted(app)
    assert client.call("/api/state")[1]["auth"] is True
    # Signing out ends it for the next server too, and an expired session is not kept.
    assert client.call("/api/logout", {})[0] == 200
    restarted(app)
    assert client.call("/api/state")[1]["auth"] is False
    (app.home / "web" / "sessions.json").write_text(json.dumps({"0" * 64: time.time() - 1, "1" * 64: time.time() + 60}))
    restarted(app)
    assert list(app.sessions) == ["1" * 64]
    (app.home / "web" / "sessions.json").write_text("not json")
    restarted(app)
    assert app.sessions == {}


def test_setup_starts_only_from_the_review_the_owner_read(site):
    app, client = site
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    saved = {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", saved)
    read = client.call("/api/state")[1]["review"]["id"]
    # A page that does not say what it showed, such as one from before an update, starts nothing.
    for body in ({"action": "apply"}, {"action": "apply", "review": "0" * 16}, {"action": "remove-guests", "guests": []}):
        status, answer = client.call("/api/jobs", body)
        assert status == 409 and answer["review"] is True and "look at it again under Setup, Review" in answer["error"]
    assert not app.jobs or all(j.action == "probe" for j in app.jobs.values())
    # The choices changed after the owner read the review: read again.
    client.call("/api/choices", {**saved, "workloads": ["agents", "linux-ci"]})
    assert client.call("/api/jobs", {"action": "apply", "review": read})[0] == 409
    now = client.call("/api/state")[1]["review"]["id"]
    assert now != read
    status, job = client.call("/api/jobs", {"action": "apply", "review": now})
    assert status == 200 and job["review"] == now and job["by"] == "owner"
    assert wait(client, job["id"])["state"] == "done"
    # The run kept the plan it started with, and which NullBill ran it.
    assert app.jobs[job["id"]].plan_file.read_text() == (app.home / "plan.yaml").read_text()
    assert stat.S_IMODE(app.jobs[job["id"]].plan_file.stat().st_mode) == 0o600
    assert app.jobs[job["id"]].meta()["setup"] == web.version.setup_digest(web.REPO)
    # What it would build is the same, so the id is: reading it again is not asked for.
    assert client.call("/api/state")[1]["review"]["id"] == now
    # One stage, the checks and reading the hardware change nothing the owner has to read first.
    stage = client.call("/api/jobs", {"action": "apply", "stage": "host"})
    assert stage[0] == 200 and not app.jobs[stage[1]["id"]].plan_file.exists()
    wait(client, stage[1]["id"])
    assert client.call("/api/jobs", {"action": "verify"})[0] == 200


def test_the_review_says_why_a_run_is_due_and_what_it_changes(site, tmp_path):
    app, client = site
    sign_in(app, client)
    review = client.call("/api/state")[1]["review"]
    assert review["ready"] is False and review["guests"] == [] and [r["kind"] for r in review["reasons"]] == ["first"]
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    saved = {"workloads": ["agents", "linux-ci"], "backups": False, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", saved)
    review = client.call("/api/state")[1]["review"]
    assert review["ready"] is True and [r["kind"] for r in review["reasons"]] == ["first"]
    assert {g["change"] for g in review["guests"]} == {"create"} and review["difference"] is None and review["sections"] == []
    assert next(g for g in review["guests"] if g["name"] == "forgejo")["kind"] == "lxc"
    job = setup(client)[1]
    assert wait(client, job["id"])["state"] == "done"
    (app.home / "guests.json").write_text(json.dumps({"guests": [g["name"] for g in review["guests"]]}))
    # The run's release stage found every guest it made on today's Debian.
    release.write(app.home, {g["name"]: {"release": "trixie"} for g in review["guests"]})
    review = client.call("/api/state")[1]["review"]
    assert review["reasons"] == [] and {g["change"] for g in review["guests"]} == {"keep"} and review["known"] is True
    # Choices saved after it: what the next run creates and deletes, and every changed line of the plan.
    config = app.home / "config.yaml"
    client.call("/api/choices", {**saved, "workloads": ["agents", "media"]})
    os.utime(config, (time.time() + 5, time.time() + 5))
    review = client.call("/api/state")[1]["review"]
    assert [r["kind"] for r in review["reasons"]] == ["choices"]
    changed = {g["name"]: g for g in review["guests"] if g["change"] != "keep"}
    assert changed["jellyfin"]["change"] == "create" and changed["linux-runner"]["change"] == "delete"
    assert "nothing of yours is on them" in changed["linux-runner"]["detail"][0]
    assert changed["router"]["detail"] == ["its network legs change"]
    assert "+  jellyfin:" in review["difference"] and "-  linux-runner:" in review["difference"]
    assert "firewall rules" in review["sections"]
    # A run that fails, and one the owner stops, leave the server unfinished.
    app.command = [sys.executable, "-c", "import sys; sys.exit(1)"]
    failed = setup(client, "remove-guests", guests=client.call("/api/state")[1]["removals"])[1]
    assert wait(client, failed["id"])["state"] == "failed"
    kinds = {r["kind"]: r["text"] for r in client.call("/api/state")[1]["review"]["reasons"]}
    # (The deletion the owner confirmed is still written down, as after any run that did not get to it.)
    assert set(kinds) == {"unfinished", "removals", "choices"} and "failed before it finished" in kinds["unfinished"]
    meta = app.jobs[failed["id"]].meta_file
    meta.write_text(json.dumps(json.loads(meta.read_text()) | {"stopped": time.time()}))
    assert "was stopped before it finished" in client.call("/api/state")[1]["review"]["reasons"][0]["text"]
    # An auth key that waits for the next run is a reason too.
    assert client.call("/api/vpn", {"key": "tskey-auth-" + "k" * 24})[0] == 200
    assert "key" in [r["kind"] for r in client.call("/api/state")[1]["review"]["reasons"]]


def test_a_server_built_before_the_page_kept_plans_is_not_called_new(site):
    app, client = site
    sign_in(app, client)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    saved = {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", saved)
    job = setup(client)[1]
    wait(client, job["id"])
    app.jobs[job["id"]].plan_file.unlink()
    # Its choices are unchanged: what is planned is what is there.
    review = client.call("/api/state")[1]["review"]
    assert {g["change"] for g in review["guests"]} == {"keep"} and review["known"] is True and review["difference"] is None
    # Changed since, with no record of the guests: nothing is called created or deleted.
    (app.home / "guests.json").unlink(missing_ok=True)
    config = app.home / "config.yaml"
    config.write_text(config.read_text())
    os.utime(config, (time.time() + 5, time.time() + 5))
    review = client.call("/api/state")[1]["review"]
    assert {g["change"] for g in review["guests"]} == {"keep"} and review["known"] is False


KEY = "k" * 32


def written_down(app):
    """What a setup run with the saved choices writes down of each playbook
    it ran (applied.json), as apply's run_playbooks and bake do."""
    codec = app.codec(app.home)
    codec.save(codec.load() | {"applied_key": KEY})
    ctx = app.quiet()
    for s in preview.steps(ctx)[0]:
        inputs = applied.prints(KEY, {}, s.inputs | dict.fromkeys(s.read, "as the run read it"))
        applied.ran(app.home, s.machine, s.playbook, s.stage, "digest", inputs, 12.0)
    for pool in apply.linux_pools(ctx):
        applied.note(app.home, pool, applied.IMAGE, stage="ci", baked=time.time(), settings=apply.image_print(ctx, pool), took_s=300.0)


def test_the_review_says_for_each_machine_what_the_run_does_to_it(site):
    app, client = site
    sign_in(app, client)
    assert client.call("/api/state")[1]["review"]["machines"] is None  # no plan yet
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    saved = {"workloads": ["agents", "linux-ci"], "backups": False, "owner_login": "owner-a@example.internal"}
    client.call("/api/choices", saved)
    review = client.call("/api/state")[1]["review"]
    wait(client, setup(client)[1]["id"])
    (app.home / "guests.json").write_text(json.dumps({"guests": [g["name"] for g in review["guests"]]}))
    # A server built before runs wrote anything down: nothing is called left alone.
    found = client.call("/api/state")[1]["review"]["machines"]
    assert found["known"] is False and found["counts"]["alone"] == 0 and found["counts"]["unknown"] == len(found["machines"])
    stamp = client.call("/api/status")[1]["stamp"]
    written_down(app)
    kept = app.codec(app.home).load()
    assert client.call("/api/status")[1]["stamp"] != stamp  # an open page reads the review anew
    found = client.call("/api/state")[1]["review"]["machines"]
    names = [m["name"] for m in found["machines"]]
    assert found["known"] is True and names[0] == preview.HOST and set(names[1:]) == {g["name"] for g in review["guests"]}
    assert found["counts"]["alone"] == len(names)
    # Other choices: what the run creates, and which neighbours it sets up again for that.
    config = app.home / "config.yaml"
    client.call("/api/choices", {**saved, "workloads": ["agents", "linux-ci", "media"]})
    os.utime(config, (time.time() + 5, time.time() + 5))
    found = client.call("/api/state")[1]["review"]["machines"]
    by_name = {m["name"]: m for m in found["machines"]}
    assert by_name["jellyfin"]["what"] == "create" and by_name["forgejo"]["what"] == "alone"
    router = by_name["router"]
    assert router["what"] == "again" and router["last"]["took_s"] == 12.0
    assert {"what": "the whole machine", "how": "reboot"} in router["restarts"] and "may restart" in router["restart_note"]
    # The page worked that out by itself: no job ran for it, and nothing was saved.
    assert app.codec(app.home).load() == kept and not (app.home / browserca.DIR).exists()
    # The last run's checks failed: the next one goes through every step.
    (app.home / apply.FULL_NEXT).write_text("")
    found = client.call("/api/state")[1]["review"]["machines"]
    assert found["full"] is True and {m["name"]: m for m in found["machines"]}["forgejo"]["why"] == [preview.ALL_AGAIN]


PLAYBOOK = "- hosts: all\n  roles:\n    - forgejo\n"
ROLE = {
    "tasks/main.yml": """
- name: Settings
  ansible.builtin.template:
    src: app.ini.j2
    dest: /etc/forgejo/app.ini
  notify: Restart forgejo
- name: Tool
  ansible.builtin.copy:
    src: nb-forgejo
    dest: /usr/local/sbin/nb-forgejo
""",
    "handlers/main.yml": "- name: Restart forgejo\n  ansible.builtin.systemd:\n    name: forgejo\n    state: restarted\n",
    "templates/app.ini.j2": "[server]\n",
    "files/nb-forgejo": "#!/bin/sh\n",
}


def test_a_waiting_update_says_what_it_does_to_each_machine(installed):
    app, client, work = installed
    sign_in(app, client)
    commit(work, PLAYBOOK, "Forgejo's playbook", "ansible/forgejo.yml")
    for path, text in ROLE.items():
        commit(work, text, f"Forgejo: {path}", f"ansible/roles/forgejo/{path}")
    wait(client, client.call("/api/jobs", {"action": "upgrade"})[1]["id"])
    restarted(app)
    newest = commit(work, "[server]\nDOMAIN = forgejo\n", "Forgejo: its name", "ansible/roles/forgejo/templates/app.ini.j2")
    # Before the server is built an update has nothing to restart.
    assert client.call("/api/updates/check", {})[1]["version"]["effect"] is None
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    client.call("/api/choices", {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"})
    wait(client, setup(client)[1]["id"])
    written_down(app)
    effect = client.call("/api/state")[1]["version"]["effect"]
    assert effect["code"] is False and effect["counts"]["again"] == 1
    forgejo = effect["machines"][0]
    assert forgejo["name"] == "forgejo" and forgejo["what"] == "again"
    assert forgejo["why"] == ["The update sets it up differently: 1 file of forgejo changed."]
    assert forgejo["restarts"] == [{"what": "forgejo", "how": "restart"}] and forgejo["last"]["took_s"] == 12.0
    assert {m["what"] for m in effect["machines"][1:]} == {"alone"} and preview.HOST in {m["name"] for m in effect["machines"]}
    assert git(app.repo, "rev-parse", "HEAD") != newest  # nothing of the update ran or was checked out
    # A tool that no task restarts for, and a change to how NullBill plans: said as such.
    commit(work, "#!/bin/sh\ntrue\n", "Forgejo: its tool", "ansible/roles/forgejo/files/nb-forgejo")
    commit(work, "A = 1\n", "Plan: a new rule", "src/nullbill/plan.py")
    effect = client.call("/api/updates/check", {})[1]["version"]["effect"]
    assert effect["code"] is True and effect["machines"][0]["restarts"] == [{"what": "forgejo", "how": "restart"}]


def test_updates_are_found_and_the_ones_that_change_setup_wait_for_a_run(installed):
    app, client, work = installed
    sign_in(app, client)
    first = git(work, "rev-parse", "HEAD")
    about = client.call("/api/state")[1]["version"]
    assert about["current"]["id"] == first[:12] and about["current"]["subject"] == "first"
    assert about["available"] == [] and about["checked"] is None and about["last"] is None
    assert about["server"] == {"built": None, "follows": None, "changes": []}
    # The server is built with the first commit.
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    client.call("/api/choices", {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"})
    wait(client, setup(client)[1]["id"])
    state = client.call("/api/state")[1]
    assert state["version"]["server"]["follows"] is True and state["version"]["server"]["built"]["id"] == first[:12]
    assert state["review"]["reasons"] == []
    # main moves on: a change to the page, and one to a role.
    page = commit(work, "body {}\n", "Page: a new colour", "src/nullbill/web/static/style.css")
    role = commit(work, "- name: install\n", "Forgejo: a newer release", "ansible/roles/forgejo/tasks/main.yml")
    assert client.call("/api/state")[1]["version"]["available"] == []  # nobody looked yet
    status, answer = client.call("/api/updates/check", {})
    assert status == 200 and answer["version"]["check_error"] is None and answer["version"]["checked"] > 0
    assert [(c["id"], c["subject"], c["setup"]) for c in answer["version"]["available"]] == [
        (role[:12], "Forgejo: a newer release", True),
        (page[:12], "Page: a new colour", False),
    ]
    assert git(app.repo, "rev-parse", "HEAD") == first  # looking moves nothing
    read = client.call("/api/state")[1]["review"]["id"]
    # Update NullBill, and the page restarts with the new version.
    assert wait(client, client.call("/api/jobs", {"action": "upgrade"})[1]["id"])["state"] == "done"
    restarted(app)
    state = client.call("/api/state")[1]
    about = state["version"]
    assert about["current"]["id"] == role[:12] and about["available"] == []
    assert about["last"]["from"] == first[:12] and [c["id"] for c in about["last"]["changes"]] == [role[:12], page[:12]]
    # The server was built with the old one: only the role's change waits for a setup run.
    assert about["server"]["follows"] is False and about["server"]["built"]["id"] == first[:12]
    assert [c["subject"] for c in about["server"]["changes"]] == ["Forgejo: a newer release"]
    reasons = state["review"]["reasons"]
    assert [r["kind"] for r in reasons] == ["update"] and "1 change to what setup builds" in reasons[0]["text"]
    assert state["review"]["commits"] == about["server"]["changes"]
    # What setup would do is not what the owner read before the update.
    assert state["review"]["id"] != read and client.call("/api/jobs", {"action": "apply", "review": read})[0] == 409
    wait(client, setup(client)[1]["id"])
    state = client.call("/api/state")[1]
    assert state["version"]["server"] == {"built": about["current"], "follows": True, "changes": []} and state["review"]["reasons"] == []
    # An update to the page alone asks for no setup run.
    commit(work, "body {{}}\n", "Page: another colour", "src/nullbill/web/static/style.css")
    wait(client, client.call("/api/jobs", {"action": "upgrade"})[1]["id"])
    restarted(app)
    state = client.call("/api/state")[1]
    assert state["version"]["current"]["subject"] == "Page: another colour" and state["version"]["server"]["follows"] is True
    assert state["version"]["server"]["built"]["id"] == role[:12] and state["review"]["reasons"] == []


def test_a_check_that_cannot_reach_the_repository_says_so_and_keeps_what_it_knew(installed, tmp_path):
    app, client, work = installed
    sign_in(app, client)
    newest = commit(work, "two\n", "second")
    assert len(client.call("/api/updates/check", {})[1]["version"]["available"]) == 1
    git(app.repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    status, answer = client.call("/api/updates/check", {})
    assert status == 200 and "could not reach the repository" in answer["version"]["check_error"]
    assert [c["id"] for c in answer["version"]["available"]] == [newest[:12]]
    # One check at a time.
    app.checking = True
    status, answer = client.call("/api/updates/check", {})
    assert status == 400 and "already on its way" in answer["error"]


def test_the_page_looks_for_updates_once_a_day_and_never_while_a_job_runs(installed, tmp_path):
    app, client, work = installed
    sign_in(app, client)
    now = time.time()
    assert app.updates_due(now)
    commit(work, "two\n", "second")
    app.scheduled(now)
    assert len(app.version_view()["available"]) == 1
    assert not app.updates_due(now + web.UPDATE_CHECK_S - 60) and app.updates_due(now + web.UPDATE_CHECK_S + 60)
    release = tmp_path / "release"
    app.command = [sys.executable, "-c", f"import os,time\nwhile not os.path.exists({str(release)!r}): time.sleep(0.02)"]
    job = client.call("/api/jobs", {"action": "verify"})[1]
    assert not app.updates_due(now + web.UPDATE_CHECK_S + 60)
    release.write_text("")
    wait(client, job["id"])


NIGHT_PINS = (
    "forgejo: {title: Forgejo, file: ansible/roles/forgejo/defaults/main.yml, key: forgejo_version, hold: 1, services: [forgejo]}\n"
)


def night(n):
    """A quarter past three in the morning, n days from now."""
    today = time.localtime()
    return time.mktime((today.tm_year, today.tm_mon, today.tm_mday + n, web.NIGHTLY_HOURS[0], 15, 0, 0, 0, -1))


def a_night(app, client, when):
    """The page's every-minute look, until tonight's update has ended."""
    for _ in range(8):
        job = app.scheduled(when)
        if job is not None:
            assert wait(client, job.id)["state"] == "done"
            if job.action == "upgrade":
                restarted(app)
        if app.nightly().get("day") and app.nightly().get("step") == "done":
            break
    return app.nightly()["ended"]


def test_each_night_the_page_installs_what_is_new_and_a_big_step_waits_for_the_owner(installed, monkeypatch):
    app, client, work = installed
    sign_in(app, client)
    told = []
    monkeypatch.setattr(app, "tell", lambda text, tag, title="": told.append(text))
    # Nothing is built: the night does nothing.
    assert app.scheduled(night(1)) is None and "day" not in app.nightly()
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    client.call("/api/choices", {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"})
    wait(client, setup(client)[1]["id"])
    # By day nothing happens; at night with nothing new, nothing is set up.
    assert app.scheduled(night(1) + 9 * 3600) is None and "day" not in app.nightly()
    assert a_night(app, client, night(1))["how"] == "same"
    # main gets its pins, then a small step: installed and set up by the night.
    commit(work, NIGHT_PINS, "Pins", "src/nullbill/data/pins.yaml")
    commit(work, "forgejo_version: 15.0.11\n", "Forgejo 15.0.11", "ansible/roles/forgejo/defaults/main.yml")
    assert a_night(app, client, night(2))["how"] == "done"
    small = commit(work, "forgejo_version: 15.0.12\n", "Forgejo 15.0.12", "ansible/roles/forgejo/defaults/main.yml")
    assert a_night(app, client, night(3))["how"] == "done"
    about = app.version_view()
    assert about["current"]["id"] == small[:12] and about["server"]["follows"] is True and about["available"] == []
    assert [(j.action, j.by) for j in app.setup_runs()][-1] == ("apply", "nightly")
    # Each such night says so, with what moved.
    done = "NullBill installed an update tonight and ran setup with it"
    assert told == [f"{done}.", f"{done}: Forgejo 15.0.12."]
    told.clear()
    # A big step: nothing is installed, the owner is told once, and the page says what waits.
    commit(work, "forgejo_version: 17.0.1\n", "Forgejo 17.0.1", "ansible/roles/forgejo/defaults/main.yml")
    ended = a_night(app, client, night(4))
    assert ended["how"] == "waits" and "Forgejo 15.0.12 to 17.0.1" in ended["text"] and told == [ended["text"]]
    assert git(app.repo, "rev-parse", "HEAD") == small
    assert a_night(app, client, night(5))["how"] == "waits" and len(told) == 1
    about = client.call("/api/state")[1]["version"]
    assert about["steps"] == [{"name": "forgejo", "title": "Forgejo", "from": "15.0.12", "to": "17.0.1", "asks": True}]
    assert about["nightly"]["enabled"] is True and about["nightly"]["ended"]["how"] == "waits"
    # Switched off, the night does nothing.
    assert client.call("/api/updates/nightly", {"enabled": False})[1]["version"]["nightly"]["enabled"] is False
    assert app.scheduled(night(6)) is None and app.nightly()["day"] != time.strftime("%Y-%m-%d", time.localtime(night(6)))
    # The owner's own Update is the yes; choices they saved and did not review keep setup theirs.
    client.call("/api/updates/nightly", {"enabled": True})
    wait(client, client.call("/api/jobs", {"action": "upgrade"})[1]["id"])
    restarted(app)
    # Setup for an update the owner installed is the owner's to start.
    runs = len(app.setup_runs())
    ended = a_night(app, client, night(7))
    assert ended["how"] == "waits" and "no night's update installed this version" in ended["text"] and len(app.setup_runs()) == runs
    commit(work, "forgejo_version: 17.0.2\n", "Forgejo 17.0.2", "ansible/roles/forgejo/defaults/main.yml")
    config = app.home / "config.yaml"
    os.utime(config, (time.time() + 5, time.time() + 5))
    ended = a_night(app, client, night(8))
    assert ended["how"] == "waits" and "saved choices" in ended["text"] and len(app.setup_runs()) == runs
    assert app.version_view()["server"]["follows"] is False


def test_a_night_whose_setup_run_fails_tells_the_owner(installed, monkeypatch):
    app, client, work = installed
    sign_in(app, client)
    told = []
    monkeypatch.setattr(app, "tell", lambda text, tag, title="": told.append(text))
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    client.call("/api/choices", {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"})
    wait(client, setup(client)[1]["id"])
    commit(work, "- name: install\n", "Forgejo: a newer release", "ansible/roles/forgejo/tasks/main.yml")
    job = app.scheduled(night(1))
    assert job.action == "upgrade" and wait(client, job.id)["state"] == "done"
    restarted(app)
    app.scheduled(night(1))
    app.command = [sys.executable, "-c", "raise SystemExit(3)"]
    job = app.scheduled(night(1))
    assert job.action == "apply" and wait(client, job.id)["state"] == "failed"
    app.scheduled(night(1))
    ended = app.nightly()["ended"]
    assert ended["how"] == "failed" and told == [ended["text"]] and "setup run failed" in ended["text"]
    # The same night does not try again, and no later night by itself:
    # that setup run is the owner's to look at and start.
    assert app.scheduled(night(1)) is None
    app.command = [sys.executable, "-c", "pass"]
    runs = len(app.setup_runs())
    ended = a_night(app, client, night(2))
    assert ended["how"] == "waits" and "failed on an earlier night" in ended["text"] and len(app.setup_runs()) == runs


def test_the_night_sets_up_only_what_the_night_installed(installed, monkeypatch):
    app, client, work = installed
    sign_in(app, client)
    monkeypatch.setattr(app, "tell", lambda text, tag, title="": None)
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    client.call("/api/choices", {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"})
    wait(client, setup(client)[1]["id"])
    # The owner installed an update and has not run setup: the night leaves it.
    commit(work, "- name: install\n", "Forgejo: a newer release", "ansible/roles/forgejo/tasks/main.yml")
    wait(client, client.call("/api/jobs", {"action": "upgrade"})[1]["id"])
    restarted(app)
    assert app.version_view()["server"]["follows"] is False
    runs = len(app.setup_runs())
    ended = a_night(app, client, night(1))
    assert ended["how"] == "waits" and "no night's update installed this version" in ended["text"] and len(app.setup_runs()) == runs
    # A job that began in the same minute is no error: the next minute looks again.
    commit(work, "- name: install again\n", "Forgejo: newer still", "ansible/roles/forgejo/tasks/main.yml")

    def taken(action, **more):
        raise web.NullBillError("another job is running")

    monkeypatch.setattr(app, "start", taken)
    assert app.scheduled(night(2)) is None
    assert app.nightly()["day"] != time.strftime("%Y-%m-%d", time.localtime(night(2)))


def test_a_setup_run_from_before_the_page_noted_its_version_is_found_in_gits_record(installed):
    app, client, work = installed
    sign_in(app, client)
    first = git(work, "rev-parse", "HEAD")
    wait(client, client.call("/api/jobs", {"action": "probe"})[1]["id"])
    client.call("/api/choices", {"workloads": ["agents"], "backups": False, "owner_login": "owner-a@example.internal"})
    job = app.jobs[setup(client)[1]["id"]]
    wait(client, job.id)
    # As an earlier page wrote it: no commit, no digest.
    meta = json.loads(job.meta_file.read_text())
    job.meta_file.write_text(json.dumps({k: v for k, v in meta.items() if k not in ("commit", "setup", "review")}))
    real_sleep(1.1)
    commit(work, "- name: install\n", "Forgejo: a newer release", "ansible/roles/forgejo/tasks/main.yml")
    wait(client, client.call("/api/jobs", {"action": "upgrade"})[1]["id"])
    restarted(app)
    server = client.call("/api/state")[1]["version"]["server"]
    assert server["follows"] is False and server["built"]["id"] == first[:12]
    assert [c["subject"] for c in server["changes"]] == ["Forgejo: a newer release"]


def test_a_nullbill_that_is_not_a_checkout_has_no_version_and_cannot_look(tmp_path, monkeypatch):
    app, client, server = start_site(tmp_path, monkeypatch, repo=tmp_path / "nowhere")
    try:
        sign_in(app, client)
        state = client.call("/api/state")[1]
        assert state["version"] is None and state["review"]["reasons"][0]["kind"] == "first"
        assert not app.updates_due(time.time())
        status, answer = client.call("/api/updates/check", {})
        assert status == 200 and client.call("/api/state")[1]["version"] is None
    finally:
        server.shutdown()
