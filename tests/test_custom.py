"""Services of the owner's own (custom.py): a program NullBill has no role
for gets a guest, its firewall and its checks from a definition in
config.yaml, and its setup script is run once per version."""

import json
import shlex
import time

import pytest
import test_chat
import test_web
import yaml
from fixturedata import fixture_config

from nullbill import apply, custom, proxy
from nullbill.errors import NullBillError
from nullbill.firewall import render_firewall
from nullbill.flows import resolve_flows
from nullbill.model import VM_SSH, build_model, shipped
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.tailnet import render_tailnet
from nullbill.web import services, workloads

# The page's server and the agent's turns, as their own tests set them up.
site = test_web.site
agent = test_chat.agent

SCRIPT = "#!/bin/bash\nset -eu\napt-get install -y nginx\n"
PAPERLESS = {
    "title": "Paperless",
    "what": "Scanned documents, searchable",
    "ports": [{"port": 8000, "from": ["role:owner", "shared"]}],
    "proxy_allow": ["paperless.example.internal", ".example.org"],
    "share": {"what": "Our documents", "open": "http://{host}:{port}/"},
    "memory_mib": 2048,
}


def added(config, name="paperless", want=PAPERLESS):
    return custom.define(config, build_model(config), name, want)


def test_define_writes_a_service_config_yaml_can_carry_alone():
    config = added(fixture_config("large"))
    svc = config["services"]["paperless"]
    assert svc == {
        "id": 64,
        "kind": "lxc",
        "zone": "apps",
        "workload": "paperless",
        "floor": {"cores": 1, "memory_mib": 2048, "disk_gib": 8},
        "description": "Scanned documents, searchable",
        "ports": [{"port": 8000, "proto": "tcp", "from": ["role:owner", "shared"]}],
        "proxy_allow": ["paperless.example.internal", ".example.org"],
        "share": {"title": "Paperless", "what": "Our documents", "open": "http://{host}:{port}/"},
    }
    assert config["power"]["paperless"]["title"] == "Paperless" and "paperless" in config["intent"]["workloads"]
    model = build_model(config)
    assert custom.names(model) == ["paperless"] and custom.workloads(model) == ["paperless"]
    assert custom.title(model, "paperless") == "Paperless"
    # The catalog's ids stay below the owner's range, so a later NullBill never collides with one.
    assert all(model["services"][n]["id"] < custom.OWN_IDS[0] for n in shipped("services"))
    # Saved and read again, it is the same service.
    assert build_model(yaml.safe_load(yaml.safe_dump(config)))["services"]["paperless"] == model["services"]["paperless"]


def test_a_service_of_the_owners_own_gets_the_platform_around_it(facts):
    config = added(fixture_config("large"))
    config["people"]["alex"] = {"role": "friend", "login": "alex@example.internal", "services": ["paperless"]}
    model = build_model(config)
    plan = build_plan(model, facts("large"))
    bare = strip_notes(plan)
    guest = bare["guests"]["paperless"]
    assert guest["kind"] == "lxc" and guest["zone"] == "apps" and guest["ip"] == "10.42.9.64"
    assert bare["custom"] == {
        "paperless": {
            "title": "Paperless",
            "what": "Scanned documents, searchable",
            "ports": ["tcp/8000"],
            "open": "http://{host}:{port}/",
            "shared": True,
        }
    }
    # Both firewall layers come from the same flows: its port from the vpn
    # guest only, node_exporter from monitoring only, and nothing else in.
    into = [f for f in resolve_flows(bare, model) if f.dest == "paperless"]
    assert sorted((f.ref, f.proto, f.port) for f in into) == [
        ("role:owner", "tcp", 8000),
        ("service:monitoring", "tcp", 9100),
        ("shared", "tcp", 8000),
    ]
    rules = render_firewall(plan, model)[f"{guest['vmid']}.fw"]
    assert "policy_in: DROP" in rules and "policy_out: DROP" in rules
    assert [line.split(" # ")[0] for line in rules.splitlines() if line.startswith("IN ")] == [
        "IN ACCEPT -source +nb-svc-vpn -p tcp -dport 8000",
        "IN ACCEPT -source +nb-svc-monitoring -p tcp -dport 9100",
    ]
    # Out: the router's DNS, the proxy, the CA, the log store and the model gateway; nothing else.
    out = [line.split(" # to ")[1] for line in rules.splitlines() if line.startswith("OUT ")]
    assert sorted(set(out)) == ["egress-proxy", "model-gateway", "monitoring", "router", "step-ca"]
    # It goes out through the proxy only, to the usual list plus its own names.
    squid = proxy.render_proxy(plan, model)["etc/squid/squid.conf"]
    assert "acl allowed_paperless dstdomain -n paperless.example.internal .example.org" in squid
    assert f"acl from_paperless src {guest['ip']}/32" in squid
    # The friend gets exactly that port, and the policy test says so.
    policy = json.loads(render_tailnet(plan, model)["policy.json"])
    assert any(g["src"] == ["alex@example.internal"] and g["dst"] == [guest["ip"]] and g["ip"] == ["tcp:8000"] for g in policy["grants"])


