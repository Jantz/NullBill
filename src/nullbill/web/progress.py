"""What a job's log says, for the page: which of apply's stages ran, which
one runs now, and, when a job fails, why in plain words and what to do.

apply prints a line `--- stage <name>, <time>` before each stage
(apply.run_apply); everything after it, up to the next, is that stage's
output. A failure ends with the `nullbill: <message>` lines cli.main prints.
"""

import json
import re
import textwrap
import time

import yaml

from nullbill.apply import STAGE_MARK, STAGE_TIME, STAGES

# Before the first stage, apply reads the config and makes the plan.
PREPARE = "prepare"
STAGE_TEXT = {
    PREPARE: "Read your choices and make the plan",
    "render": "Write the configuration files",
    "tailnet": "Put the Tailscale access policy in place",
    "host": "Secure the Proxmox host",
    "tofu": "Create the networks, containers and VMs",
    "release": "Bring guests from before to Debian 13",
    "router": "Set up the router",
    "proxy": "Set up the internet proxy",
    "updates": "Turn on automatic security updates",
    "ca": "Set up the certificate authority",
    "postgres": "Set up the database",
    "monitoring": "Set up monitoring and alerts",
    "forgejo": "Set up Forgejo (Git and CI)",
    "inference": "Set up the local AI model and its gateway",
    "files": "Share your kept disks",
    "media": "Set up Jellyfin",
    "torrents": "Set up the download client",
    "games": "Set up the game servers",
    "ci": "Build the CI runner images",
    "agents": "Set up the coding agent",
    "logs": "Send logs to the log store",
    "backups": "Set up backups",
    "vpn": "Join your Tailscale network",
    "custom": "Set up your own services",
    "verify": "Run the checks",
    "upgrade": "Update NullBill",
}
# Which stages each kind of job runs, in order.
JOB_STAGES = {
    "apply": [PREPARE] + [name for name, _ in STAGES],
    "rebake-ci": [PREPARE, "ci"],
    "close-lan": [PREPARE, "host"],
    "open-lan": [PREPARE, "host"],
}
JOB_TEXT = {
    "probe": "Look at the hardware",
    "apply": "Set up the server",
    "verify": "Run the checks",
    "upgrade": "Update NullBill",
    "close-lan": "Close the home network",
    "keep-lan-closed": "Keep the home network closed",
    "open-lan": "Open the home network again",
    "rebake-ci": "Rebuild the CI runner images",
    "remove-guests": "Delete what you confirmed and set up the server",
}
STAGE_LINE = re.compile(re.escape(STAGE_MARK) + r"([a-z-]+), (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)$")
TASK = re.compile(r"^TASK \[(?:([^:\]]+) : )?(.+?)\] \**$")
# One failed result: `fatal: [host]: FAILED! => ...`, or per item of a loop
# `failed: [host] (item=...) => ...`.
FATAL = re.compile(r"^(?:fatal|failed): \[([^\]]+)\](?: \(item=(.*?)\))?(?:: (?:FAILED|UNREACHABLE)!)? =>\s?(.*)$")
PLAYBOOK_FAILED = re.compile(r"\S+\.yml failed on .+ \(exit \d+\)$")
TOFU_WITH = re.compile(r"^\s+with (\S+?),?$")
TOFU_SOURCE = re.compile(r"^\s+on \S+ line \d+|^\s*\d+: ")
TOFU_KINDS = {"proxmox_virtual_environment_container": "Container", "proxmox_virtual_environment_vm": "VM"}
TRACEBACK = "Traceback (most recent call last):"
RAISED = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\b.*$")
# What a module says when it has nothing to say: the program's own words
# are in stderr then.
NO_MESSAGE = {"", "non-zero return code", "the command exited with a non-zero return code."}
# Result.line(): the status padded to eight characters, then the check.
VERIFY = re.compile(r"^(PASS|FAIL|SKIP|DEGRADED) {1,5}(?<=^.{9})(.+)$")
ERROR_PREFIX = "nullbill: "
STOPPED = re.compile(r"apply stopped at ([a-z-]+): (.*)$", re.S)

STOPPED_ADVICE = (
    "What finished before you stopped it stays done. Start it again with the button below; the step it was "
    "in the middle of runs again. If it hung on a download, press Update NullBill on the Updates page first, in case a fix for it "
    "is out."
)

