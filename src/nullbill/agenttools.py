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
    "ci",
    "agents",
    "logs",
    "backups",
    "vpn",
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
        "name": "set_choices",
        "description": "Change what the server runs (the Setup page's choices); the next setup run builds it. Disks "
        "are the owner's to change on the Setup page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workloads": {
                    "type": "array",
                    "items": {"enum": ["agents", "linux-ci", "inference", "media", "windows-ci", "windows-dev"]},
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