def test_a_vm_of_the_owners_own_is_reached_by_setup_like_nullbills_own():
    config = added(fixture_config("large"), "dockerbox", {"title": "Docker box", "kind": "vm", "ports": [{"port": 8080}]})
    model = build_model(config)
    svc = model["services"]["dockerbox"]
    assert VM_SSH in svc["ports"] and custom.own_ports(svc) == [{"port": 8080, "proto": "tcp", "from": ["role:owner"]}]
    # Written to config.yaml without the port the model adds.
    assert config["services"]["dockerbox"]["ports"] == [{"port": 8080, "proto": "tcp", "from": ["role:owner"]}]


@pytest.mark.parametrize(
    ("name", "want", "said"),
    [
        ("Paperless", {}, "lowercase letters"),
        ("core", {}, "lowercase letters"),
        ("forgejo", {}, "one of NullBill's own"),
        ("games", {}, "taken already"),
        ("x", {"zone": "mgmt"}, "zone 'mgmt'"),
        ("x", {"zone": "access"}, "zone 'access'"),
        ("x", {"zone": "build"}, "zone 'build'"),
        ("x", {"ports": [{"port": 22}]}, "port 22"),
        ("x", {"ports": [{"port": 9100}]}, "port 9100"),
        ("x", {"ports": [{"port": 80, "from": ["host"]}]}, "from"),
        ("x", {"ports": [{"port": 80, "from": ["zone:*"]}]}, "from"),
        ("x", {"ports": [{"port": 80, "from": ["role:agent"]}]}, "from"),
        ("x", {"ports": [{"port": 80, "from": ["service:nope"]}]}, "nope"),
        ("x", {"ports": [{"port": 70000}]}, "1 to 65535"),
        ("x", {"proxy_allow": ["*"]}, "proxy_allow"),
        ("x", {"proxy_allow": ["10.0.0.1"]}, "proxy_allow"),
        ("x", {"memory_mib": 64}, "memory_mib"),
        ("x", {"kind": "vm-pool"}, "kind is lxc"),
        ("x", {"gpu": True}, "unknown: gpu"),
        ("x", {"title": "two\nlines"}, "title"),
    ],
)
def test_define_refuses_what_would_open_more_than_a_service_of_its_own(name, want, said):
    config = fixture_config("large")
    with pytest.raises(NullBillError) as raised:
        custom.define(config, build_model(config), name, {"title": "X", **want})
    assert said in "; ".join(raised.value.messages), raised.value.messages


