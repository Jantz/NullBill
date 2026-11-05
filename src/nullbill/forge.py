"""verify's checks of Forgejo on a deployed host, run inside its guests.

From the forgejo container, through nginx with step-ca's root: Forgejo is
healthy over HTTPS, refuses sign-up and shows nothing without sign-in.
From the postgres container: every connection Forgejo holds uses TLS.
"""

import shlex

from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

ROOT_CA = "/etc/nullbill/tls/root_ca.crt"


def _checks(plan: dict) -> list[tuple[str, str, str, str]]:
    """(label, guest, command, expected output)."""
    name = f"forgejo.{plan['domain']}"
    curl = f"curl -sS --cacert {ROOT_CA} --resolve {name}:443:127.0.0.1"
    out = [
        ("forgejo is healthy over HTTPS with its step-ca certificate", "forgejo",
         f"{curl} -o /dev/null -w '%{{http_code}}' https://{name}/api/healthz", "200"),
        ("forgejo refuses sign-up", "forgejo",
         f"{curl} https://{name}/user/sign_up | grep -q 'Registration is disabled' && echo disabled", "disabled"),
        ("forgejo shows nothing without sign-in", "forgejo",
         f"code=$({curl} -o /dev/null -w '%{{http_code}}' https://{name}/api/v1/repos/search); "
         'case $code in 401 | 403) echo refused ;; *) echo "$code" ;; esac', "refused"),
    ]  # fmt: skip
    if "postgres" in plan["guests"]:
        query = (
            "SELECT count(*) || ' ' || count(*) FILTER (WHERE NOT s.ssl) "
            "FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid) WHERE a.usename = 'forgejo'"
        )
        # At least one connection (Forgejo keeps idle ones open), none in plaintext.
        sql = f"runuser -u postgres -- psql -tAc {shlex.quote(query)} | awk '{{print ($1 > 0 && $2 == 0) ? \"tls\" : $0}}'"
        out.append(("forgejo reaches postgres over TLS only", "postgres", sql, "tls"))
    return out


def enabled(plan: dict) -> bool:
    return "forgejo" in plan["guests"]


def skipped(plan_with_notes: dict, reason: str) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    return [Result("SKIP", label, reason) for label, _, _, _ in _checks(plan)] if enabled(plan) else []


def check(plan_with_notes: dict, target: str, run: Runner) -> list[Result]:
    plan = strip_notes(plan_with_notes)
    if not enabled(plan):
        return []
    results = []
    for label, guest, command, want in _checks(plan):
        vmid = plan["guests"][guest]["vmid"]
        rc, out, err = run(target, f"pct exec {vmid} -- bash -c {shlex.quote(command)}")
        got = out.strip()
        results.append(Result("PASS", label) if got == want else Result("FAIL", label, (got or err.strip() or f"exit {rc}")[:120]))
    return results
