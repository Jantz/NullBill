"""On demand (power.py): what the plan counts, what OpenTofu leaves off,
the rules nb-power gets, and what verify does with a guest that is off."""

import copy
import json
import shlex

import pytest
from conftest import fixture_config
from test_verify import PUBLIC, firewall, statuses

from nullbill import power, tofu
from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.verify import probes, run_verify

ON = {"run": "on-demand"}
TARGET = "root@pve.internal"


def small_server(facts):
    """The owner's 16 GiB server, asked for more than fits at once."""
    f = facts("mid")
    f["memory_mib"] = 15645
    f["gpus"][0]["vram_mib"] = None
    config = {
        **fixture_config("mid"),
        "intent": {"workloads": ["agents", "linux-ci", "inference", "windows-dev", "windows-ci"]},
        "services": {"pbs": {"enabled": False}},
        "gpus": {"0000:01:00.0": {"vram_mib": 12282}},
    }
    return f, config


def planned(facts, config, **rules):
    return build_plan(build_model({**config, "power": copy.deepcopy(rules)}), facts)


def errors(facts, config, **rules):
    with pytest.raises(NullBillError) as exc:
        planned(facts, config, **rules)
    return exc.value.messages


def test_on_demand_lets_a_server_hold_more_than_runs_at_once(facts):
    f, config = small_server(facts)
    assert errors(f, config)[0].startswith("memory: planned guests need 15872 MiB at their floors")
    rules = {"windows-dev": ON, "linux-ci": ON, "inference": ON, "agents": ON}
    plan = strip_notes(planned(f, config, **rules))
    found = plan["power"]
    # What always runs is counted as before; the rest shares what is left.
    always = {n: g for n, g in plan["guests"].items() if "run" not in g}
    vms = sum(g["memory_mib"] * g.get("count", 1) for g in always.values() if g["kind"] != "lxc")
    containers = sum(g["memory_mib"] for g in always.values() if g["kind"] == "lxc")
    assert plan["host"]["guests_mib"] == vms + containers // 2
    assert found["headroom_mib"] == plan["host"]["unallocated_mib"] == 15645 - 3072 - plan["host"]["guests_mib"]
    assert list(found["groups"]) == ["agents", "linux-ci", "inference", "windows-dev"]
    assert {n for n, g in plan["guests"].items() if g.get("run") == "on-demand"} == {
        "workspace",
        "runner-dispatcher",
        "linux-runner",
        "inference",
        "model-gateway",
        "windows-dev",
    }
    # A workload counts as the plan counts its guests: VMs in full,
    # containers at their share, a pool with every clone.
    assert found["groups"]["inference"]["memory_mib"] == (2048 + 1024) // 2
    assert found["groups"]["linux-ci"]["memory_mib"] == 256 // 2 + 2048
    assert found["groups"]["windows-dev"]["memory_mib"] == 4096
    # Each fits beside what always runs; all of them at once do not.
    total = sum(g["memory_mib"] for g in found["groups"].values())
    assert all(g["memory_mib"] <= found["headroom_mib"] for g in found["groups"].values())
    assert total > found["headroom_mib"] and found["together"] is False


def test_the_plan_says_how_each_workload_stops(facts):
    f, config = small_server(facts)
    plan = planned(
        f, config, **{"windows-dev": ON, "linux-ci": {**ON, "idle_minutes": 45}, "inference": {**ON, "stop": "manual"}, "agents": ON}
    )
    groups = plan["power"].value["groups"]
    assert groups["linux-ci"].why == "Stops by itself after 45 min without use."
    assert groups["windows-dev"].why == "Never stops by itself; you are told once it has gone 4 h without use."
    assert groups["inference"].why == "Runs until you stop it."
    assert "starting one may mean stopping another" in plan["power"].value["together"].why
    bare = strip_notes(plan)["power"]["groups"]
    assert (bare["linux-ci"]["stop"], bare["linux-ci"]["idle_minutes"]) == ("auto", 45)
    assert (bare["agents"]["stop"], bare["agents"]["title"]) == ("ask", "Workspace")