def test_every_problem_is_said_together_and_a_zone_with_direct_egress_is_refused():
    config = fixture_config("large") | {"zones": {"wild": {"index": 12, "egress": "direct"}}}
    with pytest.raises(NullBillError) as raised:
        custom.define(config, build_model(config), "x", {"title": "X", "zone": "wild", "cores": 0, "ports": [{"port": 0}]})
    said = "; ".join(raised.value.messages)
    assert "reach the internet directly" in said and "cores" in said and "1 to 65535" in said


def test_a_change_gives_only_what_changes_and_keeps_the_rest():
    config = added(fixture_config("large"))
    again = added(config, want={"memory_mib": 4096, "on": False})
    svc = again["services"]["paperless"]
    assert svc["floor"]["memory_mib"] == 4096 and svc["id"] == 64 and svc["ports"] == config["services"]["paperless"]["ports"]
    assert svc["share"] == config["services"]["paperless"]["share"] and svc["description"] == "Scanned documents, searchable"
    assert again["power"]["paperless"] == {"title": "Paperless"}
    assert "paperless" not in again["intent"]["workloads"]
    # Turned on again without naming anything else.
    assert "paperless" in added(again, want={"on": True})["intent"]["workloads"]
    # NullBill's own services are not changed this way.
    with pytest.raises(NullBillError, match="one of NullBill's own"):
        added(config, "jellyfin", {"memory_mib": 8192})


def test_forget_takes_the_service_and_every_trace_of_it_out_of_config_yaml():
    before = fixture_config("large")
    config = added(before)
    config["people"]["alex"] = {"role": "friend", "login": "alex@example.internal", "services": ["paperless", "forgejo"]}
    config.setdefault("overrides", {})["paperless"] = {"memory_mib": 4096}
    after = custom.forget(config, build_model(config), "paperless")
    assert "paperless" not in after.get("services", {}) and "paperless" not in after.get("power", {})
    assert "paperless" not in after["intent"]["workloads"] and "paperless" not in after.get("overrides", {})
    assert after["people"]["alex"]["services"] == ["forgejo"]
    build_model(after)
    with pytest.raises(NullBillError, match="not a service of the owner's own"):
        custom.forget(config, build_model(config), "forgejo")


def test_the_owner_reads_what_a_service_is_line_by_line():
    config = added(fixture_config("large"))
    model = build_model(config)
    lines = custom.describe(model, "paperless", SCRIPT)
    assert lines[0] == "Paperless (paperless): a container of its own in zone apps, 1 core, 2048 MiB of memory, 8 GiB of disk"
    assert "Opens tcp/8000 to you and the people you tick it for" in lines
    assert any(line.startswith("Internet: only through the proxy") and "paperless.example.internal, .example.org" in line for line in lines)
    assert "Runs: always" in lines
    on_demand = build_model(added(config, want={"run": "on-demand", "stop": "auto", "idle_minutes": 30}))
    assert "Runs: on demand; stops by itself after 30 min unused" in custom.describe(on_demand, "paperless", SCRIPT)
    assert lines[-1] == f"Setup script: 3 lines, run as root in the guest (sha256 {custom.digest(SCRIPT)[:12]})"
    assert custom.describe(model, "paperless", None)[-1] == "No setup script: an empty Debian guest, filled by hand"


def test_a_setup_script_is_a_script_of_a_size_one_can_read():
    assert custom.check_script("#!/bin/bash\r\necho hi\r\n\r\n") == "#!/bin/bash\necho hi\n"
    for text, said in (
        ("echo hi\n", "starts with #!/bin/bash"),
        ("#!/usr/bin/python3\n", "starts with"),
        ("#!/bin/sh\n" + "x" * 40000, "longer than"),
    ):
        with pytest.raises(NullBillError, match=said):
            custom.check_script(text)


