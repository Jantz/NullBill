"""nullbill command line. Only implemented subcommands are listed."""

import argparse
import hashlib
import json
import sys

from nullbill.errors import NullBillError
from nullbill.home import nullbill_home, refuse_on_pve_host
from nullbill.model import build_model, load_config
from nullbill.plan import build_plan
from nullbill.probe import run_probe
from nullbill.render import render


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


def cmd_plan(_args: argparse.Namespace) -> int:
    home = nullbill_home()
    facts_path = home / "probe" / "latest" / "facts.json"
    if not facts_path.is_file():
        raise NullBillError(f"{facts_path} not found; run `nullbill probe` first")
    model = build_model(load_config(home / "config.yaml"))
    text = plan_text(model, facts_path.read_bytes())
    (home / "plan.yaml").write_text(text)
    sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nullbill")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="read-only hardware probe of the target over SSH").set_defaults(func=cmd_probe)
    sub.add_parser("plan", help="size a commented plan from the latest probe and config.yaml").set_defaults(func=cmd_plan)
    args = parser.parse_args(argv)
    try:
        refuse_on_pve_host()
        return args.func(args)
    except NullBillError as exc:
        for message in exc.messages:
            print(f"nullbill: {message}", file=sys.stderr)
        return 1
