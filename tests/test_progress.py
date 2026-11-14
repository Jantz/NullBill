"""The page's reading of a job's log: stages, what runs now, why it stopped."""

import time

import pytest

from nullbill.apply import STAGE_MARK, STAGE_TIME, STAGES
from nullbill.web.progress import PREPARE, advice, only, sentence, summarize, telling

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
    view = summarize("apply", "\n".join(lines) + "\n", "done", times(400, 300))
    steps = view["steps"]
    assert [s["name"] for s in steps] == [PREPARE] + [n for n, _ in STAGES]
    assert all(s["status"] == "done" for s in steps)
    assert steps[0]["seconds"] == 2 and steps[1]["seconds"] == 10 and steps[-1]["seconds"] == 300 - 10 * (len(STAGES) - 1)
    assert view["verify"]["counts"] == {"PASS": 1, "FAIL": 0, "SKIP": 1, "DEGRADED": 1}
    assert view["verify"]["problems"] == [
        {"status": "DEGRADED", "text": "backups: no second disk", "name": "backups", "detail": "no second disk"}
    ]
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
    assert error["message"] == 'The step "Install nftables" failed on router.'
    assert error["items"] == [
        {"title": None, "text": "Failed to update apt cache: W:Temporary failure resolving 'deb.debian.org'.", "detail": "", "advice": None}
    ]
    assert "internet connection" in error["advice"]


# What ansible-core 2.19 prints for a failed command with apply's
# ANSIBLE_CALLBACK_RESULT_FORMAT=yaml (made with the pinned version; the
# Origin line's path shortened).
YAML_FAILURE = """\
TASK [forgejo : Install the packages Forgejo needs] ****************************
[ERROR]: Task failed: Module failed: non-zero return code
Origin: /opt/nullbill/ansible/roles/forgejo/tasks/main.yml:5:7

3   gather_facts: false
4   tasks:
5     - name: Install the packages Forgejo needs
        ^ column 7

fatal: [forgejo]: FAILED! =>{SPACE}
    changed: false
    cmd: |-
        apt-get -y install nginx

        exit 100
    delta: '0:00:00.002876'
    msg: non-zero return code
    rc: 100
    stderr: |-
        E: Failed to fetch http://deb.debian.org/debian/pool/main/n/nginx/nginx_1.22.1-9_amd64.deb  403  Forbidden [IP: 10.20.0.3 3128]
        E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?
    stderr_lines: <omitted>
    stdout: |-
        Reading package lists...

        Building dependency tree...
    stdout_lines: <omitted>

PLAY RECAP *********************************************************************
forgejo                    : ok=0    changed=0    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0
""".replace("{SPACE}", " ")


def test_a_failed_command_leads_with_what_the_program_said_and_keeps_its_output_apart():
    log = "\n".join([mark("forgejo", 0), YAML_FAILURE, "nullbill: apply stopped at forgejo: forgejo.yml failed on forgejo (exit 2)"])
    error = summarize("apply", log, "failed", times(9, 9))["error"]
    assert error["message"] == 'The step "Install the packages Forgejo needs" failed on forgejo.'
    (item,) = error["items"]
    assert item["title"] is None
    assert item["text"] == (
        "E: Failed to fetch http://deb.debian.org/debian/pool/main/n/nginx/nginx_1.22.1-9_amd64.deb  403  Forbidden"
        " [IP: 10.20.0.3 3128] (exit 100)."
    )
    assert item["detail"].splitlines() == [
        "E: Failed to fetch http://deb.debian.org/debian/pool/main/n/nginx/nginx_1.22.1-9_amd64.deb  403  Forbidden [IP: 10.20.0.3 3128]",
        "E: Unable to fetch some archives, maybe run apt-get update or try with --fix-missing?",
    ]
    assert error["advice"].startswith("NullBill's internet proxy refused a site")


def test_a_wrapped_error_leads_with_its_sentence_not_its_last_line():
    # The qBittorrent build on Debian 13 (2026-11-14): the item read
    # "provide. (exit 1)." because CMake wraps its message.
    cmake = [
        "CMake Error in src/base/CMakeLists.txt:",
        '  Imported target "OpenSSL::Crypto" includes non-existent path',
        "",
        '    "/include"',
        "",
        "  in its INTERFACE_INCLUDE_DIRECTORIES.  Possible reasons include:",
        "",
        "  * The installation package was faulty and references files it does not",
        "  provide.",
        "",
        "CMake Generate step failed.  Build files cannot be regenerated correctly.",
    ]
    stderr = "\n".join(f"        {line}" if line else "" for line in cmake)
    failure = YAML_FAILURE.replace("apt-get -y install nginx", "cmake -S src -B build").replace("rc: 100", "rc: 1")
    start, end = failure.index("    stderr: |-\n") + len("    stderr: |-\n"), failure.index("    stderr_lines")
    stopped = "nullbill: apply stopped at forgejo: forgejo.yml failed on forgejo (exit 2)"
    log = "\n".join([mark("forgejo", 0), failure[:start] + stderr + "\n" + failure[end:], stopped])
    (item,) = summarize("apply", log, "failed", times(9, 9))["error"]["items"]
    assert item["text"] == (
        'CMake Error in src/base/CMakeLists.txt: Imported target "OpenSSL::Crypto" includes non-existent path'
        ' "/include" in its INTERFACE_INCLUDE_DIRECTORIES (exit 1).'
    )