# 100 of them: the ids, the plan, both firewall layers and the page's rows.
def hundred(config):
    for n in range(100):
        want = {"title": f"World {n}", "zone": "games", "ports": [{"port": 2456, "proto": "udp", "from": ["role:owner", "shared"]}]}
        config = custom.define(config, build_model(config), f"world-{n}", want | {"run": "on-demand", "memory_mib": 2048})
    return config


def test_a_hundred_services_plan_render_and_list(facts, tmp_path):
    config = hundred(fixture_config("large"))
    model = build_model(config)
    ids = [model["services"][f"world-{n}"]["id"] for n in range(100)]
    # Never a pool's clone range, never the catalog's.
    assert ids == [*range(64, 128), *range(192, 228)] and custom.free_id(model) == 228
    plan = build_plan(model, facts("large"))
    bare = strip_notes(plan)
    assert len(bare["custom"]) == 100 and len({bare["guests"][f"world-{n}"]["ip"] for n in range(100)}) == 100
    files = render_firewall(plan, model)
    assert all(f"{bare['guests'][f'world-{n}']['vmid']}.fw" in files for n in range(100))
    json.loads(render_tailnet(plan, model)["policy.json"])
    view = workloads.options(model, facts("large"), existing=set(), home=tmp_path)
    own = [i for i in view["items"] if i["group"] == "own"]
    assert len(own) == 100 and all(i["chosen"] and i["own"]["facts"] for i in own) and view["errors"] == []
    # Each runs on demand and counts at half its limit, as every container does.
    assert sum(1 for w in view["power"] if w["run"] == "on-demand" and w["stop"] == "ask" and not w["auto"]) == 100
    assert view["memory"]["demand_mib"] == 100 * 1024
    listed = services.services(bare)["items"]
    assert sum(1 for s in listed if s.get("own")) == 100


def test_the_owners_range_ends_and_says_so():
    config = fixture_config("large")
    model = build_model(config)
    full = {**model, "services": {**model["services"], **{f"s{i}": {"id": i, "workload": f"s{i}"} for i in custom.OWN_IDS}}}
    assert custom.free_id(full) is None
    with pytest.raises(NullBillError, match="every id for services of the owner's own is taken"):
        custom.define(config, full, "one-more", {"title": "One more"})


# apply and verify.
def test_the_stage_runs_each_script_in_its_guest_and_a_failing_one_stops_nothing(ctx_custom):
    c, home = ctx_custom
    apply.stage_custom(c)
    plays = [call for call in c.reach.calls if call[0] == "playbook"]
    assert [(p[1], list(p[2])) for p in plays] == [(custom.PLAYBOOK, ["paperless"])]
    variables = plays[0][3]
    assert variables == custom.variables(c.bare, c.model, "paperless", SCRIPT, "http://10.42.2.10:3128")
    assert variables["custom_service_sha256"] == custom.digest(SCRIPT) and variables["custom_service_ports"] == ["tcp/8000"]
    assert "custom: Paperless is set up in paperless" in c.log
    assert f"custom: bare has no setup script ({custom.DIR}/bare/{custom.SCRIPT}); its guest stays as setup made it" in c.log
    # A script that fails is said, and the stage ends without raising: the checks name it.
    c.reach.fail_playbook = custom.PLAYBOOK
    custom.script(home, "paperless").write_text(SCRIPT + "false\n")
    apply.stage_custom(c)
    assert any(line.startswith("custom: paperless's setup script did not finish") for line in c.log)
    assert "custom: not set up: paperless. Their output is above; the checks name each one" in c.log
    assert apply.STAGES[-2][0] == "custom" and apply.STAGES[-1][0] == "verify"


@pytest.fixture
def ctx_custom(facts, tmp_path):
    from test_apply import TARGET, FakeReach, MemoryCodec

    from nullbill.artifacts import render_all
    from nullbill.secretfile import Secrets

    config = added(added(fixture_config("large")), "bare", {"title": "Bare"})
    model = build_model(config)
    plan = build_plan(model, facts("large"))
    path = custom.script(tmp_path, "paperless")
    path.parent.mkdir(parents=True)
    path.write_text(SCRIPT)
    return apply.Context(plan, model, tmp_path, TARGET, FakeReach(), Secrets(MemoryCodec()), render_all(plan, model)), tmp_path


