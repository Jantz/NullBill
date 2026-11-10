"""The page's agent across the platform: its model and thinking level,
pictures, a task approved once, a service set up in one go, and the tools
for the page's other features."""

import base64
import http.client
import json
import struct
import time
import zlib

import pytest
import test_chat
import yaml
from test_chat import approve, kinds, say, settle, signed_in
from test_web import OWNER, probed

from nullbill import direct
from nullbill.web import chat as chat_module

agent = test_chat.agent

SCRIPT = "#!/bin/bash\nset -eu\napt-get install -y nginx\nsystemctl enable --now nginx\n"
PAPERLESS = {"title": "Paperless", "what": "Files your documents.", "ports": [{"port": 8000, "from": ["role:owner", "shared"]}]}


def png(width=2, height=2) -> bytes:
    raw = b"".join(b"\x00" + b"\x20\x40\x80" * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def picture(data: bytes = b"", kind="image/png") -> dict:
    return {"type": kind, "data": base64.b64encode(data or png()).decode()}


def chosen(agent, mode="manual"):
    """Signed in, hardware read, choices saved: a plan with guests."""
    app, client, host = signed_in(agent, mode)
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200
    assert app.plan()["errors"] == []
    return app, client, host


def saved(app) -> dict:
    return yaml.safe_load((app.home / "config.yaml").read_text())


def call(client, tool, **args):
    return say(client, f"CALL {tool} " + json.dumps(args))


def result(view) -> str:
    return kinds(view, "tool_done")[-1]["output"]


def approved(client, proposal_id, typed=""):
    """Approve a request and wait until the page carried it out and the
    agent's turn about the result ended: the save comes first, the turn a
    moment later, so waiting for the save alone reads the chat too early."""
    assert approve(client, proposal_id, typed=typed)[0] == 200

    def done():
        events = client.call("/api/chat")[1]["events"]
        at = [n for n, e in enumerate(events) if e["kind"] == "proposal_update" and e["id"] == proposal_id and e["state"] != "running"]
        return at and any(e["kind"] == "turn" and e["state"] == "end" for e in events[at[-1] :])

    wait_for(done, "the approved request and the agent's turn about it")
    return settle(client)


def wait_for(done, what="it"):
    for _ in range(200):
        if done():
            return
        time.sleep(0.05)
    raise AssertionError(f"{what} did not happen")


# The model and how long it thinks.
def test_the_owner_picks_the_model_and_the_thinking_level(agent):
    app, client, _ = signed_in(agent)
    state = client.call("/api/state")[1]["chat"]
    assert (state["model"], state["effort"]) == ("default", "default")
    view = say(client, "EFFORT")
    # Default passes neither flag: the CLI and the account decide.
    assert "effort unset" in kinds(view, "assistant")[-1]["text"]
    status, settings = client.call("/api/chat/settings", {"model": "sonnet", "effort": "high"})
    assert status == 200 and (settings["model"], settings["effort"], settings["mode"]) == ("sonnet", "high", "manual")
    view = say(client, "EFFORT")
    assert "effort high" in kinds(view, "assistant")[-1]["text"]
    # The turn says which model answered, by the name the CLI reported, and how hard it thought.
    ended = [e for e in kinds(view, "turn") if e["state"] == "end"]
    assert (ended[-1]["model"], ended[-1]["effort"]) == ("Sonnet 5.5", "high")
    assert ended[0]["effort"] is None
    assert client.call("/api/chat")[1]["model"] == "sonnet"
    for bad in ({"model": "gpt"}, {"effort": "extreme"}):
        status, body = client.call("/api/chat/settings", bad)
        assert status == 400 and "is one of" in body["error"]
    assert app.chat.settings()["model"] == "sonnet"
    assert app.chat.audit_log()[0]["model"] == "sonnet"


@pytest.mark.parametrize(
    ("model_id", "name"),
    [
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-fable-5-1", "Fable 5.1"),
        ("claude-haiku-5-5-20261001", "Haiku 5.5"),
        ("claude-sonnet-5", "Sonnet 5"),
        ("claude-opus-5-5[1m]", "Opus 5.5"),
        ("local", "local"),
    ],
)
def test_a_model_id_reads_as_a_name(model_id, name):
    assert chat_module.model_name(model_id) == name


# Pictures.
def test_a_picture_reaches_the_agent_and_stays_for_the_page(agent):
    app, client, _ = signed_in(agent)
    status, view = client.call("/api/chat", {"text": "What is this?", "images": [picture(), picture()]})
    assert status == 200
    view = settle(client)
    user = kinds(view, "user")[-1]
    assert user["text"] == "What is this?" and [i["type"] for i in user["images"]] == ["image/png", "image/png"]
    assert "pictures: image/png, image/png" in kinds(view, "assistant")[-1]["text"]
    # The page shows it from the server, to the signed-in owner only, as the type its bytes are.
    conn = http.client.HTTPSConnection("127.0.0.1", client.port, context=client.tls, timeout=30)
    path = f"/api/chat/image?id={user['images'][0]['id']}"
    conn.request("GET", path, headers={"Cookie": client.cookie})
    response = conn.getresponse()
    assert response.status == 200 and response.getheader("Content-Type") == "image/png" and response.read() == png()
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    conn.request("GET", path)
    response = conn.getresponse()
    assert response.status == 401 and response.read()
    conn.close()
    assert client.call("/api/chat/image?id=../../secrets")[0] == 404
    assert client.call("/api/chat/image?id=0123456789abcdef")[0] == 404
    # A picture alone is a message too.
    assert client.call("/api/chat", {"text": "", "images": [picture()]})[0] == 200
    assert "attached this without words" in kinds(settle(client), "assistant")[-1]["text"]
    assert "[2 pictures attached]" in app.chat.transcript()


def test_only_real_pictures_of_a_sane_size_are_taken(agent, monkeypatch):
    app, client, _ = signed_in(agent)

    def refused(images):
        status, body = client.call("/api/chat", {"text": "look", "images": images})
        assert status == 400
        return body["error"]

    assert "at most 4 pictures" in refused([picture()] * 5)
    assert "not a PNG, JPEG, GIF or WebP" in refused([picture(kind="image/svg+xml")])
    assert "not the PNG image it says it is" in refused([picture(b"<html><script>alert(1)</script>")])
    assert "not the WEBP image" in refused([picture(b"RIFF\x00\x00\x00\x00WAVE", "image/webp")])
    assert "did not arrive whole" in refused([{"type": "image/png", "data": "not base64!"}])
    monkeypatch.setattr(chat_module, "MAX_IMAGE_BYTES", 50)
    assert "larger than" in refused([picture()])
    # Nothing of a refused message is kept or shown.
    assert not (app.chat.dir / "images").exists() and not [e for e in app.chat.events() if e["kind"] == "user" and e["text"] == "look"]


def test_a_large_body_is_read_only_from_the_signed_in_owner_for_the_chat(agent):
    app, client, _ = signed_in(agent)
    big = {"text": "x", "images": [picture(png(600, 600) + b"\x00" * 200_000)]}
    assert len(json.dumps(big)) > 64 * 1024
    assert client.call("/api/chat", big)[0] == 200
    settle(client)
    assert client.call("/api/chat/notes", {"notes": "n" * 70_000})[0] == 413
    cookie, client.cookie = client.cookie, None
    assert client.call("/api/chat", big)[0] == 413
    client.cookie = cookie


def test_kept_pictures_are_pruned(agent, monkeypatch):
    app, _, _ = signed_in(agent)
    monkeypatch.setattr(chat_module, "KEPT_IMAGES", 3)
    for _ in range(5):
        app.chat.keep_images([picture()])
    assert len(list((app.chat.dir / "images").iterdir())) == 3


# A plan the owner follows.
def test_the_plan_is_a_checklist_and_not_a_line_per_update(agent):
    app, client, _ = signed_in(agent)
    steps = [{"text": "Read the log", "state": "done"}, {"text": "Fix   the unit", "state": "doing"}, "Check it answers"]
    view = call(client, "show_plan", steps=steps)
    plan = kinds(view, "plan")[-1]["steps"]
    assert plan == [
        {"text": "Read the log", "state": "done"},
        {"text": "Fix the unit", "state": "doing"},
        {"text": "Check it answers", "state": "todo"},
    ]
    assert not [e for e in kinds(view, "tool") if e["name"] == "show_plan"] and not kinds(view, "tool_done")
    # A plan that is wrong is said, as any failed tool is.
    view = call(client, "show_plan", steps=[{"text": "x", "state": "soon"}])
    assert kinds(view, "tool")[-1]["name"] == "show_plan" and "todo, doing or done" in result(view)
    assert "[x] Read the log" in app.chat.transcript() and "[>] Fix the unit" in app.chat.transcript()


# A task approved once.
def test_a_task_lets_changes_inside_its_guests_run_and_nothing_else(agent):
    app, client, host = chosen(agent)
    view = call(client, "start_task", title="Tidy the workspace", guests=["workspace"], steps=["Look", "Clean"], why="you asked")
    proposal = kinds(view, "proposal")[-1]
    assert proposal["act"] == "task" and proposal["tier"] == "change" and proposal["title"] == "Task: Tidy the workspace"
    assert proposal["plan"] == ["Look", "Clean"] and "inside workspace" in proposal["lines"][0]
    assert client.call("/api/chat")[1]["task"] is None
    # Without the task a change inside the guest waits, as Manual says.
    view = call(client, "run_command", command="systemctl restart ssh", where="workspace")
    waiting = kinds(view, "proposal")[-1]
    assert waiting["command"] == "systemctl restart ssh"
    view = approved(client, proposal["id"])
    task = client.call("/api/chat")[1]["task"]
    assert task["title"] == "Tidy the workspace" and task["guests"] == ["workspace"] and task["jobs"] is False
    assert kinds(view, "plan")[-1]["steps"] == [{"text": "Look", "state": "todo"}, {"text": "Clean", "state": "todo"}]
    assert kinds(view, "task")[-1]["state"] == "start"

    before = len(kinds(view, "proposal"))
    view = call(client, "run_command", command="systemctl restart cron", where="workspace")
    ran = kinds(view, "ran")[-1]
    assert ran["task"] == "Tidy the workspace" and ran["command"] == "systemctl restart cron" and ran["mode"] == "manual"
    assert any("restart cron" in c for _, c in host.ran) and len(kinds(view, "proposal")) == before
    assert next(e for e in app.chat.audit_log() if e.get("action") == "run")["task"] == task["id"]
    # On Proxmox, in another guest, for a setup run it did not name, and for anything risky, it asks as before.
    for args in (
        {"command": "systemctl restart pveproxy"},
        {"command": "systemctl restart forgejo", "where": "forgejo"},
        {"command": "reboot", "where": "workspace"},
    ):
        view = call(client, "run_command", **args)
        assert kinds(view, "proposal")[-1]["command"] == args["command"] and "Waiting for the owner" in result(view)
    assert kinds(call(client, "start_job", action="apply"), "proposal")[-1]["act"] == "job"
    assert kinds(view, "proposal")[-1]["tier"] == "risky"
    view = call(client, "run_command", command="wipefs -a /dev/sdb", where="workspace")
    assert kinds(view, "refused")
    ran = [c for _, c in host.ran]
    assert not any("pveproxy" in c or "restart forgejo" in c or "reboot" in c or "wipefs" in c for c in ran)

    view = call(client, "finish_task", summary="Cleaned **two** units.")
    ended = kinds(view, "task")[-1]
    assert (ended["state"], ended["by"], ended["text"]) == ("end", "agent", "Cleaned **two** units.")
    assert client.call("/api/chat")[1]["task"] is None
    view = call(client, "run_command", command="systemctl restart cron", where="workspace")
    assert "Waiting for the owner" in result(view)
    assert "No task is on" in result(call(client, "finish_task"))


def test_a_task_never_covers_proxmox_or_the_platforms_own_guests(agent):
    app, client, _ = chosen(agent, "auto")
    for guest, said in (
        ("host", "a task covers guests only"),
        ("nullbill", "a task covers guests only"),
        ("router", "part of the platform itself"),
        ("step-ca", "part of the platform itself"),
        ("nope", "no guest 'nope'"),
    ):
        view = call(client, "start_task", title="X", guests=[guest])
        assert said in result(view) and client.call("/api/chat")[1]["task"] is None
    # In Auto a task needs no card: changes run anyway, and the owner still sees it and can end it.
    view = call(client, "start_task", title="Tune the workspace", guests=["workspace"], setup_runs=True)
    assert not kinds(view, "proposal") and "The task is on" in result(view)
    assert client.call("/api/chat")[1]["task"]["jobs"] is True
    assert client.call("/api/chat/task", {})[0] == 200
    view = settle(client)
    assert (kinds(view, "task")[-1]["state"], kinds(view, "task")[-1]["by"]) == ("end", "owner")
    assert client.call("/api/chat/task", {})[0] == 400


def test_a_task_ends_by_itself_and_with_a_new_conversation(agent, monkeypatch):
    app, client, _ = chosen(agent)
    app.chat.start_task("Long job", ["workspace"], True, "owner")
    assert app.chat.task()["title"] == "Long job"
    assert yaml.safe_load(app.tools.call("status", {})[0])["task"]["guests"] == ["workspace"]
    real = chat_module.now
    monkeypatch.setattr(chat_module, "now", lambda: real() + chat_module.TASK_TTL_S + 1)
    assert app.chat.task() is None
    monkeypatch.setattr(chat_module, "now", real)
    app.chat.start_task("Another", ["workspace"], False, "owner")
    assert client.call("/api/chat/new", {})[0] == 200
    assert app.chat.task() is None


# A service of the owner's own, in one go.
def test_one_approval_saves_a_service_builds_it_and_lets_the_agent_finish(agent):
    app, client, host = chosen(agent)
    view = call(client, "add_service", name="paperless", setup=SCRIPT, build=True, why="to keep documents", **PAPERLESS)
    proposal = kinds(view, "proposal")[-1]
    assert proposal["tier"] == "risky" and proposal["word"] == "add" and proposal["build"] is True
    assert "Starts a setup run at once" in proposal["then"][0] and "inside paperless" in proposal["then"][1]
    assert "paperless" not in saved(app).get("services", {}) and not app.jobs
    assert approve(client, proposal["id"], typed="add")[0] == 200
    wait_for(lambda: app.jobs, "the setup run")
    job = next(iter(app.jobs.values()))
    assert job.action == "apply" and "--stage" not in job.args
    assert (app.home / "custom" / "paperless" / "setup.sh").read_text() == SCRIPT
    wait_for(lambda: job.state() == "done", "the run's end")
    view = settle(client)
    assert client.call("/api/chat")[1]["task"] == app.chat.task_view()
    task = app.chat.task()
    assert task["title"] == "Set up Paperless" and task["guests"] == ["paperless"] and task["jobs"] is True
    assert kinds(view, "job")[-1]["title"] == "Run setup for Paperless"
    done = kinds(view, "proposal_update")[-1]
    # The card says what happened in the owner's words; the advice on what to do next is the agent's alone.
    assert done["state"] == "done" and "output" not in done
    assert done["says"] == "Saved Paperless. The setup run has started. The agent goes on when it ends."
    # When the run ends the agent is told, with what to do next.
    app.chat.notice_jobs()
    view = settle(client)
    heard = kinds(view, "assistant")[-1]["text"]
    assert f"The run {job.id} you are watching finished. Now call check_service paperless" in heard

    # Inside its guest it works without a card, and may correct its own script; the owner sees what changed.
    view = call(client, "run_command", command="systemctl restart nginx", where="paperless")
    assert kinds(view, "ran")[-1]["task"] == "Set up Paperless"
    fixed = SCRIPT + "systemctl restart nginx\n"
    before = len(kinds(view, "proposal"))
    view = call(client, "add_service", name="paperless", setup=fixed)
    ran = kinds(view, "ran")[-1]
    assert ran["task"] == "Set up Paperless" and "+systemctl restart nginx" in ran["diff"]
    # Only what moved, not the whole service again.
    assert ran["lines"] == [f"Setup script: {len(fixed.splitlines())} lines, {len(SCRIPT.splitlines())} before"]
    assert (app.home / "custom" / "paperless" / "setup.sh").read_text() == fixed and len(kinds(view, "proposal")) == before
    view = call(client, "start_job", action="apply", stage="custom")
    # A run says itself that it started; no second line for it.
    assert kinds(view, "job")[-1]["title"] == "Set up the server, stage custom" and len(kinds(view, "proposal")) == before
    assert kinds(view, "ran")[-1]["title"] == "Change your own service Paperless"
    # What opens the server further still waits for the typed word.
    view = call(client, "add_service", name="paperless", ports=[{"port": 8000}, {"port": 8443}])
    assert (kinds(view, "proposal")[-1]["tier"], kinds(view, "proposal")[-1]["word"]) == ("risky", "change")
    view = call(client, "add_service", name="paperless", proxy_allow=["example.org"])
    assert kinds(view, "proposal")[-1]["tier"] == "risky"
    # And a second service is read by the owner like the first.
    view = call(client, "add_service", name="wiki", title="Wiki", setup=SCRIPT, build=True)
    assert kinds(view, "proposal")[-1]["word"] == "add"


def test_a_build_waits_for_a_run_that_is_going_and_says_why_one_did_not_start(agent, monkeypatch):
    app, client, _ = chosen(agent, "yolo")
    running = app.start("probe")
    monkeypatch.setattr(type(running), "state", lambda self: "running" if self.id == running.id else "done")
    view = call(client, "add_service", name="paperless", setup=SCRIPT, build=True, **PAPERLESS)
    said = result(view)
    assert f"another run ({running.id}) is still going" in said and running.id in app.chat.watched
    assert "start_job apply" in app.chat.then[running.id]
    assert "paperless" in saved(app)["services"] and app.chat.task()["guests"] == ["paperless"]


def test_check_service_says_what_is_left_to_fix(agent):
    app, client, host = chosen(agent, "yolo")
    call(client, "add_service", name="paperless", setup=SCRIPT, **PAPERLESS)
    assert "paperless" in saved(app)["services"]
    from nullbill import custom

    answers = {"mark": "", "failed": "", "ports": ["tcp 127.0.0.1:8000"], "log": "E: Unable to locate package nginx"}

    def guest(target, command):
        host.ran.append((target, command))
        if "check paperless" not in command:
            return 0, "done\n", ""
        lines = [f"mark {answers['mark']}", f"failed {answers['failed']}", *answers["ports"], "---", answers["log"]]
        return 0, "\n".join(lines) + "\n", ""

    app.host_run = guest
    said = result(call(client, "check_service", name="paperless"))
    assert said.splitlines()[0] == "paperless: 2 things to fix"
    assert "setup script: NOT done (failed or has not run yet)" in said
    assert "tcp/8000: listens on 127.0.0.1 only, so nobody reaches it" in said
    assert "E: Unable to locate package nginx" in said
    # It ran inside the guest, read-only, logged like every command.
    target, command = host.ran[-1]
    assert "page-agent on paperless: check paperless" in command and "pct exec" in command

    answers |= {"mark": custom.digest(SCRIPT), "failed": "paperless.service", "ports": ["tcp 0.0.0.0:8000", "tcp [::]:8000"]}
    said = result(call(client, "check_service", name="paperless"))
    assert "paperless: 1 thing to fix" in said and "failed units: paperless.service" in said and "tcp/8000: listening" in said
    answers |= {"failed": "", "ports": ["tcp *:8000"]}
    said = result(call(client, "check_service", name="paperless"))
    assert said.splitlines()[:4] == [
        "paperless: all good",
        "setup script: ran to its end as it is now",
        "failed units: none",
        "tcp/8000: listening",
    ]
    answers |= {"ports": []}
    assert "tcp/8000: NOTHING LISTENS" in result(call(client, "check_service", name="paperless"))
    # A guest setup has not made yet is said, not probed.
    (app.home / "guests.json").write_text(json.dumps({"guests": ["agents"]}))
    assert "not on the server yet" in result(call(client, "check_service", name="paperless"))
    assert "no service of your own" in result(call(client, "check_service", name="forgejo"))


# The page's other features.
def test_game_settings_and_when_things_run_are_choices_and_an_agreement_is_the_owners(agent):
    app, client, _ = chosen(agent, "auto")
    workloads = [*OWNER["workloads"], "minecraft"]
    view = call(client, "set_choices", workloads=workloads, games={"minecraft": {"eula": True, "admins": "alex, sam"}})
    proposal = kinds(view, "proposal")[-1]
    # Auto runs changes; agreeing to a licence in the owner's name waits for them in every mode.
    assert (proposal["tier"], proposal["word"], proposal["title"]) == ("risky", "agree", "Change what to run")
    assert proposal["lines"] == [
        "Add minecraft",
        "Minecraft: admins: alex, sam (was the default)",
        "Minecraft: you agree to its licence (EULA)",
    ]
    assert "games" not in saved(app)
    approved(client, proposal["id"], typed="agree")
    assert saved(app)["games"]["minecraft"] == {"eula": True, "admins": ["alex", "sam"]}
    # Once agreed, other settings are a change like any other.
    view = call(client, "set_choices", games={"minecraft": {"admins": "alex"}}, power={"agents": {"run": "on-demand", "stop": "auto"}})
    ran = kinds(view, "ran")[-1]
    assert ran["title"] == "Change what to run"
    assert "Minecraft: admins: alex (was alex, sam)" in ran["lines"]
    assert "When agents runs: run: on-demand (was the default)" in ran["lines"]
    config = saved(app)
    assert config["games"]["minecraft"]["admins"] == ["alex"] and config["power"]["agents"]["run"] == "on-demand"
    assert config["intent"]["workloads"] == workloads
    # A server's own options and its memory, each as a line of the card.
    view = call(client, "set_choices", games={"minecraft": {"settings": {"difficulty": "hard"}, "memory_mib": 6144}})
    assert {"Minecraft: settings difficulty: hard (was the default)", "Minecraft: memory in MiB: 6144 (was the default)"} <= set(
        kinds(view, "ran")[-1]["lines"]
    )
    config = saved(app)
    assert config["games"]["minecraft"]["settings"] == {"difficulty": "hard"} and config["overrides"]["minecraft"]["memory_mib"] == 6144
    # What the form refuses is said before any card.
    view = call(client, "set_choices", power={"agents": {"idle_minutes": 1}})
    assert "minutes from 5 to 10080" in result(view) and not [p for p in kinds(view, "proposal") if p["state"] == "pending"][1:]
    assert "disks are the owner's" in result(call(client, "set_choices", disks={}))


def powered(app, monkeypatch, groups):
    acted = []
    monkeypatch.setattr(app, "on_demand", lambda fresh=False: {"groups": groups})

    def act(body, by="the page"):
        acted.append((body, by))
        if body.get("name") == "inference" and body["action"] == "start" and not body.get("stop"):
            return {"ok": False, "no_room": {"error": "no-room", "needs_mib": 9000, "could_stop": ["media"]}, "title": "Local AI"}
        if body["action"] == "stop" and not body.get("force"):
            return {"ok": False, "busy": "2 people watching", "title": "Jellyfin"}
        return {"ok": True, "title": "Jellyfin", "stopped": ["media"]}

    monkeypatch.setattr(app, "power_act", act)
    return acted


def test_on_demand_starts_and_stops_through_the_arbiter_and_asks_before_cutting_anyone_off(agent, monkeypatch):
    app, client, _ = chosen(agent, "auto")
    groups = [
        {"name": "media", "title": "Jellyfin", "state": "on", "guests": [{"name": "media"}]},
        {"name": "inference", "title": "Local AI", "state": "off", "guests": [{"name": "inference"}]},
    ]
    acted = powered(app, monkeypatch, groups)
    assert "nope does not run on demand; what does: inference, media" in result(call(client, "on_demand", action="start", name="nope"))
    view = call(client, "on_demand", action="start", name="inference")
    assert kinds(view, "ran")[-1]["title"] == "Start Local AI" and acted[-1] == ({"action": "start", "name": "inference"}, "the agent")
    said = result(view)
    assert "does not fit beside what is on, so nothing changed" in said and "could_stop" in said and "ask_owner" in said
    # Stopping others to make room, or something in use, is the owner's call, with a typed word.
    view = call(client, "on_demand", action="start", name="inference", stop=["media"])
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["word"]) == ("risky", "stop") and proposal["lines"] == ["Stops first, to make room: Jellyfin"]
    view = call(client, "on_demand", action="stop", name="media")
    assert "is in use (2 people watching), so it was not stopped" in result(view)
    view = call(client, "on_demand", action="stop", name="media", force=True)
    assert kinds(view, "proposal")[-1]["word"] == "stop" and kinds(view, "proposal")[-1]["title"] == "Stop Jellyfin"
    view = call(client, "on_demand", action="keep", name="media", on=True)
    assert kinds(view, "ran")[-1]["title"] == "Keep on Jellyfin" and acted[-1][0] == {"action": "keep", "name": "media", "on": True}
    assert "keep takes on" in result(call(client, "on_demand", action="keep", name="media"))


