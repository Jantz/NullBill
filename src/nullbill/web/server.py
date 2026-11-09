"""The setup and status page (docs/adr/0006-nullbill-on-the-server.md).

Standard library only. One page and a small JSON API over HTTPS, with a
self-signed certificate until apply's ca stage signs the same key with the
browser CA (browserca.py), whose root the page offers for download. Until the owner sets a
password, the one-time setup code install.sh printed is the only way in.

Long work (probe, apply, verify) runs as a `nullbill` subprocess whose
output goes to $NULLBILL_HOME/jobs/<id>.log. It keeps running when this
server restarts (an update restarting it, say), and the restarted server
shows it again.
"""

import functools
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import signal
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from nullbill import browserca, hostroot, lan, legacy, llm, names, owner, people, tailscale
from nullbill.apply import DISKS_FILE, GUESTS, NTFY, REMOVALS, SPKI, STAGES
from nullbill.cli import plan_text, reach_target
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan, guest_storage, planned_services
from nullbill.probe import ssh_run
from nullbill.render import strip_notes
from nullbill.secretfile import Codec, Secrets, Sops
from nullbill.tailnet import TAG, render_tailnet
from nullbill.tailscale import MissingScope, Tailnet, TailscaleError
from nullbill.web import chat, progress, workloads
from nullbill.web.services import PASSWORDS, services, shares

SESSION_S = 12 * 3600
APPLY_STAGES = [name for name, _ in STAGES]
MIN_PASSWORD = 12
LOCKOUT_FAILURES = 10
LOCKOUT_S = 300
WORKLOADS = workloads.WORKLOADS
# Ticked on the page before the owner has saved any choice.
DEFAULT_WORKLOADS = ["agents", "linux-ci"]
# upgrade moves NullBill itself to main's newest commit (nullbill/upgrade.py).
ACTIONS = {"probe", "apply", "verify", "upgrade"}
# Closing the LAN to Proxmox (nullbill/lan.py), run as apply's host stage.
LAN_ACTIONS = {"close-lan": ["apply", "--close-lan"], "keep-lan-closed": ["apply", "--confirm-lan"], "open-lan": ["apply", "--open-lan"]}
# The page's own weekly job: the ci stage bakes each golden image again once
# it is a week old (nullbill/ci.py). Sunday 03:00 local time, after the
# nightly backups; skipped for the week if another job runs then.
SCHEDULED = {"rebake-ci": ["apply", "--stage", "ci"]}
# Deleting the guests the last apply stopped before deleting (apply.REMOVALS).
REMOVE_ACTIONS = {"remove-guests": ["apply", "--remove-guests"]}
REBAKE_WEEKDAY, REBAKE_HOUR = 6, 3
SCHEDULE_CHECK_S = 60
ROOT_CERT = "/nullbill-root.crt"
# An optional Healthchecks.io check notices silence; alerts go to NTFY.
HEALTHCHECKS = re.compile(r"https://hc-ping\.com/[A-Za-z0-9_-]{8,}(/[a-z0-9_-]+)?")
# The agent CLI signs in with an Anthropic API key or a Claude subscription
# token from `claude setup-token`; each is kept under its own name.
AGENT_CREDENTIALS = {"sk-ant-api": ("anthropic_api_key", "API key"), "sk-ant-oat": ("claude_oauth_token", "subscription token")}
# The runtime agent on the operator may start verify and read verify's
# output (docs/runbooks/agent-access.md): a bearer token whose SHA-256 apply
# writes here, accepted only from the operator's address. It cannot start
# anything else, read other jobs' logs, or use the owner's API.
AGENT_TOKEN = "web/agent-token.sha256"
AGENT_ACTIONS = {"verify"}
# The owner's switch on the page (docs/adr/0003-agent-access.md): while it
# is on, the runtime agent has root on the PVE host (nullbill/hostroot.py)
# and may also run setup (all of it or one stage) and update NullBill
# through this API, and read every job's log. Each job it starts here, and
# how it ended, goes to the owner's ntfy topic; if that send fails, the job
# does not start.
POWER_ACTIONS = {"apply", "upgrade"}
# Remote agents, such as one on the owner's laptop, get the same verify-only
# API with a token each that the owner makes and revokes on the page. Their
# requests arrive through the vpn guest, as everything from the tailnet does,
# and are accepted from its address only. The page keeps each token's
# SHA-256 here, never the token. Every agent request goes to the journal
# with the agent's name.
REMOTE_AGENTS = "web/remote-agents.json"
DISK_CHOICES = {"legacy": "keep", "pbs": "erase", "storage": "erase", "ignore": "alone"}
# What an erased disk becomes: the backup disk, or LVM-thin storage of its own.
DISK_USES = {"backups": "pbs", "storage": "storage"}
DISK_CHOICE_TEXT = "leave it alone, keep its files, copy its files and erase it, or erase it"
REMOTE_AGENT_NAME = re.compile(r"[a-z][a-z0-9-]{0,30}")
# A Tailscale auth key logs the vpn guest in once; apply forgets it after.
# A fine-grained GitHub token for the agent's pull requests.
GITHUB_TOKEN = re.compile(r"github_pat_[A-Za-z0-9_]{20,255}")
GITHUB_API = "https://api.github.com/"
GITHUB_REPO = "Jantz/NullBill"
TAILSCALE_KEY = re.compile(r"tskey-auth-[A-Za-z0-9-]{8,200}")
TAILSCALE_CLIENT_ID = re.compile(r"[A-Za-z0-9]{6,64}")
TAILSCALE_CLIENT_SECRET = re.compile(r"tskey-client-[A-Za-z0-9-]{8,200}")
STATIC = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "text/javascript"),
    "/style.css": ("style.css", "text/css"),
    "/theme.js": ("theme.js", "text/javascript"),
    "/sw.js": ("sw.js", "text/javascript"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/icon-maskable-512.png": ("icon-maskable-512.png", "image/png"),
}
SHELL_HEADER = "X-NullBill-Shell"
SHELL_META = b'<meta name="nb-shell" content="">'
# The page's agent (web/chat.py): the CLI, the user it runs as, its home,
# and the socket its tools call the page on.
AGENT_COMMAND = ["/usr/local/bin/claude"]
AGENT_USER = "nbagent"
AGENT_HOME = Path("/var/lib/nullbill-agent")
TOOLS_SOCKET = Path("/run/nullbill-agent/tools.sock")
REPO = Path(__file__).resolve().parents[3]
WATCH_S = 5
# What Tailscale said about the tailnet's devices and policy is shown for
# this long before the People tab asks again.
TAILNET_CACHE_S = 30
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000",
    "Cache-Control": "no-store",
}


@functools.cache
def shell() -> str:
    """A digest of the page's own files, the same for as long as this server
    runs. The page is served with it (SHELL_META) and every answer carries
    it (SHELL_HEADER). An open page that reads another digest was loaded
    from an earlier NullBill, before Update NullBill restarted the server:
    its script cannot read this server's answers, so it reloads itself."""
    digest = hashlib.sha256()
    for name, _ in STATIC.values():
        digest.update(resources.files("nullbill.web").joinpath("static", name).read_bytes())
    return digest.hexdigest()[:16]


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


