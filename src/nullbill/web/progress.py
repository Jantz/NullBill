"""What a job's log says, for the page: which of apply's stages ran, which
one runs now, and, when a job fails, why in plain words and what to do.

apply prints a line `--- stage <name>, <time>` before each stage
(apply.run_apply); everything after it, up to the next, is that stage's
output. A failure ends with the `nullbill: <message>` lines cli.main prints.
"""

import json
import re
import time

from nullbill.apply import STAGE_MARK, STAGE_TIME, STAGES

# Before the first stage, apply reads the config and makes the plan.
PREPARE = "prepare"
STAGE_TEXT = {
    PREPARE: "Read your choices and make the plan",
    "render": "Write the configuration files",
    "tailnet": "Put the Tailscale access policy in place",
    "host": "Secure the Proxmox host",
    "tofu": "Create the networks, containers and VMs",
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
    "ci": "Build the CI runner images",
    "agents": "Set up the coding agent",
    "logs": "Send logs to the log store",
    "backups": "Set up backups",
    "vpn": "Join your Tailscale network",
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
}
STAGE_LINE = re.compile(re.escape(STAGE_MARK) + r"([a-z-]+), (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)$")
TASK = re.compile(r"^TASK \[(?:([^:\]]+) : )?(.+?)\] \**$")
FATAL = re.compile(r"^(?:fatal|failed): \[([^\]]+)\](?: \(item=.*?\))?: (?:FAILED|UNREACHABLE)! => (.*)$")
# Result.line(): the status padded to eight characters, then the check.
VERIFY = re.compile(r"^(PASS|FAIL|SKIP|DEGRADED) {1,5}(?<=^.{9})(.+)$")
ERROR_PREFIX = "nullbill: "
STOPPED = re.compile(r"apply stopped at ([a-z-]+): (.*)$", re.S)

# Known causes, by what the message says: what to do about each. The first
# match wins. Anything else gets the stage's own advice.
ADVICE = [
    (
        r"planned guests need \d+ MiB at their floors|guests need more memory",
        "Your choices need more memory than this server has. In step 3, untick a workload "
        "(or the on-site screen) and press Save and plan until step 4 shows no problems.",
    ),
    (
        r"storage \S+ has \d+ GiB free|guests need \d+ GiB",
        "Your choices need more disk space than Proxmox's storage has free. In step 3, untick a workload "
        "and press Save and plan, or free space in Proxmox and probe again in step 2.",
    ),
    (r"could not read VRAM", "In step 3, give the graphics card's memory in GB, as on its box, then press Save and plan."),
    (r"no host-shared GPU budgets VRAM", "Untick Local AI models in step 3, then press Save and plan."),
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
        "Jellyfin was to use the processor's graphics, but the server shows none now. Probe the server again in step 2 "
        "and run setup again; Jellyfin then transcodes on the processor itself.",
    ),
    (
        r"Jellyfin refuses nullbill-admin|first-run setup was done without NullBill",
        "Setup cannot sign in to Jellyfin as nullbill-admin. If you changed its password in Jellyfin, change it back, "
        "or run setup once with the new one in the Jellyfin guest's /etc/nullbill/jellyfin-admin-password (docs/runbooks/media.md).",
    ),
    (
        r"signatures.*wipe|has signatures",
        "A disk holds data. In step 3, choose what to do with it, and type the end of its name to erase it.",
    ),
    (r"target\.ssh is required|config\.yaml.*(missing|not found)|No such file.*config\.yaml", "Save your choices in step 3 first."),
    (r"facts\.json|no probe|probe the server", "Press Probe the server in step 2 first."),
    (r"another job is still running", "Wait until the running job finishes; its progress shows on this page."),
    (
        r"OAuth client lacks a scope",
        "In the Tailscale admin console, give the OAuth client the Policy file (write) and Devices (read) scopes, "
        "or make a new one, and save it again in step 7.",
    ),
    (
        r"Tailscale.*\b(401|403)\b|invalid_client|unauthorized",
        "Tailscale did not accept the OAuth client. Check the client ID and secret, save them again in step 7, then run setup again.",
    ),
    (
        r"Tailscale refused the policy",
        "Tailscale checked the access policy NullBill made and refused it. Nothing on the server changed. "
        "This is a NullBill bug unless you edited the policy: copy the details and report them.",
    ),
    (r"free plan allows", "Remove tagged devices you no longer use in the Tailscale admin console, then run setup again."),
    (
        r"Tailscale did not log in|auth key",
        "In Tailscale, generate a new one-time auth key tagged tag:nb-vpn, paste it in step 7, and run setup again.",
    ),
    (r"Tailscale's API did not answer", "The server could not reach Tailscale. Check its internet connection and run setup again."),
    (
        r"Windows ISO",
        "Upload the Windows 11 ISO in Proxmox (local storage, ISO Images, keep Win in its name), or untick Windows in step 3.",
    ),
    (
        r"Tunnel connection failed: 403|403 Forbidden.*proxy|proxy.*403 Forbidden",
        "NullBill's internet proxy refused a site this guest needs: the site is not on its allowed list. "
        "Update NullBill and run setup again; if it stops here again, copy the details and report them.",
    ),
    (r"No space left on device", "A disk is full. Free space on it, or choose fewer workloads in step 3, then run setup again."),
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
    (r"is still copying", "A copy is still running on the host. Wait for it to finish, then run setup again."),
    (r"checks failed", "Look at the failing checks below. Fix what they name, then run the checks again."),
]
STAGE_ADVICE = {
    PREPARE: "Fix what the message names in steps 2 to 4, then run setup again.",
    "tofu": "Proxmox refused a change. The message above names it. Fix it and run setup again; finished parts are kept.",
    "verify": "Setup itself finished. Look at the failing checks below, fix what they name, and run the checks again.",
}
DEFAULT_ADVICE = (
    "Run setup again; it is safe to repeat and picks up where it stopped. If it stops at the same place, copy the details and report them."
)


