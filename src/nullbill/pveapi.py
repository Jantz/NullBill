"""PVE API calls made from inside a guest with the token that guest holds,
and the check that a token holds exactly the privileges it should."""

import json
import shlex
from collections.abc import Callable

from nullbill.probe import Runner
from nullbill.results import Result


def _api(host_ip: str, method: str, path: str, data: str, token_file: str, ca_file: str) -> str:
    """Shell for one API call with a token kept in a guest; prints the
    status, then the body."""
    url = shlex.quote(f"https://{host_ip}:8006/api2/json{path}")
    body = f"--data {shlex.quote(data)} " if data else ""
    return (
        "set -e; f=$(mktemp); trap 'rm -f \"$f\"' EXIT; "
        f"curl -sS -m 10 -o \"$f\" -w '%{{http_code}}\\n' --cacert {ca_file} "
        f'-H "Authorization: PVEAPIToken=$(cat {token_file})" -X {method} {body}{url}; cat "$f"'
    )


def _privileges(body: str, path: str) -> set[str]:
    return set(json.loads(body or "{}").get("data", {}).get(path, {}))


Api = Callable[[str, str, str], tuple[str, str]]


def caller(plan: dict, target: str, run: Runner, vmid: int, token_file: str, ca_file: str) -> Api:
    """API calls from inside guest `vmid` with the token it holds."""
    host_ip = plan["zones"]["mgmt"]["host_ip"]

    def api(method: str, path: str, data: str = "") -> tuple[str, str]:
        command = f"pct exec {vmid} -- bash -c {shlex.quote(_api(host_ip, method, path, data, token_file, ca_file))}"
        rc, out, err = run(target, command)
        if rc != 0:
            return "error", (err or out).strip()[:120]
        status, _, body = out.partition("\n")
        return status.strip(), body

    return api


def audit(api: Api, label: str, path: str, want: set[str]) -> Result:
    """PASS when the token holds exactly `want` on `path`, Audit aside."""
    status, body = api("GET", f"/access/permissions?path={path}", "")
    if status != "200":
        return Result("FAIL", label, f"permissions query: {status} {body[:80]}")
    changes = {p for p in _privileges(body, path) if not p.endswith(".Audit")}
    return Result("PASS", label) if changes == want else Result("FAIL", label, f"holds {', '.join(sorted(changes)) or 'nothing'}")