def test_giving_a_person_a_service_takes_a_typed_word_and_says_what_it_means(agent):
    app, client, _ = chosen(agent, "auto")
    listed = yaml.safe_load(result(call(client, "people")))
    assert [p["name"] for p in listed["people"]] == ["owner"] and "No shell and no root" in listed["what_giving_means"]
    offered = listed["can_be_given"][0]
    given = offered["name"]
    view = call(client, "set_person", name="alex", login="alex@example.internal", services=[given], why="they asked")
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["word"], proposal["title"]) == ("risky", "share", "Add access for alex")
    assert proposal["lines"][0].startswith("alex (alex@example.internal) may open: ") and chat_module.SHARING in proposal["lines"]
    # What the tick opens and what the person can do behind it, as the People tab says it.
    assert proposal["lines"][1] == f"New, {offered['title']}: {offered['opens']}. {offered['access']}"
    assert "alex" not in saved(app)["people"]
    view = approved(client, proposal["id"], typed="share")
    assert saved(app)["people"]["alex"] == {"role": "friend", "login": "alex@example.internal", "services": [given]}
    assert "alex added" in kinds(view, "assistant")[-1]["text"] and "invitation" in kinds(view, "assistant")[-1]["text"]
    assert "nothing to change" in result(call(client, "set_person", name="alex", services=[given]))
    # Taking away is a change; a wrong name or login is said before any card.
    view = call(client, "set_person", name="alex", services=[])
    assert kinds(view, "ran")[-1]["title"] == "Change access for alex" and "services" not in saved(app)["people"]["alex"]
    assert "cannot be given to a person" in result(call(client, "set_person", name="alex", services=["router"]))
    assert "Tailscale login is the email" in result(call(client, "set_person", name="sam"))
    # Removing a person takes their account with them: the owner's word.
    view = call(client, "remove_person", name="alex")
    proposal = kinds(view, "proposal")[-1]
    assert (proposal["tier"], proposal["word"]) == ("risky", "remove")
    approved(client, proposal["id"], typed="remove")
    assert "alex" not in saved(app)["people"]
    assert "the owner stays" in result(call(client, "remove_person", name="owner"))


