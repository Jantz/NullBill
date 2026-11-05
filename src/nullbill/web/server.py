"""The setup and status page (docs/adr/0006-nullbill-on-the-server.md).

Standard library only. One page and a small JSON API over HTTPS, with a
self-signed certificate until the CA exists. Until the owner sets a
password, the one-time setup code install.sh printed is the only way in.

Long work (probe, apply, verify) runs as a `nullbill` subprocess whose
output goes to $NULLBILL_HOME/jobs/<id>.log, so a page reload or a restart
of this server loses nothing.
"""

import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from nullbill import lan
from nullbill.apply import NTFY
from nullbill.cli import plan_text
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.secretfile import Codec, Secrets, Sops
from nullbill.tailnet import render_tailnet

SESSION_S = 12 * 3600
MIN_PASSWORD = 12
LOCKOUT_FAILURES = 10
LOCKOUT_S = 300
WORKLOADS = ["agents", "linux-ci", "inference", "windows-ci", "windows-dev"]
ACTIONS = {"probe", "apply", "verify"}
# Closing the LAN to Proxmox (nullbill/lan.py), run as apply's host stage.
LAN_ACTIONS = {"close-lan": ["apply", "--close-lan"], "keep-lan-closed": ["apply", "--confirm-lan"], "open-lan": ["apply", "--open-lan"]}
# An optional Healthchecks.io check notices silence; alerts go to NTFY.
HEALTHCHECKS = re.compile(r"https://hc-ping\.com/[A-Za-z0-9_-]{8,}(/[a-z0-9_-]+)?")
# The agent CLI signs in with an Anthropic API key or a Claude subscription
# token from `claude setup-token`; each is kept under its own name.
AGENT_CREDENTIALS = {"sk-ant-api": ("anthropic_api_key", "API key"), "sk-ant-oat": ("claude_oauth_token", "subscription token")}
# A Tailscale auth key logs the vpn guest in once; apply forgets it after.
TAILSCALE_KEY = re.compile(r"tskey-auth-[A-Za-z0-9-]{8,200}")
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "Cache-Control": "no-store",
}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def post(url: str, body: str = "", headers: dict | None = None) -> None:
    request = urllib.request.Request(url, data=body.encode(), headers=headers or {}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def ntfy_topic() -> str:
    return "nullbill-" + secrets.token_urlsafe(18)


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 2**20)
    return {"salt": salt.hex(), "scrypt": digest.hex(), "n": 2**15, "r": 8, "p": 1}


def check_password(password: str, stored: dict) -> bool:
    salt = bytes.fromhex(stored["salt"])
    digest = hashlib.scrypt(password.encode(), salt=salt, n=stored["n"], r=stored["r"], p=stored["p"], maxmem=64 * 2**20)
    return hmac.compare_digest(digest.hex(), stored["scrypt"])


@dataclass
class Job:
    id: str
    action: str
    args: list[str]
    log: Path
    started: float
    process: subprocess.Popen | None = None
    rc: int | None = None

    def state(self) -> str:
        if self.process is not None and self.rc is None:
            self.rc = self.process.poll()
        return "running" if self.rc is None else ("done" if self.rc == 0 else "failed")