def guest_answers(plan, answers):
    """A runner for verify's two questions to each guest."""
    bare = strip_notes(plan)

    def run(target, command):
        for name, (mark, failed) in answers.items():
            guest = bare["guests"][name]
            if command == f"pct exec {guest['vmid']} -- bash -c {shlex.quote(f'cat {custom.MARK} 2>/dev/null')}":
                return (0, mark + "\n", "") if mark else (1, "", "")
            if command == f"pct exec {guest['vmid']} -- bash -c {shlex.quote(custom.FAILED_UNITS)}":
                return 0, failed, ""
        raise AssertionError(command)

    return run


def test_verify_says_whether_the_script_ran_as_it_is_and_whether_a_unit_failed(ctx_custom):
    c, home = ctx_custom
    ran, units = "paperless: its setup script ran as it is now", "no unit has failed"
    good = custom.check(
        c.plan, c.model, "root@pve", guest_answers(c.plan, {"paperless": (custom.digest(SCRIPT), ""), "bare": ("", "")}), home, {}
    )
    assert [(r.status, r.name) for r in good] == [("PASS", ran), ("PASS", f"paperless: {units}"), ("PASS", f"bare: {units}")]
    stale = custom.check(
        c.plan, c.model, "root@pve", guest_answers(c.plan, {"paperless": ("0" * 64, "paperless.service "), "bare": ("", "")}), home, {}
    )
    assert (stale[0].status, stale[0].detail) == ("FAIL", "the script changed since its last good run; run setup")
    assert (stale[1].status, stale[1].detail) == ("DEGRADED", "failed: paperless.service")
    never = custom.check(c.plan, c.model, "root@pve", guest_answers(c.plan, {"paperless": ("", ""), "bare": ("", "")}), home, {})
    assert never[0].status == "FAIL" and "failed or has not run yet" in never[0].detail
    # Off on demand is off on purpose; without a host nothing passes.
    off = custom.check(c.plan, c.model, "root@pve", guest_answers(c.plan, {"bare": ("", "")}), home, {"paperless": "Paperless"})
    assert [r.status for r in off] == ["SKIP", "SKIP", "PASS"]
    assert {r.status for r in custom.skipped(c.plan, c.model, home, "no host")} == {"SKIP"}


def test_verify_probes_its_ports_like_any_other(facts):
    from nullbill.verify import probes

    config = added(fixture_config("large"))
    model = build_model(config)
    plan = build_plan(model, facts("large"))
    names = [p.name for p in probes(plan, model, "192.0.2.80")[0]]
    assert any("paperless tcp/8000" in n for n in names)
    # Denied from another zone's guests, like every port nobody listed.
    assert any("paperless tcp/8000" in n and p.expect != "open" for n, p in zip(names, probes(plan, model, "192.0.2.80")[0], strict=True))


# The page.
def test_what_to_run_lists_them_in_their_own_group_with_what_each_is(facts, tmp_path):
    config = added(fixture_config("large"))
    path = custom.script(tmp_path, "paperless")
    path.parent.mkdir(parents=True)
    path.write_text(SCRIPT)
    model = build_model(config)
    view = workloads.options(model, facts("large"), existing={"router"}, home=tmp_path)
    row = next(i for i in view["items"] if i["name"] == "paperless")
    assert row["group"] == "own" and row["title"] == "Paperless" and row["what"] == "Scanned documents, searchable"
    assert row["chosen"] and row["memory_mib"] == 1024 and row["disk_gib"] == 8 and row["on_server"] == []
    assert row["own"]["scripts"] == ["paperless"] and row["own"]["removable"] is False
    assert row["own"]["facts"] == custom.describe(model, "paperless", SCRIPT)
    assert "runs its setup script" in row["on"] and "Deletes its guest" in row["off"]
    assert [i["group"] for i in view["items"] if i["name"] in ("agents", "minecraft", "backups")] == ["workloads", "games", "extras"]
    assert any(w["name"] == "paperless" for w in view["power"])
    # Off and with no guest on the server, it can be removed.
    off = build_model(added(config, want={"on": False}))
    assert next(i for i in workloads.options(off, facts("large"), {"router"}, tmp_path)["items"] if i["name"] == "paperless")["own"][
        "removable"
    ]
    assert workloads.loss("paperless", model) == row["off"]


