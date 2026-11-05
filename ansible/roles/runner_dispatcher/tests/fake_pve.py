"""A stand-in for the PVE API calls the dispatcher makes, for its check.

Usage: python3 fake_pve.py CERT KEY TOKEN STATE_DIR

Serves HTTPS on 127.0.0.1:8006 and answers only a request carrying TOKEN.
It holds a template (2030, in no pool) and an unrelated guest (2017). A
clone of the template is a record; starting it runs the boot-time job
pickup (nb-runner-job) with the clone's SMBIOS serial in STATE_DIR/serial,
and the clone "powers off" when that exits. Every call is appended to
STATE_DIR/calls.log as "METHOD PATH [form]", and the refusals the real PVE
would make for this token (anything outside the CI pool except cloning the
template) are answered 403.
"""

import http.server
import json
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

CERT, KEY, TOKEN, STATE = sys.argv[1:5]
POOL = "nb-ci"
NODE = "pve"
TEMPLATE = 2030
lock = threading.Lock()
vms = {
    TEMPLATE: {"vmid": TEMPLATE, "name": "linux-runner", "status": "stopped", "template": 1, "config": {"smbios1": "uuid=t"}},
    2017: {"vmid": 2017, "name": "forgejo", "status": "running", "config": {}},
}
tasks: dict[str, str] = {}


def task(what: str) -> str:
    upid = f"UPID:{NODE}:{len(tasks):08X}:{what}:"
    tasks[upid] = "OK"
    return upid


def run_clone(vmid: int, serial: str) -> None:
    with open(f"{STATE}/serial", "w") as f:
        f.write(serial)
    with open(f"{STATE}/vm-{vmid}.log", "w") as out:
        subprocess.run(["/usr/local/sbin/nb-runner-job"], stdout=out, stderr=subprocess.STDOUT, check=False)
    with lock:
        if vmid in vms:
            vms[vmid]["status"] = "stopped"


class Handler(http.server.BaseHTTPRequestHandler):
    def reply(self, code: int, data: object = None) -> None:
        body = json.dumps({"data": data}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(length).decode())) if length else {}
        url = urllib.parse.urlsplit(self.path)
        with open(f"{STATE}/calls.log", "a") as f:
            f.write(f"{method} {url.path} {json.dumps(form, sort_keys=True) if form else ''}\n".rstrip() + "\n")
        if self.headers.get("Authorization") != f"PVEAPIToken={TOKEN}":
            return self.reply(401)
        parts = url.path.removeprefix("/api2/json/").split("/")
        with lock:
            return self.route(method, parts, form)

    def route(self, method: str, parts: list[str], form: dict) -> None:
        if parts == ["cluster", "resources"]:
            now = time.time()
            out = []
            for vm in vms.values():
                entry = {k: vm[k] for k in ("vmid", "name", "status")} | {"type": "qemu", "node": NODE}
                if vm.get("pool"):
                    entry["pool"] = vm["pool"]
                if vm.get("template"):
                    entry["template"] = 1
                if vm["status"] == "running" and "since" in vm:
                    entry["uptime"] = int(now - vm["since"])
                out.append(entry)
            return self.reply(200, out)
        if parts[:3] == ["nodes", NODE, "tasks"] and parts[4:] == ["status"]:
            upid = urllib.parse.unquote(parts[3])
            return self.reply(200, {"status": "stopped", "exitstatus": tasks.get(upid, "unknown task")})
        if parts[:3] != ["nodes", NODE, "qemu"] or len(parts) < 4:
            return self.reply(404)
        vmid = int(parts[3])
        vm = vms.get(vmid)
        rest = parts[4:]
        if vm is None:
            return self.reply(404)
        if method == "POST" and rest == ["clone"]:
            newid = int(form["newid"])
            if vmid != TEMPLATE or form.get("pool") != POOL or newid in vms:
                return self.reply(403)
            vms[newid] = {"vmid": newid, "name": form["name"], "status": "stopped", "pool": POOL, "config": {"smbios1": f"uuid=u{newid}"}}
            return self.reply(200, task("qmclone"))
        if vm.get("pool") != POOL:
            return self.reply(403)
        if method == "GET" and rest == ["config"]:
            return self.reply(200, vm["config"])
        if method == "PUT" and rest == ["config"]:
            vm["config"].update(form)
            return self.reply(200)
        if method == "POST" and rest == ["status", "start"]:
            serial = dict(p.split("=", 1) for p in vm["config"]["smbios1"].split(",")).get("serial", "")
            vm["status"], vm["since"] = "running", time.time()
            threading.Thread(target=run_clone, args=(vmid, serial), daemon=True).start()
            return self.reply(200, task("qmstart"))
        if method == "POST" and rest == ["status", "stop"]:
            vm["status"] = "stopped"
            return self.reply(200, task("qmstop"))
        if method == "DELETE" and rest == []:
            del vms[vmid]
            return self.reply(200, task("qmdestroy"))
        return self.reply(404)

    def do_GET(self) -> None:  # noqa: N802
        self.handle_any("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.handle_any("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self.handle_any("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self.handle_any("DELETE")

    def log_message(self, fmt: str, *args: object) -> None:
        pass


tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(CERT, KEY)
server = http.server.ThreadingHTTPServer(("127.0.0.1", 8006), Handler)
server.socket = tls.wrap_socket(server.socket, server_side=True)
server.serve_forever()