# Known causes, by what the message says: what to do about each. The first
# match wins. Anything else gets the stage's own advice.
ADVICE = [
    (
        r"more memory than the least is set for",
        "Some guests are given more memory than the least they take. Under Setup, What to run, open Size in their rows "
        "(Core servers for NullBill's own) and give them less, or untick a workload, and press Save and plan.",
    ),
    (
        r"planned guests need \d+ MiB at their floors|guests need more memory",
        "Your choices need more memory than this server has. Under Setup, What to run, untick a workload "
        "(or the on-site screen) and press Save and plan until Plan shows no problems.",
    ),
    (
        r"storage \S+ has \d+ GiB free|guests need \d+ GiB",
        "Your choices need more disk space than Proxmox's storage has free. Under Setup, What to run, untick a workload "
        "and press Save and plan, or free space in Proxmox and read the hardware again.",
    ),
    (
        r"needs \d+ MiB while it runs",
        "One of the things you set to run on demand does not fit beside what always runs. Under Setup, What to run, "
        "set something else to run on demand, or untick a workload, and press Save and plan until Plan shows no problems.",
    ),
    (
        r"setup has to start .* to configure",
        "Setup starts what runs on demand to bring it up to date, and the server has no memory free for it right now. "
        "On Services, stop one of the things that are on, then run setup again; what it starts is stopped again when it ends.",
    ),
    (
        r"the host has no nb-power yet",
        "Run setup in full once, from Setup, Review and run: it installs what starts and stops the things that run on demand.",
    ),
    (r"could not read VRAM", "Under Setup, What to run, give the graphics card's memory in GB, as on its box, then press Save and plan."),
    (
        r"stays read-only: |cannot be read-write|not the one you said may be discarded",
        "The disk is as it was, still read-only, and nothing on it was changed. Press Read the hardware under Setup, then look "
        "at the disk under What to run: it says how Windows left it and what to do. Or untick Let me change files on it "
        "there. Then press Save and plan and run setup again.",
    ),
    (r"no host-shared GPU budgets VRAM", "Untick Local AI models under Setup, What to run, then press Save and plan."),
    (
        r"nouveau driver holds the graphics card",
        "Restart the server once (in Proxmox: the node, then Reboot), then press Run setup again. "
        "The card's old driver lets go of it only at a restart.",
    ),
    (
        r"NVIDIA driver did not load|Secure Boot",
        "The graphics card's driver would not load, most likely because Secure Boot is on. Turn Secure Boot off in the "
        "server's BIOS, start it again and run setup again (docs/runbooks/local-models.md).",
    ),
    (
        r"llama\.cpp finds no GPU|does not list the card",
        "The AI container cannot see the graphics card. Restart the server once and run setup again; if it stops "
        "here again, copy the details and report them.",
    ),
    (
        r"no render node for the integrated graphics",
        "Jellyfin was to use the processor's graphics, but the server shows none now. Read the hardware again under Setup "
        "and run setup again; Jellyfin then transcodes on the processor itself.",
    ),
    (
        r"setup would delete .* nothing was deleted",
        "Nothing was deleted. To keep what it names, tick it again under Setup, What to run, press Save and plan, then run setup. "
        "To delete it with everything on its disk, press Delete and run setup on the Setup page, under Build.",
    ),
    (
        r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed",
        "A VM answered with a new SSH key, and setup could not read that key inside the VM through Proxmox, "
        "because the VM's guest agent did not answer. Run setup again in a few minutes. "
        "If it stops here again, copy the details and report them.",
    ),
    (
        r"Jellyfin refuses nullbill-admin|first-run setup was done without NullBill",
        "Setup cannot sign in to Jellyfin as nullbill-admin. If you changed its password in Jellyfin, change it back, "
        "or run setup once with the new one in the Jellyfin guest's /etc/nullbill/jellyfin-admin-password (docs/runbooks/media.md).",
    ),
    (
        r"signatures.*wipe|has signatures",
        "A disk holds data. Under Setup, What to run, choose what to do with it, and type the end of its name to erase it.",
    ),
    (r"target\.ssh is required|config\.yaml.*(missing|not found)|No such file.*config\.yaml", "Save your choices under Setup first."),
    (r"facts\.json|no probe|probe the server", "Press Read the hardware under Setup first."),
    (r"another job is still running", "Wait until the running job finishes; its progress shows on this page."),
    (
        r"OAuth client lacks a scope",
        "In the Tailscale admin console, make a new OAuth client with Policy file and DNS, both write "
        "(Tailscale cannot add scopes to one you have), and save it under Access.",
    ),
    (
        r"Tailscale.*\b(401|403)\b|invalid_client|unauthorized",
        "Tailscale did not accept the OAuth client. Check the client ID and secret, save them again under Access, then run setup again.",
    ),
    (
        r"Tailscale refused the policy",
        "Tailscale checked the access policy NullBill made and refused it. Nothing on the server changed. "
        "This is a NullBill bug unless you edited the policy: copy the details and report them.",
    ),
    (r"free plan allows", "Remove tagged devices you no longer use in the Tailscale admin console, then run setup again."),
    (
        r"Tailscale did not log in|auth key",
        "In Tailscale, generate a new one-time auth key tagged tag:nb-vpn, paste it under Access, and run setup again.",
    ),
    (r"Tailscale's API did not answer", "The server could not reach Tailscale. Check its internet connection and run setup again."),
    (
        r"Windows ISO",
        "Upload the Windows 11 ISO in Proxmox (local storage, ISO Images, keep Win in its name), or untick Windows under Setup.",
    ),
    (
        r"Tunnel connection failed: 403|403 Forbidden.*proxy|proxy.*403 Forbidden|403\s+Forbidden \[IP: \S+ 3128\]",
        "NullBill's internet proxy refused a site this guest needs: the site is not on its allowed list. "
        "Update NullBill on the Updates page and run setup again; if it stops here again, copy the details and report them.",
    ),
    (
        # pveproxy's own code for a node that took too long, which is not
        # the internet connection the "timed out" rule below speaks of.
        r"HTTP 59\d response",
        "Proxmox took too long to answer, often while a disk is busy. Wait a few minutes and run setup again; finished parts are kept.",
    ),
    (r"No space left on device", "A disk is full. Free space on it, or choose fewer workloads under Setup, then run setup again."),
    (
        r"Could not resolve|Temporary failure (in name resolution|resolving)|Network is unreachable|timed out|Connection timed out|"
        r"Failed to download|Failed to fetch|status code (429|5\d\d)|HTTP Error (429|5\d\d)",
        "A download failed or the internet connection dropped. Check the server's internet connection and run setup again; "
        "it picks up where it stopped.",
    ),
    (
        r"Permission denied \(publickey|Host key verification failed",
        "The page's container could not sign in to the Proxmox host over SSH. "
        "Run install.sh in the Proxmox Shell again; it puts the key back.",
    ),
    (
        r"UNREACHABLE|no answer from the host|Connection refused|Connection reset|kex_exchange_identification",
        "A machine did not answer. A guest setup has just made can take a few minutes to start; wait a minute and run "
        "setup again. If it stops here again, check in Proxmox that the guest named above is running.",
    ),
    (
        r"games\.minecraft\.eula",
        "Minecraft's server runs only once you agree to its EULA (https://aka.ms/MinecraftEULA): "
        "tick that you agree beside Minecraft under Setup, What to run, or untick Minecraft.",
    ),
    (
        r"nb-game wait failed",
        "The game server was installed but did not answer in time. Run setup again; if it stops here again, "
        "the server's own log says why: in Proxmox open the game's container and run journalctl -u followed by the game's name.",
    ),
    (r"is still copying", "A copy is still running on the host. Wait for it to finish, then run setup again."),
    (r"checks failed", "Look at the failing checks below. Fix what they name, then run the checks again."),
]
STAGE_ADVICE = {
    PREPARE: "Fix what the message names under Setup, then run setup again.",
    "tofu": "Proxmox refused a change. The message above names it. Fix it and run setup again; finished parts are kept.",
    "release": "The guest it names was not upgraded to the new Debian and nothing after it was touched. "
    "The lines above are the end of the upgrade's log. Run setup again to try once more; docs/runbooks/debian-upgrade.md has the rest.",
    "verify": "Setup itself finished. Look at the failing checks below, fix what they name, and run the checks again.",
}
DEFAULT_ADVICE = (
    "Run setup again; it is safe to repeat and picks up where it stopped. If it stops at the same place, copy the details and report them."
)
BUG_ADVICE = (
    "This is a fault in NullBill, not something you did. Press Update NullBill on the Updates page in case a fix is out, "
    "then run it again. If it stops here again, copy the details and report them."
)