def advice(message: str, stage: str | None = None, default: str | None = None) -> str:
    for pattern, text in ADVICE:
        if re.search(pattern, message, re.I):
            return text
    return default or STAGE_ADVICE.get(stage or "", DEFAULT_ADVICE)


def sentence(text: str) -> str:
    """A message as the page shows it: capital first letter, a full stop."""
    text = text.strip()
    if not text:
        return text
    # A word, not a file or a name: router.yml stays as it is.
    if re.match(r"[a-z]+[\s,:;]", text):
        text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _when(stamp: str) -> float | None:
    try:
        return time.mktime(time.strptime(stamp, STAGE_TIME))
    except ValueError:
        return None


def _ansible_failure(lines: list[str]) -> dict | None:
    """The last task that failed (and was not ignored) in these lines."""
    found = None
    task = None
    for i, line in enumerate(lines):
        if m := TASK.match(line):
            task = m[2] + (f" ({m[1]})" if m[1] else "")
            continue
        m = FATAL.match(line)
        if not m:
            continue
        rest = [m[2]]
        j = i + 1
        while j < len(lines) and lines[j].strip() and not lines[j].startswith(("TASK [", "PLAY ", "...ignoring")):
            rest.append(lines[j])
            j += 1
        if j < len(lines) and lines[j].startswith("...ignoring"):
            continue
        raw = "\n".join(rest)
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"msg": raw}
        detail = str(data.get("msg") or "").strip()
        extra = data.get("stderr") or data.get("module_stderr") or ""
        if extra and str(extra).strip() not in detail:
            detail = (detail + "\n" if detail else "") + "\n".join(str(extra).strip().splitlines()[-6:])
        found = {"task": task, "host": m[1], "detail": detail[:800]}
    return found


def _tofu_errors(lines: list[str]) -> list[str]:
    return [line.removeprefix("Error: ").strip() for line in lines if line.startswith("Error: ")][-3:]


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


def verify_results(lines: list[str]) -> dict | None:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "DEGRADED": 0}
    problems = []
    for line in lines:
        if m := VERIFY.match(line):
            counts[m[1]] += 1
            if m[1] in ("FAIL", "DEGRADED"):
                problems.append({"status": m[1], "text": m[2]})
    if not any(counts.values()):
        return None
    return {"counts": counts, "problems": problems}


def summarize(action: str, text: str, state: str, times: dict) -> dict:
    """The page's view of one job's log. times: started, now, ended (None
    while running) and output (the log's last write), as Unix times."""
    lines = text.splitlines()
    names = JOB_STAGES.get(action)
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
    out: dict = {"title": JOB_TEXT.get(action, action)}
    failed_stage = None
    if state == "failed":
        joined = "\n".join(errors)
        m = STOPPED.search(joined)
        failed_stage, message = (m[1], m[2]) if m else (current, errors[0] if errors else "")
        failures = results["counts"]["FAIL"] if results else 0
        if not message and failures:
            message = f"{failures} check{'s' if failures > 1 else ''} failed"
        stage_lines = sections.get(failed_stage, [])
        ansible = _ansible_failure(stage_lines)
        tofu = _tofu_errors(stage_lines) if failed_stage == "tofu" else []
        found = " ".join([message, (ansible or {}).get("detail", ""), *tofu, *errors])
        out["error"] = {
            "stage": failed_stage if names else None,
            "stage_text": STAGE_TEXT.get(failed_stage) if names else None,
            "message": sentence(message or "it ended without saying why; the full log below may show more"),
            "more": [sentence(e) for e in errors if not STOPPED.search(e) and e != message],
            "task": ansible,
            "tofu": tofu,
            "advice": advice(found, failed_stage if names else None),
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
            seconds = round(end - begin) if status != "waiting" and begin is not None and end is not None else None
            steps.append({"name": name, "text": STAGE_TEXT.get(name, name), "status": status, "seconds": seconds})
        out["steps"] = steps
    if state == "running":
        out["activity"] = _activity(sections[current], current)
        out["quiet_s"] = round(times["now"] - times["output"]) if times.get("output") else None
    if results:
        out["verify"] = results
    return out
