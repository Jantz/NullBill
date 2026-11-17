"""The page against stand-ins, in a named state, for looking at it in a
browser: `tools/page-demo/serve.py healthy` serves https://127.0.0.1:8443
and prints the session cookie a browser needs (README.md beside this file).

Nothing here reaches a server. `nullbill` is fake_nullbill.py, Prometheus
and the host answer from this file, the secrets stay in memory and the
home is a temporary folder that goes when the process ends.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import yaml  # noqa: E402
from conftest import direct_answers, portmap_state, tailscale_peer  # noqa: E402
from fixturedata import fixture_config, games_config, torrents_config  # noqa: E402

from nullbill import backups, direct, power  # noqa: E402
from nullbill.web import backupview, demand  # noqa: E402
from nullbill.web import server as web  # noqa: E402

PASSWORD = "correct horse battery"
OWNER = "owner-a@example.internal"
STATES = ("first", "fresh", "planned", "running", "failed", "healthy", "degraded", "changed", "unreachable")
# Alerts as the monitoring role's rules word them.
ALERTS = [
    ("BackupTooOld", "page", "forgejo", "forgejo has no backup newer than 26 h; open Backups for when each guest was last backed up"),
    ("DiskAlmostFull", "warn", "monitoring", "/ on monitoring is over 85% full; ask the agent what takes the room"),
    ("RebootRequired", "warn", "pve", "pve needs a restart for an update; restart it from Proxmox when nothing runs"),
]
CHECKS = [
    ["DEGRADED", "backups share a disk with the guests", "a disk failure takes both; give backups a disk of their own on the Backups page"],
    ["DEGRADED", "the agent has no root on the PVE host", "the owner's switch is on (Agent, Settings)"],
    ["SKIP", "devices on mobile data reach the vpn guest directly", "no device away from home answered; open the page on mobile data"],
    ["SKIP", "windows-dev answers on RDP", "Windows is not installed yet"],
]


class Codec:
    """The secrets as plain JSON in the demo's home, where fake_nullbill.py
    reads and writes them too."""

    def __init__(self, home: Path):
        self.path = home / "demo-secrets.json"

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data))


class Server:
    """Prometheus and the host, as a built server answers them."""

    def __init__(self, app: web.App, state: str):
        self.app, self.state = app, state
        self.on = {"media": True, "agents": False}

    def plan(self) -> dict:
        return self.app.saved_plan()

    def prometheus(self, url: str) -> dict:
        if self.state == "unreachable":
            raise OSError("[Errno 113] No route to host")
        plan, now = self.plan(), time.time()
        if url.endswith("/alerts"):
            firing = ALERTS if self.state == "degraded" else []
            return {
                "data": {
                    "alerts": [
                        {"state": "firing", "labels": {"alertname": n, "severity": s, "instance": i}, "annotations": {"summary": text}}
                        for n, s, i, text in firing
                    ]
                }
            }
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]

        def row(value: object, **labels: str) -> dict:
            return {"metric": labels, "value": [now, str(value)]}

        guests = backups.guests(plan) if plan.get("backups", {}).get("status") not in (None, "disabled") else {}
        late = "forgejo" if self.state == "degraded" else None
        saved = {
            "listed": [row(1)],
            "last": [row(now - (30 if n == late else 5) * 3600, vmid=str(g["vmid"]), guest=n) for n, g in guests.items()],
            "exists": [row(1, vmid=str(g["vmid"]), guest=n) for n, g in guests.items()],
            "restore_ok": [row(1, vmid="2017", guest="forgejo")],
            "restore_at": [row(now - 4 * 3600, vmid="2017", guest="forgejo")],
            "restore_backup": [row(now - 5 * 3600, vmid="2017", guest="forgejo")],
            "store": [row(64 << 30, kind="size"), row((52 if self.state == "degraded" else 9) << 30, kind="used")],
            "read": [row(now - 600)],
        }
        for name, expr in backupview.QUERIES.items():
            if expr == query:
                return {"data": {"result": saved[name] if guests else []}}
        off = [n for g in self.groups() if g["state"] == "off" for n in (m["name"] for m in g["guests"])]
        down = ["forgejo"] if self.state == "degraded" else []
        answers = {
            "up": [row(0 if n in down else 1, instance=n) for n in ["pve", *plan.get("guests", {})]],
            "nb_power_guest_off == 1": [row(1, guest=n) for n in off],
            "node_reboot_required == 1": [row(1, instance="pve")] if self.state == "degraded" else [],
            'sum by (instance) (apt_upgrades_pending{origin=~".*-security.*"}) > 0': [row(3, instance="router")] if down else [],
        }
        return {"data": {"result": answers.get(query, [])}}

    def groups(self) -> list[dict]:
        """What nb-power says of each workload the plan runs on demand."""
        plan = self.plan()
        out = []
        for g in (demand.rules(plan) or {}).get("groups", []):
            on = self.on.get(g["name"], False)
            guests = [
                {"name": n, "kind": plan["guests"][n]["kind"], "vmid": plan["guests"][n]["vmid"], "state": "running" if on else "stopped"}
                for n in g["guests"]
            ]
            used = {"busy": ["playing on 1 device"], "idle_s": 0, "keep": False, "asks": False, "setup": False} if on else {}
            out.append({**g, "state": "on" if on else "off", "guests": guests} | used)
        return out

    def host(self, target: str, command: str) -> tuple[int, str, str]:
        if self.state == "unreachable":
            return 255, "", "ssh: connect to host 192.0.2.10 port 22: No route to host"
        plan = self.plan()
        if command == f"{power.COMMAND} status --json":
            groups, now = self.groups(), int(time.time())
            used = sum(g["memory_mib"] for g in groups if g["state"] == "on")
            room = (demand.rules(plan) or {}).get("headroom_mib", 0)
            status = {"now": now, "ticked": now - 20, "headroom_mib": room, "used_mib": used, "free_mib": room - used, "setup": False}
            events = [{"at": now - 1500, "group": "media", "what": "started", "why": "by the page"}]
            return 0, json.dumps(status | {"groups": groups, "events": events}), ""
        if command.startswith(power.COMMAND):
            words = command.split()
            if len(words) > 2 and words[1] in ("start", "stop"):
                self.on[words[2]] = words[1] == "start"
            return 0, json.dumps({"ok": True}), ""
        peers = [
            tailscale_peer("phone", cur_addr="198.51.100.23:41641"),
            tailscale_peer("laptop", cur_addr="192.168.1.20:41641"),
            tailscale_peer("tablet", online=False),
        ]
        if self.state == "degraded":
            peers = [tailscale_peer("phone"), tailscale_peer("laptop", cur_addr="192.168.1.20:41641")]
        try:
            # A minute before the stand-in router's own clock (conftest.ROUTER_CLOCK).
            state = portmap_state()
            answers = direct_answers(plan, state=state, peers=peers, pongs={"phone": "DERP(fra)"})
        except (KeyError, StopIteration):
            answers = {}
        if command in answers:
            return 0, answers[command], ""
        return 0, "", ""


def write_config(home: Path, config: dict) -> None:
    config = {**config, "target": {"ssh": "root@192.0.2.10"}}
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))


def rich_config() -> dict:
    """The large class with a kept disk, Jellyfin, the download client and
    every game: most of what the page can show."""
    games, torrents = games_config(), torrents_config("home")
    workloads = [*torrents["intent"]["workloads"], *(w for w in games["intent"]["workloads"] if w not in torrents["intent"]["workloads"])]
    config = {**torrents, "intent": {**torrents["intent"], "workloads": workloads}, "people": games["people"], "games": games["games"]}
    config["power"] = {"media": {"run": "on-demand", "stop": "auto", "idle_minutes": 30}, "agents": {"run": "on-demand"}}
    return config


def demo(home: Path, **how: object) -> None:
    (home / "demo.json").write_text(json.dumps(how))


def run(app: web.App, action: str, wait: bool = True, **more: object) -> web.Job:
    # As the page does before a run: the plan of the saved choices is written down.
    app.plan()
    job = app.start(action, **more)
    while wait and job.state() == "running":
        time.sleep(0.05)
    return job


def prepare(app: web.App, home: Path, state: str) -> None:
    """Brings the page to the state, by the calls the page itself makes."""
    demo(home)
    if state == "first":
        return
    app.set_password(PASSWORD)
    if state == "fresh":
        return
    run(app, "probe")
    write_config(home, rich_config() if state != "planned" else fixture_config("large"))
    if state == "planned":
        return
    codec = Codec(home)
    owned = {"claude_oauth_token": "sk-ant-oat01-token", "tailscale_auth_key": "tskey-auth-kExampleCNTRL-once"}
    owned |= {"tailscale_client_id": "kExampleCNTRL", "tailscale_client_secret": "tskey-client-kExampleCNTRL-secret"}
    codec.save(codec.load() | owned)
    if state == "failed":
        demo(home, fail={"stage": "forgejo", "output": "apt-403.txt", "message": "forgejo.yml failed on forgejo (exit 2)"})
        run(app, "apply")
        return
    if state == "running":
        demo(home, pace=6)
        run(app, "apply", wait=False)
        return
    demo(home, checks=CHECKS if state == "degraded" else CHECKS[2:])
    run(app, "apply")
    if not (home / direct.STATE).exists():
        app.path()
    if state == "changed":
        time.sleep(1.1)
        config = yaml.safe_load((home / "config.yaml").read_text())
        config["intent"]["workloads"] = [w for w in config["intent"]["workloads"] if w != "terraria"]
        write_config(home, config)
    if state == "degraded":
        demo(home, checks=[*CHECKS, ["FAIL", "forgejo answers on its ports", "nothing answers on 443"]])
        run(app, "verify")


def build(folder: Path, state: str) -> web.App:
    """The page's App over a new home in folder, brought to the state."""
    home = folder / "home"
    home.mkdir(mode=0o700)
    os.environ["NULLBILL_HOME"] = str(home)
    (home / "install.json").write_text(json.dumps({"host": "192.0.2.10", "node": "pve", "vmid": 2002}))
    app = web.App(
        home,
        command=[sys.executable, str(HERE / "fake_nullbill.py")],
        codec=Codec,
        send=lambda *a, **k: None,
        agent_command=[sys.executable, str(HERE / "fake_agent.py")],
        agent_user=None,
        agent_home=folder / "agent-home",
        tools_socket=folder / "s" / "tools.sock",
    )
    server = Server(app, state)
    app.fetch = server.prometheus
    app.host_run = app.host_act = app.host_long = server.host
    app.ensure_setup_code()
    prepare(app, home, state)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("state", choices=STATES)
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="nb-page-demo-") as folder:
        app = build(Path(folder), args.state)
        tools = web.serve_tools(app)
        site, _ = web.make_server(app, ("127.0.0.1", args.port), *web.ensure_certificate(app.home))
        threading.Thread(target=site.serve_forever, daemon=True).start()
        token = app.login(PASSWORD) if args.state != "first" else None
        print(f"https://127.0.0.1:{args.port}/  state: {args.state}", flush=True)
        if token:
            print(f"cookie: nb_session={token}", flush=True)
        else:
            print(f"setup code: {app.code_file.read_text().strip()}", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            site.shutdown()
            tools.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