def test_metrics_services_and_the_small_switches(agent):
    app, client, host = chosen(agent, "auto")
    asked = []

    def prometheus(url):
        asked.append(url)
        if "broken" in url:
            raise OSError("connection refused")
        series = [{"metric": {"__name__": "up", "instance": "forgejo", "job": "node"}, "value": [0, "1"]}]
        return {"status": "success", "data": {"resultType": "vector", "result": series}}

    app.fetch = prometheus
    view = call(client, "metrics", query="up == 1")
    assert result(view) == 'up{instance="forgejo", job="node"} 1'
    assert ":9090/api/v1/query?query=up+%3D%3D+1" in asked[-1]
    assert "does not answer (connection refused)" in result(call(client, "metrics", query="broken"))
    assert next(e for e in app.chat.audit_log() if e.get("action") == "metrics")["query"] == "broken"

    listed = yaml.safe_load(result(call(client, "services")))
    guests = {g["name"]: g for g in listed["guests"]}
    assert guests["forgejo"]["zone"] == "services" and guests["forgejo"]["ip"] and guests["forgejo"]["vmid"]
    assert any(i["name"] == "forgejo" and i["url"].startswith("https://") for i in listed["owner_opens"])
    assert "password" not in json.dumps(listed["owner_opens"]) and "agent-services.md" in listed["note"]

    assert app.port_mapping() is True
    view = call(client, "port_mapping", enabled=False)
    assert kinds(view, "ran")[-1]["title"] == "Ask the home router for the port: off" and app.port_mapping() is False
    assert "off already" in result(call(client, "port_mapping", enabled=False))
    # Without `enabled` it reads how devices reach the server, as the page
    # measures it: the owner's phone was direct while the agent, with nothing
    # to read, switched the asking off to change a label.
    phone = direct.Device("phone", direct.DIRECT, direct.AWAY)
    direct.record(app.home, direct.Finding(direct.OFF, "the home router is not asked for UDP 41642", devices=(phone,)))
    app.path = lambda: direct.recorded(app.home) | {"live": True}
    view = call(client, "port_mapping")
    read = yaml.safe_load(result(view))
    assert kinds(view, "tool")[-1]["label"] == "Read how devices reach the server"
    assert (read["path"], read["devices_measured"], read["read_just_now"]) == ("measured", ["phone: direct, away from home"], True)
    assert read["means"].startswith("a device away from home is connected directly") and read["port"].startswith(
        "the home router is not asked"
    )
    assert read["asks_home_router_for_the_port"] is False and "never makes a connection direct" in read["the_switch"]
    assert yaml.safe_load(app.tools.call("status", {})[0])["path_to_the_server"] == read
    app.path = lambda: None
    assert "nothing to read yet" in result(call(client, "port_mapping"))

    assert "sent a test through ntfy" in result(call(client, "test_alert"))
    assert app.send and app.send[-1][2]["Title"] == "NullBill: test alert"

    job = app.start("probe")
    wait_for(lambda: job.state() != "running", "the probe")
    assert "is not running any more" in result(call(client, "stop_job", id=job.id))
    assert "no job nope" in result(call(client, "stop_job", id="nope"))


