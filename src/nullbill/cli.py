"""nullbill command line. Only implemented subcommands are listed."""

import argparse
import hashlib
import json
import sys

from nullbill.artifacts import render_all, write_build
from nullbill.errors import NullBillError
from nullbill.home import nullbill_home, refuse_on_pve_host
from nullbill.model import build_model, load_config
from nullbill.plan import build_plan
from nullbill.probe import run_probe, ssh_run
from nullbill.render import render
from nullbill.verify import run_verify


def cmd_probe(_args: argparse.Namespace) -> int:
    home = nullbill_home()
    config = load_config(home / "config.yaml")
    if "target" not in config:
        raise NullBillError(f"{home / 'config.yaml'}: target.ssh is required for probe")
    run_dir = run_probe(config["target"]["ssh"], home)
    print(f"probe stored in {run_dir}")
    return 0


def plan_text(model: dict, facts_bytes: bytes) -> str:
    plan = build_plan(model, json.loads(facts_bytes))
    digest = hashlib.sha256(facts_bytes).hexdigest()[:16]
    header = f"NullBill plan. Generated; change config.yaml instead of this file.\nfacts sha256: {digest}"
    return render(plan, header)


def _load(home):
    facts_path = home / "probe" / "latest" / "facts.json"
    if not facts_path.is_file():
        raise NullBillError(f"{facts_path} not found; run `nullbill probe` first")
    return build_model(load_config(home / "config.yaml")), facts_path.read_bytes()


def cmd_plan(_args: argparse.Namespace) -> int:
    home = nullbill_home()
    model, facts_bytes = _load(home)
    text = plan_text(model, facts_bytes)
    (home / "plan.yaml").write_text(text)
    sys.stdout.write(text)
    return 0


def cmd_apply(_args: argparse.Namespace) -> int:
    # Only the dry run exists so far: it renders what apply would place on the
    # host and touches nothing remote.
    home = nullbill_home()
    model, facts_bytes = _load(home)
    files = render_all(build_plan(model, json.loads(facts_bytes)), model)
    write_build(files, home / "build")
    print(f"rendered {len(files)} files into {home / 'build'}:")
    for rel in files:
        print(f"  {rel}")
    return 0


def cmd_verify(_args: argparse.Namespace) -> int:
    home = nullbill_home()
    model, facts_bytes = _load(home)
    config = load_config(home / "config.yaml")
    target = config.get("target", {}).get("ssh")
    results = run_verify(build_plan(model, json.loads(facts_bytes)), model, target, ssh_run)
    for result in results:
        print(result.line())
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "FAIL", "SKIP", "DEGRADED")}
    print(", ".join(f"{n} {s}" for s, n in counts.items()))
    return 1 if counts["FAIL"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nullbill")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="read-only hardware probe of the target over SSH").set_defaults(func=cmd_probe)
    sub.add_parser("plan", help="size a commented plan from the latest probe and config.yaml").set_defaults(func=cmd_plan)
    apply = sub.add_parser("apply", help="render host artifacts; only --dry-run exists so far")
    apply.add_argument("--dry-run", action="store_true", required=True, help="render into $NULLBILL_HOME/build, change nothing")
    apply.set_defaults(func=cmd_apply)
    verify = sub.add_parser("verify", help="probe allowed and denied flows from each zone and the LAN; exits 1 on FAIL")
    verify.set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    try:
        refuse_on_pve_host()
        return args.func(args)
    except NullBillError as exc:
        for message in exc.messages:
            print(f"nullbill: {message}", file=sys.stderr)
        return 1
