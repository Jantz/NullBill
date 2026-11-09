"""verify's checks of the local model and the model gateway, and the
container's device entries apply sets."""

from conftest import fixture_config, service_answers

from nullbill import llm, probe
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

TARGET = "root@pve.internal"


def checked(facts, cls="mid", wrong=""):
    plan = build_plan(build_model(fixture_config(cls)), facts(cls))
    answers = service_answers(plan, wrong)
    results = llm.check(plan, TARGET, lambda target, command: (0, answers[command], ""))
    return plan, {r.name: (r.status, r.detail) for r in results}


def test_a_working_model_and_gateway_pass(facts):
    version, _ = llm.nvidia_pin()
    _, results = checked(facts)
    assert results == {
        f"the host runs NVIDIA's driver {version}": ("PASS", ""),
        "the local model sits in the graphics card's memory": ("PASS", ""),
        "the local model answers through the model gateway with its key": ("PASS", ""),
        "the model gateway refuses a wrong key": ("PASS", ""),
    }


def test_a_gateway_that_lets_a_wrong_key_through_fails(facts):
    _, results = checked(facts, wrong="the model gateway refuses a wrong key")
    assert results["the model gateway refuses a wrong key"] == ("FAIL", "nope")


def test_the_gateway_is_asked_from_inside_its_guest_with_its_own_key(facts):
    plan = strip_notes(build_plan(build_model(fixture_config("mid")), facts("mid")))
    commands = {label: command for label, command, _ in llm.commands(plan)}
    ask = commands["the local model answers through the model gateway with its key"]
    assert ask.startswith(f"pct exec {plan['guests']['model-gateway']['vmid']} -- bash -c ")
    assert "/etc/litellm/env" in ask and "--cacert /etc/nullbill/tls/root_ca.crt" in ask
    # Most of the model file must be in VRAM.
    assert f"s >= {plan['inference']['model_mib'] * 9 // 10}" in commands["the local model sits in the graphics card's memory"]


def test_without_local_models_nothing_is_checked_or_skipped(facts):
    plan = build_plan(build_model(fixture_config("small")), facts("small"))
    assert llm.check(plan, TARGET, None) == [] and llm.skipped(plan, "no target") == []


def test_the_container_gets_each_card_and_the_shared_nodes_as_group_video():
    assert llm.wanted_devices([1, 0]) == [
        "/dev/nvidia0,gid=44,mode=0660",
        "/dev/nvidia1,gid=44,mode=0660",
        "/dev/nvidiactl,gid=44,mode=0660",
        "/dev/nvidia-uvm,gid=44,mode=0660",
        "/dev/nvidia-uvm-tools,gid=44,mode=0660",
    ]
    assert llm.current_devices("arch: amd64\ndev0: /dev/nvidia0,gid=44\ndev3: /dev/nvidiactl\nmp0: x\n") == {
        0: "/dev/nvidia0,gid=44",
        3: "/dev/nvidiactl",
    }


def test_a_model_busy_with_another_answer_is_degraded_not_failed(facts):
    plan = build_plan(build_model(fixture_config("mid")), facts("mid"))
    answers = service_answers(plan, "")
    ask = next(c for label, c, _ in llm.commands(strip_notes(plan)) if label.startswith("the local model answers"))
    answers[ask] = llm.BUSY + "\n"
    results = {r.name: r.status for r in llm.check(plan, TARGET, lambda target, command: (0, answers[command], ""))}
    assert results["the local model answers through the model gateway with its key"] == "DEGRADED"
    # curl gives up before ssh_run would, and says so in the guest.
    assert f"--max-time {llm.ANSWER_SECONDS} " in ask and llm.ANSWER_SECONDS < probe.SSH_TIMEOUT_S
    assert 'if [ "$rc" = 28 ]; then echo busy' in ask