def test_the_services_page_says_how_to_open_one(facts):
    config = added(added(fixture_config("large")), "bare", {"title": "Bare", "ports": [{"port": 7000}, {"port": 7001, "proto": "udp"}]})
    bare = strip_notes(build_plan(build_model(config), facts("large")))
    by_name = {s["name"]: s for s in services.services(bare)["items"]}
    paperless = by_name["paperless"]
    assert paperless["url"] == "http://10.42.9.64:8000/" and paperless["own"] and paperless["watch"] == "paperless"
    assert "Access, People" in [s.get("link") for s in paperless["steps"]]
    assert paperless["steps"][-1]["code"] == f"pct enter {bare['guests']['paperless']['vmid']}"
    assert by_name["bare"]["addresses"] == ["10.42.9.65:7000", "10.42.9.65:7001"] and "url" not in by_name["bare"]
    named = {s["name"]: s for s in services.services(bare, by_name=True)["items"]}
    assert named["paperless"]["url"] == "http://paperless.nullbill.internal:8000/"
    assert named["paperless"]["addresses"] == ["By address: http://10.42.9.64:8000/"]


# The page's API and the agent's tools.
def saved(app):
    return yaml.safe_load((app.home / "config.yaml").read_text())


def test_the_page_shows_a_script_and_removes_only_what_is_off_and_gone(site):
    from test_web import OWNER, probed, sign_in

    app, client = site
    assert client.call("/api/own-service?name=paperless")[0] == 401
    sign_in(app, client)
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200
    change = app.service_change("paperless", PAPERLESS, SCRIPT)
    assert change["new"] and change["risky"] and not change["same"] and change["diff"].startswith("--- setup.sh now")
    app.save_service("paperless", change["config"], change["script"])
    path = app.home / "custom" / "paperless" / "setup.sh"
    assert path.read_text() == SCRIPT and path.stat().st_mode & 0o077 == 0
    # The same again changes nothing; a new title alone brings no code and opens nothing.
    assert app.service_change("paperless", PAPERLESS, SCRIPT)["same"]
    renamed = app.service_change("paperless", {"title": "Documents"}, None)
    assert not renamed["risky"] and not renamed["same"] and renamed["script"] is None and renamed["diff"] == ""
    assert app.service_change("paperless", {"ports": [{"port": 8001}]}, None)["risky"]
    assert app.service_change("paperless", {}, SCRIPT + "echo more\n")["risky"]

    status, own = client.call("/api/own-service?name=paperless")
    assert status == 200 and own == {"name": "paperless", "title": "Paperless", "script": SCRIPT, "path": "custom/paperless/setup.sh"}
    assert client.call("/api/own-service?name=forgejo")[0] == 404
    state = client.call("/api/state")[1]
    row = next(i for i in state["options"]["items"] if i["name"] == "paperless")
    assert row["chosen"] and row["own"]["scripts"] == ["paperless"] and not row["own"]["removable"]
    assert any(s["name"] == "paperless" and s["url"] == "http://10.42.9.64:8000/" for s in state["services"]["items"])

    # On: refused. Off but still on the server: refused. Off and gone: removed, the script set aside.
    status, body = client.call("/api/own-service/remove", {"name": "paperless"})
    assert status == 400 and "turned on" in body["error"]
    # Saving the choices keeps the definition; only its tick goes.
    assert client.call("/api/choices", OWNER)[0] == 200
    assert "paperless" in saved(app)["services"] and "paperless" not in saved(app)["intent"]["workloads"]
    (app.home / "guests.json").write_text(json.dumps({"guests": ["router", "paperless"]}))
    status, body = client.call("/api/own-service/remove", {"name": "paperless"})
    assert status == 400 and "still on the server" in body["error"]
    (app.home / "guests.json").write_text(json.dumps({"guests": ["router"]}))
    assert client.call("/api/own-service/remove", {"name": "paperless"}) == (200, {"ok": True, "removed": "Paperless"})
    assert "paperless" not in saved(app).get("services", {}) and not path.exists()
    (kept,) = (app.home / "custom" / ".removed").iterdir()
    assert kept.name.startswith("paperless-") and (kept / "setup.sh").read_text() == SCRIPT
    assert client.call("/api/own-service/remove", {"name": "forgejo"})[0] == 400


