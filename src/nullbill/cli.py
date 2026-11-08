"""nullbill command line. Only implemented subcommands are listed."""

import argparse
import hashlib
import json
import sys

from nullbill import lan
from nullbill.apply import STAGES, Context, SshReach, confirm_lan, run_apply, split_target
from nullbill.artifacts import render_all, write_build
from nullbill.errors import NullBillError
from nullbill.home import REPO_ROOT, nullbill_home, refuse_on_pve_host
from nullbill.model import build_model, load_config
from nullbill.plan import build_plan
from nullbill.probe import run_probe, ssh_run
from nullbill.render import render, strip_notes
from nullbill.secretfile import Secrets, Sops
from nullbill.verify import run_verify


def probe_target(home) -> str:
    """target.ssh from config.yaml, or before the first choices the host
    install.sh recorded; the host's mgmt address once the LAN is closing or
    closed, as for apply."""
    path = home / "config.yaml"
    configured = (load_config(path) if path.is_file() else {}).get("target", {}).get("ssh")
    install = home / "install.json"
    if not configured and install.is_file():
        configured = f"root@{json.loads(install.read_text())['host']}"
    if not configured:
        raise NullBillError(f"{path}: target.ssh is required for probe")
    if lan.state(home) == lan.OPEN:
        return configured
    model, facts_bytes = _load(home)
    return reach_target(home, configured, build_plan(model, json.loads(facts_bytes)))


def cmd_probe(_args: argparse.Namespace) -> int:
    home = nullbill_home()
    run_dir = run_probe(probe_target(home), home)
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


def cmd_apply(args: argparse.Namespace) -> int:
    home = nullbill_home()
    model, facts_bytes = _load(home)
    plan = build_plan(model, json.loads(facts_bytes))
    files = render_all(plan, model)
    if args.dry_run:
        # Renders what apply would place on the host and touches nothing remote.
        write_build(files, home / "build")
        print(f"rendered {len(files)} files into {home / 'build'}:")
        for rel in files:
            print(f"  {rel}")
        return 0
    configured = load_config(home / "config.yaml").get("target", {}).get("ssh")
    if not configured:
        raise NullBillError(f"{home / 'config.yaml'}: target.ssh is required for apply")
    if args.close_lan and args.open_lan:
        raise NullBillError("--close-lan and --open-lan contradict each other")
    target = reach_target(home, configured, plan)
    reach = SshReach(target, REPO_ROOT / ".ansible" / "collections")
    ctx = Context(
        plan,
        model,
        home,
        target,
        reach,
        Secrets(Sops(home)),
        files,
        close_lan=args.close_lan,
        open_lan=args.open_lan,
        full=args.full,
        lan_host=split_target(configured)[1],
        run=ssh_run,
    )
    if args.confirm_lan:
        confirm_lan(ctx)
        return 0
    # Changing LAN access is the host stage's work; it ends the LAN path,
    # so nothing runs after it in the same apply.
    run_apply(ctx, only="host" if args.close_lan or args.open_lan else args.stage)
    return 0


def reach_target(home, configured: str, plan: dict) -> str:
    """target.ssh, or the host's mgmt address once the LAN is closing or closed."""
    if lan.state(home) == lan.OPEN:
        return configured
    user, _ = split_target(configured)
    return f"{user}@{strip_notes(plan)['zones']['mgmt']['host_ip']}"


def cmd_verify(_args: argparse.Namespace) -> int:
    home = nullbill_home()
    model, facts_bytes = _load(home)
    config = load_config(home / "config.yaml")
    configured = config.get("target", {}).get("ssh")
    plan = build_plan(model, json.loads(facts_bytes))
    target = reach_target(home, configured, plan) if configured else None
    lan_host = split_target(configured)[1] if configured else None
    results = run_verify(
        plan, model, target, ssh_run, lan_state=lan.state(home), lan_host=lan_host, say=lambda r: print(r.line(), flush=True)
    )
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "FAIL", "SKIP", "DEGRADED")}
    print(", ".join(f"{n} {s}" for s, n in counts.items()))
    return 1 if counts["FAIL"] else 0


def cmd_upgrade(_args: argparse.Namespace) -> int:
    from nullbill.upgrade import upgrade

    upgrade(REPO_ROOT, nullbill_home(), say=lambda line: print(line, flush=True))
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    from nullbill.web.server import serve

    serve(nullbill_home(), args.listen)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nullbill")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="read-only hardware probe of the target over SSH").set_defaults(func=cmd_probe)
    sub.add_parser("plan", help="size a commented plan from the latest probe and config.yaml").set_defaults(func=cmd_plan)
    stages = ", ".join(name for name, _ in STAGES)
    apply = sub.add_parser("apply", help=f"bring the host to the plan, stage by stage: {stages}")
    apply.add_argument("--dry-run", action="store_true", help="render into $NULLBILL_HOME/build, change nothing")
    apply.add_argument("--stage", choices=[name for name, _ in STAGES], help="run only this stage")
    apply.add_argument("--full", action="store_true", help="run every playbook, also where nothing it depends on changed")
    apply.add_argument(
        "--close-lan", action="store_true", help="close the LAN to 8006 and 22; reopens by itself unless confirmed over Tailscale"
    )
    apply.add_argument("--confirm-lan", action="store_true", help="keep the LAN closed: stop the timer that would reopen it")
    apply.add_argument("--open-lan", action="store_true", help="give the LAN access to 8006 and 22 again")
    apply.set_defaults(func=cmd_apply)
    verify = sub.add_parser("verify", help="probe allowed and denied flows from each zone and the LAN; exits 1 on FAIL")
    verify.set_defaults(func=cmd_verify)
    upgrade = sub.add_parser("upgrade", help="move this controller's NullBill to main's newest commit and restart the page")
    upgrade.set_defaults(func=cmd_upgrade)
    web = sub.add_parser("web", help="serve the setup and status page over HTTPS")
    web.add_argument("--listen", default="0.0.0.0:8443", help="address:port (default 0.0.0.0:8443)")
    web.set_defaults(func=cmd_web)
    args = parser.parse_args(argv)
    try:
        refuse_on_pve_host()
        return args.func(args)
    except NullBillError as exc:
        for message in exc.messages:
            print(f"nullbill: {message}", file=sys.stderr)
        return 1
