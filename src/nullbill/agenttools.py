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
    "torrents",
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
        "runs, home network and Tailscale state, monitoring alerts and the owner's access mode. Also what a setup run "
        "started now, or a waiting update, would do to each machine: left alone, set up again and what may restart, "
        "created or deleted; quote that when asked what a run or an update does to a service. Start here.",
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
        "name": "stop_job",
        "description": "End a run that hangs. What it finished stays; the rest waits for the next run.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "why": WHY}, "required": ["id"]},
    },
    {
        "name": "services",
        "description": "Every guest on the server and what the owner opens on each (the Services page): address, name, "
        "sign-in name, whether it is on. Passwords are never shown. Read docs/runbooks/agent-services.md for how to work "
        "inside each guest.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "metrics",
        "description": "Ask the server's Prometheus one PromQL question, such as up == 0, "
        'node_filesystem_avail_bytes{mountpoint="/"} or nb_game_up. Every guest and the host report there.',
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "on_demand",
        "description": "Start or stop a workload that runs on demand, or keep it on (status lists them under on_demand). "
        "A start that does not fit and a stop of something in use change nothing and say what the owner must decide.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"enum": ["start", "stop", "keep"]},
                "name": {"type": "string", "description": "The workload's name, as status lists it."},
                "stop": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "With start: workloads to stop first to make room, once the owner agreed to those.",
                },
                "force": {"type": "boolean", "description": "With stop: stop it although it is in use, once the owner agreed."},
                "on": {"type": "boolean", "description": "With keep: true keeps it on until told otherwise, false lets it stop again."},
                "why": WHY,
            },
            "required": ["action", "name"],
        },
    },
    {
        "name": "backups",
        "description": "The Backups page: whether backups are on, where they go (the server's own disk, which a failing disk "
        "takes along, or a disk of their own), each service's newest backup, the last restore test and the room they use, as "
        "the server measured them, and whether the owner has downloaded the key.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "people",
        "description": "Access, People: who may open what over Tailscale, what can be given to a person, and what giving a service means.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_person",
        "description": "Add a person or change what they may open. The owner approves it; giving more needs a typed "
        "word. The invitation to Tailscale is the owner's to send.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Lowercase letters, digits and dashes, such as alex."},
                "login": {"type": "string", "description": "The email address they sign in to Tailscale with. Leave out to keep it."},
                "services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every service they may open, by the names people lists under can_be_given. Leave out to keep them.",
                },
                "why": WHY,
            },
            "required": ["name"],
        },
    },
    {
        "name": "remove_person",
        "description": "Take a person off the server: they can open nothing any more, and their Tailscale account "
        "leaves the owner's network when the page may remove it.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "why": WHY}, "required": ["name"]},
    },
    {
        "name": "port_mapping",
        "description": "Without `enabled`: how devices reach the server right now (Access, Tailscale, Path), measured on the "
        "server: each device on the tailnet with its path, direct or through a relay, and what the home router answered about "
        "Tailscale's port. With `enabled`: whether the router asks the home router to forward that port (UPnP or NAT-PMP). The "
        "switch only decides who opens the port, the server or the owner by hand. Switching it off never makes a connection "
        "direct and is no fix for a Path that says relayed or not measured; a port the owner forwarded by hand works with it on or off.",
        "inputSchema": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "why": WHY}},
    },
    {
        "name": "check_updates",
        "description": "Ask whether NullBill itself has updates waiting (the Updates page's Check for updates).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "test_alert",
        "description": "Send one test alert to the owner's phone, to show that alerts arrive.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "show_plan",
        "description": "Show the owner your plan for a longer job as a checklist, and keep it current: call it again "
        "with the whole list each time a step starts or ends. Steps are in the owner's words, a few words each.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}, "state": {"enum": ["todo", "doing", "done"]}},
                        "required": ["text"],
                    },
                },
            },
            "required": ["steps"],
        },
    },
    {
        "name": "start_task",
        "description": "Ask the owner to approve a bigger job once. Until it ends, commands and file changes inside the "
        "guests you name (and setup runs, with setup_runs) go ahead without a card each; risky ones still ask, blocked "
        "ones stay blocked, and Proxmox itself and the platform's own guests are never covered. Not needed for "
        "add_service with build, which starts its own.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The job in the owner's words, such as Move Paperless to Postgres."},
                "guests": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
                "setup_runs": {"type": "boolean", "description": "Whether the job starts setup runs or checks."},
                "steps": {"type": "array", "items": {"type": "string"}, "description": "The plan, a few words a step."},
                "why": WHY,
            },
            "required": ["title", "guests"],
        },
    },
    {
        "name": "finish_task",
        "description": "Close the task once the job is done and you checked it. From then on changes ask again.",
        "inputSchema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "Two or three lines: what is done and what you checked."}},
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
        "docs/runbooks/custom-services.md first. With build=true the same approval also starts the setup run and "
        "lets you work inside that guest until it answers.",
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
                "build": {
                    "type": "boolean",
                    "description": "Set it up in one go: once the owner approves, the page saves it, starts the setup "
                    "run and tells you when that ends; then check_service.",
                },
                "why": WHY,
            },
            "required": ["name"],
        },
    },
    {
        "name": "check_service",
        "description": "Whether one of the owner's own services came up: its setup script ran as it is now, no unit "
        "failed, each port it declares listens (and not on 127.0.0.1 only), and the script's last output.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
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
        "name": "game_server",
        "description": "A game server as it runs, asked of the server now: whether it answers, how many play (and who, "
        "where the game says), the end of its log, and its worlds (Factorio: maps) by id, with which one runs. A server "
        "runs one world and keeps the others beside it.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The server's name, as what_to_run lists it."}},
            "required": ["name"],
        },
    },
    {
        "name": "game_world",
        "description": "Change a game server's worlds. new: the world that runs is set aside (kept, never deleted) and the "
        "server makes a new one with these options and runs it; this is how the owner gets, say, an expert Terraria "
        "world or another seed. use: run a kept world instead. rename. delete: a kept world, for good (risky, a typed "
        "word). The server stops for a moment; with players connected nothing changes until the owner agreed and kick is "
        "true. Uploading and downloading a world file is the owner's, on the Services page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The server's name."},
                "action": {"enum": ["new", "use", "rename", "delete"]},
                "id": {"type": "string", "description": "The world, by its id from game_server (use, rename, delete)."},
                "title": {"type": "string", "description": "The world's name (new, rename), 1 to 40 characters."},
                "options": {
                    "type": "object",
                    "description": "For new: what the game takes when it makes a world, by the names game_server lists under "
                    "new_world_takes, such as {difficulty: 1, autocreate: 3} for a large expert Terraria world.",
                },
                "kick": {"type": "boolean", "description": "Go ahead although players are connected, once the owner agreed."},
                "why": WHY,
            },
            "required": ["name", "action"],
        },
    },
    {
        "name": "game_mods",
        "description": "Which server runs a Minecraft game server, and its plugins or mods. server: vanilla (Mojang's own, "
        "takes neither), paper (plugins; players join with the ordinary game) or fabric (mods; some must also be in each "
        "player's own game, which the answer says). list: the kind and the list now, and whether the server runs it yet. "
        "search: Modrinth's plugins or mods for this server's kind and Minecraft, most used first for an empty query. "
        "add: one by its project name from search, in its newest version for this server, with what it needs. remove. "
        "check: which have a newer version; update takes them. Changes are saved to the choices; the server follows "
        "when setup's games stage runs (start_job apply, stage games), which restarts it. Changing the kind drops the "
        "list (a plugin is no mod), and a world Paper has opened opens only under Paper afterwards (leaving Paper sets it "
        "aside and starts a new world): the tool says what a change costs, which needs the owner's yes and yes=true. "
        "Only Modrinth, only for Minecraft.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The Minecraft server's name, as what_to_run lists it."},
                "action": {"enum": ["list", "search", "check", "add", "remove", "update", "server"]},
                "query": {"type": "string", "description": "With search: words to look for; empty lists the most used."},
                "project": {
                    "type": "string",
                    "description": "With add and remove: the project's name as search and list give it, such as lithium.",
                },
                "server": {"enum": ["vanilla", "paper", "fabric"], "description": "With server: the kind to run."},
                "yes": {"type": "boolean", "description": "With server: go ahead with what the change costs, once the owner agreed."},
                "why": WHY,
            },
            "required": ["name", "action"],
        },
    },
    {
        "name": "game_command",
        "description": "Restart a game server, or type one line on its console as its admin (such as say, kick, whitelist "
        "add; Factorio's begin with /). The answer to a console line is in the server's log, which comes back with it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The server's name."},
                "action": {"enum": ["restart", "console"]},
                "line": {"type": "string", "description": "With console: the line, at most 200 characters."},
                "why": WHY,
            },
            "required": ["name", "action"],
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
                "games": {
                    "type": "object",
                    "description": "Per game server by its name (minecraft, terraria, factorio, or a further server that "
                    "add_game_server made): settings (the server's own options, such as {difficulty: hard}; "
                    "docs/runbooks/games.md lists what NullBill sets itself), admins (player names), memory_mib, title "
                    "(a further server's) and, for the game itself, eula (true only when the owner said they agree; it "
                    "always waits for them).",
                },
                "power": {
                    "type": "object",
                    "description": "When a workload runs, by its name: run (always or on-demand), stop (auto, ask or "
                    "manual) and idle_minutes (5 to 10080).",
                },
                "torrents": {
                    "type": "object",
                    "description": "The download client (workload torrents): share (the kept disk downloads go to), folder, "
                    "keep_free_gib (downloads pause below it) and route (vpn or home: whose address others in a download "
                    "see, so it always waits for the owner; never choose it for them). The VPN's WireGuard file holds a "
                    "key and is pasted by the owner on the page, never given here.",
                },
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