def specific(message: str) -> str | None:
    """What to do about a cause the page knows, or None."""
    for pattern, text in ADVICE:
        if re.search(pattern, message, re.I):
            return text
    return None


def advice(message: str, stage: str | None = None, default: str | None = None) -> str:
    return specific(message) or default or STAGE_ADVICE.get(stage or "", DEFAULT_ADVICE)


def sentence(text: str) -> str:
    """A message as the page shows it: capital first letter, a full stop."""
    text = text.strip()
    if not text:
        return text
    # A word, not a file or a name: router.yml stays as it is.
    if re.match(r"[a-z]+[\s,:;]", text):
        text = text[0].upper() + text[1:]
    text = text.rstrip(":;, ")
    return text if text[-1:] in (".", "!", "?", "…") else text + "."


def _when(stamp: str) -> float | None:
    try:
        return time.mktime(time.strptime(stamp, STAGE_TIME))
    except ValueError:
        return None


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _result(first: str, rest: list[str]) -> dict:
    """What a failed task returned. Ansible prints it as YAML under the
    line (apply asks for that), or as JSON on it in logs from before."""
    try:
        if first.lstrip().startswith("{"):
            data = json.loads("\n".join([first, *rest]))
        else:
            data = yaml.safe_load(textwrap.dedent("\n".join(rest)))
    except (ValueError, yaml.YAMLError):
        data = None
    return data if isinstance(data, dict) else {"msg": "\n".join(line.strip() for line in [first, *rest] if line.strip())}


