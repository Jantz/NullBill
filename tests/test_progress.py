"""The page's reading of a job's log: stages, what runs now, why it stopped."""

import time

from nullbill.apply import STAGE_MARK, STAGE_TIME, STAGES
from nullbill.web.progress import PREPARE, advice, sentence, summarize

T0 = time.mktime(time.strptime("2026-11-07 18:00:00", STAGE_TIME))


def mark(name, seconds):
    return f"{STAGE_MARK}{name}, {time.strftime(STAGE_TIME, time.localtime(T0 + seconds))}"


def times(now, ended=None):
    return {"started": T0 - 2, "now": T0 + now, "ended": None if ended is None else T0 + ended, "output": T0 + now - 3}


def by_name(view):
    return {s["name"]: s for s in view["steps"]}


def test_a_finished_apply_has_every_stage_done_with_its_time():
    lines = []
    for i, (name, _) in enumerate(STAGES):
        lines += [mark(name, 10 * i), f"{name}: something"]
    lines += ["PASS     lan: 8006 closed", "SKIP     agent: no credential", "DEGRADED backups: no second disk"]
    view = summarize("apply", "\n".join(lines) + "\n", "done", times(300, 200))
    steps = view["steps"]
    assert [s["name"] for s in steps] == [PREPARE] + [n for n, _ in STAGES]
    assert all(s["status"] == "done" for s in steps)
    assert steps[0]["seconds"] == 2 and steps[1]["seconds"] == 10 and steps[-1]["seconds"] == 200 - 10 * (len(STAGES) - 1)
    assert view["verify"]["counts"] == {"PASS": 1, "FAIL": 0, "SKIP": 1, "DEGRADED": 1}
    assert view["verify"]["problems"] == [{"status": "DEGRADED", "text": "backups: no second disk"}]
    assert "error" not in view


def test_a_running_apply_names_its_stage_and_the_task_it_runs():
    log = "\n".join(
        [
            mark("render", 0),
            "render: 30 files in /var/lib/nullbill/build",
            mark("tailnet", 1),
            mark("host", 2),
            "host: host.yml on PVE host",
            "PLAY [pve] *******",
            "TASK [host_baseline : install auditd] *********",
            "ok: [pve]",
        ]
    )
    view = summarize("apply", log, "running", times(30))
    steps = by_name(view)
    assert steps["render"]["status"] == steps["tailnet"]["status"] == "done"
    assert steps["host"]["status"] == "running" and steps["host"]["seconds"] == 28
    assert steps["tofu"]["status"] == "waiting" and steps["tofu"]["seconds"] is None
    assert view["activity"] == "Install auditd (host_baseline)"
    assert view["quiet_s"] == 3


def test_a_refused_tailnet_policy_says_where_it_stopped_and_what_to_do():
    log = "\n".join(
        [
            mark("render", 0),
            "render: 30 files in /var/lib/nullbill/build",
            mark("tailnet", 1),
            'nullbill: apply stopped at tailnet: Tailscale refused the policy: group not found: "group:nb-default"',
        ]
    )
    view = summarize("apply", log, "failed", times(5, 5))
    steps = by_name(view)
    assert steps["render"]["status"] == "done" and steps["tailnet"]["status"] == "failed"
    assert steps["host"]["status"] == "waiting"
    error = view["error"]
    assert error["stage"] == "tailnet" and error["stage_text"] == "Put the Tailscale access policy in place"
    assert error["message"] == 'Tailscale refused the policy: group not found: "group:nb-default".'
    assert "refused it" in error["advice"]


def test_a_failed_ansible_task_is_shown_with_its_message_not_an_exit_code():
    log = "\n".join(
        [
            mark("render", 0),
            mark("router", 5),
            "TASK [router : check the ruleset] ******",
            'fatal: [router]: FAILED! => {"changed": false, "msg": "an earlier one", "rc": 1}',
            "...ignoring",
            "TASK [router : install nftables] ******",
            'fatal: [router]: FAILED! => {"changed": false, "msg": "Failed to update apt cache: W:Temporary failure '
            "resolving 'deb.debian.org'\"}",
            "PLAY RECAP *****",
            "nullbill: apply stopped at router: router.yml failed on router (exit 2)",
        ]
    )
    error = summarize("apply", log, "failed", times(9, 9))["error"]
    assert error["task"] == {
        "task": "install nftables (router)",
        "host": "router",
        "detail": "Failed to update apt cache: W:Temporary failure resolving 'deb.debian.org'",
    }
    assert error["message"] == "router.yml failed on router (exit 2)."
    assert "internet connection" in error["advice"]


def test_a_plan_that_does_not_fit_stops_before_the_first_stage():
    log = (
        "nullbill: memory: planned guests need 23040 MiB at their floors; the host has 15645 MiB minus a 3584 MiB host reserve\n"
        "nullbill: intent asks for inference, but no host-shared GPU budgets VRAM to it\n"
    )
    view = summarize("apply", log, "failed", times(1, 1))
    assert view["steps"][0] == {"name": PREPARE, "text": "Read your choices and make the plan", "status": "failed", "seconds": 3}
    assert all(s["status"] == "waiting" for s in view["steps"][1:])
    error = view["error"]
    assert error["message"].startswith("Memory: planned guests need 23040 MiB")
    assert error["more"] == ["Intent asks for inference, but no host-shared GPU budgets VRAM to it."]
    assert "more memory" in error["advice"]


def test_failed_checks_are_listed():
    log = "PASS     zone services: dns\nFAIL     lan: 8006 answers from the LAN\nPASS 1, FAIL 1, SKIP 0, DEGRADED 0\n"
    view = summarize("verify", log, "failed", times(4, 4))
    assert "steps" not in view
    assert view["verify"]["counts"] == {"PASS": 1, "FAIL": 1, "SKIP": 0, "DEGRADED": 0}
    assert view["verify"]["problems"] == [{"status": "FAIL", "text": "lan: 8006 answers from the LAN"}]
    assert view["error"]["message"] == "1 check failed."


def test_a_job_that_ends_silently_still_says_so():
    view = summarize("probe", "", "failed", times(1, 1))
    assert view["error"]["message"].startswith("It ended without saying why")


def test_advice_by_cause_then_by_stage():
    assert "auth key" in advice("Tailscale did not log in (NeedsLogin); make a new one-time auth key")
    assert "OAuth client" in advice("tailnet: the OAuth client lacks a scope (x)")
    assert advice("something new", "tofu").startswith("Proxmox refused a change")
    assert advice("something new").startswith("Run setup again")
    assert sentence("wait until the running job finishes") == "Wait until the running job finishes."


def test_a_proxy_refusal_says_the_site_is_not_allowed():
    text = "Request failed: <urlopen error Tunnel connection failed: 403 Forbidden>"
    assert advice(text, "ca").startswith("NullBill's internet proxy refused a site")
