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

import copy
import difflib
import functools
import hashlib
import hmac
import http.server
import json
import math
import os
import re
import secrets
import shlex
import shutil
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
from typing import BinaryIO

import yaml

from nullbill import (
    applied,
    backups,
    browserca,
    custom,
    direct,
    games,
    hostroot,
    lan,
    legacy,
    llm,
    mods,
    names,
    owner,
    people,
    pins,
    power,
    preview,
    release,
    tailscale,
    torrents,
    version,
)
from nullbill import plan as planning
from nullbill import sizes as sizing
from nullbill.apply import DISKS_FILE, FULL_NEXT, GUESTS, NTFY, REMOVALS, SPKI, STAGES, Context, built_guests, split_target
from nullbill.artifacts import render_all
from nullbill.cli import plan_text, reach_target
from nullbill.errors import NullBillError, Refused
from nullbill.model import POWER_DEFAULTS, build_model
from nullbill.plan import build_plan, guest_storage, planned_services
from nullbill.probe import ssh_run
from nullbill.render import strip_notes
from nullbill.secretfile import Codec, Secrets, Sops
from nullbill.tailnet import TAG, render_tailnet
from nullbill.tailscale import MissingScope, Tailnet, TailscaleError
from nullbill.upgrade import LAST as LAST_UPDATE
from nullbill.web import backupview, chat, demand, gameroom, progress, workloads
from nullbill.web import review as reviewing
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
# The page's own nightly job (docs/adr/0022-latest-stable.md): fetch main,
# install what it brings and run setup when that changes what setup builds.
# A step the owner is asked about (nullbill/pins.py) stops it before it
# installs anything. Between these hours, local time, once a night, after
# the backups and whenever no other job runs; its state is in NIGHTLY.
NIGHTLY = "web/nightly.json"
NIGHTLY_HOURS = range(3, 6)
# Setup runs of everything, as the page starts them. The owner starts one
# only with the id of the review the page showed them (App.review).
SETUP_ACTIONS = {"apply", *REMOVE_ACTIONS}
# The page asks the repository for main's newest commit once a day, and when
# the owner presses Check for updates. What it found is kept here.
UPDATE_CHECK = "web/update-check.json"
UPDATE_CHECK_S = 24 * 3600
# Signed-in browsers, by the SHA-256 of each session's token, so an update
# restarting the page does not sign the owner out in the middle of it.
SESSIONS = "web/sessions.json"
SCHEDULE_CHECK_S = 60
ROOT_CERT = "/nullbill-root.crt"
# An optional Healthchecks.io check notices silence; alerts go to NTFY.
HEALTHCHECKS = re.compile(r"https://hc-ping\.com/[A-Za-z0-9_-]{8,}(/[a-z0-9_-]+)?")
# The page's own origin, as the browser that asked for a test alert names it.
ORIGIN = re.compile(r"https://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?")
# Which test alerts were sent and when one was last opened on a device.
HEARD = "web/alerts-heard.json"
# The agent CLI signs in with an Anthropic API key or a Claude subscription
# token from `claude setup-token`; each is kept under its own name.
AGENT_CREDENTIALS = {"sk-ant-api": ("anthropic_api_key", "API key"), "sk-ant-oat": ("claude_oauth_token", "subscription token")}
# The runtime agent on the operator may start verify and read verify's
# output (docs/runbooks/agent-access.md): a bearer token whose SHA-256 apply
# writes here, accepted only from the operator's address. It cannot start
# anything else, read other jobs' logs, or use the owner's API.
AGENT_TOKEN = "web/agent-token.sha256"
AGENT_ACTIONS = {"verify"}
# Who a job the nightly update starts is `by`.
NIGHTLY_BY = "nightly"
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
REVIEW_AGAIN = "what setup would do is not what this page showed you; look at it again under Setup, Review, and start it there"
SHELL_HEADER = "X-NullBill-Shell"
SHELL_META = b'<meta name="nb-shell" content="">'
# The page names its script and styles with the digest, so a browser or the
# service worker never pairs one version's page with another's files.
VERSIONED = ("app.js", "style.css", "theme.js")
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
# What runs on demand is asked of the host over SSH, at most this often.
POWER_CACHE_S = 5
# A start waits for the guest to boot, a stop for a clean shutdown: a VM
# gets three minutes for it (nb-power's stop_guest).
POWER_TIMEOUT_S = 330
# A game server's block on the Services page (web/gameroom.py): its report
# is asked for at most this often, and a new world may take this long
# (Terraria's largest, with the wait nb-game itself gives it).
GAME_CACHE_S = 5
GAME_TIMEOUT_S = 720
STREAM_CHUNK = 256 * 1024