def test_a_service_the_hardware_cannot_hold_is_refused_before_anything_is_saved(site):
    from test_web import OWNER, probed, sign_in

    app, client = site
    sign_in(app, client)
    probed(app, "small")
    assert client.call("/api/choices", OWNER)[0] == 200
    before = (app.home / "config.yaml").read_text()
    with pytest.raises(NullBillError, match="planned guests need"):
        app.service_change("big", {"title": "Big", "kind": "vm", "memory_mib": 65536}, SCRIPT)
    assert (app.home / "config.yaml").read_text() == before and not (app.home / "custom").exists()


def add_call(**want):
    return "CALL add_service " + json.dumps({"name": "paperless", "why": "to keep documents", **want})


def test_the_agent_adds_a_service_only_after_the_owner_typed_the_word(agent):
    from test_chat import approve, kinds, say, settle, signed_in
    from test_web import OWNER, probed

    app, client, _ = signed_in(agent, "auto")
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200
    # Auto runs changes unasked; a program NullBill does not know still waits, with its whole script to read.
    view = say(client, add_call(setup=SCRIPT, **PAPERLESS))
    proposal = kinds(view, "proposal")[-1]
    assert proposal["tier"] == "risky" and proposal["word"] == "add" and proposal["title"] == "Add your own service Paperless"
    assert proposal["act"] == "service" and "content" not in proposal
    assert all("+" + line in proposal["diff"] for line in SCRIPT.splitlines())
    assert "Opens tcp/8000 to you and the people you tick it for" in proposal["lines"]
    assert "paperless" not in saved(app).get("services", {}) and not (app.home / "custom").exists()
    assert approve(client, proposal["id"], typed="yes")[0] == 400
    assert approve(client, proposal["id"], typed="add")[0] == 200
    view = settle(client)
    for _ in range(100):
        if "paperless" in saved(app).get("services", {}):
            break
        time.sleep(0.05)
    settle(client)
    assert saved(app)["services"]["paperless"]["zone"] == "apps" and "paperless" in saved(app)["intent"]["workloads"]
    assert (app.home / "custom" / "paperless" / "setup.sh").read_text() == SCRIPT

    # What neither brings code nor opens anything is a change like any other, and runs in Auto.
    view = say(client, add_call(title="Documents"))
    assert kinds(view, "ran")[-1]["title"] == "Change your own service Documents"
    assert saved(app)["power"]["paperless"]["title"] == "Documents"
    assert "nothing to change" in kinds(say(client, add_call(title="Documents")), "tool_done")[-1]["output"]
    # A changed script, or another port, waits again.
    view = say(client, add_call(setup=SCRIPT + "echo more\n"))
    assert (kinds(view, "proposal")[-1]["tier"], kinds(view, "proposal")[-1]["word"]) == ("risky", "change")
    view = say(client, add_call(ports=[{"port": 8000}, {"port": 8443}]))
    assert kinds(view, "proposal")[-1]["tier"] == "risky"
    assert (app.home / "custom" / "paperless" / "setup.sh").read_text() == SCRIPT

    read = kinds(say(client, 'CALL read_service {"name": "paperless"}'), "tool_done")[-1]["output"]
    assert "Documents (paperless): a container of its own in zone apps" in read and "apt-get install -y nginx" in read
    done = kinds(say(client, "CALL status {}"), "tool_done")[-1]
    status = yaml.safe_load(app.chat.output(done["full"]) if done.get("full") else done["output"])
    assert next(i for i in status["what_to_run"] if i["name"] == "paperless")["own"] is True


