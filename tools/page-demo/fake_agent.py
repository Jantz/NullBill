"""Stands in for the agent CLI the page starts per turn (web/chat.py). It
reads the owner's message, starts the MCP server its config names, and
prints what `claude -p --output-format stream-json` prints.

A line `CALL <tool> <json>` in the message calls that tool through the
page's gate, so a request card can be made on purpose, for example
`CALL run_command {"command": "pct list", "why": "see what runs"}`.
Anything else gets a short answer.
"""

import json
import subprocess
import sys


def out(message: dict) -> None:
    print(json.dumps(message), flush=True)


def main() -> int:
    args = sys.argv[1:]
    config = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["nullbill"]
    content = json.loads(sys.stdin.readline())["message"]["content"]
    prompt = "\n".join(block["text"] for block in content if block["type"] == "text")
    model = args[args.index("--model") + 1] if "--model" in args else "opus"
    init = {"type": "system", "subtype": "init", "session_id": "demo", "model": f"claude-{model}-5-5"}
    out(init | {"mcp_servers": [{"name": "nullbill", "status": "connected"}]})
    env = config["env"] | {"PATH": "/usr/bin:/bin"}
    mcp = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)

    def rpc(n: int, method: str, params: dict) -> dict:
        mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\n")
        mcp.stdin.flush()
        return json.loads(mcp.stdout.readline())

    rpc(0, "initialize", {"protocolVersion": "2025-06-18"})
    calls = [line.split(" ", 2) for line in prompt.splitlines() if line.startswith("CALL ")]
    for n, (_, name, rest) in enumerate(calls, start=1):
        use = {"type": "tool_use", "id": f"t{n}", "name": "mcp__nullbill__" + name, "input": json.loads(rest)}
        out({"type": "assistant", "message": {"content": [use]}})
        result = rpc(n, "tools/call", {"name": name, "arguments": json.loads(rest)})["result"]
        done = {"type": "tool_result", "tool_use_id": f"t{n}", "content": result["content"][0]["text"], "is_error": result["isError"]}
        out({"type": "user", "message": {"content": [done]}})
    text = "Done." if calls else "This is the stand-in agent of the page demo. It answers nothing real; `CALL <tool> <json>` calls a tool."
    out({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "demo", "total_cost_usd": 0.0})
    return 0


sys.exit(main())
