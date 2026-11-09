"""The page's agent: turns, its tools through the gate, approvals, questions."""

import json
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path

import pytest
import yaml
from test_web import FAKE_PROBE, Client, MemoryCodec, Sent, sign_in

from nullbill import agenttools
from nullbill.web import server as web

# Stands in for the agent CLI: reads the prompt, starts the MCP server its
# config names, and for each line `CALL <tool> <json>` calls that tool,
# printing what `claude -p --output-format stream-json` prints.
FAKE_AGENT = r"""
import json, subprocess, sys
args = sys.argv[1:]
config = json.load(open(args[args.index("--mcp-config") + 1]))["mcpServers"]["nullbill"]
prompt = sys.stdin.read()
out = lambda m: print(json.dumps(m), flush=True)
init = {"type": "system", "subtype": "init", "session_id": "s-1"}
out(init | {"mcp_servers": [{"name": "nullbill", "status": "connected"}]})
env = config["env"] | {"PATH": "/usr/bin:/bin"}
mcp = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
def rpc(n, method, params):
    mcp.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params}) + "\n")
    mcp.stdin.flush()
    return json.loads(mcp.stdout.readline())
rpc(0, "initialize", {"protocolVersion": "2025-06-18"})
n = 1
said = []
for line in prompt.splitlines():
    if line.startswith("CALL "):
        _, name, rest = line.split(" ", 2)
        n += 1
        use = {"type": "tool_use", "id": f"t{n}", "name": "mcp__nullbill__" + name, "input": json.loads(rest)}
        out({"type": "assistant", "message": {"content": [use]}})
        result = rpc(n, "tools/call", {"name": name, "arguments": json.loads(rest)})["result"]
        text = result["content"][0]["text"]
        done = {"type": "tool_result", "tool_use_id": f"t{n}", "content": text, "is_error": result["isError"]}
        out({"type": "user", "message": {"content": [done]}})
        said.append(text)
out({"type": "assistant", "message": {"content": [{"type": "text", "text": "heard: " + prompt[:200]}]}})
out({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": "s-1", "total_cost_usd": 0.01})
"""


class Host:
    """Stands in for SSH to the host: records each command, answers by it."""

    def __init__(self):
        self.ran = []

    def __call__(self, target, command):
        self.ran.append((target, command))
        if "pct list" in command:
            return 0, "VMID Status Name\n2002 running nullbill\nsk-ant-api03-SECRETVALUE123\n", ""
        if "dist-upgrade" in command and "apt-get -s" in command:
            return 0, "Inst openssl [3.0.1] (3.0.2 Debian)\nInst pve-kernel-6.8 (6.8.2)\nRemv oldlib [1.0]\n", ""
        if "FAILME" in command:
            return 1, "", "it broke\n"
        if "cat --" in command:
            return 0, "old line\nsame line\n", ""
        return 0, "done\n", ""


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    (home / "install.json").write_text(json.dumps({"host": "192.0.2.10", "node": "pve", "vmid": 2002}))
    probe = tmp_path / "fake_nullbill.py"
    probe.write_text(FAKE_PROBE.format(tests=str(Path(__file__).parent)))
    fake = tmp_path / "fake_agent.py"
    fake.write_text(FAKE_AGENT)
    codec = MemoryCodec()
    host = Host()
    app = web.App(
        home,
        command=[sys.executable, str(probe)],
        codec=lambda _: codec,
        send=Sent(),
        host_run=host,
        agent_command=[sys.executable, str(fake)],
        agent_user=None,
        agent_home=tmp_path / "agent-home",
        tools_socket=tmp_path / "s" / "tools.sock",
    )
    app.ensure_setup_code()
    tools = web.serve_tools(app)
    cert, key = web.ensure_certificate(home)
    server, _ = web.make_server(app, ("127.0.0.1", 0), cert, key)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    client = Client(server.server_address[1], cert)
    sign_in(app, client)
    yield app, client, host
    server.shutdown()
    tools.shutdown()