def ssh_pipe(target: str, command: str) -> subprocess.Popen:
    """A command on the host with its input and output as they are, for a
    file on its way to or from a guest."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command]
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


# The path devices take to the server (nullbill/direct.py) is read on the
# host while Access, Tailscale is open, at most this often.
PATH_CACHE_S = 10
# A close of the home network that ran out is asked of the host this often
# until the host answers.
LAN_ASK_S = 30
# What an open page shows changes when one of these does, under
# NULLBILL_HOME (App.stamp). Not plan.yaml, which each look at the page
# writes again, and not direct.json, which the page reads for itself.
STAMPED = (
    "config.yaml",
    "probe/latest/facts.json",
    "lan.json",
    "names.json",
    "guests.json",
    "removals.json",
    "releases.json",
    "disks.json",
    "secrets.sops.yaml",
    "applied.json",
    "apply-full-next",
    "browser-ca/ca.crt",
    "web/owner.json",
    "web/update-check.json",
    "web/last-update.json",
    NIGHTLY,
    "web/agent-powers.json",
    "web/alerts-heard.json",
    "web/remote-agents.json",
    backupview.KEY_SAVED,
)
# The runs that end with the checks, or are the checks.
CHECKED = ("apply", "remove-guests", "verify")
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

    @property
    def plan_file(self) -> Path:
        """The plan a setup run started with (web/review.py)."""
        return self.log.with_suffix(".plan.yaml")

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


def problems(view: dict, other: str | None, model: dict | None = None) -> dict:
    """A view's `errors` with what to do: `problems` pairs each with the
    advice for its cause where the page knows the cause, and `advice` lists
    every line once, in the problems' order, `other` standing in for a
    cause it does not know. A memory problem that names guests set above
    their least keeps them apart, a line each (`items`), with the model."""
    known = [progress.specific(m) for m in view["errors"]]
    view["problems"] = [{"text": m, "advice": a} | raised_apart(m, model) for m, a in zip(view["errors"], known, strict=True)]
    view["advice"] = list(dict.fromkeys(a or other for a in known if a or other))
    return view


def raised_apart(message: str, model: dict | None) -> dict:
    """plan's memory problem without its last sentence, and that sentence's
    guests as items: each by the name the page gives it, with its memory
    and its least."""
    if model is None or planning.RAISED not in message:
        return {}
    text, names = message.rsplit(f". {planning.RAISED}", 1)
    items = []
    for name in names.split(", "):
        if not sizing.sizable(model, name):
            return {}
        set_to = (model.get("overrides", {}).get(name) or {}).get("memory_mib", sizing.least(model, name)["memory_mib"])
        items.append(f"{sizing.label(model, name)}: {sizing.gib(set_to)}, its least {sizing.gib(sizing.least(model, name)['memory_mib'])}")
    return {"text": text, "items": items}


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
    # Starting and stopping what runs on demand, which takes longer.
    host_act: Callable[[str, str], tuple[int, str, str]] = functools.partial(ssh_run, timeout=POWER_TIMEOUT_S)
    # Worlds: making one takes minutes, and a world file is bytes, not text.
    host_long: Callable[[str, str], tuple[int, str, str]] = functools.partial(ssh_run, timeout=GAME_TIMEOUT_S)
    host_pipe: Callable[[str, str], subprocess.Popen] = ssh_pipe
    # Modrinth's list of plugins and mods (nullbill/mods.py).
    modrinth: Callable[..., object] = mods.fetch
    game_seen: dict[str, tuple[float, dict]] = field(default_factory=dict)
    game_acting: dict[str, str] = field(default_factory=dict)
    power_seen: tuple[float, dict | None] | None = None
    power_acting: dict[str, str] = field(default_factory=dict)
    path_seen: tuple[float, dict | None] | None = None
    lan_asked: float = 0.0
    # What each finished run's checks came to, read from its log once.
    checked: dict = field(default_factory=dict, repr=False)
    # Tailscale's API with the owner's OAuth client, for the People tab.
    tailnet_client: Callable[[str, str], Tailnet] = Tailnet
    tailnet_seen: tuple[float, dict] | None = None
    # How each failed run ended, for Activity's list and Overview.
    failed_stages: dict = field(default_factory=dict, repr=False)
    people_busy: bool = False
    agent_command: list[str] = field(default_factory=lambda: list(AGENT_COMMAND))
    agent_user: str | None = AGENT_USER
    agent_home: Path = AGENT_HOME
    tools_socket: Path = TOOLS_SOCKET
    agent_cwd: Path = REPO
    # The checkout this NullBill runs from (nullbill/version.py).
    repo: Path = REPO
    version_seen: tuple[tuple, dict] | None = None
    foreseen: tuple[tuple, dict | None] | None = None
    update_seen: tuple[tuple, dict | None] | None = None
    checking: bool = False

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
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + SESSION_S
            self._save_sessions()
            return token

    def valid(self, token: str | None) -> bool:
        with self.lock:
            expiry = self.sessions.get(hashlib.sha256((token or "").encode()).hexdigest())
            return expiry is not None and expiry > time.time()

    def logout(self, token: str | None) -> None:
        with self.lock:
            self.sessions.pop(hashlib.sha256((token or "").encode()).hexdigest(), None)
            self._save_sessions()

    def _save_sessions(self) -> None:
        now = time.time()
        self.sessions = {digest: expiry for digest, expiry in self.sessions.items() if expiry > now}
        _write_private(self.home / SESSIONS, json.dumps(self.sessions) + "\n")

    def load_sessions(self) -> None:
        """Sessions an earlier server kept, so its restart signs nobody out."""
        try:
            saved = json.loads((self.home / SESSIONS).read_text())
            kept = {str(digest): float(expiry) for digest, expiry in saved.items() if float(expiry) > time.time()}
        except (OSError, ValueError, AttributeError, TypeError):
            return
        with self.lock:
            self.sessions.update(kept)

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
                    "instance": a["labels"].get("instance", ""),
                }
                for a in self.fetch(api + "alerts")["data"]["alerts"]
                if a.get("state") == "firing" and a["labels"].get("alertname") != "Watchdog"
            ]
            up = [(r["metric"].get("instance", "?"), r["value"][1]) for r in query("up")]
            # Off on purpose is not down (nb-power's metrics, from the host).
            on_demand = bool((plan or {}).get("power"))
            off = {r["metric"].get("guest", "?") for r in query("nb_power_guest_off == 1")} if on_demand else set()
            asks = query("nb_power_group_idle_seconds and on (group) (nb_power_group_asks == 1)") if on_demand else []
            return {
                "monitoring": "ok",
                "at": time.time(),
                "alerts": sorted(alerts, key=lambda a: (a["severity"] != "page", a["name"])),
                "targets": sum(1 for name, _ in up if name not in off),
                "down": sorted(name for name, value in up if value == "0" and name not in off),
                "answering": sorted(name for name, value in up if value == "1"),
                "off": sorted(off),
                "asks": sorted(
                    (
                        {"name": r["metric"].get("group", "?"), "title": r["metric"].get("title", "?"), "idle_s": int(float(r["value"][1]))}
                        for r in asks
                    ),
                    key=lambda a: a["name"],
                ),
                "security_waiting": instances('sum by (instance) (apt_upgrades_pending{origin=~".*-security.*"}) > 0'),
                "reboot_waiting": instances("node_reboot_required == 1"),
            }
        except (OSError, ValueError, KeyError) as exc:
            return {"monitoring": "unreachable", "at": time.time(), "error": str(exc)[:200]}

    def stamp(self) -> str:
        """Changes whenever what /api/state says may have: a job started or
        ended, or a file the state is read from was written. Every answer
        to the page's regular question for its status carries it, so an open
        page asks for the state again when something happened elsewhere (a
        run the agent or the weekly job started, a choice saved on another
        device) and not otherwise."""
        seen: list[object] = [(job.id, job.state()) for job in sorted(self.jobs.values(), key=lambda j: j.started)[-5:]]
        for name in STAMPED:
            try:
                seen.append((self.home / name).stat().st_mtime_ns)
            except OSError:
                seen.append(None)
        seen.append(lan.overdue(self.home))
        return hashlib.sha256(json.dumps(seen).encode()).hexdigest()[:16]

    # Alerts.
    def alerts(self) -> dict:
        data = Secrets(self.codec(self.home)).data
        topic = data.get("ntfy_topic")
        return {
            "ntfy_url": NTFY + topic if topic else None,
            "healthchecks_url": data.get("healthchecks_url", ""),
            "heard": self.heard(topic)["at"] if topic else None,
        }

    # Whether alerts reach a device, which the server cannot ask ntfy: each
    # test alert links to the page with a one-time word, and opening that
    # link on the device it reached says so. A new topic starts over.
    def heard(self, topic: str) -> dict:
        try:
            found = json.loads((self.home / HEARD).read_text())
        except (OSError, ValueError):
            found = {}
        mark = hashlib.sha256(topic.encode()).hexdigest()[:16]
        if not isinstance(found, dict) or found.get("topic") != mark:
            return {"topic": mark, "sent": [], "at": None}
        return {"topic": mark, "sent": list(found.get("sent") or [])[-5:], "at": found.get("at")}

    def alert_heard(self, word: str) -> float:
        topic = Secrets(self.codec(self.home)).data.get("ntfy_topic")
        if not topic:
            raise NullBillError("set a password first; that makes the ntfy topic")
        with self.lock:
            record = self.heard(topic)
            if hashlib.sha256(str(word).encode()).hexdigest() not in record["sent"]:
                raise NullBillError("that link is from an older test alert: send another from Alerts")
            record["at"] = time.time()
            _write_private(self.home / HEARD, json.dumps(record) + "\n")
        return record["at"]

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

    def test_alerts(self, origin: str = "") -> list[str]:
        """Send one ntfy message and one Healthchecks.io ping; name what was
        sent. With the page's origin, opening the message opens the page,
        which then knows alerts reach a device."""
        alerts = self.alerts()
        if not alerts["ntfy_url"]:
            raise NullBillError("set a password first; that makes the ntfy topic")
        sent = []
        try:
            title = {"Title": "NullBill: test alert", "Tags": "white_check_mark"}
            body = "Alerts from your server reach this device."
            if ORIGIN.fullmatch(origin):
                word = secrets.token_urlsafe(16)
                title["Click"] = f"{origin}/#alerts/heard/{word}"
                body += " Tap to tell the page."
                with self.lock:
                    record = self.heard(Secrets(self.codec(self.home)).data["ntfy_topic"])
                    record["sent"] = [*record["sent"][-4:], hashlib.sha256(word.encode()).hexdigest()]
                    _write_private(self.home / HEARD, json.dumps(record) + "\n")
            self.send(alerts["ntfy_url"], body, title)
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
        # A blank disk the plan took for the backups without a pin.
        try:
            backup_disk = backups.disk(self.saved_plan())
        except (KeyError, TypeError, AttributeError):
            backup_disk = None
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
                    "backups": not pin and disk["id"] == backup_disk,
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

    def path(self) -> dict | None:
        """How devices reach the vpn guest right now (direct.py), read on
        the host at most every PATH_CACHE_S and kept, as apply and verify
        keep it. `live` is False when the host could not be asked (nothing
        built, or a job is at work on it): what is shown then is the last
        reading, which says when it was made."""
        target = self.host_target() if self.built() else None
        config = self.config()
        with self.lock:
            busy = any(job.state() == "running" for job in self.jobs.values())
            seen = self.path_seen
        if target is None or config is None or busy:
            found = direct.recorded(self.home)
            return found | {"live": False} if found else None
        now = time.monotonic()
        if seen is None or now - seen[0] > PATH_CACHE_S:
            try:
                found = direct.find(self.saved_plan(), build_model(config), target, self.host_run)
            except (NullBillError, KeyError):
                found = None
            if found is not None:
                direct.record(self.home, found)
            seen = (now, direct.recorded(self.home) if found is not None else None)
            with self.lock:
                self.path_seen = seen
        return seen[1] | {"live": True} if seen[1] else None

    def port_mapping(self) -> bool:
        """Whether the router asks the home router for Tailscale's port (direct.py)."""
        return (self.config() or {}).get("tailnet", {}).get("port_mapping", True) is not False

    def save_port_mapping(self, body: dict) -> bool:
        """Off for an owner who forwards the port by hand, or wants none open;
        the next setup run tells the router."""
        asked = body.get("enabled") is True
        config = self.config()
        if config is None:
            raise NullBillError("save your choices under Setup, What to run, first")
        tailnet = config.setdefault("tailnet", {})
        if asked:
            tailnet.pop("port_mapping", None)  # the catalog's default
        else:
            tailnet["port_mapping"] = False
        if not tailnet:
            del config["tailnet"]
        build_model(config)
        _write_private(self.home / "config.yaml", yaml.safe_dump(config, sort_keys=False))
        return asked

    # Backups (web/backupview.py): where they go, what the host measured,
    # and the key they are encrypted with.
    def backups_view(self, plan: dict | None = None) -> dict:
        plan = plan or self.plan()
        config = self.config()
        try:
            model = build_model(config) if config else None
        except NullBillError:
            model = None
        built = self.built_plan()
        secret = Secrets(self.codec(self.home)).data.get("pbs_encryption_key")
        return backupview.view(
            config,
            model,
            self.facts(),
            self.saved_plan() if plan["text"] else None,
            yaml.safe_load(built) if built else None,
            backupview.key_view(secret, self._key_record()),
        )

    def _key_record(self) -> dict:
        try:
            found = json.loads((self.home / backupview.KEY_SAVED).read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def backups_live(self) -> dict:
        """The host's last reading of the backups, from Prometheus."""
        built = self.built_plan()
        plan = yaml.safe_load(built) if built else None
        monitoring = ((plan or {}).get("guests") or {}).get("monitoring")
        if not monitoring:
            return {"state": "none", "at": time.time()}
        api = f"http://{monitoring['ip']}:9090/api/v1/query?"
        return backupview.live(plan, lambda expr: self.fetch(api + urllib.parse.urlencode({"query": expr}))["data"]["result"])

    def save_backups(self, body: dict) -> None:
        """Backups on, at the place the owner picked. Saved only when the
        plan still holds with the backup server in it; a setup run then
        makes it so."""
        config = self.config()
        if config is None or self.facts() is None:
            raise NullBillError("read the hardware and save your choices under Setup first")
        new = backupview.save(config, self.backups_view(), body, lambda c, chosen: self._disk_pins(c, chosen))
        _, _, reasons = self.planned(new)
        if reasons:
            raise Refused("The plan does not hold with these backups", reasons)
        self._save_config(new)

    def backup_key(self) -> str:
        """The key the backups are encrypted with, for the owner to keep off
        the server; the page then says when this key was last taken."""
        secret = Secrets(self.codec(self.home)).data.get("pbs_encryption_key")
        if not secret:
            raise NullBillError("no key yet; the first setup run with backups on makes it")
        record = {"at": time.time(), "fingerprint": backupview.key_view(secret, {})["fingerprint"]}
        _write_private(self.home / backupview.KEY_SAVED, json.dumps(record) + "\n")
        return secret

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

    # On demand (nullbill/power.py): what the host's nb-power says is on,
    # and the owner's Start, Stop and Keep on.
    def saved_plan(self) -> dict:
        path = self.home / "plan.yaml"
        return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}

    def on_demand(self, fresh: bool = False) -> dict | None:
        """What runs on demand and what each is doing; None when nothing
        does. The host is asked only when the plan or a change not built
        yet says something may run on demand there."""
        planned = demand.rules(self.saved_plan())
        if not planned and not self.config_changed():
            return None
        target = self.host_target() if self.built() else None
        if target is None:
            return demand.view(planned, None, {}, "Nothing is on the server yet: run setup.") if planned else None
        now = time.monotonic()
        with self.lock:
            seen = self.power_seen
        if fresh or seen is None or now - seen[0] > POWER_CACHE_S:
            live = power.status(target, self.host_run)
            with self.lock:
                self.power_seen = (now, live)
        else:
            live = seen[1]
        if not planned and not (live and live["groups"]):
            return None
        why = None if live else "The server does not say what is on right now. If you just chose what runs on demand, run setup."
        with self.lock:
            acting = dict(self.power_acting)
        return demand.view(planned, live, acting, why)

    def power_act(self, body: dict, by: str = "the page") -> dict:
        """Start or stop a workload that runs on demand, or keep it on. A
        start that does not fit and a stop of something in use change
        nothing and say so, so the page asks before it sends it again with
        what the owner agreed to (`stop`, `force`)."""
        target = self.host_target() if self.built() else None
        live = power.status(target, self.host_run) if target else None
        if live is None:
            raise NullBillError("the server does not answer for what runs on demand; run setup")
        groups = {g["name"]: g for g in live["groups"]}
        action, name, line = demand.command(body, set(groups), by)
        group = groups[name]
        if action == "stop" and group.get("setup"):
            raise NullBillError(f"a setup run is using {group['title']} and stops it again when it is done")
        if action == "stop" and group.get("busy") and body.get("force") is not True:
            return {"ok": False, "busy": group["busy"], "title": group["title"], "view": self.on_demand()}
        with self.lock:
            if name in self.power_acting:
                raise NullBillError(f"{group['title']} is {self.power_acting[name]}; wait for it")
            if action != "keep":
                self.power_acting[name] = "starting" if action == "start" else "stopping"
        print(f"nullbill-web: on demand: {action} {name} by {by}", file=sys.stderr, flush=True)
        try:
            rc, out, err = self.host_act(target, line)
        finally:
            with self.lock:
                self.power_acting.pop(name, None)
                self.power_seen = None
        try:
            answer = json.loads(out)
        except ValueError:
            answer = None
        if rc == power.NO_ROOM and isinstance(answer, dict) and answer.get("error") == "no-room":
            return {"ok": False, "no_room": answer, "title": group["title"], "view": self.on_demand(fresh=True)}
        if rc != 0 or not isinstance(answer, dict) or not answer.get("ok"):
            said = answer.get("error") if isinstance(answer, dict) else None
            raise NullBillError(str(said or err.strip()[:300] or f"nb-power: exit {rc}"))
        keys = ("already", "stopped", "warnings", "keep")
        return {"ok": True, "title": group["title"], **{k: answer[k] for k in keys if k in answer}, "view": self.on_demand(fresh=True)}

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

    def release_waiting(self) -> list[str]:
        """Guests the next full setup run upgrades to the Debian release
        guests run now: those of the last finished OpenTofu apply that no
        run has found on it yet (release.unknown). Read from what the page
        has; the server is not asked."""
        config, plan = self.config(), self.home / "plan.yaml"
        if config is None or not plan.is_file():
            return []
        try:
            model = build_model(config)
            planned = yaml.safe_load(plan.read_text()) or {"guests": {}}
            # A server built by a version that kept no list of its guests has them all the same.
            return release.unknown(self.home, planned, model, built_guests(self.home, planned))
        except (NullBillError, ValueError, KeyError, TypeError):
            return []

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
            return problems({"items": [], "power": [], "memory": None, "errors": exc.messages, "adds": None, "removes": None}, None)
        out = workloads.options(model, self.facts(), self.existing_guests(), self.home)
        return problems(out, "Turn off a choice, or change one, until this no longer applies.", model)

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
                raise NullBillError(f"what setup would delete is now {names}; look at it again under Setup, Review, before you confirm")
            path = self.home / REMOVALS
            try:
                gone = json.loads(path.read_text()).get("gone", []) if path.is_file() else []
            except (ValueError, AttributeError):
                gone = []
            names = sorted({*stopped, *planned})
            _write_private(path, json.dumps({"guests": names, "gone": sorted({*gone, *planned})}, indent=2) + "\n")

    def save_choices(self, body: dict) -> None:
        config = self._choices_config(body)
        pasted = self._pasted_vpn(config, body.get("torrents"), socket.gethostbyname)
        if pasted is None:
            return self._save_config(config)
        # The file first: a server the firewall opens to without its file
        # would stop the next setup run, a file without that does nothing.
        build_model(config)
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError(
                    "a WireGuard file is saved with NullBill's secrets, which the running job holds; wait until it finishes"
                )
            Secrets(self.codec(self.home)).set(torrents.VPN_SECRET, pasted)
        self._save_config(config)

    def forget_torrents_vpn(self) -> None:
        """Drop the saved WireGuard file and the server the firewall opens
        to. Downloads set to go through the VPN then wait for a new file."""
        config = self.config()
        with self.lock:
            if any(job.state() == "running" for job in self.jobs.values()):
                raise NullBillError("wait until the running job finishes")
            store = Secrets(self.codec(self.home))
            if torrents.VPN_SECRET in store.data:
                del store.data[torrents.VPN_SECRET]
                store.codec.save(store.data)
        if config and "vpn" in (config.get("torrents") or {}):
            del config["torrents"]["vpn"]
            if not config["torrents"]:
                del config["torrents"]
            self._save_config(config)

    @staticmethod
    def _pasted_vpn(config: dict, form: object, resolve: Callable | None) -> str | None:
        """The WireGuard file the form brings, checked and reduced to what
        NullBill keeps of it; its server goes into `config`, where the
        firewall is made from. None without a file. resolve: looks up a
        server given by name (only a save does)."""
        text = form.get("conf") if isinstance(form, dict) else None
        if not isinstance(text, str) or not text.strip():
            return None
        conf = torrents.read_conf(text, build_model({k: v for k, v in config.items() if k == "network"})["network"]["base"], resolve)
        config["torrents"] = {**(config.get("torrents") or {}), "vpn": {"endpoint": conf["endpoint"]}}
        return torrents.saved_conf(conf)

    @staticmethod
    def _torrents_config(config: dict, form: object) -> None:
        """What the page asks beside the download client's tick, put into
        config.yaml: the way downloads go (the owner's choice, and their
        answer to the client's notice; none is no choice yet), the kept disk
        and folder they land in, and how much room stays free. A key the
        form leaves out stays as it is; what equals the catalog is not
        written down. Every problem is raised together."""
        if not isinstance(form, dict):
            return
        chosen = dict(config.get("torrents") or {})
        start = build_model({})["torrents"]
        problems: list[str] = []
        if "route" in form:
            if form["route"] in (torrents.VPN, torrents.HOME):
                chosen["route"] = form["route"]
            elif form["route"] in ("", None):
                chosen.pop("route", None)
            else:
                problems.append("Downloads: the way they go is through your VPN or from your home connection")
        if "share" in form:
            share = str(form["share"] or "").strip()
            chosen = {k: v for k, v in chosen.items() if k != "share"} | ({"share": share} if share else {})
        if "folder" in form:
            folder = str(form["folder"] or "").strip() or start["folder"]
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}", folder):
                problems.append(
                    "Downloads: the folder is one name of up to 40 letters, digits, spaces, dots and dashes, such as Downloads; "
                    "it is made on the kept disk if it is not there"
                )
            chosen["folder"] = folder
        if "keep_free_gib" in form:
            room = form["keep_free_gib"]
            if room in ("", None):
                room = start["keep_free_gib"]
            if isinstance(room, bool) or not isinstance(room, int) or not 1 <= room <= 100000:
                problems.append("Downloads: the room to keep free is a whole number of GiB, 1 or more")
            chosen["keep_free_gib"] = room
        if "port_mapping" in form:
            chosen["port_mapping"] = bool(form["port_mapping"])
        if problems:
            raise (
                Refused("The download client's settings cannot be used, so nothing was saved", problems)
                if len(problems) > 1
                else NullBillError(*problems)
            )
        chosen = {key: value for key, value in chosen.items() if value != start.get(key)}
        if chosen:
            config["torrents"] = chosen
        else:
            config.pop("torrents", None)

    def _save_config(self, config: dict) -> None:
        """Write config.yaml, once it passes the schema, and before that
        what is on the server now, where no setup run has recorded it."""
        build_model(config)
        existing = self.existing_guests()
        if existing is not None and not (self.home / GUESTS).exists():
            _write_private(self.home / GUESTS, json.dumps({"guests": sorted(existing)}, indent=2) + "\n")
        _write_private(self.home / "config.yaml", yaml.safe_dump(config, sort_keys=False))

    # Services of the owner's own (nullbill/custom.py): ones NullBill has no
    # role for. The page's agent adds one with the owner's approval
    # (chat.Tools.t_add_service), or the owner writes it into config.yaml.
    # The page lists them under Setup, What to run, like every choice.
    def own_workloads(self, config: dict) -> list[str]:
        """The choices config.yaml adds to the catalog's: further game
        servers and the workloads of the owner's own services."""
        try:
            return workloads.added(build_model(config))
        except NullBillError:
            return []

    def own_service(self, name: str) -> dict:
        """One such service's setup script, for the page to show."""
        model = build_model(self.config() or {})
        if name not in custom.names(model):
            raise NullBillError("no service of your own has that name")
        return {
            "name": name,
            "title": custom.title(model, name),
            "script": custom.read_script(self.home, name),
            "path": f"{custom.DIR}/{name}/{custom.SCRIPT}",
        }

    def service_change(self, name: str, want: dict, script: str | None) -> dict:
        """What adding a service of the owner's own, or changing one, comes
        to, with nothing saved: the new config.yaml, the service line by
        line, the setup script's diff, and whether the change opens the
        server further or brings code to run (`risky`). script None leaves
        the script it has. Raises what config.yaml or the plan say against it."""
        config = self.config()
        if config is None:
            raise NullBillError("nothing is chosen yet; the owner saves their choices under Setup, What to run, first")
        model = build_model(config)
        new = custom.define(config, model, name, want)
        new_model = build_model(new)
        facts = self.facts()
        if facts is not None and name in new["intent"]["workloads"]:
            build_plan(new_model, facts)
        before = custom.read_script(self.home, name)
        text = before if script is None else custom.check_script(script)
        old, now = model["services"].get(name), new_model["services"][name]
        opens = (
            old is None
            or custom.own_ports(old) != custom.own_ports(now)
            or any(old.get(k) != now.get(k) for k in ("zone", "kind", "proxy_allow"))
        )
        diff = "".join(
            difflib.unified_diff((before or "").splitlines(True), (text or "").splitlines(True), "setup.sh now", "setup.sh new", n=3)
        )
        return {
            "config": new,
            "script": text if text != before else None,
            "new": old is None,
            "title": custom.title(new_model, name),
            "lines": custom.describe(new_model, name, text),
            "diff": diff,
            "risky": opens or text != before,
            "opens": opens,
            "same": new == config and text == before,
        }

    def save_service(self, name: str, config: dict, script: str | None) -> None:
        """Keep what service_change made: the script first, so config.yaml
        never names a service whose script is the one before. The plan
        follows at once, as after saved choices."""
        build_model(config)
        if script is not None:
            path = custom.script(self.home, name)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_private(path, script)
        self._save_config(config)
        self.plan()

    def forget_service(self, name: str, save: bool = True) -> str:
        """Take a service of the owner's own out of config.yaml and set its
        setup script aside (custom/.removed). Only one that is off and has
        no guest on the server: deleting a guest is setup's, after it asks
        the owner. save False only says whether it may."""
        config = self.config() or {}
        model = build_model(config)
        new = custom.forget(config, model, name)
        workload = model["services"][name]["workload"]
        title = custom.title(model, name)
        if workload not in custom.workloads(model):
            raise NullBillError(f"{name} belongs to the workload {workload}, which is not its own; take it out of config.yaml by hand")
        if workload in config.get("intent", {}).get("workloads", []):
            raise NullBillError(f"{title} is turned on; turn it off under Setup, What to run, and save first")
        if name in (self.existing_guests() or set()):
            raise NullBillError(
                f"{title}'s guest is still on the server; run setup, which deletes it after asking you, then remove it here"
            )
        if not save:
            return title
        self._save_config(new)
        kept = self.home / custom.DIR / name
        if kept.is_dir():
            aside = self.home / custom.DIR / custom.REMOVED
            aside.mkdir(mode=0o700, exist_ok=True)
            shutil.move(kept, aside / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}")
        self.plan()
        return title

    # Game servers (nullbill/games.py). Besides the catalog's one of each
    # game, config.yaml can name more (games.<name>.game): the page's Add a
    # game server and the agent's add_game_server make one, and every
    # server's row carries its own settings, admins and memory.
    def add_game(self, game: str, title: str, form: dict | None = None, on: bool = True, save: bool = True) -> dict:
        """One more server of a game, saved and planned. form: what the
        page's `games` form takes for a server (_games_config). on False
        leaves it unticked. save False only says what it would be (`lines`)
        and raises what config.yaml or the plan say against it, as the
        agent's request does before the owner sees it."""
        config = self.config()
        if config is None:
            raise NullBillError("nothing is chosen yet; save your choices under Setup, What to run, first")
        new, name = games.add(config, build_model(config), str(game), title)
        if on:
            ticked = new.setdefault("intent", {}).setdefault("workloads", list(DEFAULT_WORKLOADS))
            ticked.append(name)
        if form:
            self._games_config(new, {name: form}, facts=self.facts())
        model = build_model(new)
        out = {"name": name, "title": model["games"][name]["title"], "lines": games.describe(model, name, on)}
        if not save:
            facts = self.facts()
            if facts is not None and on:
                build_plan(model, facts)
            return out
        self._save_config(new)
        self.plan()
        return out

    def forget_game(self, name: str, save: bool = True) -> str:
        """Take a further game server out of config.yaml. Only one that is
        off and has no guest on the server: deleting a guest, and the world
        in it, is setup's, after it asks the owner. save False only says
        whether it may."""
        config = self.config() or {}
        model = build_model(config)
        new = games.forget(config, model, name)
        title = model["games"][name]["title"]
        if name in config.get("intent", {}).get("workloads", []):
            raise NullBillError(f"{title} is turned on; turn it off under Setup, What to run, and save first")
        if name in (self.existing_guests() or set()):
            raise NullBillError(
                f"{title}'s guest is still on the server; run setup, which deletes it after asking you, then remove it here"
            )
        if save:
            self._save_config(new)
            self.plan()
        return title

    # A game server as it runs (web/gameroom.py): the Services page's block
    # for it, and the agent's game_server and game_world. The guest's
    # nb-game holds the worlds and does every change; the page only asks.
    def _game_guest(self, name: str) -> tuple[dict, dict, str]:
        """(model, plan, where the host is) for a game server that a setup
        run has made."""
        model, plan = build_model(self.config() or {}), self.saved_plan()
        target = self.host_target() if self.built() else None
        if name not in model["games"] or name not in (plan.get("guests") or {}) or name not in (plan.get("games") or {}):
            raise NullBillError(f"no game server {name[:40]!r} is planned; Setup, What to run, lists them")
        if target is None or name not in (self.existing_guests() or set()):
            raise NullBillError(f"{model['games'][name]['title']} is not on the server yet: run setup")
        return model, plan, target

    def _game_off(self, name: str) -> bool:
        """Whether the server's guest is off because it runs on demand."""
        for group in (self.on_demand() or {}).get("groups", []):
            if any(m["name"] == name and m.get("state") not in (None, "running") for m in group["guests"]):
                return True
        return False

    def game_view(self, name: str, fresh: bool = False) -> dict:
        model, plan, target = self._game_guest(name)
        now = time.monotonic()
        with self.lock:
            seen, acting = self.game_seen.get(name), self.game_acting.get(name)
        if fresh or seen is None or now - seen[0] > GAME_CACHE_S:
            rc, out, err = self.host_run(target, games.guest_command(plan, name, "report"))
            try:
                report = json.loads(out) if rc == 0 else None
            except ValueError:
                report = None
            if isinstance(report, dict):
                view = gameroom.view(model, name, report)
            elif self._game_off(name):
                view = gameroom.view(model, name, None, "It is off. Start it to see its worlds.", off=True)
            else:
                said = err.strip().splitlines()[-1][:200] if err.strip() else f"exit {rc}"
                view = gameroom.view(model, name, None, f"The server's guest did not answer ({said}). Run setup if it was just updated.")
            view["at"] = time.time()
            with self.lock:
                self.game_seen[name] = (now, view)
        else:
            view = seen[1]
        return view | ({"acting": acting} if acting else {})

    def game_act(self, name: str, body: dict, by: str = "the page") -> dict:
        """Restart a game server, type on its console, or change its
        worlds. A change that would disconnect players changes nothing and
        says how many, until it comes again with `kick`."""
        model, plan, target = self._game_guest(name)
        argv, doing, slow = gameroom.arguments(model, name, body)
        title = model["games"][name]["title"]
        with self.lock:
            if name in self.game_acting:
                raise NullBillError(f"{title} is busy {self.game_acting[name]}; wait for it")
            if slow:
                self.game_acting[name] = doing
        # The console line may be a password or a name: the log gets the action, not the text.
        print(f"nullbill-web: game {name}: {body.get('action')} by {by}", file=sys.stderr, flush=True)
        try:
            rc, out, err = (self.host_long if slow else self.host_run)(target, games.guest_command(plan, name, *argv))
        finally:
            with self.lock:
                self.game_acting.pop(name, None)
                self.game_seen.pop(name, None)
        if argv[0] != "world":
            if rc != 0:
                raise NullBillError((err.strip().removeprefix("nb-game: ") or f"exit {rc}")[:300])
            return {"ok": True, "view": self.game_view(name, fresh=True)}
        found = gameroom.answer(out, err, rc)
        if not found["ok"] and found.get("players"):
            return {"ok": False, "players": found["players"], "title": title}
        if not found["ok"]:
            said, last = str(found.get("error") or "the game server refused"), [str(line) for line in found.get("log") or []]
            # With the server's last lines: what did not happen, then what it said.
            raise Refused(said, last) if last else NullBillError(said)
        return {"ok": True, "id": found.get("id"), "deleted": found.get("deleted"), "view": self.game_view(name, fresh=True)}

    # Which server runs a Minecraft game server, and its plugins or mods
    # (nullbill/mods.py, docs/adr/0021-minecraft-mods.md). The page asks
    # Modrinth and writes config.yaml; the server's guest gets the files
    # with the next run of setup's games stage.
    def mods_view(self, name: str) -> dict:
        model = build_model(self.config() or {})
        if name not in model["games"]:
            raise NullBillError(f"no game server {name[:40]!r} is planned; Setup, What to run, lists them")
        view = mods.view(model, name)
        if not view["takes"]:
            return view
        # What the server's guest holds, where the page asked it lately:
        # the list is the owner's choice, and this says whether it runs.
        with self.lock:
            seen = self.game_seen.get(name)
        view["applied"] = None
        if seen is not None and seen[1].get("live"):
            guest = seen[1]
            view["applied"] = guest.get("server") == view["server"] and sorted(guest.get("mods") or []) == sorted(
                m["file"] for m in view["mods"]
            )
            view["players"] = int(guest.get("players") or 0)
        return view

    def mods_act(self, name: str, body: dict, by: str = "the page") -> dict:
        """Search Modrinth, or change a Minecraft server's kind or its
        list of plugins or mods. A change of kind that costs something (the
        list, or which servers open the world) changes nothing and says
        what, until it comes with `yes`."""
        config = self.config()
        if config is None:
            raise NullBillError("nothing is chosen yet; save your choices under Setup, What to run, first")
        model = build_model(config)
        if name not in model["games"] or not mods.takes(model, name):
            raise NullBillError("only a Minecraft server takes plugins or mods")
        action, title = body.get("action"), model["games"][name]["title"]
        if action == "search":
            return {"ok": True, "hits": mods.search(self.modrinth, model, name, body.get("query"))}
        have = {mod["project"]: mod for mod in model["games"][name].get("mods") or []}
        if action in ("check", "update"):
            found = mods.newer(self.modrinth, model, name)
            updates = [
                {
                    "project": e["project"],
                    "title": e["title"],
                    "to": e["version"],
                    "from": have[e["project"]]["version"] if e["project"] in have else None,
                }
                for e in found
            ]
            if action == "check" or not found:
                return {"ok": True, "updates": updates, "view": self.mods_view(name)}
            new, out = mods.put(config, model, name, found), {"updated": updates}
        elif action == "server":
            new, gone = mods.set_server(config, model, name, body.get("server"))
            costs = mods.consequences(model, name, str(body.get("server")), gone)
            if costs and body.get("yes") is not True:
                return {"ok": False, "costs": costs, "title": title, "to": mods.LABEL[str(body.get("server"))]}
            out = {"dropped": gone}
        elif action == "add":
            entries = mods.resolve(self.modrinth, model, name, body.get("project"))
            new, out = mods.put(config, model, name, entries), {"added": mods.describe(entries)}
        elif action == "remove":
            new, gone = mods.drop(config, model, name, body.get("project"))
            out = {"removed": gone}
        else:
            raise NullBillError("send action: search, check, update, server, add or remove")
        print(f"nullbill-web: game {name}: mods {action} by {by}", file=sys.stderr, flush=True)
        self._save_config(new)
        self.plan()
        return {"ok": True, **out, "view": self.mods_view(name)}

    def world_download(self, name: str, ident: str) -> tuple[str, int, subprocess.Popen, Callable[[], None]]:
        """(file name, bytes, the stream, what to call when done) for one
        of a server's worlds: the guest writes it out as one file, which
        then comes through the host as it is."""
        model, plan, target = self._game_guest(name)
        rc, out, err = self.host_long(target, games.guest_command(plan, name, "world", "export", gameroom.world_id(ident)))
        found = gameroom.answer(out, err, rc)
        if not found["ok"]:
            raise NullBillError(str(found.get("error")))
        path = str(found["file"])
        if not re.fullmatch(r"/var/lib/nullbill/world-transfer/[0-9a-f]{16}\.(zip|wld)", path):
            raise NullBillError("the game server named a file that is not a world's")
        vmid = str(plan["guests"][name]["vmid"])
        stream = self.host_pipe(target, shlex.join(["pct", "exec", vmid, "--", "cat", path]))
        if stream.stdin:
            stream.stdin.close()
        print(f"nullbill-web: game {name}: download of world {ident}", file=sys.stderr, flush=True)

        def done() -> None:
            stream.kill()
            stream.wait()
            self.host_run(target, games.guest_command(plan, name, "world", "sent", path))

        safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(found.get("name") or "world"))
        return f"{name}-{safe}", int(found["bytes"]), stream, done

    def world_upload(self, name: str, title: str, length: int, source: BinaryIO) -> tuple[dict, bool]:
        """A world file from the owner's browser, passed on as it comes to
        the guest, which checks it and keeps it beside the server's other
        worlds. Returns the answer and whether all of the request was read."""
        model, plan, target = self._game_guest(name)
        made = games.read_world_title(title)
        if not 0 < length <= games.MAX_WORLD_BYTES:
            raise NullBillError(f"a world file is at most {games.MAX_WORLD_BYTES // 1024**3} GiB")
        with self.lock:
            if name in self.game_acting:
                raise NullBillError(f"{model['games'][name]['title']} is busy {self.game_acting[name]}; wait for it")
            self.game_acting[name] = "taking in an upload"
        print(f"nullbill-web: game {name}: upload of {length} bytes", file=sys.stderr, flush=True)
        left = length
        try:
            stream = self.host_pipe(target, games.guest_command(plan, name, "world", "receive", str(length), made))
            try:
                while left:
                    chunk = source.read(min(STREAM_CHUNK, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    stream.stdin.write(chunk)
                stream.stdin.close()
            except (BrokenPipeError, OSError):
                # The guest stopped reading: it says why below.
                pass
            out, err = stream.stdout.read().decode(errors="replace"), stream.stderr.read().decode(errors="replace")
            rc = stream.wait()
        finally:
            with self.lock:
                self.game_acting.pop(name, None)
                self.game_seen.pop(name, None)
        found = gameroom.answer(out, err, rc)
        if not found["ok"]:
            raise NullBillError(str(found.get("error") or "the game server refused the file"))
        return {"ok": True, "id": found.get("id"), "view": self.game_view(name, fresh=True)}, left == 0

    @staticmethod
    def _games_config(config: dict, form: object, sizes: object = None, facts: dict | None = None, strict: bool = True) -> None:
        """What the page asks beside each game server's tick, put into
        config.yaml: the owner's agreement to the game's EULA (once for all
        its servers), the admins' names, the server's settings (`settings`
        by name, `other` as lines of name = value) and a further server's
        title. With them each guest's size (`sizes`, nullbill/sizes.py);
        a game server's `memory_mib` here, as older pages and the agent's
        add_game_server send it, is read as its size. A key the form leaves
        out stays as it is, and a `memory_mib` of null leaves the size as it
        is; nothing changes without either form. Every problem is raised
        together, a message each; strict False (the preview) leaves out a
        size it cannot read."""
        if not isinstance(form, dict) and sizes is None:
            return
        chosen = copy.deepcopy(config.get("games") or {})
        overrides = copy.deepcopy(config.get("overrides") or {})
        # Only games and sizes matter here; the rest of the form may not be whole yet.
        model = build_model({"games": chosen, "services": config.get("services") or {}})
        catalog = build_model({})["games"]
        problems: list[str] = []
        # What is not a size is left to sizing.read, which says so.
        asked_sizes = {str(k): v for k, v in sizes.items()} if isinstance(sizes, dict) else sizes
        for name, asked in (form if isinstance(form, dict) else {}).items():
            if name not in model["games"] or not isinstance(asked, dict):
                continue
            of = games.kind(model, name)
            game = dict(chosen.get(name) or {})
            if "eula" in catalog[of] and "eula" in asked:
                # The agreement is the game's, whichever of its servers asks.
                agreed = {**(chosen.get(of) or {}), "eula": bool(asked["eula"])}
                if of == name:
                    game = agreed
                elif agreed["eula"] != catalog[of]["eula"]:
                    chosen[of] = agreed
                else:
                    chosen[of] = {k: v for k, v in agreed.items() if k != "eula"}
                    if not chosen[of]:
                        del chosen[of]
            if asked.get("memory_mib") is not None and asked_sizes is None:
                asked_sizes = {}
            if asked.get("memory_mib") is not None and isinstance(asked_sizes, dict):
                size = asked_sizes.get(name, {})
                if isinstance(size, dict) and "memory_mib" not in size:
                    asked_sizes[name] = {**size, "memory_mib": asked["memory_mib"]}
            # Each part is read on its own, so one refusal does not hide the next.
            found = len(problems)
            if "admins" in catalog[of] and "admins" in asked:
                given = asked["admins"]
                admins = list(dict.fromkeys(a for a in (given if isinstance(given, list) else re.split(r"[\s,]+", str(given))) if a))
                if any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", a) for a in admins):
                    problems.append(f"{model['games'][name]['title']}: admins are player names, as shown in the game, separated by commas")
                game["admins"] = admins
            for part in ("title", "settings"):
                try:
                    if part == "title" and of != name and "title" in asked:
                        game["title"] = games.read_title(model, name, asked["title"])
                    if part == "settings" and ("settings" in asked or "other" in asked):
                        game["settings"] = games.read_settings(model, name, asked.get("settings"), asked.get("other"))
                except NullBillError as exc:
                    problems.extend(exc.messages)
            if len(problems) > found:
                continue
            # What equals the catalog is not written down; a further server
            # always keeps which game it is and its address.
            start = catalog.get(name) or {"settings": {}, "admins": []}
            game = {key: value for key, value in game.items() if key in ("game", "id") or value != start.get(key)}
            if game:
                chosen[name] = game
            else:
                chosen.pop(name, None)
        overrides, wrong = sizing.read(model, overrides, asked_sizes, facts, strict)
        titles = tuple(f"{g['title']}:" for g in model["games"].values())
        about_games = all(message.startswith(titles) for message in wrong)
        sizing.raise_problems(
            problems + wrong,
            "Some game server settings cannot be used, so nothing was saved"
            if about_games
            else "Some sizes cannot be used, so nothing was saved"
            if not problems
            else "Some settings cannot be used, so nothing was saved",
        )
        for section, value in (("games", chosen), ("overrides", overrides)):
            if value:
                config[section] = value
            else:
                config.pop(section, None)

    def _choices_config(self, body: dict, saving: bool = True) -> dict:
        """config.yaml with the page's form put into it. Saving checks what
        only a save needs: the owner's login and each erase confirmation."""
        login = str(body.get("owner_login", "")).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", login):
            if saving:
                raise NullBillError("your Tailscale login is the email address you sign in to Tailscale with")
            login = "owner@example.internal"
        install = self.install()
        if not install.get("host"):
            raise NullBillError("install.json is missing; run install.sh on the Proxmox host again")
        config = self.config() or {}
        known = {*WORKLOADS, *self.own_workloads(config)}
        config["target"] = {"ssh": f"root@{install['host']}"}
        config["intent"] = {"workloads": [w for w in body.get("workloads", []) if w in known]}
        people = {n: p for n, p in config.get("people", {}).items() if p.get("role") != "owner"}
        keys = owner.keys(config)
        config["people"] = {"owner": {"role": "owner", "login": login} | ({"ssh_keys": keys} if keys else {}), **people}
        facts = self.facts()
        self._games_config(config, body.get("games"), body.get("sizes"), facts, strict=saving)
        self._torrents_config(config, body.get("torrents"))
        if not saving:
            # A pasted WireGuard file counts in the preview once it reads;
            # until then the plan says that the VPN is still missing.
            try:
                self._pasted_vpn(config, body.get("torrents"), None)
            except NullBillError:
                pass
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
        if isinstance(body.get("power"), dict):
            self._power_pins(config, body["power"], strict=saving)
        if isinstance(body.get("disks"), dict):
            pins = self._disk_pins(config, body["disks"], confirm=saving)
            if pins:
                config["disks"] = pins
            else:
                config.pop("disks", None)
        if saving and config.get("overrides") != (self.config() or {}).get("overrides"):
            # A size that takes the plan past the server's memory is refused
            # here, with what fits; a tick that does is saved and planned.
            try:
                before, after = build_model(self.config() or {}), build_model(config)
            except NullBillError:
                return config
            sizing.raise_problems(
                sizing.too_much(before, after, facts), "These sizes do not fit in the server's memory, so nothing was saved"
            )
        return config

    def _power_pins(self, config: dict, chosen: dict, strict: bool = True) -> None:
        """When it runs, per workload on the form: config.yaml's `power`
        keeps only what differs from the catalog, and what the page does
        not set (a title, busy.cpu_pct) stays as the owner wrote it."""
        catalog = build_model({})
        known = ({svc["workload"] for svc in catalog["services"].values()} - {"core"}) | set(self.own_workloads(config))
        pins = config.get("power") or {}
        for name, want in chosen.items():
            if name not in known or not isinstance(want, dict):
                continue
            # A further game server starts with its game's rule (model._more_games).
            of = (config.get("games") or {}).get(name, {}).get("game", name)
            default = {**POWER_DEFAULTS, **catalog["power"].get(of, {})}
            pin = {k: v for k, v in pins.get(name, {}).items() if k not in POWER_DEFAULTS}
            for key in POWER_DEFAULTS:
                value = want.get(key, default[key])
                if key == "idle_minutes":
                    try:
                        value = int(value)
                        if not 5 <= value <= 10080:
                            raise ValueError
                    except (TypeError, ValueError):
                        if strict:
                            title = default.get("title", name)
                            raise NullBillError(f"{title}: the time without use is a number of minutes from 5 to 10080 (a week)") from None
                        value = pins.get(name, {}).get(key, default[key])
                if value != default[key]:
                    pin[key] = value
            if pin:
                pins[name] = pin
            else:
                pins.pop(name, None)
        if pins:
            config["power"] = pins
        else:
            config.pop("power", None)

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
        model = None
        try:
            model = build_model(config)
            text = plan_text(model, facts.read_bytes())
        except NullBillError as exc:
            other = "Change your choices under Setup, What to run, so this no longer applies, and press Save and plan."
            return problems({"text": None, "errors": exc.messages, "summary": None, "tailnet_policy": None}, other, model)
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
                raise Refused("The plan does not hold with this change, so nothing was saved", new) if len(new) > 1 else NullBillError(*new)
            ready = self.tailnet_wait(plan, problems) is None
            if ready:
                want = json.loads(render_tailnet(plan, model)["policy.json"])
                try:
                    tailscale.sync_policy(self.tailnet(), want, TAG, model["tailnet"]["free_limits"]["tagged_devices"])
                except TailscaleError as exc:
                    raise Refused("Tailscale did not take it, so nothing changed", exc.reasons) from exc
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

    def tell(self, message: str, tag: str, title: str = "NullBill agent") -> None:
        """One message to the owner's ntfy topic, or NullBillError."""
        topic = Secrets(self.codec(self.home)).data.get("ntfy_topic")
        if not topic:
            raise NullBillError("the owner has no ntfy topic yet, so they could not be told; set a password on the page first")
        try:
            self.send(NTFY + topic, message, {"Title": title, "Tags": tag})
        except OSError as exc:
            raise NullBillError(f"could not tell the owner through ntfy ({exc}); nothing started") from exc

    def tell_agent_jobs(self) -> None:
        """How each job an agent started ended, once."""
        for job in list(self.jobs.values()):
            # The nightly update tells of its own runs (nightly_step).
            if job.by in ("owner", NIGHTLY_BY) or job.state() == "running":
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
    def start(
        self,
        action: str,
        stage: str | None = None,
        client: str = "",
        by: str = "owner",
        guests: list[str] | None = None,
        review: str | None = None,
    ) -> Job:
        """review: the id of the review the owner started a setup run from."""
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
            if by not in ("owner", NIGHTLY_BY) and not (by == "agent" and action in AGENT_ACTIONS | {"probe"}):
                # Before the job exists: an action the owner is not told of does not run.
                self.tell(f"The agent started: {progress.JOB_TEXT.get(action, action)}{f' (stage {stage})' if stage else ''}", "robot")
            env = os.environ | {"NULLBILL_HOME": str(self.home), "PYTHONUNBUFFERED": "1", "ANSIBLE_FORCE_COLOR": "0"}
            fd = os.open(job.log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                argv = ["sh", "-c", JOB_WRAPPER, str(job.rc_file), *self.command, *args]
                job.process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
            # Which NullBill the job runs with: what a finished setup run
            # built the server from (built_with).
            commit = version.head(self.repo)
            meta = {"action": action, "args": args, "started": job.started, "pid": job.process.pid, "by": by, "commit": commit}
            if action in SETUP_ACTIONS and not stage:
                plan = self.home / "plan.yaml"
                if plan.is_file():
                    _write_private(job.plan_file, plan.read_text())
                meta |= {"setup": version.setup_digest(self.repo) if commit else None, "review": review}
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

    def updates_due(self, now: float) -> bool:
        """Once a day, and not while a job runs: an update fetches too."""
        if version.head(self.repo) is None or any(j.state() == "running" for j in self.jobs.values()):
            return False
        return now - float(self.update_check().get("checked") or 0) >= UPDATE_CHECK_S

    def scheduled(self, now: float) -> Job | None:
        self.tell_agent_jobs()
        if self.updates_due(now):
            try:
                self.check_updates()
            except NullBillError:
                pass
        if not self.rebake_due(now):
            return self.nightly_step(now)
        try:
            job = self.start("rebake-ci")
        except NullBillError:
            return None
        _write_private(self.home / "web" / "last-rebake", f"{now}\n")
        return job

    # The nightly update (docs/adr/0022-latest-stable.md).
    def nightly(self) -> dict:
        try:
            found = json.loads((self.home / NIGHTLY).read_text())
        except (OSError, ValueError):
            found = {}
        return {"enabled": True} | (found if isinstance(found, dict) else {})

    def set_nightly(self, enabled: bool) -> dict:
        record = self.nightly() | {"enabled": bool(enabled)}
        _write_private(self.home / NIGHTLY, json.dumps(record) + "\n")
        return record

    def update_steps(self, head: str, newest: str) -> list[dict]:
        """The pinned versions an update moves (nullbill/pins.py), each with
        `asks`: a step this server's owner is asked about."""
        found = pins.steps(version.reader(self.repo, head), version.reader(self.repo, newest))
        text = self.built_plan()
        asked = {s["name"] for s in pins.asks(found, yaml.safe_load(text) if text else None)}
        return [{k: s[k] for k in ("name", "title", "from", "to")} | {"asks": s["name"] in asked} for s in found]

    def nightly_step(self, now: float) -> Job | None:
        """One step of tonight's update, at most one job at a time: look
        for an update; install it unless it asks the owner something; run
        setup when the server no longer follows the version. Each minute
        takes the next step, also after the page restarted with the update."""
        record = self.nightly()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if not record["enabled"] or not self.built() or version.head(self.repo) is None:
            return None
        if any(j.state() == "running" for j in self.jobs.values()):
            return None
        if record.get("day") != today:
            if time.localtime(now).tm_hour not in NIGHTLY_HOURS:
                return None
            record |= {"day": today, "step": "look", "job": None, "ended": None}

        def keep(**fields: object) -> None:
            record.update(fields)
            _write_private(self.home / NIGHTLY, json.dumps(record) + "\n")

        def end(how: str, text: str, tell: bool = False) -> None:
            said = record.get("said")
            keep(step="done", ended={"how": how, "text": text, "at": now})
            # Once per thing that stays true night after night; a night's work each time.
            if tell and (said != text or how == "done"):
                try:
                    self.tell(text, "arrows_counterclockwise" if how == "waits" else "warning", "NullBill updates")
                    keep(said=text)
                except NullBillError as exc:
                    print(f"nullbill-web: {exc}", file=sys.stderr, flush=True)

        def failed(what: str) -> bool:
            job = self.jobs.get(record.get("job") or "")
            if job is None or job.state() == "done":
                return False
            end("failed", f"Tonight's {what} failed. Open Activity on the page to see why.", tell=True)
            return True

        step = record.get("step")
        if step == "look":
            try:
                newest = self.check_updates().get("commit")
            except NullBillError:
                return None
            head = version.head(self.repo)
            if not newest or not version.behind(self.repo, head, newest):
                keep(step="updated")
                return None
            steps = self.update_steps(head, newest)
            asks = [s for s in steps if s["asks"]]
            if asks:
                names = ", ".join(f"{s['title']} {s['from']} to {s['to']}" for s in asks)
                end("waits", f"An update waits for your yes: {names}. Open Updates on the page.", tell=True)
                return None
            try:
                job = self.start("upgrade", by=NIGHTLY_BY)
            except NullBillError:
                # Something else began this minute; the next one looks again.
                return None
            keep(step="updating", job=job.id, to=newest, moves=[f"{s['title']} {s['to']}" for s in steps])
            return job
        if step == "updating":
            if not failed("update of NullBill"):
                # Setup follows only an update this job installed itself.
                keep(step="updated", installed=record.get("to"))
            return None
        if step == "updated":
            follows = (self.version_view() or {}).get("server", {}).get("follows")
            head = version.head(self.repo)
            if follows is not False:
                end("same", "Nothing to set up.")
            elif record.get("installed") != head:
                # An update the owner installed, or a run that failed or was
                # stopped: starting setup for it is theirs, not the night's.
                end(
                    "waits",
                    "Setup did not run by itself: no night's update installed this version. Start it under Setup, Review and run.",
                )
            elif record.get("tried") == head:
                end("waits", "Setup for this version failed on an earlier night and was not started again. Open Activity.")
            elif self.config_changed() or self.removals() or self.planned_removals():
                # Choices the owner saved and has not reviewed are theirs to start.
                end("waits", "Setup did not run by itself: your saved choices wait for you under Setup, Review and run.")
            elif self.release_waiting():
                # Every guest is away in turn, and the page at the end.
                end("waits", "Setup did not run by itself: it upgrades Debian in every guest, which waits for you under Setup, Review.")
            else:
                try:
                    job = self.start("apply", by=NIGHTLY_BY)
                except NullBillError:
                    return None
                keep(step="setup", job=job.id, tried=head)
                return job
            return None
        if step == "setup" and not failed("setup run"):
            moved = ", ".join(record.get("moves") or [])
            end("done", f"NullBill installed an update tonight and ran setup with it{': ' + moved if moved else ''}.", tell=True)
        return None

    def job_view(self, job: Job, offset: int = 0) -> dict:
        data = job.log.read_bytes()
        state = job.state()
        ended = job.rc_file.stat().st_mtime if state != "running" and job.rc_file.exists() else None
        now = time.time()
        # A run that ended unrecorded ended when its log last grew, not now.
        unrecorded = state == "failed" and job.rc == -1 and not job.rc_file.exists()
        if unrecorded:
            ended = job.log.stat().st_mtime
        times = {"started": job.started, "now": now, "ended": ended or (None if state == "running" else now)}
        times["output"] = job.log.stat().st_mtime
        times["stopped"] = job.meta().get("stopped")
        times["unrecorded"] = unrecorded
        only = progress.only(job.action, job.args)
        return {
            "id": job.id,
            "action": job.action,
            "args": job.args,
            "only": only,
            "state": state,
            "stopped": state == "failed" and bool(times["stopped"]),
            "rc": job.rc,
            "text": data[offset:].decode(errors="replace"),
            "offset": len(data),
            "started": job.started,
            "seconds": round((times["ended"] or now) - job.started),
            "by": job.by,
            "review": job.meta().get("review"),
            "progress": progress.summarize(job.action, data.decode(errors="replace"), state, times, only and only["name"]),
        }

    def last_jobs(self) -> dict:
        """Each action's latest job: how it ended, and when. A failed one
        carries where it stopped and what it said (`error`), so Overview
        tells of it whichever run the page has open."""
        latest: dict[str, Job] = {}
        for job in sorted(self.jobs.values(), key=lambda j: j.started):
            latest[job.action] = job
        out: dict[str, dict] = {}
        for action, job in latest.items():
            state = job.state()
            stopped = state == "failed" and bool(job.meta().get("stopped"))
            out[action] = {"id": job.id, "state": state, "started": job.started, "stopped": stopped}
            if state == "failed" and not stopped:
                found = self.failure(job)
                out[action]["error"] = {"stage_text": found["stage_text"], "message": found["message"]}
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
                    "only": progress.only(job.action, job.args),
                    "state": state,
                    "stopped": stopped,
                    "started": job.started,
                    "seconds": seconds,
                    "by": job.by,
                    "where": self.failed_at(job) if state == "failed" and not stopped else None,
                }
            )
        return out

    def failed_at(self, job: Job) -> str | None:
        """Where a failed run stopped, for Activity's list: its stage in the
        page's words, or how many checks failed."""
        return self.failure(job)["where"]

    def failure(self, job: Job) -> dict:
        """How a failed run ended: `where` for Activity's list, its stage and
        message for Overview. Read from its log once: a run that ended does
        not change."""
        if job.id not in self.failed_stages:
            view = self.job_view(job)["progress"]
            error = view.get("error") or {}
            stage = error.get("stage_text")
            failed = (view.get("verify") or {}).get("counts", {}).get("FAIL", 0)
            where = f'At "{stage}"' if stage else f"{failed} check{'s' if failed > 1 else ''}" if failed else None
            self.failed_stages[job.id] = {"where": where, "stage_text": stage, "message": error.get("message") or ""}
        return self.failed_stages[job.id]

    def checks(self) -> dict | None:
        """What the checks came to the last time they ran to their end: in
        a setup run, whose last stage they are, or on their own. None before
        any did. Overview shows it with the run's time; nothing is checked
        again in between."""
        for job in sorted(self.jobs.values(), key=lambda j: j.started, reverse=True):
            if job.action not in CHECKED or job.state() == "running":
                continue
            if job.id not in self.checked:
                found = self.job_view(job)["progress"].get("verify")
                ended = job.rc_file.stat().st_mtime if job.rc_file.exists() else job.started
                degraded = [x["name"] for x in (found or {}).get("problems", []) if x["status"] == "DEGRADED"]
                kept = {"id": job.id, "action": job.action, "only": bool(progress.only(job.action, job.args)), "at": ended}
                self.checked[job.id] = kept | {"counts": found["counts"], "degraded": degraded} if found else None
            if self.checked[job.id]:
                return self.checked[job.id]
        return None

    def lan_view(self, client: str) -> dict:
        """The home network's state for the page. A close past its deadline
        is settled with the host first (lan.settle): the timer there opens
        the LAN again without telling the page."""
        if lan.overdue(self.home) and time.monotonic() - self.lan_asked > LAN_ASK_S:
            self.lan_asked = time.monotonic()
            target = self.host_target()
            with self.lock:
                busy = any(job.state() == "running" for job in self.jobs.values())
            if target is not None and not busy:
                lan.settle(self.home, lambda command: self.host_run(target, command)[1])
        found = lan.read(self.home)
        closing = found["state"] == lan.CLOSING and found.get("deadline") is not None
        return {
            "state": found["state"],
            "via_vpn": self.via_vpn(client),
            "minutes": lan.REVERT_AFTER_S // 60,
            # Seconds until the host opens the LAN again; 0 once that time has come.
            "left_s": max(0, round(found["deadline"] - time.time())) if closing else None,
            "lapsed": found.get("lapsed") if found["state"] == lan.OPEN else None,
        }

    def built(self) -> bool:
        """A setup run has finished at least once: the guests exist, even
        while a later run is going or after one stopped."""
        return any(j.action in ("apply", "remove-guests") and j.state() == "done" for j in self.jobs.values())

    def config_changed(self) -> bool:
        """The choices were saved after the start of the last setup run that
        finished, so the server does not follow them yet."""
        config = self.home / "config.yaml"
        good = self.last_setup()
        return good is not None and config.exists() and config.stat().st_mtime > good.started

    def setup_runs(self) -> list[Job]:
        """Setup runs of everything, oldest first; one stage alone is not one."""
        runs = [j for j in self.jobs.values() if j.action in SETUP_ACTIONS and "--stage" not in j.args]
        return sorted(runs, key=lambda j: j.started)

    def last_setup(self) -> Job | None:
        """The last setup run that finished: what the server was built by."""
        return next((j for j in reversed(self.setup_runs()) if j.state() == "done"), None)

    def built_with(self, job: Job) -> tuple[str | None, str | None]:
        """(commit, setup digest) of the NullBill a setup run ran with. A
        run from before the page wrote that down is looked up in git's
        record of where the checkout was when it started."""
        meta = job.meta()
        if meta.get("setup"):
            return meta.get("commit"), meta["setup"]
        commit = version.at(self.repo, job.started)
        return commit, version.setup_digest(self.repo, commit) if commit else None

    # NullBill's own version (nullbill/version.py).
    def update_check(self) -> dict:
        try:
            found = json.loads((self.home / UPDATE_CHECK).read_text())
            return found if isinstance(found, dict) else {}
        except (OSError, ValueError):
            return {}

    def check_updates(self) -> dict:
        """Ask the repository for main's newest commit and keep the answer,
        or why there is none."""
        with self.lock:
            if self.checking:
                raise NullBillError("a check for updates is already on its way")
            # git allows one fetch at a time in a checkout, and upgrade fetches.
            if any(j.state() == "running" and j.action == "upgrade" for j in self.jobs.values()):
                raise NullBillError("NullBill is updating right now")
            self.checking = True
        try:
            found: dict = {"checked": time.time(), "commit": version.fetch(self.repo), "error": None}
        except NullBillError as exc:
            found = {"checked": time.time(), "commit": self.update_check().get("commit"), "error": exc.messages[0]}
        finally:
            with self.lock:
                self.checking = False
        _write_private(self.home / UPDATE_CHECK, json.dumps(found) + "\n")
        return found

    def version_view(self) -> dict | None:
        """Which NullBill runs here, what main holds beyond it, and whether
        the server was built with it. None when this is not a checkout."""
        head = version.head(self.repo)
        if head is None:
            return None
        check = self.update_check()
        built = self.last_setup()
        moved = self.home / LAST_UPDATE
        key = (head, check.get("commit"), built.id if built else None, moved.stat().st_mtime if moved.exists() else None)
        if self.version_seen is None or self.version_seen[0] != key:
            newest = check.get("commit")
            ahead = bool(newest) and version.behind(self.repo, head, newest)
            server: dict = {"built": None, "follows": None, "changes": []}
            if built is not None:
                commit, digest = self.built_with(built)
                server["built"] = version.describe(self.repo, commit) if commit else None
                if digest is not None:
                    server["follows"] = digest == version.setup_digest(self.repo)
                if server["follows"] is False and commit:
                    server["changes"] = [c for c in version.changes(self.repo, commit, head) if c["setup"]]
            last = None
            try:
                record = json.loads(moved.read_text()) if moved.exists() else None
                if record and record["to"] == head:
                    last = {"at": record["at"], "from": record["from"][:12], "changes": version.changes(self.repo, record["from"], head)}
            except (ValueError, KeyError, TypeError):
                last = None
            view = {
                "current": version.describe(self.repo, head),
                "available": version.changes(self.repo, head, newest) if ahead else [],
                # The versions the waiting update moves; `asks` waits for the owner.
                "steps": self.update_steps(head, newest) if ahead else [],
                "server": server,
                "last": last,
                "docs": version.docs(self.repo, head),
            }
            self.version_seen = (key, view)
        view = self.version_seen[1] | {"checked": check.get("checked"), "check_error": check.get("error")}
        night = self.nightly()
        view["nightly"] = {"enabled": night["enabled"], "hour": NIGHTLY_HOURS[0], "day": night.get("day"), "ended": night.get("ended")}
        # What installing the waiting update, and the setup run after it, does to each machine.
        view["effect"] = self.update_effect(head, check["commit"]) if view["available"] and self.built() else None
        return view

    # What a setup run started now would do (web/review.py).
    def built_plan(self) -> str | None:
        """The plan the server was built from: the one the last setup run
        that finished started with."""
        built = self.last_setup()
        return built.plan_file.read_text() if built is not None and built.plan_file.is_file() else None

    def review(self, plan: dict | None = None, about: dict | None = None) -> dict:
        """What the owner confirms before a setup run: why one is due, and
        what it creates, deletes and changes. `id` names exactly this; the
        page sends it back to start the run, and a run is refused when what
        it would do is no longer what the owner read."""
        plan = self.plan() if plan is None else plan
        about = self.version_view() if about is None else about
        built = self.last_setup()
        runs = self.setup_runs()
        stopped = self.removals()
        removals = [*stopped, *(n for n in self.planned_removals() if n not in stopped)]
        reasons: list[dict] = []

        def why(kind: str, text: str) -> None:
            reasons.append({"kind": kind, "text": text})

        if built is None:
            why("first", "Nothing is built yet. This run builds everything in the plan.")
        if runs and runs[-1].state() == "failed":
            by_owner = bool(runs[-1].meta().get("stopped"))
            why("unfinished", f"The last setup run {'was stopped' if by_owner else 'failed'} before it finished.")
        if stopped:
            why("removals", "Setup stopped before deleting guests and waits for your word.")
        if self.config_changed():
            why("choices", "Your saved choices changed since the last setup run.")
        waiting = (about or {}).get("server") or {}
        if waiting.get("follows") is False:
            count = len(waiting["changes"])
            what = f"{count} change{'' if count == 1 else 's'} to what setup builds" if count else "what setup builds changed"
            why("update", f"NullBill was updated since the last setup run: {what}.")
        if self.tailscale_key():
            why("key", "A Tailscale auth key waits to be used.")
        upgrading = self.release_waiting()
        if upgrading and built is not None:
            n, debian = len(upgrading), build_model(self.config() or {})["images"]["release"]["version"]
            why(
                "release",
                f"{n} guest{' was' if n == 1 else 's were'} built on the Debian before. This run moves "
                f"{'it' if n == 1 else 'each'} to Debian {debian} first, with a snapshot taken before: "
                f"{'it is' if n == 1 else 'each is'} away for some minutes, and at the end this page for about ten.",
            )

        text = plan.get("text")
        config = self.home / "config.yaml"
        seen = [text or "", config.read_text() if config.exists() else "", version.head(self.repo) or "", *sorted(removals), *upgrading]
        out: dict = {
            "id": hashlib.sha256("\0".join(seen).encode()).hexdigest()[:16],
            "ready": bool(text),
            "reasons": reasons,
            "guests": [],
            # What the run does to the host and each guest (nullbill/preview.py);
            # None where that could not be worked out.
            "machines": None,
            "disks": [],
            "sections": [],
            "difference": None,
            # False: built by a NullBill that kept no record of its guests.
            "known": True,
            "commits": waiting.get("changes") or [],
        }
        if not text:
            return out
        now = yaml.safe_load(text)
        was_text = self.built_plan()
        was = yaml.safe_load(was_text) if was_text else None
        existing = self.existing_guests()
        if existing is None and built is not None and was is None:
            existing, out["known"] = set(now.get("guests") or {}), False
        out["guests"] = reviewing.guests(now, was, existing, self.removal_notes(removals))
        out["machines"] = self.foresee(out["id"], out["guests"])
        out["disks"] = reviewing.disks(now, was, self.facts() or {})
        out["sections"] = reviewing.sections(now, was)
        out["difference"] = reviewing.difference(was_text, text)
        return out

    def quiet(self) -> Context | None:
        """The saved choices as apply would take them up, in a context that
        can only be read from (nullbill/preview.py). None before there is a
        plan. Nothing in it reaches the server."""
        config, facts = self.config(), self.facts()
        if config is None or facts is None:
            return None
        model = build_model(config)
        plan = build_plan(model, facts)
        lan_host = split_target(((config.get("target") or {}).get("ssh")) or "")[1] or None
        data = Secrets(self.codec(self.home)).data
        return preview.context(plan, model, self.home, data, render_all(plan, model), lan_host)

    def _seen(self, *names: str) -> list:
        out = []
        for name in names:
            try:
                out.append((self.home / name).stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return out

    def foresee(self, review: str, guests: list[dict]) -> dict | None:
        """What a setup run started now does to the host and each guest:
        left alone, set up again and what that can restart, created or
        deleted. Worked out from the controller's own record of the last
        runs; the server is asked nothing. None where it cannot be said."""
        # The hour too: a CI image comes due for its weekly bake by the clock.
        key = (review, int(time.time() // 3600), *self._seen(applied.RECORD, "secrets.sops.yaml", FULL_NEXT, "lan.json", "plan.yaml"))
        if self.foreseen is None or self.foreseen[0] != key:
            found = None
            try:
                ctx = self.quiet()
                if ctx is not None:
                    built = self.last_setup()
                    commit = self.built_with(built)[0] if built is not None else None
                    base = preview.Baseline(
                        ctx.secrets.data.get("applied_key"), applied.record(self.home), (self.home / FULL_NEXT).exists(), self.repo, commit
                    )
                    found = preview.machines(ctx, base, guests)
            except (NullBillError, KeyError, OSError, ValueError, TypeError):
                # The review stands without it: guests and disks, as before.
                found = None
            self.foreseen = (key, found)
        return self.foreseen[1]

    def update_effect(self, head: str, newest: str) -> dict | None:
        """What the waiting update changes for the host and each guest, read
        from the files the two commits hold. None where git cannot say."""
        key = (head, newest, *self._seen(applied.RECORD, "plan.yaml", "config.yaml"))
        if self.update_seen is None or self.update_seen[0] != key:
            found = None
            try:
                ctx = self.quiet()
                record = applied.record(self.home)
                ran = [(s.machine, s.playbook) for s in preview.steps(ctx)[0]] if ctx is not None else []
                if not ran:
                    # No plan yet: the machines the last runs wrote down.
                    ran = [(None if m == applied.HOST else m, pb) for m, pb in (entry.split("/", 1) for entry in record)]
                    ran = [(m, pb) for m, pb in ran if pb != applied.IMAGE]
                found = preview.update(self.repo, head, newest, ran, record)
            except (NullBillError, KeyError, OSError, ValueError, TypeError):
                found = None
            self.update_seen = (key, found)
        return self.update_seen[1]

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
            "read_at": (self.home / "probe" / "latest" / "facts.json").stat().st_mtime,
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
        plan = self.plan()
        about = self.version_view()
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
            "plan": plan,
            "review": self.review(plan, about),
            "version": about,
            "on_demand": demand.rules(self.saved_plan()),
            "alerts": self.alerts() if has_password else None,
            "agent_credential": self.agent_credential(),
            "tailscale_key": self.tailscale_key(),
            "tailscale_client": self.tailscale_client(),
            "direct": direct.recorded(self.home),
            "port_mapping": self.port_mapping(),
            "remote_agents": sorted(self.remote_agents()),
            "trust": self.trust(),
            "agent_powers": self.agent_powers(),
            "agent_github": self.agent_github(),
            "lan": self.lan_view(client),
            "checks": self.checks(),
            "backups": self.backups_view(plan),
            "stamp": self.stamp(),
            "at": time.time(),
            "chat": self.chat.summary(),
            # Without its log, which the page asks for from the start.
            "job": self.job_view(latest) | {"text": "", "offset": 0} if latest else None,
        }


def refusal(exc: NullBillError) -> dict:
    """A refusal as the page gets it. `error` is all of it in one line, for
    the line beside a button. With several reasons, `errors` lists them and
    `headline` says what did not happen, so the page can show a list in
    place of one long sentence."""
    headline = getattr(exc, "headline", "")
    line = "; ".join(exc.messages)
    body: dict = {"error": f"{headline}: {line}" if headline else line}
    if len(exc.messages) > 1:
        body.update(headline=headline or "That did not work", errors=exc.messages)
    return body


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
                    for versioned in VERSIONED:
                        body = body.replace(f'"/{versioned}"'.encode(), f'"/{versioned}?v={shell()}"'.encode())
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
                return self.json(200, app.status() | {"stamp": app.stamp()})
            if path == "/api/path":
                return self.json(200, {"direct": app.path()})
            if path == "/api/on-demand":
                return self.json(200, {"view": app.on_demand()})
            if path == "/api/backups":
                return self.json(200, {"backups": app.backups_view(), "live": app.backups_live()})
            if path == "/api/backups/key":
                try:
                    key = app.backup_key()
                except NullBillError as exc:
                    return self.json(404, refusal(exc))
                disposition = {"Content-Disposition": f'attachment; filename="{backupview.KEY_FILE}"'}
                return self.send(200, key.encode() + b"\n", "application/json", disposition)
            if path == "/api/game":
                asked = urllib.parse.parse_qs(query)
                try:
                    return self.json(200, {"view": app.game_view(asked.get("name", [""])[0], fresh="fresh" in asked)})
                except NullBillError as exc:
                    return self.json(404, refusal(exc))
            if path == "/api/game/mods":
                try:
                    return self.json(200, {"view": app.mods_view(urllib.parse.parse_qs(query).get("name", [""])[0])})
                except NullBillError as exc:
                    return self.json(404, refusal(exc))
            if path == "/api/game/world":
                return self.world_download(urllib.parse.parse_qs(query))
            if path == "/api/own-service":
                try:
                    return self.json(200, app.own_service(urllib.parse.parse_qs(query).get("name", [""])[0]))
                except NullBillError as exc:
                    return self.json(404, refusal(exc))
            if path == "/api/people":
                try:
                    return self.json(200, app.people_view(fresh="fresh=1" in query.split("&")))
                except NullBillError as exc:
                    return self.json(400, refusal(exc))
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
            if path == "/api/chat/image":
                # A picture the owner attached. Its type was checked by its
                # bytes when it was kept, and the page's headers hold the browser to it.
                try:
                    kind, data = app.chat.image(dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("id", ""))
                except NullBillError as exc:
                    return self.json(404, {"error": exc.messages[0]})
                return self.send(200, data, kind, {"Cache-Control": "private, max-age=86400"})
            match = re.fullmatch(r"/api/jobs/([0-9T]+-[a-z-]+)", path)
            if match and match[1] in app.jobs:
                return self.json(200, app.job_view(app.jobs[match[1]], offset(query)))
            return self.json(404, {"error": "not found"})

        def world_download(self, asked: dict) -> None:
            """One of a game server's worlds as a file. Sent with its length:
            a stream that ends short closes the connection, so the browser
            says the download failed instead of keeping half a world."""
            try:
                name, size, stream, done = app.world_download(asked.get("name", [""])[0], asked.get("id", [""])[0])
            except NullBillError as exc:
                return self.json(400, refusal(exc))
            try:
                self.send_response(200)
                kind = "application/zip" if name.endswith(".zip") else "application/octet-stream"
                fixed = SECURITY_HEADERS | {SHELL_HEADER: shell(), "Content-Type": kind, "Content-Length": str(size)}
                for header, value in (fixed | {"Content-Disposition": f'attachment; filename="{name}"'}).items():
                    self.send_header(header, value)
                self.end_headers()
                left = size
                while left and (chunk := stream.stdout.read(min(STREAM_CHUNK, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
                if left:
                    self.close_connection = True
            except OSError:
                self.close_connection = True
            finally:
                done()

        def world_upload(self) -> None:
            """A world file as the request's body. Who sends it is checked
            before a byte of it is read; a refusal closes the connection
            instead of reading gigabytes to throw away."""
            self.close_connection = True
            if self.closed():
                return None
            if self.headers.get("X-NullBill") != "1":
                return self.json(403, {"error": "missing X-NullBill header"})
            if not app.valid(self.token()):
                return self.json(401, {"error": "sign in first"})
            asked = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            try:
                answer, whole = app.world_upload(asked.get("name", [""])[0], asked.get("title", [""])[0], length, self.rfile)
            except NullBillError as exc:
                return self.json(400, refusal(exc))
            self.close_connection = not whole
            return self.json(200, answer)

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
            if self.path.partition("?")[0] == "/api/game/world":
                return self.world_upload()
            # The body is read before any answer: on a kept-alive
            # connection, unread bytes would be taken for the next request.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            # Only a signed-in owner's message to the agent may be large: it carries pictures.
            limit = 64 * 1024
            if self.path == "/api/chat" and app.valid(self.token()):
                limit += chat.MAX_IMAGES * (chat.MAX_IMAGE_BYTES * 4 // 3 + 1024)
            if not 0 <= length <= limit:
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
                    return self.json(409, refusal(exc))
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
                if self.path == "/api/own-service/remove":
                    return self.json(200, {"ok": True, "removed": app.forget_service(str(body.get("name", "")))})
                if self.path == "/api/game-server":
                    return self.json(200, {"ok": True, "added": app.add_game(str(body.get("game", "")), str(body.get("title", "")))})
                if self.path == "/api/game-server/remove":
                    return self.json(200, {"ok": True, "removed": app.forget_game(str(body.get("name", "")))})
                if self.path == "/api/on-demand":
                    return self.json(200, app.power_act(body))
                if self.path == "/api/game":
                    return self.json(200, app.game_act(str(body.get("name", "")), body))
                if self.path == "/api/game/mods":
                    return self.json(200, app.mods_act(str(body.get("name", "")), body))
                if self.path == "/api/alerts":
                    app.save_alerts(body)
                    return self.json(200, {"ok": True, "alerts": app.alerts()})
                if self.path == "/api/agent":
                    app.save_agent_credential(body)
                    app.chat.welcome()
                    return self.json(200, {"ok": True, "agent_credential": app.agent_credential(), "chat": app.chat.summary()})
                if self.path == "/api/chat":
                    app.chat.say(str(body.get("text", "")), images=body.get("images"))
                    return self.json(200, app.chat.view(int(body.get("after") or 0)))
                if self.path == "/api/chat/task":
                    if app.chat.end_task("owner", "You ended it.") is None:
                        raise NullBillError("no task is on")
                    return self.json(200, {"ok": True})
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
                if self.path == "/api/backups":
                    app.save_backups(body)
                    plan = app.plan()
                    return self.json(200, {"ok": True, "plan": plan, "backups": app.backups_view(plan)})
                if self.path == "/api/torrents/forget-vpn":
                    app.forget_torrents_vpn()
                    return self.json(200, {"ok": True})
                if self.path == "/api/port-mapping":
                    return self.json(200, {"ok": True, "port_mapping": app.save_port_mapping(body)})
                if self.path == "/api/password-reveal":
                    return self.json(200, {"password": app.password(str(body.get("name", "")))})
                if self.path == "/api/alerts/test":
                    return self.json(200, {"ok": True, "sent": app.test_alerts(str(body.get("origin", "")))})
                if self.path == "/api/alerts/heard":
                    app.alert_heard(str(body.get("word", "")))
                    return self.json(200, {"ok": True, "alerts": app.alerts()})
                if match := re.fullmatch(r"/api/jobs/([0-9T]+-[a-z-]+)/stop", self.path):
                    return self.json(200, app.job_view(app.stop(match[1])))
                if self.path == "/api/updates/nightly":
                    app.set_nightly(body.get("enabled") is True)
                    return self.json(200, {"ok": True, "version": app.version_view()})
                if self.path == "/api/updates/check":
                    app.check_updates()
                    return self.json(200, {"ok": True, "version": app.version_view()})
                if self.path == "/api/jobs":
                    stage = body.get("stage")
                    seen = body.get("guests")
                    guests = [str(n) for n in seen] if isinstance(seen, list) else None
                    action, review = str(body.get("action")), None
                    if action in SETUP_ACTIONS and not stage:
                        # The owner starts what they read, or reads again.
                        review = str(body.get("review") or "")
                        if review != app.review()["id"]:
                            return self.json(409, {"error": REVIEW_AGAIN, "review": True})
                    job = app.start(action, str(stage) if stage else None, self.client(), guests=guests, review=review)
                    return self.json(200, app.job_view(job))
                if self.path == "/api/logout":
                    app.logout(self.token())
                    return self.json(200, {"ok": True}, {"Set-Cookie": "nb_session=; Path=/; Secure; HttpOnly; Max-Age=0"})
            except NullBillError as exc:
                return self.json(400, refusal(exc))
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
    app.load_sessions()
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