@dataclass
class App:
    home: Path
    command: list[str] = field(default_factory=lambda: [sys.executable, "-m", "nullbill"])
    sessions: dict[str, float] = field(default_factory=dict)
    failures: list[float] = field(default_factory=list)
    jobs: dict[str, Job] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # The secrets file, opened afresh for each use so apply's writes show.
    codec: Callable[[Path], Codec] = Sops
    send: Callable[..., None] = post
    fetch: Callable[[str], dict] = fetch_json

    # Files under NULLBILL_HOME.
    @property
    def owner_file(self) -> Path:
        return self.home / "web" / "owner.json"

    @property
    def code_file(self) -> Path:
        return self.home / "setup-code"

    def install(self) -> dict:
        path = self.home / "install.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def ensure_setup_code(self) -> None:
        """Before the owner sets a password, a one-time code lets them in."""
        if not self.owner_file.exists() and not self.code_file.exists():
            words = [secrets.token_hex(2).upper() for _ in range(4)]
            _write_private(self.code_file, "-".join(words) + "\n")

    # Sessions.
    def login(self, secret: str) -> str | None:
        now = time.time()
        with self.lock:
            self.failures = [t for t in self.failures if now - t < LOCKOUT_S]
            if len(self.failures) >= LOCKOUT_FAILURES:
                return None
        if self.owner_file.exists():
            ok = check_password(secret, json.loads(self.owner_file.read_text()))
        else:
            code = self.code_file.read_text().strip() if self.code_file.exists() else ""
            ok = bool(code) and hmac.compare_digest(secret.strip().upper(), code)
        with self.lock:
            if not ok:
                self.failures.append(now)
                return None
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + SESSION_S
            return token

    def valid(self, token: str | None) -> bool:
        with self.lock:
            expiry = self.sessions.get(token or "")
            return expiry is not None and expiry > time.time()

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise NullBillError(f"the password needs at least {MIN_PASSWORD} characters")
        _write_private(self.owner_file, json.dumps(hash_password(password)))
        self.code_file.unlink(missing_ok=True)
        # No job runs before a password exists, so nothing else writes the
        # secrets file now.
        Secrets(self.codec(self.home)).ensure("ntfy_topic", ntfy_topic)

    # Status, from Prometheus on the monitoring guest.
    def status(self) -> dict:
        path = self.home / "plan.yaml"
        plan = yaml.safe_load(path.read_text()) if path.exists() else None
        monitoring = ((plan or {}).get("guests") or {}).get("monitoring")
        if not monitoring:
            return {"monitoring": "not planned"}
        api = f"http://{monitoring['ip']}:9090/api/v1/"

        def query(expr: str) -> list[dict]:
            return self.fetch(api + "query?" + urllib.parse.urlencode({"query": expr}))["data"]["result"]

        def instances(expr: str) -> list[str]:
            return sorted(r["metric"].get("instance", "?") for r in query(expr))

        try:
            alerts = [
                {
                    "name": a["labels"]["alertname"],
                    "severity": a["labels"].get("severity", ""),
                    "summary": a.get("annotations", {}).get("summary", ""),
                }
                for a in self.fetch(api + "alerts")["data"]["alerts"]
                if a.get("state") == "firing" and a["labels"].get("alertname") != "Watchdog"
            ]
            up = query("up")
            return {
                "monitoring": "ok",
                "alerts": sorted(alerts, key=lambda a: (a["severity"] != "page", a["name"])),
                "targets": len(up),
                "down": sorted(r["metric"].get("instance", "?") for r in up if r["value"][1] == "0"),
                "security_waiting": instances('sum by (instance) (apt_upgrades_pending{origin=~".*-security.*"}) > 0'),
                "reboot_waiting": instances("node_reboot_required == 1"),
            }
        except (OSError, ValueError, KeyError) as exc:
            return {"monitoring": "unreachable", "error": str(exc)[:200]}

    # Alerts.
    def alerts(self) -> dict:
        data = Secrets(self.codec(self.home)).data
        topic = data.get("ntfy_topic")
        return {"ntfy_url": NTFY + topic if topic else None, "healthchecks_url": data.get("healthchecks_url", "")}

    def save_alerts(self, body: dict) -> None:
        url = str(body.get("healthchecks_url", "")).strip()
        if url and not HEALTHCHECKS.fullmatch(url):
            raise NullBillError("paste the ping URL from your Healthchecks.io check, like https://hc-ping.com/<uuid>")
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            store = Secrets(self.codec(self.home))
            if url:
                store.set("healthchecks_url", url)
            elif "healthchecks_url" in store.data:
                del store.data["healthchecks_url"]
                store.codec.save(store.data)

    def test_alerts(self) -> list[str]:
        """Send one ntfy message and one Healthchecks.io ping; name what was sent."""
        alerts = self.alerts()
        if not alerts["ntfy_url"]:
            raise NullBillError("set a password first; that makes the ntfy topic")
        sent = []
        try:
            title = {"Title": "NullBill: test alert", "Tags": "white_check_mark"}
            self.send(alerts["ntfy_url"], "Alerts from your server reach this device.", title)
            sent.append("ntfy")
            if alerts["healthchecks_url"]:
                self.send(alerts["healthchecks_url"])
                sent.append("Healthchecks.io")
        except OSError as exc:
            raise NullBillError(f"sending failed after {sent or 'nothing'}: {exc}") from exc
        return sent

    # The agent CLI's credential: which kind is stored, never the value.
    def agent_credential(self) -> str | None:
        data = Secrets(self.codec(self.home)).data
        return next((label for name, label in AGENT_CREDENTIALS.values() if data.get(name)), None)

    def save_agent_credential(self, body: dict) -> None:
        value = str(body.get("credential", "")).strip()
        kind = next((v for prefix, v in AGENT_CREDENTIALS.items() if value.startswith(prefix)), None)
        if kind is None or len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise NullBillError("paste an Anthropic API key (sk-ant-api...) or the token `claude setup-token` prints (sk-ant-oat...)")
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            store = Secrets(self.codec(self.home))
            for name, _ in AGENT_CREDENTIALS.values():
                store.data.pop(name, None)
            store.data[kind[0]] = value
            store.codec.save(store.data)

    # The vpn guest's one-time auth key: whether one waits, never the value.
    def tailscale_key(self) -> bool:
        return bool(Secrets(self.codec(self.home)).data.get("tailscale_auth_key"))

    def save_tailscale_key(self, body: dict) -> None:
        value = str(body.get("key", "")).strip()
        if not TAILSCALE_KEY.fullmatch(value):
            raise NullBillError("paste the auth key Tailscale shows once you generate it (tskey-auth-...)")
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            Secrets(self.codec(self.home)).set("tailscale_auth_key", value)

    # Config and plan.
    def config(self) -> dict | None:
        path = self.home / "config.yaml"
        return yaml.safe_load(path.read_text()) if path.exists() else None

    def choices(self) -> dict:
        config = self.config() or {}
        owner = next((p["login"] for p in config.get("people", {}).values() if p.get("role") == "owner"), "")
        return {
            "workloads": config.get("intent", {}).get("workloads", ["agents", "linux-ci"]),
            "backups": config.get("services", {}).get("pbs", {}).get("enabled", True),
            "console": config.get("host", {}).get("console", {}).get("enabled", False),
            "owner_login": owner,
        }

    def save_choices(self, body: dict) -> None:
        workloads = [w for w in body.get("workloads", []) if w in WORKLOADS]
        login = str(body.get("owner_login", "")).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", login):
            raise NullBillError("your Tailscale login is the email address you sign in to Tailscale with")
        install = self.install()
        if not install.get("host"):
            raise NullBillError("install.json is missing; run install.sh on the Proxmox host again")
        config = self.config() or {}
        config["target"] = {"ssh": f"root@{install['host']}"}
        config["intent"] = {"workloads": workloads}
        people = {n: p for n, p in config.get("people", {}).items() if p.get("role") != "owner"}
        config["people"] = {"owner": {"role": "owner", "login": login}, **people}
        services = config.setdefault("services", {})
        if body.get("backups", True):
            services.get("pbs", {}).pop("enabled", None)
            if services.get("pbs") == {}:
                del services["pbs"]
        else:
            services["pbs"] = {**services.get("pbs", {}), "enabled": False}
        if not services:
            del config["services"]
        host = config.setdefault("host", {})
        if body.get("console"):
            host["console"] = {**host.get("console", {}), "enabled": True}
        else:
            host.get("console", {}).pop("enabled", None)
            if host.get("console") == {}:
                del host["console"]
        if not host:
            del config["host"]
        build_model(config)  # validates against the schema before anything is written
        _write_private(self.home / "config.yaml", yaml.safe_dump(config, sort_keys=False))

    def plan(self) -> dict:
        facts = self.home / "probe" / "latest" / "facts.json"
        config = self.config()
        if config is None or not facts.is_file():
            return {"text": None, "errors": [], "tailnet_policy": None}
        try:
            model = build_model(config)
            text = plan_text(model, facts.read_bytes())
        except NullBillError as exc:
            return {"text": None, "errors": exc.messages, "tailnet_policy": None}
        _write_private(self.home / "plan.yaml", text)
        policy = render_tailnet(yaml.safe_load(text), model)["policy.json"]
        return {"text": text, "errors": [], "tailnet_policy": policy}

    # The LAN. Its addresses come from the plan, so nothing is known before one.
    def network(self) -> tuple[str | None, str | None]:
        """(the NullBill network, the vpn guest's address)."""
        path = self.home / "plan.yaml"
        config = self.config()
        if config is None or not path.exists():
            return None, None
        plan = yaml.safe_load(path.read_text()) or {}
        return build_model(config)["network"]["base"], plan.get("guests", {}).get("vpn", {}).get("ip")

    def refused(self, client: str) -> bool:
        """Once the LAN is closed, the page answers only inside the NullBill
        network: over Tailscale, through the vpn guest, or the on-site screen."""
        if lan.state(self.home) != lan.CLOSED:
            return False
        base, _ = self.network()
        return base is not None and not lan.inside(client, base)

    def via_vpn(self, client: str) -> bool:
        return client.removeprefix("::ffff:") == self.network()[1]

    # Jobs.
    def start(self, action: str, stage: str | None = None, client: str = "") -> Job:
        if action == "keep-lan-closed" and not self.via_vpn(client):
            raise NullBillError("press Keep it closed on the page opened over Tailscale; that press is what shows Tailscale works")
        if action not in ACTIONS and action not in LAN_ACTIONS:
            raise NullBillError(f"unknown action {action!r}")
        with self.lock:
            if any(j.state() == "running" for j in self.jobs.values()):
                raise NullBillError("another job is still running")
            jobs = self.home / "jobs"
            jobs.mkdir(mode=0o700, exist_ok=True)
            job_id = time.strftime("%Y%m%dT%H%M%S") + "-" + action
            args = LAN_ACTIONS[action] if action in LAN_ACTIONS else [action] + (["--stage", stage] if stage else [])
            job = Job(job_id, action, args, jobs / f"{job_id}.log", time.time())
            env = os.environ | {"NULLBILL_HOME": str(self.home), "PYTHONUNBUFFERED": "1", "ANSIBLE_FORCE_COLOR": "0"}
            fd = os.open(job.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                argv = [*self.command, *args]
                job.process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
            self.jobs[job_id] = job
            return job

    def job_view(self, job: Job, offset: int = 0) -> dict:
        data = job.log.read_bytes()
        return {
            "id": job.id,
            "action": job.action,
            "args": job.args,
            "state": job.state(),
            "rc": job.rc,
            "text": data[offset:].decode(errors="replace"),
            "offset": len(data),
        }

    def state(self, authed: bool, client: str = "") -> dict:
        has_password = self.owner_file.exists()
        if not authed:
            return {"auth": False, "has_password": has_password}
        latest = max(self.jobs.values(), key=lambda j: j.started, default=None)
        return {
            "auth": True,
            "has_password": has_password,
            "install": self.install(),
            "probed": (self.home / "probe" / "latest" / "facts.json").is_file(),
            "choices": self.choices(),
            "workloads": WORKLOADS,
            "plan": self.plan(),
            "alerts": self.alerts() if has_password else None,
            "agent_credential": self.agent_credential(),
            "tailscale_key": self.tailscale_key(),
            "lan": {"state": lan.state(self.home), "via_vpn": self.via_vpn(client), "minutes": lan.REVERT_AFTER_S // 60},
            "job": self.job_view(latest) | {"text": ""} if latest else None,
        }


def make_handler(app: App) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "nullbill"
        sys_version = ""

        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def send(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
            self.send_response(code)
            for name, value in (SECURITY_HEADERS | {"Content-Type": kind, "Content-Length": str(len(body))} | (headers or {})).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, code: int, data: object, headers: dict | None = None) -> None:
            self.send(code, json.dumps(data).encode(), "application/json", headers)

        def token(self) -> str | None:
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "nb_session":
                    return value
            return None

        def client(self) -> str:
            return str(self.client_address[0])

        def closed(self) -> bool:
            if not app.refused(self.client()):
                return False
            self.send(403, b"NullBill is closed to this network; open it over Tailscale.\n", "text/plain; charset=utf-8")
            return True

        def do_GET(self) -> None:  # noqa: N802
            if self.closed():
                return
            path, _, query = self.path.partition("?")
            if path in STATIC:
                name, kind = STATIC[path]
                body = resources.files("nullbill.web").joinpath("static", name).read_bytes()
                return self.send(200, body, f"{kind}; charset=utf-8")
            authed = app.valid(self.token())
            if path == "/api/state":
                return self.json(200, app.state(authed, self.client()))
            if not authed:
                return self.json(401, {"error": "sign in first"})
            if path == "/api/status":
                return self.json(200, app.status())
            match = re.fullmatch(r"/api/jobs/([0-9T]+-[a-z-]+)", path)
            if match and match[1] in app.jobs:
                offset = int(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("offset", "0") or 0)
                return self.json(200, app.job_view(app.jobs[match[1]], offset))
            return self.json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.closed():
                return
            # A custom header cannot be sent cross-site without a CORS
            # preflight, which this server never grants.
            if self.headers.get("X-NullBill") != "1":
                return self.json(403, {"error": "missing X-NullBill header"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 64 * 1024:
                return self.json(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.json(400, {"error": "body is not JSON"})
            if self.path == "/api/login":
                token = app.login(str(body.get("secret", "")))
                if token is None:
                    time.sleep(1)
                    return self.json(401, {"error": "wrong code or password"})
                cookie = f"nb_session={token}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={SESSION_S}"
                return self.json(200, {"ok": True}, {"Set-Cookie": cookie})
            if not app.valid(self.token()):
                return self.json(401, {"error": "sign in first"})
            try:
                if self.path == "/api/password":
                    app.set_password(str(body.get("password", "")))
                    return self.json(200, {"ok": True})
                if self.path == "/api/choices":
                    app.save_choices(body)
                    return self.json(200, {"ok": True, "plan": app.plan()})
                if self.path == "/api/alerts":
                    app.save_alerts(body)
                    return self.json(200, {"ok": True, "alerts": app.alerts()})
                if self.path == "/api/agent":
                    app.save_agent_credential(body)
                    return self.json(200, {"ok": True, "agent_credential": app.agent_credential()})
                if self.path == "/api/vpn":
                    app.save_tailscale_key(body)
                    return self.json(200, {"ok": True, "tailscale_key": app.tailscale_key()})
                if self.path == "/api/alerts/test":
                    return self.json(200, {"ok": True, "sent": app.test_alerts()})
                if self.path == "/api/jobs":
                    stage = body.get("stage")
                    job = app.start(str(body.get("action")), str(stage) if stage else None, self.client())
                    return self.json(200, app.job_view(job))
                if self.path == "/api/logout":
                    with app.lock:
                        app.sessions.pop(self.token() or "", None)
                    return self.json(200, {"ok": True}, {"Set-Cookie": "nb_session=; Path=/; Secure; HttpOnly; Max-Age=0"})
            except NullBillError as exc:
                return self.json(400, {"error": "; ".join(exc.messages)})
            return self.json(404, {"error": "not found"})

    return Handler


def ensure_certificate(home: Path) -> tuple[Path, Path]:
    """A self-signed certificate for the page until step-ca issues one."""
    cert, key = home / "web" / "cert.pem", home / "web" / "key.pem"
    if not cert.exists():
        cert.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv = ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "3650"]
        argv += ["-subj", "/CN=nullbill", "-keyout", str(key), "-out", str(cert)]
        subprocess.run(argv, check=True, capture_output=True)
        key.chmod(0o600)
    return cert, key


def fingerprint(cert: Path) -> str:
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"], capture_output=True, text=True)
    return out.stdout.strip().partition("=")[2]


def serve(home: Path, listen: str) -> None:
    host, _, port = listen.rpartition(":")
    app = App(home)
    app.ensure_setup_code()
    cert, key = ensure_certificate(home)
    server = http.server.ThreadingHTTPServer((host or "0.0.0.0", int(port)), make_handler(app))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key)
    # The handshake runs in each request's thread, so a stalled client
    # cannot hold up the accept loop.
    server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    print(f"nullbill web on https://{listen}, certificate SHA-256 {fingerprint(cert)}", flush=True)
    server.serve_forever()