def settle(client, after=0):
    for _ in range(300):
        status, view = client.call(f"/api/chat?after={after}")
        assert status == 200
        if not view["busy"] and not view["queued"]:
            return view
        time.sleep(0.05)
    raise AssertionError("the turn did not end")


def kinds(view, kind):
    return [e for e in view["events"] if e["kind"] == kind]


def say(client, text):
    status, view = client.call("/api/chat", {"text": text})
    assert status == 200, view
    return settle(client)


def test_needs_a_credential(agent):
    _, client, _ = agent
    status, body = client.call("/api/chat", {"text": "hello"})
    assert status == 400 and "sign the agent in" in body["error"]
    assert client.call("/api/state")[1]["chat"]["signed_in"] is False


def test_sign_in_greets_and_reads_freely(agent):
    app, client, host = agent
    assert client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})[0] == 200
    view = settle(client)
    assert "signed you in" in kinds(view, "assistant")[0]["text"]
    view = say(client, 'CALL run_command {"command": "pct list", "why": "see guests"}')
    done = kinds(view, "tool_done")[-1]
    assert done["ok"] and "2002 running nullbill" in done["output"]
    # The credential is a secret the page knows: it never reaches the model.
    assert "SECRETVALUE" not in done["output"] and "[secret]" in done["output"]
    target, command = host.ran[-1]
    assert target == "root@192.0.2.10"
    assert command.startswith("logger --socket-errors=on -t nb-cmd -- 'page-agent on host: pct list'")
    assert [e["what"] if "what" in e else e["action"] for e in app.chat.audit_log()][:2] == ["ran", "Run on Proxmox"]
    # The token of a finished turn opens nothing.
    assert app.chat.token is None


def approve(client, proposal_id, **extra):
    return client.call("/api/chat/decide", {"id": proposal_id, "approve": True, **extra})


def test_manual_mode_asks_then_runs_what_was_approved(agent):
    app, client, host = agent
    client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})
    settle(client)
    view = say(client, 'CALL run_command {"command": "systemctl restart pveproxy", "why": "the UI hangs"}')
    proposal = kinds(view, "proposal")[-1]
    assert proposal["tier"] == "change" and proposal["command"] == "systemctl restart pveproxy" and proposal["why"] == "the UI hangs"
    assert "Waiting for the owner" in kinds(view, "tool_done")[-1]["output"]
    assert not any("restart" in c for _, c in host.ran)
    assert client.call("/api/state")[1]["chat"]["pending"] == 1
    assert approve(client, proposal["id"])[0] == 200
    view = settle(client)
    for _ in range(100):
        if any("restart pveproxy" in c for _, c in host.ran):
            break
        time.sleep(0.05)
    view = settle(client)
    assert any("systemctl restart pveproxy" in c for _, c in host.ran)
    assert [u["state"] for u in kinds(view, "proposal_update")][-1] == "done"
    assert 'approved "Run on Proxmox"' in kinds(view, "assistant")[-1]["text"]
    status, body = approve(client, proposal["id"])
    assert status == 400 and "already" in body["error"]