def test_nothing_on_demand_leaves_the_plan_as_it_was(facts):
    plan = strip_notes(build_plan(build_model(fixture_config("mid")), facts("mid")))
    assert "power" not in plan and not [g for g in plan["guests"].values() if "run" in g]
    assert power.on_demand(plan) == {}
    assert power.policy(plan) == {"groups": {}, "headroom_mib": 0}
    # A rule for a workload that is not planned changes nothing either.
    again = strip_notes(build_plan(build_model({**fixture_config("mid"), "power": {"media": ON}}), facts("mid")))
    assert again == plan


def test_a_workload_must_fit_beside_what_always_runs(facts):
    f, config = small_server(facts)
    assert errors(f, config, **{"windows-dev": ON}) == [
        "memory: Windows desktop needs 4096 MiB while it runs, and with everything else on demand off 797 MiB is left "
        "beside the guests that always run (the host has 15645 MiB minus a 3072 MiB host reserve)"
    ]


def test_rules_that_cannot_work_are_refused_with_what_to_set(facts):
    f, config = small_server(facts)
    found = errors(f, config, core=ON, nope=ON, **{"windows-ci": ON, "windows-dev": {**ON, "stop": "auto"}})
    assert "power.core: core guests always run" in found
    assert "power.nope: no service has the workload 'nope'" in found
    assert "power.windows-ci.run: nothing to start or stop: windows-runner only makes clones" in found
    # Nothing inside Windows says whether it is in use.
    assert (
        "power.windows-dev.stop: auto needs a way to see that Windows desktop is in use, and a VM shows none; "
        "set power.windows-dev.busy.cpu_pct, or stop: ask"
    ) in found
    with pytest.raises(NullBillError, match="power.inference.idle_minutes: 2 is less than the minimum of 5"):
        build_model({**config, "power": {"inference": {**ON, "idle_minutes": 2}}})
    with pytest.raises(NullBillError, match="power.inference.run: 'sometimes' is not one of"):
        build_model({**config, "power": {"inference": {"run": "sometimes"}}})


def test_a_vm_may_stop_by_itself_once_its_cpu_says_when_it_is_in_use(facts):
    f, config = small_server(facts)
    rules = {"windows-dev": {**ON, "stop": "auto", "busy": {"cpu_pct": 15}}, "linux-ci": ON, "inference": ON, "agents": ON}
    plan = planned(f, config, **rules)
    assert strip_notes(plan)["power"]["groups"]["windows-dev"]["cpu_pct"] == 15
    assert power.policy(plan)["groups"]["windows-dev"]["cpu_pct"] == 15
    assert "cpu_pct" not in power.policy(plan)["groups"]["inference"]


def test_a_workload_the_catalog_has_no_rule_for_gets_one(facts):
    """A new workload (a game server, say) goes on demand from config alone."""
    config = fixture_config("mid")
    config["services"] = {
        "game": {"id": 40, "kind": "lxc", "zone": "services", "workload": "games", "floor": {"cores": 1, "memory_mib": 2048, "disk_gib": 8}}
    }
    config["intent"] = {"workloads": [*config["intent"]["workloads"], "games"]}
    plan = strip_notes(build_plan(build_model({**config, "power": {"games": ON}}), facts("mid")))
    assert plan["power"]["groups"]["games"] == {
        "title": "games",
        "guests": ["game"],
        "memory_mib": 1024,
        "stop": "ask",
        "idle_minutes": 120,
    }


def test_opentofu_leaves_on_demand_guests_off_and_never_starts_or_stops_them(facts):
    f, config = small_server(facts)
    model = build_model({**config, "power": {"windows-dev": ON, "linux-ci": ON, "inference": ON, "agents": ON}})
    plan = build_plan(model, f)
    resources = rendered(plan, model)
    containers, vms = resources["proxmox_virtual_environment_container"], resources["proxmox_virtual_environment_vm"]
    for name in ("inference", "model-gateway", "workspace", "runner-dispatcher"):
        assert (containers[name]["started"], containers[name]["start_on_boot"]) == (False, False), name
        assert "started" in containers[name]["lifecycle"]["ignore_changes"], name
    assert (vms["windows-dev"]["started"], vms["windows-dev"]["on_boot"]) == (False, False)
    assert "started" in vms["windows-dev"]["lifecycle"]["ignore_changes"]
    # What a guest ignored before, it still does.
    large = build_model(fixture_config("large"))
    always = rendered(build_plan(large, facts("large")), large)
    before = always["proxmox_virtual_environment_container"]["inference"]["lifecycle"]["ignore_changes"]
    assert set(before) < set(containers["inference"]["lifecycle"]["ignore_changes"])
    # Guests that always run start with the server, as before.
    assert containers["forgejo"].get("started", True) is True and containers["forgejo"]["start_on_boot"] is True
    assert "started" not in containers["forgejo"].get("lifecycle", {}).get("ignore_changes", [])
    # A pool's golden image is a template; nothing about it changes.
    assert "started" not in vms["linux-runner"].get("lifecycle", {}).get("ignore_changes", [])


