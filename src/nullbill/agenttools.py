"""`nullbill agent-tools`: the page agent's tools, as an MCP server on stdio.

The agent CLI starts this for each turn (web/chat.py writes its config).
It holds nothing of its own: each call goes to the page over a local
socket with the turn's token, and the page decides what runs
(docs/adr/0010-page-agent.md). Standard library only: newline-delimited
JSON-RPC 2.0, the MCP stdio transport.
"""

import http.client
import json
import os
import socket
import sys

PROTOCOL = "2025-06-18"
# apply's stages, in order (apply.STAGES; tests/test_chat.py keeps them equal).
STAGES = [
    "render",
    "tailnet",
    "host",
    "tofu",
    "router",
    "proxy",
    "updates",
    "ca",
    "postgres",
    "monitoring",
    "forgejo",
    "inference",
    "files",
    "media",
    "games",
    "ci",
    "agents",
    "logs",
    "backups",
    "vpn",
    "custom",
    "verify",
]
WHY = {"type": "string", "description": "One sentence for the owner: why you want this. Shown on their approval card."}
TOOLS = [
    {
        "name": "status",
        "description": "The server at a glance: hardware, the owner's choices, whether the plan fits, the guests, recent "
        "runs, home network and Tailscale state, monitoring alerts and the owner's access mode. Start here.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_jobs",
        "description": "Recent runs (probe, setup, checks, updates) with their ids and how each ended.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "read_job",
        "description": "One run's log: its stages, why it failed in plain words, and the last lines, or the lines "
        "around each match of find.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "lines": {"type": "integer", "description": "How many lines from the end (default 200, at most 2000)."},
                "find": {"type": "string", "description": "Show the lines around each line holding this text instead."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "wait_for_job",
        "description": "Wait until a run ends, at most 120 s, then show how it ended. For a longer run use watch_job.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "seconds": {"type": "integer"}}, "required": ["id"]},
    },
    {
        "name": "watch_job",
        "description": "Be told when a run ends: end your turn after this, and a note with how it ended starts your next "
        "turn. Use it for setup runs, which take many minutes.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "read_file",
        "description": "NullBill's own files about this server: plan (what setup builds, with addresses and sizes), "
        "config (the owner's choices), probe (the hardware facts) or tailnet (the Tailscale policy).",
        "inputSchema": {"type": "object", "properties": {"which": {"enum": ["plan", "config", "probe", "tailnet"]}}, "required": ["which"]},
    },
    {
        "name": "run_command",
        "description": "Run one shell command as root on the Proxmox host (where: host) or inside a guest by its plan "
        "name (where: forgejo, router, ...). Read-only commands run at once. Others run or wait for the owner's "
        "approval by their access mode; some are refused in every mode. Output over 20000 characters is cut; "
        "commands time out after 120 s (use background for longer ones). No interactive programs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "where": {"type": "string", "default": "host"},
                "background": {
                    "type": "boolean",
                    "description": "Run it apart, for anything that may take over a minute (apt, a build); read_run shows its output.",
                },
                "why": WHY,
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_run",
        "description": "The state, exit status and last lines of output of a command you started with run_command background=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The 8 characters run_command gave."},
                "where": {"type": "string", "default": "host"},
                "lines": {"type": "integer", "description": "How many lines from the end (default 200)."},
            },
            "required": ["id"],
        },
    },
    {
        "name": "run_steps",
        "description": "Several commands the owner approves as one plan (one card), run in order; the run stops at the "
        "first that fails. Use it for a fix that takes a few steps, so the owner taps once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}, "where": {"type": "string", "default": "host"}},
                        "required": ["command"],
                    },
                },
                "why": WHY,
            },
            "required": ["steps"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a whole file on the host or a guest. The owner's card shows the diff against the file now; "
        "the old file is kept in /var/lib/nullbill-agent-backup. Use it instead of echo or sed for config files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "One absolute path."},
                "content": {"type": "string", "description": "The whole new file."},
                "where": {"type": "string", "default": "host"},
                "why": WHY,
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "update_packages",
        "description": "Install the waiting Debian or Proxmox updates on the host or a guest. The page first simulates "
        "the upgrade and shows the owner what it installs or removes, then runs it in the background.",
        "inputSchema": {"type": "object", "properties": {"where": {"type": "string", "default": "host"}, "why": WHY}},
    },
    {
        "name": "check_reach",
        "description": "How the page reaches Proxmox: name, port 22, SSH with timing, the clock difference and free "
        "space. Use it first when a run fails to connect.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "remember",
        "description": "Keep one short fact about this server for later conversations (your notes, which the owner can "
        "read and edit). Not for secrets.",
        "inputSchema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
    },
    {
        "name": "start_job",
        "description": "Start a run: probe (read the hardware) or verify (run the checks) start at once; apply (setup, "
        "all stages or one) and upgrade (update NullBill from main) run or wait for the owner by their access mode.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"enum": ["probe", "verify", "apply", "upgrade"]},
                "stage": {"enum": STAGES, "description": "With apply: run this one stage only."},
                "why": WHY,
            },
            "required": ["action"],
        },
    },
    {
        "name": "read_service",
        "description": "One of the owner's own services (status marks them own): what it is, what it opens, and its setup script.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "add_service",
        "description": "Add a program NullBill has no built-in support for as a service of the owner's own, or change "
        "one (give only what changes). NullBill makes a Debian 12 guest of its own for it with an address and a name, "
        "the firewall for exactly the ports listed, updates, monitoring, backups and on-demand power; the setup script "
        "puts the program in, run as root inside the guest by the next setup run, and again whenever the script "
        "changes. The owner reads the definition and the whole script before it is saved. Read "
        "docs/runbooks/custom-services.md first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Lowercase letters, digits and dashes; the guest's name.",
                },
                "title": {"type": "string", "description": "What the page calls it, such as Paperless."},
                "what": {"type": "string", "description": "One line for the owner: what it is for."},
                "setup": {
                    "type": "string",
                    "description": "The whole setup script (#!/bin/bash, set -eu). It runs as root with no terminal, "
                    "must be safe to run again, and gets NB_SERVICE, NB_ADDRESS, NB_DOMAIN, NB_PORTS and the proxy "
                    "variables (http_proxy, https_proxy). Leave out to keep the script it has.",
                },
                "kind": {
                    "enum": ["lxc", "vm"],
                    "description": "lxc (a container); vm when it needs its own kernel or runs Docker.",
                },
                "zone": {"type": "string", "description": "apps unless it is a game server (games)."},
                "cores": {"type": "integer"},
                "memory_mib": {"type": "integer"},
                "disk_gib": {"type": "integer"},
                "ports": {
                    "type": "array",
                    "description": "Each port the program listens on and who may reach it. from: role:owner (the "
                    "owner's devices), shared (people the owner ticks it for), zone:<name>, service:<name>.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "port": {"type": "integer"},
                            "proto": {"enum": ["tcp", "udp"]},
                            "from": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["port"],
                    },
                },
                "proxy_allow": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names this guest may fetch from besides the server's usual list (Debian, GitHub, "
                    "PyPI, npm, crates.io, Docker Hub, ghcr.io, Hugging Face); a leading dot takes subdomains too.",
                },
                "share": {
                    "type": "object",
                    "description": "How it reads to people it is shared with: what (one line), open (such as http://{host}:{port}/), note.",
                    "properties": {"what": {"type": "string"}, "open": {"type": "string"}, "note": {"type": "string"}},
                },
                "run": {"enum": ["always", "on-demand"]},
                "stop": {"enum": ["auto", "ask", "manual"]},
                "idle_minutes": {"type": "integer"},
                "on": {"type": "boolean", "description": "Whether it is ticked under Setup, What to run. A new one is on."},
                "why": WHY,
            },
            "required": ["name"],
        },
    },
    {
        "name": "remove_service",
        "description": "Forget one of the owner's own services: only one that is turned off (set_choices) and whose "
        "guest a setup run has deleted, which asks the owner first. Its setup script is set aside, not erased.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "why": WHY}, "required": ["name"]},
    },
    {
        "name": "add_game_server",
        "description": "One more server of a game NullBill has built in (status, what_to_run, names each game server "
        "and its game), in a container of its own with its own world. The next setup run builds it. Its name on the "
        "server comes from the title. Refused when the plan does not fit; then say what would have to change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "game": {"enum": ["minecraft", "terraria", "factorio"]},
                "title": {"type": "string", "description": "What the owner and their friends see it called, such as Creative world."},
                "settings": {
                    "type": "object",
                    "description": "The server's own options by their own names (server.properties, serverconfig.txt, "
                    "server-settings.json), such as gamemode: creative. NullBill sets the port and who may connect itself.",
                },
                "admins": {"type": "array", "items": {"type": "string"}, "description": "Player names; Terraria has none."},
                "memory_mib": {"type": "integer", "description": "The container's memory; the game's own when omitted."},
                "on": {"type": "boolean", "description": "Whether it is ticked under Setup, What to run. A new one is on."},
                "why": WHY,
            },
            "required": ["game", "title"],
        },
    },
    {
        "name": "remove_game_server",
        "description": "Forget a further game server: only one that is turned off (set_choices) and whose guest a setup "
        "run has deleted, which asks the owner first. A game's first server is turned off, never removed.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "why": WHY}, "required": ["name"]},
    },
    {
        "name": "set_choices",
        "description": "Change what the server runs (the Setup page's choices); the next setup run builds it. Disks "
        "are the owner's to change on the Setup page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workloads": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every workload that should run, by the names status lists under what_to_run "
                    "(built-in ones and the owner's own); one left out is turned off.",
                },
                "backups": {"type": "boolean"},
                "console": {"type": "boolean"},
                "owner_login": {"type": "string"},
                "gpus": {"type": "object", "description": "PCI address to the card's memory in GB, for cards the probe could not read."},
                "why": WHY,
            },
        },
    },
    {
        "name": "ask_owner",
        "description": "Ask the owner a question on their page, with up to 6 short options they can tap (they can also "
        "write their own answer). Use it for decisions that are theirs. Then end your turn.",
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
            "required": ["question"],
        },
    },
]


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=1000)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def call(name: str, args: dict) -> tuple[str, bool]:
    conn = UnixConnection(os.environ["NB_TOOLS_SOCKET"])
    body = json.dumps({"name": name, "args": args})
    try:
        conn.request(
            "POST",
            "/call",
            body=body,
            headers={"Authorization": f"Bearer {os.environ['NB_TOOLS_TOKEN']}", "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
    except (OSError, ValueError) as exc:
        return f"the NullBill page did not answer: {exc}", True
    if response.status != 200:
        return str(data.get("error", f"the page answered {response.status}")), True
    return str(data.get("text", "")), bool(data.get("is_error"))


def answer(request: dict) -> dict | None:
    method, rid = request.get("method"), request.get("id")
    if rid is None:
        return None  # a notification
    if method == "initialize":
        version = (request.get("params") or {}).get("protocolVersion") or PROTOCOL
        result: dict = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": {"name": "nullbill", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        text, failed = call(str(params.get("name")), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout) -> int:
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except ValueError:
            reply: dict | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = answer(request)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0