def _said(data: dict) -> tuple[str, str]:
    """One failure as a line and, when there is more, what it printed: the
    message, or the program's last line when the module only gives an exit
    code, then the end of its output."""
    message = str(data.get("msg") or "").strip()
    errors = [x for x in str(data.get("stderr") or data.get("module_stderr") or "").splitlines() if x.strip()]
    output = [x for x in str(data.get("stdout") or data.get("module_stdout") or "").splitlines() if x.strip()]
    printed = errors or output
    lines = message.splitlines()
    if message.lower() in NO_MESSAGE:
        text = printed[-1].strip() if printed else "it failed without a message"
        if data.get("rc") not in (None, 0):
            text += f" (exit {data['rc']})"
        lines = []
    else:
        text = lines[0].strip()
    more = [*lines[1:], *(x for x in printed[-12:] if x.strip() not in message)]
    detail = "\n".join(more).strip() if more else ""
    if len(text) > 300:
        detail = "\n".join(filter(None, [message, detail]))
    return cut(text, 300), detail[-1500:]


def _ansible_failures(lines: list[str]) -> list[dict]:
    """Every failure of the last task that failed and was not ignored: one
    per machine, or per item of its loop."""
    tasks: list[tuple[str | None, list[dict]]] = [(None, [])]
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if m := TASK.match(line):
            tasks.append((m[2] + (f" ({m[1]})" if m[1] else ""), []))
            continue
        if line.startswith("...ignoring"):
            tasks[-1][1].clear()
            continue
        m = FATAL.match(line)
        if not m:
            continue
        # The result goes on in indented lines; a blank line belongs to it
        # only when more of it follows.
        rest = []
        while i < len(lines):
            if lines[i].startswith((" ", "\t")) or lines[i] == "}":
                rest.append(lines[i])
            elif not lines[i].strip() and i + 1 < len(lines) and lines[i + 1].startswith((" ", "\t")):
                rest.append("")
            else:
                break
            i += 1
        text, detail = _said(_result(m[3], rest))
        tasks[-1][1].append({"task": tasks[-1][0], "host": m[1], "item": m[2], "text": text, "detail": detail})
    return next((found for _, found in reversed(tasks) if found), [])