def rendered(plan, model):
    return json.loads(tofu.render_tofu(plan, model)["main.tf.json"])["resource"]


def on_demand_mid(facts, *workloads):
    model = build_model({**fixture_config("mid"), "power": dict.fromkeys(workloads, ON)})
    return build_plan(model, facts("mid")), model


def nb_power(plan, running=(), ticked_ago=20, used=None):
    """What `nb-power status --json` answers on a host where only these on-demand guests run."""
    policy = power.policy(plan)
    groups = []
    for name, group in policy["groups"].items():
        guests = [
            {"name": g["name"], "kind": g["kind"], "state": "running" if g["name"] in running else "stopped"} for g in group["guests"]
        ]
        switches = [g for g in guests if g["kind"] != "vm-pool"]
        on = [g for g in switches if g["state"] == "running"]
        state = "on" if len(on) == len(switches) else "partly" if on else "off"
        groups.append({"name": name, "title": group["title"], "state": state, "memory_mib": group["memory_mib"], "guests": guests})
    took = sum(g["memory_mib"] for g in groups if g["state"] != "off")
    return {
        "now": 1_800_000_000,
        "ticked": 1_800_000_000 - ticked_ago,
        "headroom_mib": policy["headroom_mib"],
        "used_mib": took if used is None else used,
        "groups": groups,
    }


def host(plan, model, status, onboot=(), policy=None, timer="active"):
    """A correct host for verify whose nb-power answers `status`; a command
    into a guest that is off fails as pct does."""
    bare = strip_notes(plan)
    base = firewall(plan, model)
    off = {str(bare["guests"][n]["vmid"]) for n in power.off_guests(status) if bare["guests"][n]["kind"] == "lxc"}
    reached = []

    def run(target, command):
        words = command.split()
        if words[:2] == ["pct", "exec"]:
            reached.append(words[2])
            if words[2] in off:
                return 255, "", f"CT {words[2]} not running\n"
        if command == f"{power.COMMAND} status --json":
            return (0, json.dumps(status), "") if status is not None else (127, "", "not found")
        if command == f"systemctl is-active {power.TIMER}":
            return (0, "active\n", "") if timer == "active" else (3, f"{timer}\n", "")
        if command == f"cat {shlex.quote(power.POLICY_FILE)}":
            return 0, power.render_policy(plan) if policy is None else policy, ""
        if words[0] in ("pct", "qm") and words[1] == "config":
            return 0, f"cores: 2\nonboot: {1 if int(words[2]) in onboot else 0}\nmemory: 2048\n", ""
        return base(target, command)

    run.reached = reached
    run.off = off
    return run


def verify(plan, model, run):
    return run_verify(plan, model, TARGET, run, lambda h, p: "timeout", lambda n: PUBLIC)


def test_verify_skips_what_is_off_and_never_reaches_into_it(facts):
    plan, model = on_demand_mid(facts, "inference", "agents")
    run = host(plan, model, nb_power(plan))
    results = verify(plan, model, run)
    by = statuses(results)
    assert by["FAIL"] == [] and by["DEGRADED"] == []
    assert not set(run.reached) & run.off
    skipped = {r.name: r.detail for r in by["SKIP"]}
    why = "Local AI models is off (it runs on demand); start it on the page, under Services, to check it"
    # Probes to it, its own checks, and its zone, where nothing else runs.
    assert skipped["egress-proxy (services) is refused inference tcp/8080"] == why
    assert skipped["the local model answers through the model gateway with its key"] == why
    assert skipped["zone inference"] == "no container to probe from while Local AI models is off"
    asleep = "Workspace is off (it runs on demand); start it on the page, under Services, to check it"
    assert skipped["vpn (access) reaches workspace tcp/22"] == asleep
    assert skipped["zone build"] == "no container to probe from while Workspace is off"
    # The agent's SSH check needs an agent-managed guest that runs.
    passed = [r.name for r in by["PASS"]]
    assert skipped["agent reaches workspace over SSH as agent"] == asleep and "agent SSH to router is refused" in skipped
    assert not [n for n in passed if " tcp/" in n and {"workspace", "inference", "model-gateway"} & set(n.split())]
    assert "the host starts and stops what runs on demand" in passed
    assert "the host's on-demand rules are the plan's" in passed
    assert "inference does not start with the server" in passed and "workspace does not start with the server" in passed
    assert "what runs on demand fits the memory left for it" in passed


