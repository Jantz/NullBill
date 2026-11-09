"""The local model and the model gateway: what apply's inference stage needs
from the plan and the host, and verify's checks of them.

The host owns the NVIDIA card and its driver (ansible/roles/nvidia_host);
the inference container gets the card's device nodes and runs llama.cpp on
it; the model-gateway container runs LiteLLM in front
(docs/adr/0008-local-models.md). verify checks, on the host, that the
driver is the pinned one and the model sits in the card's memory, and from
inside the gateway, over HTTPS, that the local model answers with the
gateway's key and nothing does without it.
"""

import re
import shlex

import yaml

from nullbill.home import REPO_ROOT
from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

INFERENCE = "inference"
GATEWAY = "model-gateway"
GATEWAY_PORT = 4000
INFERENCE_PORT = 8080
ROOT_CA = "/etc/nullbill/tls/root_ca.crt"
# The nodes the NVIDIA driver serves compute through, besides one per card.
SHARED_NODES = ("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools")
# Debian's video group, which the llama user is in (ansible/roles/inference).
VIDEO_GID = 44
DEV_LINE = re.compile(r"^dev(\d+): (.+)$")


def nvidia_pin() -> tuple[str, str]:
    """The NVIDIA driver release and its installer's SHA-256, kept with the
    host role, which the inference container must match."""
    defaults = yaml.safe_load((REPO_ROOT / "ansible/roles/nvidia_host/defaults/main.yml").read_text())
    return str(defaults["nvidia_host_version"]), defaults["nvidia_host_sha256"]


def enabled(plan: dict) -> bool:
    return INFERENCE in plan["guests"] and GATEWAY in plan["guests"]


def model(plan: dict, model_data: dict) -> tuple[str, dict]:
    name = plan["inference"]["model"]
    return name, model_data["models"][name]


def wanted_devices(minors: list[int]) -> list[str]:
    """pct devN values that give the container every inference card, as
    group video."""
    nodes = [f"/dev/nvidia{m}" for m in sorted(minors)] + list(SHARED_NODES)
    return [f"{node},gid={VIDEO_GID},mode=0660" for node in nodes]


def current_devices(pct_config: str) -> dict[int, str]:
    """devN entries in `pct config` output, by N."""
    found = {}
    for line in pct_config.splitlines():
        m = DEV_LINE.match(line.strip())
        if m:
            found[int(m[1])] = m[2]
    return found


def gateway_clients(plan: dict, model_data: dict) -> list[str]:
    """Containers in the zones the catalog lets reach the gateway: they get
    step-ca's root, so the gateway's certificate checks out."""
    zones = {
        source.removeprefix("zone:")
        for port in model_data["services"][GATEWAY].get("ports", [])
        for source in port["from"]
        if source.startswith("zone:")
    }
    return sorted(n for n, g in plan["guests"].items() if g["zone"] in zones and g["kind"] == "lxc")


def _host_checks(plan: dict) -> list[tuple[str, str, str]]:
    """(label, host command, expected output)."""
    version, _ = nvidia_pin()
    smi = "nvidia-smi --format=csv,noheader,nounits --query-gpu"
    # Most of the file lands in VRAM; the rest is context cache.
    need = plan["inference"]["model_mib"] * 9 // 10
    return [
        (f"the host runs NVIDIA's driver {version}", f"{smi}=driver_version | sort -u", version),
        (
            "the local model sits in the graphics card's memory",
            f'{smi}=memory.used | awk \'{{s += $1}} END {{print (s >= {need}) ? "loaded" : s " MiB used"}}\'',
            "loaded",
        ),
    ]


ANSWER_SECONDS = 100
RETRY_SECONDS = 5
BUSY = "busy"
LOADING = "loading"


def _gateway_checks(plan: dict) -> list[tuple[str, str, str]]:
    """(label, command in the gateway guest, expected output)."""
    name = f"{GATEWAY}.{plan['domain']}"
    url = f"https://{name}:{GATEWAY_PORT}/v1/chat/completions"
    body = shlex.quote('{"model": "local", "max_tokens": 16, "messages": [{"role": "user", "content": "Say OK."}]}')

    def curl(max_time: str) -> str:
        return (
            f"curl -sS --max-time {max_time} --cacert {ROOT_CA} --resolve {name}:{GATEWAY_PORT}:127.0.0.1 -o /dev/null "
            f"-w '%{{http_code}}' -H 'Content-Type: application/json' -d {body} {url}"
        )

    # All of it under ssh_run's limit, so a model that does not answer in
    # time is reported here rather than cut off there. llama-server answers
    # one request at a time, so a long answer to someone else holds this one
    # back (curl's timeout, 28: busy); while it loads the model after a
    # start it answers 503, which is asked again until the time is up.
    with_key = (
        f". /etc/litellm/env; end=$((SECONDS + {ANSWER_SECONDS})); "
        "while :; do "
        f'code=$({curl("$((end - SECONDS))")} -H "Authorization: Bearer $NB_GATEWAY_KEY"); rc=$?; '
        f'if [ "$rc" = 28 ]; then echo {BUSY}; break; fi; '
        f'if [ "$code" = 503 ]; then '
        f"if [ $((end - SECONDS)) -gt $(({RETRY_SECONDS} * 2)) ]; then sleep {RETRY_SECONDS}; continue; fi; "
        f"echo {LOADING}; break; fi; "
        'echo "$code"; break; done'
    )
    return [
        ("the local model answers through the model gateway with its key", with_key, "200"),
        (
            "the model gateway refuses a wrong key",
            f'wrong=not-the-key; code=$({curl(str(ANSWER_SECONDS))} -H "Authorization: Bearer $wrong"); '
            'case $code in 2*) echo "$code" ;; *) echo refused ;; esac',
            "refused",
        ),
    ]


def commands(plan: dict) -> list[tuple[str, str, str]]:
    """(label, command on the host, expected output) for each check."""
    vmid = plan["guests"][GATEWAY]["vmid"]
    return _host_checks(plan) + [
        (label, f"pct exec {vmid} -- bash -c {shlex.quote(command)}", want) for label, command, want in _gateway_checks(plan)
    ]


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    return [Result("SKIP", label, reason) for label, _, _ in commands(plan)] if enabled(plan) else []


WAITING = {
    BUSY: (
        f"no answer within {ANSWER_SECONDS} s; the model answers one request at a time "
        "and may be busy with another, so run the checks again when it is idle"
    ),
    LOADING: f"still loading the model after {ANSWER_SECONDS} s; run the checks again in a few minutes",
}


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if not enabled(plan):
        return []
    results = []
    for label, command, want in commands(plan):
        rc, out, err = run(target, command)
        got = out.strip()
        if got in WAITING:
            results.append(Result("DEGRADED", label, WAITING[got]))
            continue
        results.append(Result("PASS", label) if got == want else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:120]))
    return results