def _tofu_errors(lines: list[str]) -> list[dict]:
    """OpenTofu's errors, each with the resource it names and what it adds
    below; where in the rendered file it stands helps nobody."""
    found = []
    block: list[str] | None = None
    for line in [*lines, "Error: "]:
        if line.startswith(("Error: ", "Warning: ")):
            if block:
                found.append(block)
            block = [line.removeprefix("Error: ")] if line.startswith("Error: ") else None
        elif block is not None and len(block) < 40:
            block.append(line)
    out = []
    for block in found[-5:]:
        head = [block[0].strip()]
        rest = block[1:]
        while rest and rest[0].strip():
            head.append(rest.pop(0).strip())
        resource = next((m[1] for line in rest if (m := TOFU_WITH.match(line))), None)
        kind, _, name = (resource or "").partition(".")
        body = [line.strip() for line in rest if not TOFU_WITH.match(line) and not TOFU_SOURCE.match(line)]
        detail = "\n".join([*head[1:], *body]).strip()
        out.append(
            {
                "title": f"{TOFU_KINDS[kind]} {name.replace('_', '-')}" if kind in TOFU_KINDS and name else resource,
                "text": sentence(cut(head[0], 300)),
                "detail": re.sub(r"\n{3,}", "\n\n", detail)[:1500],
            }
        )
    return out


def _crash(lines: list[str]) -> dict | None:
    """A Python traceback at the end of the log: the program itself broke."""
    start = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip() == TRACEBACK), None)
    if start is None:
        return None
    trace = [line for line in lines[start:] if line.strip()]
    raised = next((line.strip() for line in reversed(trace) if RAISED.match(line.strip())), None)
    return {"raised": cut(raised or "an error it did not name", 200), "detail": "\n".join(trace[-25:])[-2000:]}


def _activity(lines: list[str], stage: str) -> str | None:
    """What the running stage last said it does, in its own words."""
    for line in reversed(lines):
        if m := TASK.match(line):
            return m[2][0].upper() + m[2][1:] + (f" ({m[1]})" if m[1] else "")
        text = line.strip()
        if not text or text.startswith(("PLAY", "ok:", "changed:", "skipping:", "included:", "*")) or set(text) <= {"*", " "}:
            continue
        if STAGE_LINE.match(text):
            continue
        return sentence(text.removeprefix(f"{stage}: "))[:200]
    return None


def _check(text: str) -> dict:
    """A check's line as its name and, after the first colon, what it found."""
    name, _, detail = text.partition(": ")
    return {"name": name, "detail": detail}


def verify_results(lines: list[str]) -> dict | None:
    """The checks' counts, the ones that found a problem, and the ones that
    did not run, each with what it said: a skipped check is no pass, and the
    page says why it did not run."""
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "DEGRADED": 0}
    problems, skipped = [], []
    for line in lines:
        if m := VERIFY.match(line):
            counts[m[1]] += 1
            if m[1] in ("FAIL", "DEGRADED"):
                problems.append({"status": m[1], "text": m[2]} | _check(m[2]))
            elif m[1] == "SKIP":
                skipped.append(_check(m[2]))
    if not any(counts.values()):
        return None
    return {"counts": counts, "problems": problems, "skipped": skipped}


def only(action: str, args: list[str]) -> dict | None:
    """The one stage a setup run was started for (`apply --stage router`),
    with its text, or None for a full run: such a run is not "setup"."""
    if action != "apply" or "--stage" not in args[:-1]:
        return None
    name = args[args.index("--stage") + 1]
    return {"name": name, "text": STAGE_TEXT.get(name, name)}