def test_verify_checks_what_is_on_as_ever(facts):
    plan, model = on_demand_mid(facts, "inference", "agents")
    on = ["inference", "model-gateway", "workspace"]
    by = statuses(verify(plan, model, host(plan, model, nb_power(plan, running=on))))
    always, model_always = on_demand_mid(facts)
    before = statuses(verify(always, model_always, firewall(always, model_always)))
    assert by["FAIL"] == []
    assert {r.name for r in before["PASS"]} < {r.name for r in by["PASS"]}
    assert [r.name for r in by["SKIP"]] == [r.name for r in before["SKIP"]]


def test_verify_finds_a_host_that_does_not_keep_the_rules(facts):
    plan, model = on_demand_mid(facts, "inference")
    vmid = strip_notes(plan)["guests"]["inference"]["vmid"]

    def found(**kw):
        status = kw.pop("status", nb_power(plan))
        return {r.name: (r.status, r.detail) for r in verify(plan, model, host(plan, model, status, **kw))}

    name = "the host starts and stops what runs on demand"
    assert found(timer="inactive")[name] == ("FAIL", "nb-power.timer is inactive; run setup")
    assert found(status=nb_power(plan, ticked_ago=1200))[name] == ("FAIL", "nb-power last ran 20 min ago")
    gone = found(status=None)
    assert gone[name] == ("FAIL", "/usr/local/sbin/nb-power status does not answer; run setup")
    assert gone["what runs on demand fits the memory left for it"] == ("SKIP", "nb-power does not answer")
    assert found(policy="{}\n")["the host's on-demand rules are the plan's"] == (
        "FAIL",
        "/etc/nullbill/power.json differs from the plan; run setup",
    )
    assert found(onboot={vmid})["inference does not start with the server"] == ("FAIL", "it starts at boot (onboot: 1); run setup")
    # Started around nb-power, say in Proxmox: more runs than the plan counts.
    headroom = power.policy(plan)["headroom_mib"]
    over = found(status=nb_power(plan, running=["inference", "model-gateway"], used=headroom + 300))
    assert over["what runs on demand fits the memory left for it"] == (
        "DEGRADED",
        "Local AI models run at once, 300 MiB more than the plan counts; stop one on the page",
    )


def test_without_a_target_the_on_demand_checks_are_skipped_too(facts):
    plan, model = on_demand_mid(facts, "inference")
    results = run_verify(plan, model, None, None)
    names = [r.name for r in results if r.status == "SKIP"]
    assert "the host starts and stops what runs on demand" in names and "inference does not start with the server" in names
    assert not [r for r in results if r.status == "PASS"]


def test_probes_from_or_to_a_guest_that_is_off_are_marked(facts):
    plan, model = on_demand_mid(facts, "linux-ci")
    off = {"runner-dispatcher": "Linux CI runners", "linux-runner": "Linux CI runners"}
    marked = [p for p in probes(plan, model, PUBLIC, off)[0] if p.skip]
    # The dispatcher holds a PVE token, so its own reach is probed: skipped with it.
    assert [p for p in marked if p.name.startswith("runner-dispatcher (services) ")]
    assert [p for p in marked if p.name.endswith(" runner-dispatcher tcp/8443")]
    assert all("runner-dispatcher" in p.name for p in marked)
    assert not [p for p in probes(plan, model, PUBLIC)[0] if p.skip]


def test_the_agents_ssh_check_uses_a_guest_that_runs(facts):
    from nullbill import agent

    plan = strip_notes(on_demand_mid(facts, "inference", "agents")[0])
    assert agent.ssh_guest(plan) == "workspace"
    assert agent.ssh_guest(plan, {"workspace": "Workspace"}) == "inference"
    assert agent.ssh_guest(plan, {"workspace": "Workspace", "inference": "Local AI models"}) == "workspace"