def github_json(url: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "nullbill"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
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


# A job runs under sh, which writes its exit code next to its log, so a job
# outlives a restart of this server (the unit's KillMode=process) and the
# restarted server finds it again (App.load_jobs).
JOB_WRAPPER = '"$@"; echo $? > "$0.tmp" && mv "$0.tmp" "$0"'
# Stopping a job: each process under its sh gets SIGTERM, and SIGKILL if it
# is still there this many seconds later. sh itself records how it ended.
STOP_WAIT_S = 10


def descendants(pid: int) -> list[int]:
    """Every process under pid, by the parent each names in /proc."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            ppid = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    out: list[int] = []
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            out.append(child)
            todo.append(child)
    return out


@dataclass
class Job:
    id: str
    action: str
    args: list[str]
    log: Path
    started: float
    process: subprocess.Popen | None = None
    rc: int | None = None
    by: str = "owner"

    @property
    def rc_file(self) -> Path:
        return self.log.with_suffix(".rc")

    @property
    def meta_file(self) -> Path:
        return self.log.with_suffix(".json")

    def meta(self) -> dict:
        try:
            return json.loads(self.meta_file.read_text())
        except (OSError, ValueError):
            return {}

    def alive(self) -> bool:
        """For a job an earlier server started: is its sh still running?"""
        try:
            pid = self.meta()["pid"]
            return str(self.rc_file) in Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except (OSError, KeyError):
            return False

    def state(self) -> str:
        if self.rc is None and self.rc_file.exists():
            text = self.rc_file.read_text().strip()
            self.rc = int(text) if text.lstrip("-").isdigit() else 1
        if self.rc is None:
            if self.process is not None:
                ended = self.process.poll()
                if ended is not None and not self.rc_file.exists():
                    self.rc = ended or 1  # killed before sh could write its code
            elif not self.alive():
                self.rc = -1  # an earlier server's job that ended unrecorded
        if self.rc is None:
            return "running"
        return "done" if self.rc == 0 else "failed"


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
    github: Callable[[str, str], dict] = github_json
    # The page reaches the host over SSH for the agent's switch.
    host_run: Callable[[str, str], tuple[int, str, str]] = ssh_run
    # Tailscale's API with the owner's OAuth client, for the People tab.
    tailnet_client: Callable[[str, str], Tailnet] = Tailnet
    tailnet_seen: tuple[float, dict] | None = None
    people_busy: bool = False
    agent_command: list[str] = field(default_factory=lambda: list(AGENT_COMMAND))
    agent_user: str | None = AGENT_USER
    agent_home: Path = AGENT_HOME
    tools_socket: Path = TOOLS_SOCKET
    agent_cwd: Path = REPO

    def __post_init__(self) -> None:
        self.chat = chat.Chat(self)
        self.tools = chat.Tools(self)

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
                "answering": sorted(r["metric"].get("instance", "?") for r in up if r["value"][1] == "1"),
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
        # A token copied from a terminal can come with its line breaks or
        # spaces where the terminal wrapped it; neither kind ever holds any.
        value = re.sub(r"\s+", "", str(body.get("credential", "")))
        kind = next((v for prefix, v in AGENT_CREDENTIALS.items() if value.startswith(prefix)), None)
        if kind is None:
            raise NullBillError("paste an Anthropic API key (sk-ant-api...) or the token `claude setup-token` prints (sk-ant-oat...)")
        # Say what is wrong without echoing the secret: where, and only a
        # character no credential holds.
        odd = re.search(r"[^A-Za-z0-9_-]", value)
        if odd:
            char = odd.group()
            shown = repr(char) if char.isascii() and char.isprintable() else unicodedata.name(char, f"U+{ord(char):04X}")
            raise NullBillError(
                f"the pasted {kind[1]} has {shown} at character {odd.start() + 1} of {len(value)}, which a "
                f"{kind[1]} never holds; copy it again, or type that part by hand"
            )
        if len(value) > 512:
            raise NullBillError(f"the pasted {kind[1]} is {len(value)} characters long; copy only the token itself")
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

    # The OAuth client apply pushes the tailnet policy with: whether one is
    # saved, never the values.
    def tailscale_client(self) -> bool:
        data = Secrets(self.codec(self.home)).data
        return bool(data.get("tailscale_client_id") and data.get("tailscale_client_secret"))

    def save_tailscale_client(self, body: dict) -> None:
        client_id, secret = (str(body.get(k, "")).strip() for k in ("client_id", "secret"))
        if not TAILSCALE_CLIENT_ID.fullmatch(client_id) or not TAILSCALE_CLIENT_SECRET.fullmatch(secret):
            raise NullBillError("paste the client ID and the secret (tskey-client-...) Tailscale shows once you generate the OAuth client")
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            store = Secrets(self.codec(self.home))
            store.data |= {"tailscale_client_id": client_id, "tailscale_client_secret": secret}
            store.codec.save(store.data)

    # A GitHub token the agent opens pull requests on NullBill's repository
    # with; apply copies it to the operator. Its owner's login and id make
    # the noreply identity the agent's commits use.
    def agent_github(self) -> str | None:
        return Secrets(self.codec(self.home)).data.get("agent_github_login") or None

    def save_agent_github(self, body: dict) -> str | None:
        token = re.sub(r"\s", "", str(body.get("token", "")))
        if token and not GITHUB_TOKEN.fullmatch(token):
            raise NullBillError("paste the token GitHub shows once you generate it (github_pat_...)")
        found: dict = {}
        if token:
            try:
                user = self.github(GITHUB_API + "user", token)
                repo = self.github(GITHUB_API + "repos/" + GITHUB_REPO, token)
            except (OSError, ValueError) as exc:
                raise NullBillError(f"GitHub did not accept the token: {exc}") from exc
            if not (repo.get("permissions") or {}).get("push"):
                raise NullBillError(f"the token cannot push to {GITHUB_REPO}; give it Contents and Pull requests, read and write")
            found = {"agent_github_token": token, "agent_github_login": str(user["login"]), "agent_github_id": str(user["id"])}
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            store = Secrets(self.codec(self.home))
            store.data = {k: v for k, v in store.data.items() if not k.startswith("agent_github_")} | found
            store.codec.save(store.data)
        return found.get("agent_github_login")

    # Config and plan.
    def config(self) -> dict | None:
        path = self.home / "config.yaml"
        return yaml.safe_load(path.read_text()) if path.exists() else None

    def choices(self) -> dict:
        config = self.config() or {}
        login = (owner.person(config) or {}).get("login", "")
        return {
            "workloads": config.get("intent", {}).get("workloads", DEFAULT_WORKLOADS),
            "backups": config.get("services", {}).get("pbs", {}).get("enabled", True),
            "console": config.get("host", {}).get("console", {}).get("enabled", False),
            "owner_login": login,
            "ssh_keys": owner.keys(config),
        }

    # Disks besides the system disk: what the probe saw, and what the owner
    # chose for each. Erasing one asks for the end of its name, here and not
    # only on the page.
    def facts(self) -> dict | None:
        path = self.home / "probe" / "latest" / "facts.json"
        return json.loads(path.read_text()) if path.is_file() else None

    # Discrete GPUs: the probe reads their memory through the NVIDIA driver,
    # which a fresh Proxmox host lacks; then the owner says it on the page.
    def gpus(self) -> list[dict]:
        pins = (self.config() or {}).get("gpus") or {}
        return [
            {
                "pci": g["pci"],
                "name": g["name"],
                "probed_mib": g["vram_mib"],
                "vram_mib": g["vram_mib"] or pins.get(g["pci"], {}).get("vram_mib"),
            }
            for g in (self.facts() or {}).get("gpus", [])
            if not g.get("integrated")
        ]

    def _kept_parts(self, disk: dict, pin: dict) -> list[dict]:
        """The partitions of a disk that keeping it mounts: those its pin
        names, else every one a kept disk can hold."""
        wanted = pin.get("partitions") if pin.get("use") == "legacy" else None
        return [
            p
            for p in disk.get("partitions", [])
            if p.get("fstype") in legacy.FILESYSTEMS and p.get("parttype") not in legacy.SKIPPED_TYPES and (not wanted or p["n"] in wanted)
        ]

    def disks(self) -> list[dict]:
        facts = self.facts() or {}
        pins = (self.config() or {}).get("disks") or {}
        storage = guest_storage(facts) if facts.get("storage") else None
        # What the last setup run mounted read-write (apply's disks.json);
        # the probe may be older than that run.
        record = self.home / DISKS_FILE
        mounted = json.loads(record.read_text()).get("mounts", []) if record.exists() else []
        out = []
        for disk in facts.get("disks", []):
            if disk.get("system"):
                continue
            pin = pins.get(disk["id"]) or {}
            parts = [
                {
                    "n": p["n"],
                    "size_mib": p["size_mib"],
                    "what": legacy.describe(p),
                    "label": p.get("label"),
                    "keepable": p.get("fstype") in legacy.FILESYSTEMS and p.get("parttype") not in legacy.SKIPPED_TYPES,
                }
                for p in disk.get("partitions", [])
            ]
            kept = legacy.kept_mounts(disk)
            choice = DISK_CHOICES.get(pin.get("use"), "alone")
            if choice == "erase" and pin.get("evacuate"):
                choice = "copy"
            used = [k.get("used_bytes") for k in kept]
            out.append(
                {
                    "id": disk["id"],
                    "size_gib": disk["size_gib"],
                    "rotational": disk.get("rotational", False),
                    "health": (disk.get("smart") or {}).get("health", "unknown"),
                    "power_on_hours": (disk.get("smart") or {}).get("power_on_hours"),
                    "partitions": parts,
                    "choice": choice,
                    "for": "storage" if pin.get("use") == "storage" else "backups",
                    "rw": pin.get("mode") == "rw",
                    # Whether Windows left it fit for that (legacy.write_state).
                    "write": legacy.write_state(
                        self._kept_parts(disk, pin),
                        pin.get("discard_journal", []),
                        {m["n"] for m in mounted if m["disk"] == disk["id"] and m["mode"] == "rw"},
                    ),
                    "confirm": disk["id"][-6:],
                    # Mounted as a kept disk now: erasing it asks how its files are kept.
                    "kept": bool(kept),
                    "used_gib": math.ceil(sum(used) / legacy.GIB) if kept and None not in used else None,
                    "copy_to": storage["name"] if storage and storage["type"] in legacy.COPY_STORAGE else None,
                }
            )
        return out

    def _disk_pins(self, config: dict, chosen: dict, confirm: bool = True) -> dict:
        pins = dict(config.get("disks") or {})
        known = {d["id"]: d for d in self.disks()}
        for disk_id, want in chosen.items():
            if disk_id not in known:
                raise NullBillError(f"{disk_id}: no such disk in the last probe; read the hardware again under Setup")
            choice, rw = str(want.get("choice", "alone")), bool(want.get("rw"))
            discard = rw and bool(want.get("discard"))
            use_for = str(want.get("for", "backups"))
            if use_for not in DISK_USES:
                raise NullBillError(f"{disk_id}: an erased disk is for backups or storage")
            current = known[disk_id]
            same_for = choice not in ("erase", "copy") or use_for == current["for"]
            write = current["write"]
            same_mode = rw == current["rw"] and discard == (current["rw"] and write["discard"])
            if choice == current["choice"] and (choice != "keep" or same_mode) and same_for:
                continue  # unchanged: keep the pin as it is, with any keys the page does not show
            if choice == "alone":
                pins.pop(disk_id, None)
            elif choice == "keep":
                old = pins.get(disk_id) if current["choice"] == "keep" else None
                pin = {k: v for k, v in (old or {}).items() if k not in ("mode", "discard_journal")} | {"use": "legacy"}
                if rw:
                    pin |= {"mode": "rw"} | self._discard_pin(disk_id, current, pin, discard, want, confirm)
                pins[disk_id] = pin
            elif choice in ("erase", "copy"):
                if choice == "copy" and not current["kept"]:
                    raise NullBillError(f"{disk_id}: only a disk kept and mounted on the server can be copied before it is erased")
                if choice == "copy" and not current["copy_to"]:
                    raise NullBillError(f"{disk_id}: the guest storage is not LVM or LVM-thin, so the page cannot copy a disk there")
                # Also when only what it becomes changes: a backup disk made
                # storage loses its backups.
                if confirm and str(want.get("confirm", "")).strip() != current["confirm"]:
                    raise NullBillError(f"{disk_id}: type {current['confirm']}, the end of its name, to erase everything on it")
                pin = {"use": DISK_USES[use_for], "wipe": True}
                if choice == "copy":
                    pin["evacuate"] = current["copy_to"]
                elif current["kept"]:
                    # The owner says the files are elsewhere: nothing is copied.
                    pin["discard_data"] = True
                pins[disk_id] = pin
            else:
                raise NullBillError(f"{disk_id}: choose {DISK_CHOICE_TEXT}")
        return pins

    def _discard_pin(self, disk_id: str, current: dict, pin: dict, discard: bool, want: dict, confirm: bool = True) -> dict:
        """Changing files on a kept disk, as the last probe found it: refused
        with what to do while Windows has not left it fit, and, when the
        owner says what Windows left unfinished may go, the pin that names
        those journals, for the end of the disk's name typed out."""
        write = current["write"]
        state = write["state"]
        if state == "other":
            raise NullBillError(f"{disk_id}: its files cannot be changed: {write['why']}, and only NTFS can be changed")
        if state in legacy.RW_WHY and state != "unclean":
            raise NullBillError(
                f"{disk_id}: its files cannot be changed yet: {legacy.RW_WHY[state]}. {legacy.RW_FIX[state]}, then read the hardware again"
            )
        if state != "unclean":
            return {}
        if not discard:
            raise NullBillError(
                f"{disk_id}: its files cannot be changed yet: {legacy.RW_WHY[state]}. Shut it down fully from a Windows PC "
                "and read the hardware again, or tick Discard what Windows left unfinished"
            )
        if not write["can_discard"]:
            raise NullBillError(f"{disk_id}: its journal could not be read; read the hardware again")
        if confirm and not write["discard"] and str(want.get("discard_confirm", "")).strip() != current["confirm"]:
            raise NullBillError(f"{disk_id}: type {current['confirm']}, the end of its name, to discard what Windows left unfinished on it")
        disk = next(d for d in (self.facts() or {}).get("disks", []) if d["id"] == disk_id)
        return {"discard_journal": legacy.journals(self._kept_parts(disk, pin))}

    def files(self) -> dict | None:
        """Where the owner finds the kept disks' shares, once a plan has them."""
        path = self.home / "plan.yaml"
        plan = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        ip = (plan.get("guests") or {}).get("files", {}).get("ip")
        if not ip:
            return None
        has_password = bool(Secrets(self.codec(self.home)).data.get("files_share_password"))
        return {"ip": ip, "shares": shares(plan), "user": legacy.SHARE_USER, "password_set": has_password}

    def services(self) -> dict:
        path = self.home / "plan.yaml"
        plan = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        data = Secrets(self.codec(self.home)).data
        # The owner's own Forgejo user, once setup made it with a password.
        forgejo_user = data.get("forgejo_owner_user") if data.get("forgejo_owner_password") else None
        return services(plan, names.active(self.home), forgejo_user=forgejo_user, ssh_keys=len(owner.keys(self.config() or {})))

    def save_ssh_keys(self, body: dict) -> list[str]:
        """The owner's SSH public keys, for the workspace and their Forgejo user
        at the next setup run."""
        keys = owner.parse(str(body.get("keys", "")))
        config = self.config() or {}
        person = owner.person(config)
        if person is None:
            raise NullBillError("save your choices under Setup, What to run, first; they hold your Tailscale login")
        if keys:
            person["ssh_keys"] = keys
        else:
            person.pop("ssh_keys", None)
        build_model(config)
        _write_private(self.home / "config.yaml", yaml.safe_dump(config, sort_keys=False))
        return keys

    def password(self, name: str) -> str:
        if name not in PASSWORDS:
            raise NullBillError(f"the page shows no password for {name!r}")
        value = Secrets(self.codec(self.home)).data.get(PASSWORDS[name])
        if not value:
            raise NullBillError("run setup first; it makes the password")
        return value

    def gateway(self) -> dict | None:
        """Where the model gateway answers and which model it serves, once a
        plan has local models."""
        path = self.home / "plan.yaml"
        plan = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
        guest = (plan.get("guests") or {}).get(llm.GATEWAY, {})
        model = (plan.get("inference") or {}).get("model")
        if not guest.get("ip") or not model:
            return None
        has_key = bool(Secrets(self.codec(self.home)).data.get("model_gateway_key"))
        name = f"{llm.GATEWAY}.{plan['domain']}"
        return {"url": f"https://{name}:{llm.GATEWAY_PORT}/v1", "name": name, "ip": guest["ip"], "model": model, "key_set": has_key}

    def removals(self) -> list[str]:
        """Guests the last setup stopped before deleting, until the owner
        confirms. One that was gone from the plan and is in the saved plan
        again is no longer waiting: the next setup run keeps it."""
        path = self.home / REMOVALS
        try:
            waiting = json.loads(path.read_text()) if path.is_file() else {}
            names, gone = [str(n) for n in waiting.get("guests", [])], set(waiting.get("gone", []))
        except (ValueError, AttributeError):
            return []
        plan = self.home / "plan.yaml"
        planned = (yaml.safe_load(plan.read_text()) or {}).get("guests") or {} if plan.is_file() else {}
        return [n for n in names if not (n in gone and n in planned)]

    def planned_guests(self) -> set[str] | None:
        """The guests of the saved choices; None without a probe or choices."""
        config, facts = self.config(), self.facts()
        if config is None or facts is None:
            return None
        try:
            return set(planned_services(build_model(config), facts))
        except NullBillError:
            return None

    def existing_guests(self) -> set[str] | None:
        """The guests on the server: those of the last OpenTofu apply that
        finished (apply.GUESTS). A server built before apply kept that
        record follows the saved choices until they change, which is when
        save_choices writes it down. None when nothing says."""
        path = self.home / GUESTS
        try:
            if path.is_file():
                return {str(n) for n in json.loads(path.read_text())["guests"]}
        except (ValueError, KeyError, TypeError):
            return None
        if self.built() and not self.config_changed():
            return self.planned_guests()
        return None

    def planned_removals(self) -> list[str]:
        """Guests on the server that the saved choices no longer plan: the
        next setup run would delete them, and stops to ask first."""
        existing, planned = self.existing_guests(), self.planned_guests()
        if existing is None or planned is None:
            return []
        return sorted(existing - planned)

    def options(self, config: dict | None = None) -> dict | None:
        """What to run, choice by choice (workloads.options), for the saved
        choices or for `config`, the form as it stands on the page."""
        if config is None:
            config = self.config() or {"intent": {"workloads": DEFAULT_WORKLOADS}}
        try:
            model = build_model(config)
        except NullBillError as exc:
            return {"items": [], "memory": None, "errors": exc.messages, "adds": None, "removes": None, "advice": []}
        out = workloads.options(model, self.facts(), self.existing_guests())
        other = "Turn off a choice, or change one, until this no longer applies."
        out["advice"] = list(dict.fromkeys(progress.advice(m, default=other) for m in out["errors"]))
        return out

    def preview(self, body: dict) -> dict | None:
        """The page's form, not saved: what it would come to."""
        return self.options(self._choices_config(body, saving=False))

    def removal_notes(self, names: list[str]) -> dict[str, str]:
        """What deleting each of those guests loses."""
        if not names:
            return {}
        try:
            model = build_model(self.config() or {})
        except NullBillError:
            return {}
        return {name: workloads.loss(name, model) for name in names}

    def confirm_removals(self, seen: list[str] | None) -> None:
        """The owner pressed Delete and run setup: REMOVALS names every guest
        the page showed them (`seen`), those the last run stopped before
        deleting and those the saved choices no longer plan. apply deletes
        only guests it finds named there (apply.check_removals)."""
        stopped, planned = self.removals(), self.planned_removals()
        if not stopped and not planned:
            raise NullBillError("setup is not waiting to delete anything")
        if set(planned) - set(stopped):
            if seen is None or set(seen) != {*stopped, *planned}:
                names = ", ".join(sorted({*stopped, *planned}))
                raise NullBillError(f"what setup would delete is now {names}; look at it again under Setup, Build, before you confirm")
            path = self.home / REMOVALS
            try:
                gone = json.loads(path.read_text()).get("gone", []) if path.is_file() else []
            except (ValueError, AttributeError):
                gone = []
            names = sorted({*stopped, *planned})
            _write_private(path, json.dumps({"guests": names, "gone": sorted({*gone, *planned})}, indent=2) + "\n")

    def save_choices(self, body: dict) -> None:
        config = self._choices_config(body)
        build_model(config)  # validates against the schema before anything is written
        existing = self.existing_guests()
        if existing is not None and not (self.home / GUESTS).exists():
            _write_private(self.home / GUESTS, json.dumps({"guests": sorted(existing)}, indent=2) + "\n")
        _write_private(self.home / "config.yaml", yaml.safe_dump(config, sort_keys=False))

    @staticmethod
    def _games_config(config: dict, form: object) -> None:
        """What the page asks beside each game's tick, put into config.yaml's
        games: the owner's agreement to an EULA and the admins' names.
        Nothing changes without the form's `games` (the agent's choices have
        none: an agreement is the owner's to give)."""
        if not isinstance(form, dict):
            return
        catalog = build_model({})["games"]
        chosen = config.get("games") or {}
        for name, asked in form.items():
            if name not in catalog or not isinstance(asked, dict):
                continue
            game = dict(chosen.get(name) or {})
            if "eula" in catalog[name] and "eula" in asked:
                game["eula"] = bool(asked["eula"])
            if "admins" in catalog[name] and "admins" in asked:
                admins = list(dict.fromkeys(a for a in re.split(r"[\s,]+", str(asked["admins"])) if a))
                if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", a) for a in admins):
                    raise NullBillError(f"{catalog[name]['title']} admins are player names, as shown in the game, separated by commas")
                game["admins"] = admins
            # What equals the catalog is not written down.
            game = {key: value for key, value in game.items() if value != catalog[name].get(key)}
            if game:
                chosen[name] = game
            else:
                chosen.pop(name, None)
        if chosen:
            config["games"] = chosen
        else:
            config.pop("games", None)

    def _choices_config(self, body: dict, saving: bool = True) -> dict:
        """config.yaml with the page's form put into it. Saving checks what
        only a save needs: the owner's login and each erase confirmation."""
        workloads = [w for w in body.get("workloads", []) if w in WORKLOADS]
        login = str(body.get("owner_login", "")).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", login):
            if saving:
                raise NullBillError("your Tailscale login is the email address you sign in to Tailscale with")
            login = "owner@example.internal"
        install = self.install()
        if not install.get("host"):
            raise NullBillError("install.json is missing; run install.sh on the Proxmox host again")
        config = self.config() or {}
        config["target"] = {"ssh": f"root@{install['host']}"}
        config["intent"] = {"workloads": workloads}
        people = {n: p for n, p in config.get("people", {}).items() if p.get("role") != "owner"}
        keys = owner.keys(config)
        config["people"] = {"owner": {"role": "owner", "login": login} | ({"ssh_keys": keys} if keys else {}), **people}
        self._games_config(config, body.get("games"))
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
        if isinstance(body.get("gpus"), dict):
            self._gpu_pins(config, body["gpus"])
        if isinstance(body.get("disks"), dict):
            pins = self._disk_pins(config, body["disks"], confirm=saving)
            if pins:
                config["disks"] = pins
            else:
                config.pop("disks", None)
        return config

    def _gpu_pins(self, config: dict, answers: dict) -> None:
        """Pin the VRAM the owner gave, in GB, for GPUs the probe could not read."""
        unread = {g["pci"] for g in self.gpus() if not g["probed_mib"]}
        pins = config.get("gpus") or {}
        for pci, gb in answers.items():
            if pci not in unread:
                continue
            pin = {k: v for k, v in pins.get(pci, {}).items() if k != "vram_mib"}
            if gb not in (None, ""):
                try:
                    value = float(gb)
                except (TypeError, ValueError):
                    raise NullBillError(f"the GPU's memory is a number of GB, not {gb!r}") from None
                if not 1 <= value <= 256:
                    raise NullBillError("the GPU's memory is between 1 and 256 GB")
                pin["vram_mib"] = round(value * 1024)
            if pin:
                pins[pci] = pin
            else:
                pins.pop(pci, None)
        if pins:
            config["gpus"] = pins
        else:
            config.pop("gpus", None)

    def plan(self) -> dict:
        facts = self.home / "probe" / "latest" / "facts.json"
        config = self.config()
        if config is None or not facts.is_file():
            return {"text": None, "errors": [], "advice": [], "summary": None, "tailnet_policy": None}
        try:
            model = build_model(config)
            text = plan_text(model, facts.read_bytes())
        except NullBillError as exc:
            # One line of advice per cause, in the order the problems are listed.
            other = "Change your choices under Setup, What to run, so this no longer applies, and press Save and plan."
            advice = list(dict.fromkeys(progress.advice(m, default=other) for m in exc.messages))
            return {"text": None, "errors": exc.messages, "advice": advice, "summary": None, "tailnet_policy": None}
        _write_private(self.home / "plan.yaml", text)
        loaded = yaml.safe_load(text)
        policy = render_tailnet(loaded, model)["policy.json"]
        return {"text": text, "errors": [], "advice": [], "summary": plan_summary(loaded), "tailnet_policy": policy}

    # People besides the owner (nullbill/people.py): who they are and which
    # services each may open. A change goes to Tailscale first, whose own
    # tests of the policy must pass, and is saved only then.
    def tailnet(self) -> Tailnet | None:
        return tailscale.from_secrets(Secrets(self.codec(self.home)).data, self.tailnet_client)

    def planned(self, config: dict) -> tuple[dict, dict | None, list[str]]:
        """A config's model and plan, or no plan and why: nothing probed yet
        (no reasons), or the plan's problems."""
        model = build_model(config)
        facts = self.home / "probe" / "latest" / "facts.json"
        if not facts.is_file():
            return model, None, []
        try:
            return model, build_plan(model, json.loads(facts.read_bytes())), []
        except NullBillError as exc:
            return model, None, exc.messages

    def tailnet_wait(self, plan: dict | None, problems: list[str]) -> str | None:
        """Why a change to people cannot go to Tailscale now, or None when
        it can. Until then setup's tailnet stage is what brings it there."""
        if problems:
            return "The plan has problems, so what you save here waits. Settle them under Setup, Plan, then run setup."
        if plan is None or "vpn" not in strip_notes(plan)["guests"]:
            return "Nothing is planned yet. People you add now get their access with the first setup run."
        if not self.tailscale_client():
            return "No Tailscale OAuth client is saved, so what you save here waits. Paste one on the Tailscale tab, then run setup."
        if not self.built():
            return "People you add now get their access with the first setup run."
        return None

    def tailnet_now(self, fresh: bool) -> dict:
        """Each login's devices on the tailnet and the policy in force, or
        why Tailscale did not say."""
        seen = self.tailnet_seen
        if not fresh and seen and time.time() - seen[0] < TAILNET_CACHE_S:
            return seen[1]
        api = self.tailnet()
        try:
            found = {"devices": people.devices(api.devices()), "policy": api.policy()[0]}
        except TailscaleError as exc:
            found = {"error": "; ".join(exc.messages)}
        self.tailnet_seen = (time.time(), found)
        return found

    def people_view(self, fresh: bool = False) -> dict:
        config = self.config() or {}
        model, plan, problems = self.planned(config)
        wait = self.tailnet_wait(plan, problems)
        now = {} if wait else self.tailnet_now(fresh)
        on_tailnet = now.get("devices")
        rows = people.rows(model, plan, on_tailnet)
        by_name = names.active(self.home)
        root = (self.home / browserca.DIR / "ca.crt").is_file()
        for row in rows:
            if not row["owner"]:
                row["instructions"] = people.instructions(model, plan, row["name"], by_name, root)
                # The owner sends the root along (Access, Trust this server, has the file).
                row["root"] = root and people.wants_root(model, plan, row["name"])
        in_place = None
        if "policy" in now:
            in_place = now["policy"] == json.loads(render_tailnet(plan, model)["policy.json"])
        return {
            "people": rows,
            "shareable": people.shareable(model, plan),
            "limit": {"used": len(model["people"]), "max": model["tailnet"]["free_limits"]["users"]},
            "tailscale": {"waits": wait, "in_place": in_place, "error": now.get("error")},
            "strangers": people.strangers(model, on_tailnet) if on_tailnet is not None else [],
            "users_page": people.USERS_PAGE,
        }

    def change_people(self, change: Callable[[dict, dict], dict]) -> bool:
        """Save what `change(config, model)` returns, which differs from the
        config in `people` only. True when Tailscale has it already; False
        when the next setup run brings it there."""
        with self.lock:
            if self.people_busy or any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            self.people_busy = True
        try:
            path = self.home / "config.yaml"
            config = self.config()
            if config is None:
                raise NullBillError("save your choices under Setup, What to run, first; that names you as the owner")
            _, _, before = self.planned(config)
            candidate = change(config, build_model(config))
            model, plan, problems = self.planned(candidate)
            new = [m for m in problems if m not in before]
            if new:
                raise NullBillError(*new)
            ready = self.tailnet_wait(plan, problems) is None
            if ready:
                want = json.loads(render_tailnet(plan, model)["policy.json"])
                try:
                    tailscale.sync_policy(self.tailnet(), want, TAG, model["tailnet"]["free_limits"]["tagged_devices"])
                except TailscaleError as exc:
                    raise NullBillError(f"Tailscale did not take it, so nothing changed: {'; '.join(exc.messages)}") from exc
            saved = path.stat()
            _write_private(path, yaml.safe_dump(candidate, sort_keys=False))
            if ready:
                # In force already: not a choice that waits for a setup run
                # (config_changed).
                os.utime(path, ns=(saved.st_atime_ns, saved.st_mtime_ns))
            self.tailnet_seen = None
            return ready
        finally:
            self.people_busy = False

    def save_person(self, body: dict) -> dict:
        name = str(body.get("name", "")).strip().lower()
        picked = body.get("services") or []
        if not isinstance(picked, list):
            raise NullBillError("services is a list of names")
        services = [str(s) for s in picked]
        new = name not in ((self.config() or {}).get("people") or {})
        live = self.change_people(lambda config, model: people.save(config, model, name, str(body.get("login", "")), services))
        given = ", ".join(services) or "nothing"
        print(f"nullbill-web: person {name} {'added' if new else 'changed'}: {given}", file=sys.stderr, flush=True)
        what = "In force now." if live else "It takes effect with the next setup run."
        return {"ok": True, "message": f"{name} {'added' if new else 'saved'}. {what}", "added": new, **self.people_view(fresh=True)}

    def remove_person(self, body: dict) -> dict:
        name = str(body.get("name", ""))
        login = (((self.config() or {}).get("people") or {}).get(name) or {}).get("login", "")
        live = self.change_people(lambda config, _model: people.remove(config, name))
        print(f"nullbill-web: person {name} removed", file=sys.stderr, flush=True)
        gone = "can open nothing any more" if live else "is removed here; the next setup run takes their access away"
        return {"ok": True, "message": f"{name} {gone}. {self.forget_user(login)}", **self.people_view(fresh=True)}

    def forget_user(self, login: str) -> str:
        """Take a removed person's user off the tailnet, so their devices
        leave and their place is free; or say what is left for the owner."""
        by_hand = f"Remove {login} on Tailscale's Users page too: their account counts toward your people until then"
        api = self.tailnet()
        if api is None:
            return f"{by_hand}."
        try:
            user = people.find_user(api.users(), login)
            if user is None:
                return "They had not joined; if an invitation is still open, delete it on Tailscale's Users page."
            api.delete_user(str(user["id"]))
        except MissingScope:
            return f"{by_hand} (an OAuth client with Users, write, lets this page do it)."
        except TailscaleError as exc:
            return f"{by_hand} ({'; '.join(exc.messages)})."
        print(f"nullbill-web: tailnet user of a removed person deleted ({user['id']})", file=sys.stderr, flush=True)
        return "Their account and devices are off your Tailscale network."

    def sync_people(self) -> dict:
        """Put the plan's policy in place again: after someone changed it in
        Tailscale's admin console, or a change was saved while Tailscale
        could not be reached."""
        if not self.change_people(lambda config, _model: config):
            raise NullBillError("Tailscale gets the policy from the next setup run")
        return {"ok": True, "message": "Tailscale has the policy again.", **self.people_view(fresh=True)}

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

    def guest_ip(self, name: str) -> str | None:
        plan = self.home / "plan.yaml"
        loaded = (yaml.safe_load(plan.read_text()) or {}) if plan.exists() else {}
        return ((loaded.get("guests") or {}).get(name) or {}).get("ip")

    def agent(self, authorization: str | None, client: str) -> str | None:
        """Which agent a request is from: "operator" for the runtime agent's
        token from the operator, "remote:<name>" for a remote agent's token
        through the vpn guest, else None."""
        scheme, _, token = (authorization or "").partition(" ")
        if scheme != "Bearer" or not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        client = client.removeprefix("::ffff:")
        path = self.home / AGENT_TOKEN
        operator = self.guest_ip("operator")
        if operator is not None and client == operator and path.exists():
            if hmac.compare_digest(digest, path.read_text().strip()):
                return "operator"
        vpn = self.guest_ip("vpn")
        if vpn is not None and client == vpn:
            # Every name is compared, so the time taken does not tell which matched.
            matched = [name for name, known in self.remote_agents().items() if hmac.compare_digest(digest, known)]
            if matched:
                return f"remote:{matched[0]}"
        return None

    def remote_agents(self) -> dict[str, str]:
        path = self.home / REMOTE_AGENTS
        return json.loads(path.read_text()) if path.exists() else {}

    def add_remote_agent(self, body: dict) -> dict:
        """A new remote agent's token, shown once, with what it needs to reach the page."""
        name = str(body.get("name", "")).strip()
        if not REMOTE_AGENT_NAME.fullmatch(name):
            raise NullBillError("name the agent with lowercase letters, digits and dashes, such as laptop")
        page = self.guest_ip("nullbill")
        if page is None or self.guest_ip("vpn") is None:
            raise NullBillError("remote agents come in over Tailscale; plan the vpn guest and run setup first")
        token = secrets.token_urlsafe(32)
        with self.lock:
            known = self.remote_agents()
            if name in known:
                raise NullBillError(f"an agent named {name} exists; revoke it first to make a new token")
            known[name] = hashlib.sha256(token.encode()).hexdigest()
            _write_private(self.home / REMOTE_AGENTS, json.dumps(known, indent=2) + "\n")
        print(f"nullbill-web: remote agent {name} added", file=sys.stderr, flush=True)
        return {"name": name, "token": token, "url": f"https://{page}:8443/", "spki": self.spki()}

    def revoke_remote_agent(self, body: dict) -> None:
        name = str(body.get("name", ""))
        with self.lock:
            known = self.remote_agents()
            if known.pop(name, None) is None:
                raise NullBillError(f"no remote agent named {name!r}")
            _write_private(self.home / REMOTE_AGENTS, json.dumps(known, indent=2) + "\n")
        print(f"nullbill-web: remote agent {name} revoked", file=sys.stderr, flush=True)

    def spki(self) -> str:
        cert = self.home / "web" / "cert.pem"
        done = subprocess.run(["sh", "-c", SPKI, "sh", str(cert)], capture_output=True, text=True, check=False)
        return done.stdout.strip()

    def agent_jobs(self) -> list[dict]:
        listed = sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)
        return [{"id": j.id, "action": j.action, "state": j.state(), "rc": j.rc, "started": j.started} for j in listed]

    # What the runtime agent may do beyond verify: the owner's switch.
    def agent_powers(self) -> bool:
        return hostroot.enabled(self.home)

    def host_target(self) -> str | None:
        """Where the page reaches the host over SSH, as apply does."""
        configured = ((self.config() or {}).get("target") or {}).get("ssh")
        path = self.home / "plan.yaml"
        if not configured or not path.exists():
            return None
        return reach_target(self.home, configured, yaml.safe_load(path.read_text()) or {})

    def set_agent_powers(self, body: dict) -> None:
        """On: the agent's key gets root on the host. Off: the key is taken
        out first, and the switch is off even when that fails."""
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise NullBillError("send enabled: true or false")
        key, operator, target = hostroot.public_key(self.home), self.guest_ip("operator"), self.host_target()
        if enabled and not (key and operator and target):
            raise NullBillError("the agent's key comes from the operator: run setup first")
        rc, _, err = self.host_run(target, hostroot.command(hostroot.line(key, operator) if enabled else None)) if target else (0, "", "")
        if rc != 0 and enabled:
            raise NullBillError(f"could not give the agent's key root on the host: {err.strip()[:200] or f'exit {rc}'}")
        _write_private(self.home / hostroot.SWITCH, json.dumps({"enabled": enabled, "changed": time.time()}) + "\n")
        print(f"nullbill-web: agent root on the host: {'on' if enabled else 'off'}", file=sys.stderr, flush=True)
        if rc != 0:
            raise NullBillError(
                f"the agent can no longer run setup here, but taking its key off the host failed ({err.strip()[:200] or f'exit {rc}'}); "
                "stop the operator container in Proxmox to cut it off now"
            )

    def may(self, agent: str, action: str, read: bool = False) -> bool:
        """May this agent start this action, or with read, read its jobs' logs?"""
        if action in AGENT_ACTIONS:
            return True
        if agent != "operator" or not self.agent_powers():
            return False
        return read or action in POWER_ACTIONS

    def tell(self, message: str, tag: str) -> None:
        """One message to the owner's ntfy topic, or NullBillError."""
        topic = Secrets(self.codec(self.home)).data.get("ntfy_topic")
        if not topic:
            raise NullBillError("the owner has no ntfy topic yet, so they could not be told; set a password on the page first")
        try:
            self.send(NTFY + topic, message, {"Title": "NullBill agent", "Tags": tag})
        except OSError as exc:
            raise NullBillError(f"could not tell the owner through ntfy ({exc}); nothing started") from exc

    def tell_agent_jobs(self) -> None:
        """How each job an agent started ended, once."""
        for job in list(self.jobs.values()):
            if job.by == "owner" or job.state() == "running":
                continue
            meta = json.loads(job.meta_file.read_text())
            if meta.get("told"):
                continue
            ended = "finished" if job.state() == "done" else f"failed (exit {job.rc})"
            try:
                self.tell(f"{progress.JOB_TEXT.get(job.action, job.action)}, started by the agent, {ended}.", "robot")
            except NullBillError as exc:
                print(f"nullbill-web: {exc}", file=sys.stderr, flush=True)
                continue
            _write_private(job.meta_file, json.dumps(meta | {"told": time.time()}))

    def via_vpn(self, client: str) -> bool:
        return client.removeprefix("::ffff:") == self.network()[1]

    # Jobs.
    def start(self, action: str, stage: str | None = None, client: str = "", by: str = "owner", guests: list[str] | None = None) -> Job:
        if action == "keep-lan-closed" and not self.via_vpn(client):
            raise NullBillError("press Keep it closed on the page opened over Tailscale; that press is what shows Tailscale works")
        fixed = LAN_ACTIONS | SCHEDULED | REMOVE_ACTIONS
        if action not in ACTIONS and action not in fixed:
            raise NullBillError(f"unknown action {action!r}")
        if action in REMOVE_ACTIONS:
            self.confirm_removals(guests)
        with self.lock:
            if any(j.state() == "running" for j in self.jobs.values()):
                raise NullBillError("another job is still running")
            if self.people_busy:
                raise NullBillError("a change under Access, People is on its way to Tailscale; try again in a moment")
            jobs = self.home / "jobs"
            jobs.mkdir(mode=0o700, exist_ok=True)
            # To the millisecond: the weekly job and a press can share a second.
            now = time.time()
            job_id = time.strftime("%Y%m%dT%H%M%S", time.localtime(now)) + f"{int(now * 1000) % 1000:03d}-{action}"
            args = fixed[action] if action in fixed else [action] + (["--stage", stage] if stage else [])
            job = Job(job_id, action, args, jobs / f"{job_id}.log", time.time(), by=by)
            # The page's own agent announces setup runs and updates; the
            # owner approved or allowed the rest on the page.
            if by != "owner" and not (by == "agent" and action in AGENT_ACTIONS | {"probe"}):
                # Before the job exists: an action the owner is not told of does not run.
                self.tell(f"The agent started: {progress.JOB_TEXT.get(action, action)}{f' (stage {stage})' if stage else ''}", "robot")
            env = os.environ | {"NULLBILL_HOME": str(self.home), "PYTHONUNBUFFERED": "1", "ANSIBLE_FORCE_COLOR": "0"}
            fd = os.open(job.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                argv = ["sh", "-c", JOB_WRAPPER, str(job.rc_file), *self.command, *args]
                job.process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
            meta = {"action": action, "args": args, "started": job.started, "pid": job.process.pid, "by": by}
            _write_private(job.meta_file, json.dumps(meta))
            self.jobs[job_id] = job
            return job

    def stop(self, job_id: str) -> Job:
        """End a running job: what it finished stays, the rest waits for the
        next run. The page offers it when a run hangs."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.state() != "running":
                raise NullBillError("that job is not running any more")
            meta = job.meta()
            # The pid is this job's sh only while its command line names the job.
            if "pid" not in meta or not job.alive():
                raise NullBillError("could not find that job's processes; restart the NullBill guest to end it")
            _write_private(job.meta_file, json.dumps(meta | {"stopped": time.time()}))
        procs = descendants(meta["pid"])
        for pid in procs:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + STOP_WAIT_S
        while time.monotonic() < deadline and any(Path(f"/proc/{pid}").exists() for pid in procs):
            time.sleep(0.2)
        for pid in procs:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        print(f"nullbill-web: stopped job {job.id} ({len(procs)} processes)", file=sys.stderr, flush=True)
        return job

    def load_jobs(self) -> None:
        """Jobs an earlier server started, finished or still running."""
        for meta_file in sorted((self.home / "jobs").glob("*.json")):
            try:
                meta = json.loads(meta_file.read_text())
            except ValueError:
                continue
            job_id = meta_file.stem
            log = meta_file.with_suffix(".log")
            if job_id not in self.jobs and log.exists():
                self.jobs[job_id] = Job(job_id, meta["action"], meta["args"], log, meta["started"], by=meta.get("by", "owner"))

    def rebake_due(self, now: float) -> bool:
        """Sunday's 03:00 hour, once a week, after a first apply made the CI pools."""
        t = time.localtime(now)
        if (t.tm_wday, t.tm_hour) != (REBAKE_WEEKDAY, REBAKE_HOUR):
            return False
        stamp = self.home / "web" / "last-rebake"
        if stamp.exists() and now - float(stamp.read_text() or 0) < 6 * 86400:
            return False
        path = self.home / "plan.yaml"
        if not (self.home / "tofu").is_dir() or not path.exists():
            return False
        guests = (yaml.safe_load(path.read_text()) or {}).get("guests") or {}
        return any(g.get("kind") == "vm-pool" and g.get("zone") != "windows" for g in guests.values())

    def scheduled(self, now: float) -> Job | None:
        self.tell_agent_jobs()
        if not self.rebake_due(now):
            return None
        try:
            job = self.start("rebake-ci")
        except NullBillError:
            return None
        _write_private(self.home / "web" / "last-rebake", f"{now}\n")
        return job

    def job_view(self, job: Job, offset: int = 0) -> dict:
        data = job.log.read_bytes()
        state = job.state()
        ended = job.rc_file.stat().st_mtime if state != "running" and job.rc_file.exists() else None
        now = time.time()
        times = {"started": job.started, "now": now, "ended": ended or (None if state == "running" else now)}
        times["output"] = job.log.stat().st_mtime
        times["stopped"] = job.meta().get("stopped")
        return {
            "id": job.id,
            "action": job.action,
            "args": job.args,
            "state": state,
            "stopped": state == "failed" and bool(times["stopped"]),
            "rc": job.rc,
            "text": data[offset:].decode(errors="replace"),
            "offset": len(data),
            "started": job.started,
            "seconds": round((times["ended"] or now) - job.started),
            "progress": progress.summarize(job.action, data.decode(errors="replace"), state, times),
        }

    def last_jobs(self) -> dict:
        """Each action's latest job: how it ended, and when."""
        out: dict[str, dict] = {}
        for job in sorted(self.jobs.values(), key=lambda j: j.started):
            state = job.state()
            out[job.action] = {
                "id": job.id,
                "state": state,
                "started": job.started,
                "stopped": state == "failed" and bool(job.meta().get("stopped")),
            }
        return out

    def runs(self, limit: int = 30) -> list[dict]:
        """The latest jobs, newest first, for the page's Activity list."""
        out = []
        for job in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True)[:limit]:
            state = job.state()
            ended = job.rc_file.stat().st_mtime if state != "running" and job.rc_file.exists() else None
            seconds = round((ended or time.time()) - job.started)
            stopped = state == "failed" and bool(job.meta().get("stopped"))
            out.append(
                {
                    "id": job.id,
                    "action": job.action,
                    "state": state,
                    "stopped": stopped,
                    "started": job.started,
                    "seconds": seconds,
                    "by": job.by,
                }
            )
        return out

    def built(self) -> bool:
        """A setup run has finished at least once: the guests exist, even
        while a later run is going or after one stopped."""
        return any(j.action in ("apply", "remove-guests") and j.state() == "done" for j in self.jobs.values())

    def config_changed(self) -> bool:
        """The choices were saved after the start of the last setup run that
        finished, so the server does not follow them yet."""
        config = self.home / "config.yaml"
        good = [j.started for j in self.jobs.values() if j.action == "apply" and j.state() == "done"]
        return bool(good) and config.exists() and config.stat().st_mtime > max(good)

    def hardware(self) -> dict | None:
        """The last probe, in a line or two."""
        facts = self.facts()
        if not facts:
            return None
        cpu = facts.get("cpu") or {}
        return {
            "cpu": cpu.get("model"),
            "threads": cpu.get("threads"),
            "memory_mib": facts.get("memory_mib"),
            "gpus": [g.get("name") for g in facts.get("gpus") or [] if not g.get("integrated")],
            "disks": len(facts.get("disks") or []),
            "pve_version": facts.get("pve_version"),
        }

    def browser_root(self) -> Path | None:
        crt = self.home / browserca.DIR / "ca.crt"
        return crt if crt.is_file() else None

    def trust(self) -> dict:
        """The root certificate a device installs so browsers trust this server."""
        crt = self.browser_root()
        return {"ready": crt is not None, "fingerprint": browserca.fingerprint(crt) if crt else None}

    def state(self, authed: bool, client: str = "") -> dict:
        has_password = self.owner_file.exists()
        if not authed:
            return {"auth": False, "has_password": has_password}
        latest = max(self.jobs.values(), key=lambda j: j.started, default=None)
        stopped = self.removals()
        removals = [*stopped, *(n for n in self.planned_removals() if n not in stopped)]
        return {
            "auth": True,
            "has_password": has_password,
            "install": self.install(),
            "probed": (self.home / "probe" / "latest" / "facts.json").is_file(),
            "hardware": self.hardware(),
            "last_jobs": self.last_jobs(),
            "runs": self.runs(),
            "built": self.built(),
            "config_changed": self.config_changed(),
            "remote_agents_ready": self.guest_ip("nullbill") is not None and self.guest_ip("vpn") is not None,
            "choices": self.choices(),
            "disks": self.disks(),
            "gpus": self.gpus(),
            "files": self.files(),
            "services": self.services(),
            "gateway": self.gateway(),
            "removals": removals,
            "removals_stopped": bool(stopped),
            "removal_notes": self.removal_notes(removals),
            "options": self.options(),
            "plan": self.plan(),
            "alerts": self.alerts() if has_password else None,
            "agent_credential": self.agent_credential(),
            "tailscale_key": self.tailscale_key(),
            "tailscale_client": self.tailscale_client(),
            "remote_agents": sorted(self.remote_agents()),
            "trust": self.trust(),
            "agent_powers": self.agent_powers(),
            "agent_github": self.agent_github(),
            "lan": {"state": lan.state(self.home), "via_vpn": self.via_vpn(client), "minutes": lan.REVERT_AFTER_S // 60},
            "chat": self.chat.summary(),
            "job": self.job_view(latest) | {"text": ""} if latest else None,
        }


def plan_summary(plan: dict) -> dict:
    """The plan's memory and guests, for the page's table."""
    host = plan.get("host") or {}
    guests = [
        {
            "name": name,
            "kind": g.get("kind"),
            "zone": g.get("zone"),
            "cores": g.get("cores"),
            "memory_mib": g.get("memory_mib"),
            "count": g.get("count"),
            "installed": bool(g.get("installed")),
        }
        for name, g in (plan.get("guests") or {}).items()
    ]
    memory = {k: host.get(k) for k in ("host_mib", "reserve_mib", "guests_mib", "limits_mib", "unallocated_mib")}
    return {"memory": memory, "guests": guests}


def offset(query: str, name: str = "offset") -> int:
    value = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get(name, "0") or "0"
    return int(value) if value.isdigit() else 0


# A connection that sits idle this long is closed; one that has sent its
# last request gets this long to close its side first.
IDLE_S = 30
LINGER_S = 5


class PageServer(http.server.ThreadingHTTPServer):
    """The page's HTTPS server, kind to slow links.

    Phones reach the page through the vpn guest's Tailscale, whose
    userspace network stack carries each connection to the page as a
    second connection. A page that closes a socket with bytes still unread
    makes the kernel answer with a reset, and the stack passes the reset on
    to the phone, dropping what it had not yet delivered over a slow line:
    Firefox on mobile data said "The connection was reset". So connections
    stay open between requests (HTTP/1.1), the listen queue is long enough
    for a browser's parallel connections, and a closing connection says
    it is done (FIN) and then reads until the other side has closed too.
    """

    request_queue_size = 128
    daemon_threads = True

    def shutdown_request(self, request) -> None:  # noqa: ANN001
        try:
            request.shutdown(socket.SHUT_WR)
            request.settimeout(LINGER_S)
            deadline = time.monotonic() + LINGER_S
            while time.monotonic() < deadline and request.recv(4096):
                pass
        except OSError:
            pass
        self.close_request(request)


def make_server(app: App, address: tuple[str, int], cert: Path, key: Path) -> tuple[PageServer, ssl.SSLContext]:
    server = PageServer(address, make_handler(app))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(cert, key)
    # The handshake runs in each request's thread, so a stalled client
    # cannot hold up the accept loop.
    server.socket = tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server, tls


def make_handler(app: App) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "nullbill"
        sys_version = ""
        # Keep-alive: every answer carries its Content-Length (send).
        protocol_version = "HTTP/1.1"
        timeout = IDLE_S

        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def send(self, code: int, body: bytes, kind: str, headers: dict | None = None) -> None:
            self.send_response(code)
            fixed = SECURITY_HEADERS | {SHELL_HEADER: shell(), "Content-Type": kind, "Content-Length": str(len(body))}
            for name, value in (fixed | (headers or {})).items():
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
                if path == "/":
                    body = body.replace(SHELL_META, SHELL_META.replace(b'""', f'"{shell()}"'.encode()))
                return self.send(200, body, kind if kind.startswith("image/") and kind != "image/svg+xml" else f"{kind}; charset=utf-8")
            # Public: a certificate, which a phone downloads to install.
            if path == ROOT_CERT:
                crt = app.browser_root()
                if crt is None:
                    return self.json(404, {"error": "no root certificate yet; run setup first"})
                disposition = {"Content-Disposition": 'attachment; filename="nullbill-root.crt"'}
                return self.send(200, browserca.der(crt), "application/x-x509-ca-cert", disposition)
            if path.startswith("/api/agent/"):
                return self.agent_get(path, query)
            authed = app.valid(self.token())
            if path == "/api/state":
                return self.json(200, app.state(authed, self.client()))
            if not authed:
                return self.json(401, {"error": "sign in first"})
            if path == "/api/status":
                return self.json(200, app.status())
            if path == "/api/people":
                try:
                    return self.json(200, app.people_view(fresh="fresh=1" in query.split("&")))
                except NullBillError as exc:
                    return self.json(400, {"error": "; ".join(exc.messages)})
            if path == "/api/chat":
                return self.json(200, app.chat.view(offset(query, "after")))
            if path == "/api/agent-log":
                return self.json(200, {"entries": app.chat.audit_log()})
            if path == "/api/chat/notes":
                return self.json(200, {"notes": app.chat.notes()})
            if path == "/api/chat/output":
                try:
                    return self.json(
                        200, {"text": app.chat.output(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("id", ""))}
                    )
                except NullBillError as exc:
                    return self.json(404, {"error": exc.messages[0]})
            if path == "/api/chat/transcript":
                return self.json(200, {"text": app.chat.transcript()})
            match = re.fullmatch(r"/api/jobs/([0-9T]+-[a-z-]+)", path)
            if match and match[1] in app.jobs:
                return self.json(200, app.job_view(app.jobs[match[1]], offset(query)))
            return self.json(404, {"error": "not found"})

        def agent_authed(self) -> str | None:
            """The agent's name, logged with the request, or None after a 401."""
            agent = app.agent(self.headers.get("Authorization"), self.client())
            path = self.path.partition("?")[0]
            print(f"nullbill-web: agent {agent or 'refused'} from {self.client()}: {self.command} {path}", file=sys.stderr, flush=True)
            if agent is None:
                self.json(401, {"error": "not the runtime agent"})
            return agent

        def agent_get(self, path: str, query: str) -> None:
            agent = self.agent_authed()
            if not agent:
                return
            if path == "/api/agent/jobs":
                return self.json(200, app.agent_jobs())
            match = re.fullmatch(r"/api/agent/jobs/([0-9T]+-[a-z-]+)", path)
            job = app.jobs.get(match[1]) if match else None
            if job is not None and app.may(agent, job.action, read=True):
                return self.json(200, app.job_view(job, offset(query)))
            return self.json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            # The body is read before any answer: on a kept-alive
            # connection, unread bytes would be taken for the next request.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= 64 * 1024:
                self.close_connection = True
                return self.json(413, {"error": "request too large"})
            raw = self.rfile.read(length)
            if self.closed():
                return
            # A custom header cannot be sent cross-site without a CORS
            # preflight, which this server never grants.
            if self.headers.get("X-NullBill") != "1":
                return self.json(403, {"error": "missing X-NullBill header"})
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return self.json(400, {"error": "body is not JSON"})
            if match := re.fullmatch(r"/api/agent/(verify|apply|upgrade)", self.path):
                agent = self.agent_authed()
                if not agent:
                    return
                if not app.may(agent, match[1]):
                    refused = "the owner has not let the agent run setup and updates (Agent page, Settings, Root on Proxmox)"
                    return self.json(403, {"error": refused})
                stage = body.get("stage") if match[1] == "apply" else None
                if stage is not None and stage not in APPLY_STAGES:
                    return self.json(400, {"error": f"no stage {stage!r}; stages: {', '.join(APPLY_STAGES)}"})
                try:
                    return self.json(200, app.job_view(app.start(match[1], stage, client=self.client(), by=agent)))
                except NullBillError as exc:
                    return self.json(409, {"error": "; ".join(exc.messages)})
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
                if self.path == "/api/choices/preview":
                    return self.json(200, app.preview(body))
                if self.path == "/api/alerts":
                    app.save_alerts(body)
                    return self.json(200, {"ok": True, "alerts": app.alerts()})
                if self.path == "/api/agent":
                    app.save_agent_credential(body)
                    app.chat.welcome()
                    return self.json(200, {"ok": True, "agent_credential": app.agent_credential(), "chat": app.chat.summary()})
                if self.path == "/api/chat":
                    app.chat.say(str(body.get("text", "")))
                    return self.json(200, app.chat.view(int(body.get("after") or 0)))
                if self.path == "/api/chat/stop":
                    app.chat.stop()
                    return self.json(200, {"ok": True})
                if self.path == "/api/chat/new":
                    app.chat.new_conversation()
                    return self.json(200, {"ok": True})
                if self.path == "/api/chat/decide":
                    approve = body.get("approve")
                    if not isinstance(approve, bool):
                        raise NullBillError("send approve: true or false")
                    proposal = app.chat.decide(str(body.get("id", "")), approve, str(body.get("typed", "")), str(body.get("note", "")))
                    return self.json(200, {"ok": True, "state": proposal["state"]})
                if self.path == "/api/chat/answer":
                    app.chat.answer(str(body.get("id", "")), str(body.get("text", "")))
                    return self.json(200, {"ok": True})
                if self.path == "/api/chat/settings":
                    return self.json(200, {"ok": True, **app.chat.save_settings(body)})
                if self.path == "/api/chat/notes":
                    notes = app.chat.save_notes(str(body.get("notes", "")))
                    app.chat.audit(action="notes", by="owner", chars=len(notes))
                    return self.json(200, {"ok": True, "notes": notes})
                if self.path == "/api/agent-github":
                    return self.json(200, {"ok": True, "agent_github": app.save_agent_github(body)})
                if self.path == "/api/agent-powers":
                    app.set_agent_powers(body)
                    return self.json(200, {"ok": True, "agent_powers": app.agent_powers()})
                if self.path == "/api/remote-agents":
                    return self.json(200, app.add_remote_agent(body))
                if self.path == "/api/remote-agents/revoke":
                    app.revoke_remote_agent(body)
                    return self.json(200, {"ok": True, "remote_agents": sorted(app.remote_agents())})
                if self.path == "/api/people":
                    return self.json(200, app.save_person(body))
                if self.path == "/api/people/remove":
                    return self.json(200, app.remove_person(body))
                if self.path == "/api/people/sync":
                    return self.json(200, app.sync_people())
                if self.path == "/api/tailscale-client":
                    app.save_tailscale_client(body)
                    return self.json(200, {"ok": True, "tailscale_client": app.tailscale_client()})
                if self.path == "/api/vpn":
                    app.save_tailscale_key(body)
                    return self.json(200, {"ok": True, "tailscale_key": app.tailscale_key()})
                if self.path == "/api/ssh-keys":
                    return self.json(200, {"ok": True, "ssh_keys": app.save_ssh_keys(body)})
                if self.path == "/api/password-reveal":
                    return self.json(200, {"password": app.password(str(body.get("name", "")))})
                if self.path == "/api/alerts/test":
                    return self.json(200, {"ok": True, "sent": app.test_alerts()})
                if match := re.fullmatch(r"/api/jobs/([0-9T]+-[a-z-]+)/stop", self.path):
                    return self.json(200, app.job_view(app.stop(match[1])))
                if self.path == "/api/jobs":
                    stage = body.get("stage")
                    seen = body.get("guests")
                    guests = [str(n) for n in seen] if isinstance(seen, list) else None
                    job = app.start(str(body.get("action")), str(stage) if stage else None, self.client(), guests=guests)
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


class ToolServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def tools_handler(app: App) -> type[http.server.BaseHTTPRequestHandler]:
    """The page agent's tools call here (nullbill/agenttools.py), over a
    socket only the agent's user can reach, with the running turn's token."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            pass

        def address_string(self) -> str:
            return "agent"

        def reply(self, code: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            # Read the body first: a reply before it is read can reach a
            # client still sending it as a broken pipe.
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 256 * 1024))
            if self.path != "/call":
                return self.reply(404, {"error": "not found"})
            if not chat.valid_token(app.chat, self.headers.get("Authorization")):
                return self.reply(401, {"error": "this turn's token is not valid any more"})
            try:
                body = json.loads(raw or b"{}")
                name, args = str(body["name"]), body.get("args") or {}
                if not isinstance(args, dict):
                    raise ValueError("args is not an object")
            except (ValueError, KeyError) as exc:
                return self.reply(400, {"error": f"bad request: {exc}"})
            text, failed = app.tools.call(name, args)
            return self.reply(200, {"text": text, "is_error": failed})

    return Handler


def serve_tools(app: App) -> ToolServer:
    """The socket, in a directory only root and the agent's user may enter."""
    path = app.tools_socket
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = ToolServer(str(path), tools_handler(app))
    if app.agent_user:
        import pwd

        gid = pwd.getpwnam(app.agent_user).pw_gid
        os.chown(path.parent, 0, gid)
        os.chown(path, 0, gid)
    # Set, not asked of mkdir: the page's UMask=0077 turned mode=0o750 into
    # 0700, and the agent's tools got "Permission denied" on every call.
    path.parent.chmod(0o750)
    path.chmod(0o660)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def watch(app: App) -> None:
    """Failed runs for the agent to look into, and turns past their time."""
    while True:
        time.sleep(WATCH_S)
        try:
            app.chat.notice_jobs()
            app.chat.watch_turn()
        except Exception as exc:  # noqa: BLE001 - a watcher that dies would stop for good
            print(f"nullbill-web: agent watcher: {exc}", file=sys.stderr, flush=True)


def serve(home: Path, listen: str) -> None:
    host, _, port = listen.rpartition(":")
    app = App(home)
    app.load_jobs()
    app.ensure_setup_code()
    cert, key = ensure_certificate(home)
    server, tls = make_server(app, (host or "0.0.0.0", int(port)), cert, key)
    print(f"nullbill web on https://{listen}, certificate SHA-256 {fingerprint(cert)}", flush=True)

    def schedule() -> None:
        loaded = cert.stat().st_mtime
        while True:
            time.sleep(SCHEDULE_CHECK_S)
            # apply's ca stage signs the same key anew (apply.page_certificate);
            # connections from now on get the new certificate.
            if cert.stat().st_mtime != loaded:
                loaded = cert.stat().st_mtime
                tls.load_cert_chain(cert, key)
                print(f"nullbill web: new certificate, SHA-256 {fingerprint(cert)}", flush=True)
            app.scheduled(time.time())

    threading.Thread(target=schedule, daemon=True).start()
    try:
        serve_tools(app)
    except (OSError, KeyError) as exc:
        print(f"nullbill-web: the agent's tools are off: {exc}", file=sys.stderr, flush=True)
    threading.Thread(target=watch, args=(app,), daemon=True).start()
    server.serve_forever()