@pytest.mark.parametrize(
    ("printed", "line"),
    [
        (["Traceback (most recent call last):", '  File "x", line 1', "ValueError: bad"], "ValueError: bad"),
        (
            [
                "Traceback (most recent call last):",
                '  File "/usr/lib/python3.13/urllib/request.py", line 533, in error',
                "    return self._call_chain(*args)",
                "urllib.error.HTTPError: HTTP Error 403: Forbidden",
            ],
            "urllib.error.HTTPError: HTTP Error 403: Forbidden",
        ),
        (["x.c:1:2: error: expected ';'", "make: *** [all] Error 1"], "x.c:1:2: error: expected ';'"),
        (["nothing named", "the last line"], "the last line"),
    ],
)
def test_the_telling_line_of_a_programs_output(printed, line):
    assert telling(printed) == line


def test_each_item_of_a_loop_that_failed_is_an_item_of_its_own():
    log = "\n".join(
        [
            mark("forgejo", 0),
            "TASK [forgejo : check the release] ****",
            "failed: [forgejo] (item=old) => ",
            "    msg: not this one",
            "...ignoring",
            "TASK [forgejo : fetch the pinned release] ****",
            "failed: [forgejo] (item=forgejo-12.0.1-linux-amd64) => ",
            "    ansible_loop_var: item",
            "    msg: 'Request failed: <urlopen error Tunnel connection failed: 502 Bad Gateway>'",
            "    url: https://example.invalid/forgejo-12.0.1-linux-amd64",
            'failed: [forgejo] (item=forgejo-12.0.1-linux-amd64.sha256) => {"changed": false, "msg": "HTTP Error 404: Not Found"}',
            "",
            "PLAY RECAP ****",
            "nullbill: apply stopped at forgejo: forgejo.yml failed on forgejo (exit 2)",
        ]
    )
    error = summarize("apply", log, "failed", times(9, 9))["error"]
    assert error["message"] == 'The step "Fetch the pinned release" failed on forgejo, for 2 items.'
    assert [(i["title"], i["text"]) for i in error["items"]] == [
        ("forgejo-12.0.1-linux-amd64", "Request failed: <urlopen error Tunnel connection failed: 502 Bad Gateway>."),
        ("forgejo-12.0.1-linux-amd64.sha256", "HTTP Error 404: Not Found."),
    ]


def test_opentofu_errors_are_items_with_the_guest_they_name():
    log = "\n".join(
        [
            mark("tofu", 0),
            "proxmox_virtual_environment_vm.router: Creating...",
            "",
            "Error: error creating container: received an HTTP 500 response - Reason: unable to create CT 2014",
            "",
            "  with proxmox_virtual_environment_container.forgejo,",
            "  on main.tf.json line 233, in resource.proxmox_virtual_environment_container.forgejo:",
            " 233:       }",
            "",
            "",
            "Error: error waiting for VM clone: All attempts fail:",
            "#1: error retrieving task status: received an HTTP 596 response - Reason: Connection timed out",
            "",
            "  with proxmox_virtual_environment_vm.router,",
            "  on main.tf.json line 412, in resource.proxmox_virtual_environment_vm.router:",
            " 412:       }",
            "",
            "The task did not finish within 1800 seconds.",
            "",
            "nullbill: apply stopped at tofu: tofu apply failed (exit 1)",
        ]
    )
    error = summarize("apply", log, "failed", times(9, 9))["error"]
    assert error["message"] == "OpenTofu stopped with 2 errors."
    first, second = error["items"]
    assert first["title"] == "Container forgejo" and first["detail"] == ""
    assert first["text"] == "Error creating container: received an HTTP 500 response - Reason: unable to create CT 2014."
    assert second["title"] == "VM router" and second["text"] == "Error waiting for VM clone: All attempts fail."
    assert second["detail"].startswith("#1: error retrieving task status: received an HTTP 596 response")
    assert second["detail"].endswith("\n\nThe task did not finish within 1800 seconds.")
    # A node that answers late is not the internet connection: each cause gets its own advice.
    assert first["advice"] is None and second["advice"].startswith("Proxmox took too long to answer")
    assert error["advice"].startswith("Proxmox refused a change")


def test_a_traceback_is_said_to_be_nullbills_own_fault():
    log = "\n".join(
        [
            mark("ca", 0),
            "Traceback (most recent call last):",
            '  File "/opt/nullbill/src/nullbill/apply.py", line 1411, in stage_ca',
            '    token = issue["token"]',
            "KeyError: 'token'",
        ]
    )
    error = summarize("apply", log, "failed", times(9, 9))["error"]
    assert error["stage"] == "ca"
    assert error["message"] == "NullBill stopped with an error of its own: KeyError: 'token'."
    assert error["items"][0]["text"] == "" and error["items"][0]["detail"].startswith("Traceback")
    assert error["advice"].startswith("This is a fault in NullBill")