def test_several_problems_reach_the_agent_and_the_owner_one_a_line(agent):
    app, client, _ = chosen(agent, "yolo")
    view = call(client, "add_service", name="paperless", title="P", zone="mgmt", ports=[{"port": 22}])
    said = result(view).splitlines()
    assert len(said) == 2 and "zone 'mgmt'" in said[0] and "port 22" in said[1]
    # A plan that stops fitting is a list too.
    assert app.tools.fits() == "the plan fits"
    app.plan = lambda: {"errors": ["memory: 4 GiB short", "disk: 10 GiB short"]}
    assert app.tools.fits() == "the plan does not fit:\n- memory: 4 GiB short\n- disk: 10 GiB short"


def test_status_says_what_a_setup_run_would_do_to_each_machine(agent):
    """The answer to "what happens to my Minecraft server when I run this":
    the page's own preview, which asks the server nothing."""
    app, client, host = chosen(agent, "auto")
    view = call(client, "status")
    # The agent reads all of it; the page shows only the end of a long answer.
    done = kinds(view, "tool_done")[-1]
    text = app.chat.output(done["full"]) if done.get("full") else done["output"]
    assert len(text) < chat_module.MAX_OUTPUT
    would = yaml.safe_load(text)["a_setup_run_now_would"]
    # Nothing built yet: every machine is created, and none is called left alone.
    assert "forgejo" in would["creates"] and "leaves_alone" not in would
    assert "no setup run has written down yet" in would["note"]


def test_the_preview_reads_for_the_agent_as_it_does_on_the_page():
    row = {"name": "minecraft", "what": "again", "why": ["Changed: settings (difficulty)."]}
    row |= {"restart_note": "Minecraft may restart.", "last": {"took_s": 41.0, "answered_s": 34.0, "at": 1}}
    rest = [{"name": "factorio", "what": "alone"}, {"name": "pbs", "what": "unknown"}, {"name": "jellyfin", "what": "create"}]
    found = {"known": True, "full": False, "machines": [row, *rest]}
    assert chat_module.foreseen(found) == {
        "minecraft": {
            "does": "set up again",
            "why": row["why"],
            "restarts": "Minecraft may restart.",
            "its_setup_took_s_last_time": 41.0,
            "answered_again_after_s": 34.0,
        },
        "leaves_alone": ["factorio"],
        "not_known": ["pbs"],
        "creates": ["jellyfin"],
    }
    assert chat_module.foreseen(None) == "not known: the page could not work it out"
    assert "also changes how NullBill works out settings" in chat_module.foreseen({"code": True, "machines": []})["note"]