def summarize(action: str, text: str, state: str, times: dict, stage: str | None = None) -> dict:
    """The page's view of one job's log. times: started, now, ended (None
    while running) and output (the log's last write), as Unix times. stage:
    the one stage the run was started for, which is then all it lists."""
    lines = text.splitlines()
    names = [PREPARE, stage] if stage else JOB_STAGES.get(action)
    errors = [line.removeprefix(ERROR_PREFIX) for line in lines if line.startswith(ERROR_PREFIX)]
    # Each stage's lines and start time, in the order they ran.
    sections: dict[str, list[str]] = {PREPARE: []}
    begun: dict[str, float | None] = {PREPARE: times["started"]}
    current = PREPARE
    for line in lines:
        if m := STAGE_LINE.match(line):
            current = m[1]
            sections[current] = []
            begun[current] = _when(m[2])
        elif not line.startswith(ERROR_PREFIX):
            sections[current].append(line)
    results = verify_results(lines if action == "verify" else sections.get("verify", []))
    out: dict = {"title": STAGE_TEXT.get(stage, stage) if stage else JOB_TEXT.get(action, action)}
    failed_stage = None
    stopped = state == "failed" and times.get("stopped")
    if stopped:
        # What the killed processes said on their way out is not the cause.
        errors = []
    if state == "failed":
        joined = "\n".join(errors)
        m = STOPPED.search(joined)
        failed_stage, message = (m[1], m[2].split("\n")[0]) if m else (current, errors[0] if errors else "")
        others = [e for e in errors if not STOPPED.search(e) and e != message]
        stage_lines = sections.get(failed_stage, [])
        failures = results["counts"]["FAIL"] if results else 0
        items: list[dict] = []
        default = None
        if stopped:
            message = "you stopped this run from the page"
        elif not message and failures:
            message = f"{failures} check{'s' if failures > 1 else ''} failed"
        elif not message and (crash := _crash(stage_lines)):
            message = f"NullBill stopped with an error of its own: {crash['raised']}"
            items = [{"title": None, "text": "", "detail": crash["detail"]}]
            default = BUG_ADVICE
        elif (not message or PLAYBOOK_FAILED.search(message)) and (failed := _ansible_failures(stage_lines)):
            # The step and what it said, not the playbook's exit code.
            step = (failed[0]["task"] or "a step").split(" (")[0]
            hosts = list(dict.fromkeys(f["host"] for f in failed))
            where = hosts[0] if len(hosts) == 1 else f"{len(hosts)} machines"
            times_over = f", for {len(failed)} items" if len(failed) > 1 and len(hosts) == 1 else ""
            message = f'the step "{step[0].upper() + step[1:]}" failed on {where}{times_over}'
            many = len(failed) > 1
            items = [
                {"title": (f["item"] or f["host"]) if many else None, "text": sentence(f["text"]), "detail": f["detail"]} for f in failed
            ]
        elif failed_stage == "tofu" and (said := _tofu_errors(stage_lines)):
            items = said
            message = "OpenTofu stopped with an error" if len(said) == 1 else f"OpenTofu stopped with {len(said)} errors"
        elif others and not m:
            # Several causes at once, as plan reports them: none is the first.
            others = [message, *others]
            message = f"the plan has {len(others)} problems" if failed_stage == PREPARE else f"it stopped with {len(others)} errors"
        if not stopped:
            items += [{"title": None, "text": sentence(e), "detail": ""} for e in others]
        # What to do. Several causes each get their own line beside them;
        # one cause, or none the page knows, gets the one under the card.
        fallback = default or advice("", failed_stage if names else None)
        for item in items:
            item["advice"] = specific(f"{item['text']}\n{item['detail']}") if len(items) > 1 else None
        if stopped:
            todo = STOPPED_ADVICE
        elif len(items) > 1:
            todo = fallback
        else:
            todo = specific("\n".join([message, *(f"{i['text']}\n{i['detail']}" for i in items)])) or fallback
        out["error"] = {
            "stage": failed_stage if names else None,
            "stage_text": STAGE_TEXT.get(failed_stage) if names else None,
            "message": sentence(message or "it ended without saying why; the full log below may show more"),
            "items": items,
            "advice": todo,
        }
    if names:
        steps = []
        ran = [n for n in names if n in sections]
        for name in names:
            later = ran[ran.index(name) + 1 :] if name in ran else []
            if name == failed_stage:
                status = "failed"
            elif name in sections and (later or state == "done"):
                status = "done"
            elif name in sections and state == "running":
                status = "running"
            else:
                status = "waiting"
            end = begun.get(later[0]) if later else (times["now"] if state == "running" else times["ended"])
            begin = begun.get(name)
            seconds = max(0, round(end - begin)) if status != "waiting" and begin is not None and end is not None else None
            steps.append({"name": name, "text": STAGE_TEXT.get(name, name), "status": status, "seconds": seconds})
        out["steps"] = steps
    if state == "running":
        out["activity"] = _activity(sections[current], current)
        out["quiet_s"] = round(times["now"] - times["output"]) if times.get("output") else None
    if results:
        out["verify"] = results
    return out