def test_the_agent_cannot_widen_a_service_past_its_own_guest_or_hide_a_secret_in_a_script(agent):
    from test_chat import kinds, say, signed_in
    from test_web import OWNER, probed

    app, client, _ = signed_in(agent, "yolo")
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200

    def refused(**want):
        view = say(client, add_call(setup=SCRIPT, **want))
        assert not kinds(view, "proposal") and not kinds(view, "ran")
        return kinds(view, "tool_done")[-1]["output"]

    assert "zone 'mgmt'" in refused(title="X", zone="mgmt")
    assert "port 22" in refused(title="X", ports=[{"port": 22}])
    assert "from" in refused(title="X", ports=[{"port": 80, "from": ["host"]}])
    own = kinds(say(client, "CALL add_service " + json.dumps({"name": "forgejo", "title": "X"})), "tool_done")
    assert "one of NullBill's own" in own[-1]["output"]
    said = kinds(say(client, add_call(title="X", setup="#!/bin/bash\necho sk-ant-api03-SECRETVALUE123 > /etc/key\n")), "tool_done")
    assert "holds one of the server's secrets" in said[-1]["output"]
    assert "paperless" not in saved(app).get("services", {}) and not (app.home / "custom").exists()


def test_the_agent_removes_a_service_only_once_it_is_off_and_its_guest_is_gone(agent):
    from test_chat import kinds, say, signed_in
    from test_web import OWNER, probed

    app, client, _ = signed_in(agent, "auto")
    probed(app)
    assert client.call("/api/choices", OWNER)[0] == 200
    change = app.service_change("paperless", PAPERLESS, SCRIPT)
    app.save_service("paperless", change["config"], change["script"])
    out = kinds(say(client, 'CALL remove_service {"name": "paperless"}'), "tool_done")[-1]["output"]
    assert "turned on" in out and "paperless" in saved(app)["services"]
    assert client.call("/api/choices", OWNER)[0] == 200
    view = say(client, 'CALL remove_service {"name": "paperless", "why": "no longer used"}')
    assert kinds(view, "ran")[-1]["title"] == "Remove your own service Paperless"
    assert "paperless" not in saved(app).get("services", {})


def test_a_container_that_shows_no_use_never_stops_by_itself(facts):
    # nb-busy counts TCP connections; a UDP-only world would be stopped under its players.
    config = fixture_config("large")
    udp = {"title": "World", "zone": "games", "run": "on-demand", "ports": [{"port": 2456, "proto": "udp"}]}
    with pytest.raises(NullBillError, match="stop auto: it listens on no TCP port"):
        custom.define(config, build_model(config), "world", udp | {"stop": "auto"})
    config = custom.define(config, build_model(config), "world", udp)
    assert custom.define(config, build_model(config), "world", {"ports": [{"port": 2456}], "stop": "auto"})
    # Written by hand, plan says the same.
    config["power"]["world"]["stop"] = "auto"
    with pytest.raises(NullBillError) as raised:
        build_plan(build_model(config), facts("large"))
    assert raised.value.messages == [f"power.world.stop: auto: {custom.NO_AUTO}; use stop: ask"]