def test_risky_needs_the_word_and_blocked_never_runs(agent):
    app, client, host = agent
    client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})
    settle(client)
    assert client.call("/api/chat/settings", {"mode": "auto"})[1]["mode"] == "auto"
    view = say(client, 'CALL run_command {"command": "systemctl restart pveproxy"}\nCALL run_command {"command": "pct destroy 105"}')
    assert any("systemctl restart pveproxy" in c for _, c in host.ran)  # auto: a change runs
    proposal = kinds(view, "proposal")[-1]
    assert proposal["tier"] == "risky" and proposal["word"] == "105"
    status, body = approve(client, proposal["id"], typed="yes")
    assert status == 400 and "type 105" in body["error"]
    assert approve(client, proposal["id"], typed="105")[0] == 200
    settle(client)
    view = say(client, 'CALL run_command {"command": "wipefs -a /dev/sdb"}\nCALL run_command {"command": "ls", "where": "nullbill"}')
    refused = kinds(view, "refused")
    assert len(refused) == 2 and "erases" in refused[0]["why"]
    assert not any("wipefs" in c for _, c in host.ran)
    assert client.call("/api/chat/settings", {"mode": "yolo"})[0] == 200
    view = say(client, 'CALL run_command {"command": "cat /var/lib/nullbill/secrets.sops.yaml"}')
    assert "Refused by the page in every mode" in kinds(view, "tool_done")[-1]["output"]
    assert client.call("/api/chat/settings", {"mode": "root"})[0] == 400


def test_decline_and_questions_reach_the_agent(agent):
    _, client, _ = agent
    client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})
    settle(client)
    view = say(
        client,
        'CALL start_job {"action": "upgrade", "why": "a fix is out"}\n'
        'CALL ask_owner {"question": "Keep Jellyfin?", "options": ["Yes", "No"]}',
    )
    proposal = kinds(view, "proposal")[-1]
    ask = kinds(view, "ask")[-1]
    assert proposal["title"] == "Update NullBill" and ask["options"] == ["Yes", "No"]
    assert client.call("/api/chat/decide", {"id": proposal["id"], "approve": False, "note": "not now"})[0] == 200
    view = settle(client)
    assert 'declined "Update NullBill". They said: not now' in kinds(view, "assistant")[-1]["text"]
    assert client.call("/api/chat/answer", {"id": ask["id"], "text": "No"})[0] == 200
    view = settle(client)
    assert 'answered your question "Keep Jellyfin?": No' in kinds(view, "assistant")[-1]["text"]
    status, body = client.call("/api/chat/answer", {"id": ask["id"], "text": "Yes"})
    assert status == 400 and "already" in body["error"]


def test_read_jobs_start_at_once_and_failed_runs_are_looked_into(agent):
    app, client, _ = agent
    client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})
    settle(client)
    view = say(client, 'CALL start_job {"action": "probe"}')
    assert "started job" in kinds(view, "tool_done")[-1]["output"]
    assert not app.send  # reading the hardware is not announced
    job = kinds(view, "job")[-1]
    for _ in range(200):
        if app.jobs[job["id"]].state() != "running":
            break
        time.sleep(0.05)
    failing = app.start("apply", "host")  # the fake exits 3 for a stage
    for _ in range(200):
        if failing.state() != "running":
            break
        time.sleep(0.05)
    app.chat.notice_jobs()
    view = settle(client)
    assert any("stopped" in e["text"] and "looking into it" in e["text"] for e in kinds(view, "system"))
    assert f"The job {failing.id}" in kinds(view, "assistant")[-1]["text"]
    app.chat.notice_jobs()  # once only
    assert not app.chat.busy()


def test_new_conversation_and_stop(agent):
    app, client, _ = agent
    client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})
    settle(client)
    assert client.call("/api/chat/stop", {})[0] == 400
    assert client.call("/api/chat/new", {})[0] == 200
    view = settle(client)
    assert [e["kind"] for e in view["events"]] == ["system"]
    assert json.loads((app.home / "agent" / "session.json").read_text()) == {}


def test_tool_socket_needs_the_turns_token(agent):
    app, _, _ = agent
    conn = agenttools.UnixConnection(str(app.tools_socket))
    conn.request("POST", "/call", body=json.dumps({"name": "status", "args": {}}), headers={"Authorization": "Bearer nope"})
    assert conn.getresponse().status == 401