def test_a_plan_that_does_not_fit_stops_before_the_first_stage():
    log = (
        "nullbill: memory: planned guests need 23040 MiB at their floors; the host has 15645 MiB minus a 3584 MiB host reserve\n"
        "nullbill: intent asks for inference, but no host-shared GPU budgets VRAM to it\n"
    )
    view = summarize("apply", log, "failed", times(1, 1))
    assert view["steps"][0] == {"name": PREPARE, "text": "Read your choices and make the plan", "status": "failed", "seconds": 3}
    assert all(s["status"] == "waiting" for s in view["steps"][1:])
    error = view["error"]
    # Each problem is an item with what to do about it; none is the headline.
    assert error["message"] == "The plan has 2 problems."
    memory, gpu = error["items"]
    assert memory["text"].startswith("Memory: planned guests need 23040 MiB") and "more memory" in memory["advice"]
    assert gpu["text"] == "Intent asks for inference, but no host-shared GPU budgets VRAM to it."
    assert gpu["advice"].startswith("Untick Local AI models")
    assert error["advice"] == "Fix what the message names under Setup, then run setup again."


def test_failed_checks_are_listed():
    log = "PASS     zone services: dns\nFAIL     lan: 8006 answers from the LAN\nPASS 1, FAIL 1, SKIP 0, DEGRADED 0\n"
    view = summarize("verify", log, "failed", times(4, 4))
    assert "steps" not in view
    assert view["verify"]["counts"] == {"PASS": 1, "FAIL": 1, "SKIP": 0, "DEGRADED": 0}
    assert view["verify"]["problems"] == [
        {"status": "FAIL", "text": "lan: 8006 answers from the LAN", "name": "lan", "detail": "8006 answers from the LAN"}
    ]
    assert view["error"]["message"] == "1 check failed."


def test_checks_that_did_not_run_are_listed_with_why():
    log = "PASS     lan: 8006 closed\nSKIP     agent: no credential saved\nSKIP     backups are fresh\nDEGRADED backups: switched off\n"
    found = summarize("verify", log, "done", times(4, 4))["verify"]
    # A skip is no pass: the page says what was not checked.
    assert found["counts"] == {"PASS": 1, "FAIL": 0, "SKIP": 2, "DEGRADED": 1}
    assert found["skipped"] == [{"name": "agent", "detail": "no credential saved"}, {"name": "backups are fresh", "detail": ""}]
    assert [x["name"] for x in found["problems"]] == ["backups"]


def test_a_run_of_one_stage_lists_that_stage_and_is_not_called_setup():
    assert only("apply", ["apply", "--stage", "router"]) == {"name": "router", "text": "Set up the router"}
    # A full run, the weekly image build (its own kind of run) and a flag without a name are none.
    assert only("apply", ["apply"]) is None and only("apply", ["apply", "--stage"]) is None
    assert only("rebake-ci", ["apply", "--stage", "ci"]) is None and only("verify", ["verify"]) is None
    log = mark("router", 2) + "\nrouter: nothing to change\n"
    view = summarize("apply", log, "done", times(25, 25), "router")
    assert view["title"] == "Set up the router"
    # Not 23 stages of which 22 "wait" for ever.
    assert [(s["name"], s["status"]) for s in view["steps"]] == [(PREPARE, "done"), ("router", "done")]
    failed = summarize("apply", log + "error: stage router: it broke\n", "failed", times(25, 25), "router")
    assert [(s["name"], s["status"]) for s in failed["steps"]] == [(PREPARE, "done"), ("router", "failed")]


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


def test_a_refused_read_write_disk_points_at_its_card():
    """From plan, from apply's early check and from the host script alike."""
    for message in (
        "disks.ata-SYNTHETIC_HDD_0002.mode: partition 2 stays read-only: Windows did not shut it down fully",
        "nb-legacy-disk on the host: nb-legacy-disk: /dev/disk/by-id/x cannot be read-write: it holds a hibernated Windows",
        "nb-legacy-disk: the journal on it now (j1f63daad72c7) is not the one you said may be discarded (jb95e30db0b42)",
    ):
        assert "look at the disk under What to run" in advice(message, "tofu")


def test_a_run_nullbill_restarted_under_says_how_far_its_log_got():
    # 2026-11-14: every stage ran and 385 checks passed, then NullBill's own
    # Debian upgrade restarted it; the page said "It ended without saying why"
    # and "Look at the failing checks below" with none failing.
    lines = [mark(name, i) for i, (name, _) in enumerate(STAGES)]
    lines += ["PASS     router answers", "DEGRADED backups: switched off", "PASS     forgejo answers"]
    view = summarize("apply", "\n".join(lines), "failed", times(99, 99) | {"unrecorded": True})
    error = view["error"]
    assert error["message"] == (
        'NullBill restarted before this run wrote down how it ended; its log ends in "Run the checks", '
        "with 2 checks passed and 0 failed so far."
    )
    assert error["items"] == []
    assert error["advice"].startswith("Run the checks to see where the server stands")