def test_tool_socket_is_open_to_the_agents_group_under_the_pages_umask(tmp_path):
    # The page runs with UMask=0077; mkdir(mode=0o750) under it made the
    # directory 0700 and nbagent could not reach the socket.
    app = types.SimpleNamespace(tools_socket=tmp_path / "run" / "tools.sock", agent_user=None)
    old = os.umask(0o077)
    try:
        server = web.serve_tools(app)
    finally:
        os.umask(old)
    try:
        assert stat.S_IMODE(app.tools_socket.parent.stat().st_mode) == 0o750
        assert stat.S_IMODE(app.tools_socket.stat().st_mode) == 0o660
    finally:
        server.shutdown()


def test_mcp_server_lists_and_refuses_unknown_methods():
    reply = agenttools.answer({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in reply["result"]["tools"]}
    assert {"status", "run_command", "start_job", "ask_owner", "read_job"} <= names
    assert agenttools.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert agenttools.answer({"jsonrpc": "2.0", "id": 2, "method": "nope"})["error"]["code"] == -32601


def signed_in(agent, mode="manual"):
    app, client, host = agent
    client.call("/api/agent", {"credential": "sk-ant-api03-SECRETVALUE123"})
    settle(client)
    if mode != "manual":
        assert client.call("/api/chat/settings", {"mode": mode})[0] == 200
    return app, client, host


def test_every_tool_has_a_handler_and_the_stages_match():
    from nullbill.web.chat import Tools

    names = {t["name"] for t in agenttools.TOOLS}
    assert names == {n[2:] for n in dir(Tools) if n.startswith("t_")}
    assert agenttools.STAGES == web.APPLY_STAGES


def test_steps_are_one_card_and_stop_at_the_first_failure(agent):
    app, client, host = signed_in(agent, "auto")
    steps = [
        {"command": "systemctl restart pveproxy"},
        {"command": "echo FAILME"},
        {"command": "pct destroy 105"},
        {"command": "systemctl restart pvedaemon"},
    ]
    view = say(client, "CALL run_steps " + json.dumps({"steps": steps, "why": "fix the UI"}))
    proposal = kinds(view, "proposal")[-1]
    # Auto runs changes, but the plan holds a risky step: the whole plan waits, with its word.
    assert proposal["act"] == "steps" and proposal["tier"] == "risky" and proposal["word"] == "105"
    assert [s["tier"] for s in proposal["steps"]] == ["change", "read", "risky", "change"]
    assert not any("restart pveproxy" in c for _, c in host.ran)
    assert approve(client, proposal["id"], typed="105")[0] == 200
    for _ in range(100):
        if [u["state"] for u in kinds(settle(client), "proposal_update")][-1:] in (["done"], ["failed"]):
            break
        time.sleep(0.05)
    ran = [c for _, c in host.ran]
    assert any("restart pveproxy" in c for c in ran) and any("FAILME" in c for c in ran)
    assert not any("pct destroy" in c for c in ran) and not any("pvedaemon" in c for c in ran)


def test_write_file_shows_a_diff_and_keeps_the_old_file(agent):
    app, client, host = signed_in(agent)
    view = say(client, "CALL write_file " + json.dumps({"path": "/etc/default/grub", "content": "new line\nsame line\n"}))
    proposal = kinds(view, "proposal")[-1]
    assert proposal["act"] == "write" and proposal["tier"] == "change"
    assert "-old line" in proposal["diff"] and "+new line" in proposal["diff"]
    assert "content" not in proposal  # the card shows the diff, not the file
    assert approve(client, proposal["id"])[0] == 200
    for _ in range(100):
        if any("base64 -d" in c for _, c in host.ran):
            break
        time.sleep(0.05)
    script = next(c for _, c in host.ran if "base64 -d" in c)
    assert "/var/lib/nullbill-agent-backup" in script and "/etc/default/grub.nb-new" in script
    view = say(client, "CALL write_file " + json.dumps({"path": "/etc/network/interfaces", "content": "x"}))
    assert kinds(view, "proposal")[-1]["tier"] == "risky" and kinds(view, "proposal")[-1]["word"] == "write"
    view = say(client, "CALL write_file " + json.dumps({"path": "/root/.ssh/authorized_keys", "content": "x"}))
    assert kinds(view, "refused")


def test_background_runs_and_what_ran_unasked_is_shown(agent):
    app, client, host = signed_in(agent, "auto")
    view = say(client, 'CALL run_command {"command": "apt-get install -y htop", "background": true}')
    output = kinds(view, "tool_done")[-1]["output"]
    run_id = output.split("background as ")[1][:8]
    assert any("systemd-run --unit nb-agent-" + run_id in c for _, c in host.ran)
    ran = kinds(view, "ran")[-1]
    assert ran["tier"] == "change" and ran["mode"] == "auto" and ran["command"] == "apt-get install -y htop"
    view = say(client, "CALL read_run " + json.dumps({"id": run_id}))
    assert kinds(view, "tool_done")[-1]["ok"]
    assert f"/var/log/nb-agent/{run_id}.log" in host.ran[-1][1]


def test_updates_are_simulated_first(agent):
    app, client, host = signed_in(agent, "auto")
    view = say(client, "CALL update_packages {}")
    proposal = kinds(view, "proposal")[-1]
    # It removes a package, so even Auto waits, and the card says what goes.
    assert proposal["tier"] == "risky" and proposal["background"]
    assert any("Remove: oldlib" in line for line in proposal["lines"])
    assert any("restart" in line for line in proposal["lines"])
    assert not any("apt-get -y" in c for _, c in host.ran)


def test_notes_and_questions_answered_in_the_chat(agent):
    app, client, host = signed_in(agent)
    view = say(client, 'CALL remember {"fact": "The owner reads the page from a phone."}\nCALL ask_owner {"question": "Keep it?"}')
    assert "phone" in client.call("/api/chat/notes")[1]["notes"]
    assert client.call("/api/chat/notes", {"notes": "- Mine now.\n"})[1]["notes"] == "- Mine now.\n"
    assert app.chat.notes() == "- Mine now.\n"
    ask = kinds(view, "ask")[-1]
    view = say(client, "never mind")
    answers = [e for e in kinds(view, "answer") if e["id"] == ask["id"]]
    assert answers and answers[-1]["text"] == "(answered in the chat)"


def test_a_turn_cut_off_by_a_restart_ends_with_a_note(agent):
    app, client, host = signed_in(agent)
    app.chat.emit("turn", state="start")
    again = type(app.chat)(app)
    last = [e for e in again.events(0) if e["kind"] == "turn"][-1]
    assert last["state"] == "end" and "carry on" in last["error"]


def test_the_backup_server_cannot_be_destroyed(agent, tmp_path):
    app, client, host = signed_in(agent, "yolo")
    (app.home / "plan.yaml").write_text("guests:\n  pbs: {kind: vm, vmid: 140, zone: backup}\n")
    view = say(client, 'CALL run_command {"command": "qm destroy 140 --purge"}')
    assert "backup server" in kinds(view, "refused")[-1]["why"]


def test_status_does_not_call_planned_guests_not_installed(agent, monkeypatch):
    # The plan's `installed` is "made by install.sh"; as "installed: false"
    # the agent told Jan most running guests were not installed.
    app, _, _ = agent
    plan = yaml.safe_load((Path(__file__).parent / "golden" / "small.plan.yaml").read_text())
    state = app.state
    monkeypatch.setattr(app, "state", lambda *a: state(*a) | {"plan": {"text": "x", "errors": [], "summary": web.plan_summary(plan)}})
    text, failed = app.tools.call("status", {})
    assert not failed
    guests = {g["name"]: g for g in yaml.safe_load(text)["guests"]}
    assert guests and all("installed" not in g for g in guests.values())
    assert guests["nullbill"].get("made_by_install_sh") is True
    assert sum(1 for g in guests.values() if g.get("made_by_install_sh")) == 1
