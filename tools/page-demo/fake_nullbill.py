"""Stands in for the `nullbill` command the page starts. A setup run is the
real run_apply against the stand-in host the tests use (tests/test_apply.py's
FakeReach), so it prints what a real run prints and leaves every record a
real run leaves under $NULLBILL_HOME; nothing reaches a server. How a run
goes (its pace, the stage it fails in, what the checks find) is read from
$NULLBILL_HOME/demo.json, which serve.py writes.
"""

import json
import os
import pickle
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "tests"))

import yaml  # noqa: E402
from conftest import FakeTailnet  # noqa: E402
from fixturedata import write_raw  # noqa: E402
from test_apply import FakeReach  # noqa: E402

from nullbill import apply as real  # noqa: E402
from nullbill.artifacts import render_all  # noqa: E402
from nullbill.errors import NullBillError  # noqa: E402
from nullbill.model import build_model  # noqa: E402
from nullbill.plan import build_plan  # noqa: E402
from nullbill.probe import parse  # noqa: E402
from nullbill.results import Result  # noqa: E402
from nullbill.secretfile import Secrets  # noqa: E402

HOME = Path(os.environ["NULLBILL_HOME"])
DEMO = json.loads((HOME / "demo.json").read_text())
HOST = HOME / "demo-host.pickle"
TARGET = "root@192.0.2.10"


class FileCodec:
    """The secrets as plain JSON in the demo's home, which the page's
    process reads too (serve.py)."""

    path = HOME / "demo-secrets.json"

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data))


def planned() -> tuple[dict, dict]:
    facts = json.loads((HOME / "probe" / "latest" / "facts.json").read_text())
    model = build_model(yaml.safe_load((HOME / "config.yaml").read_text()))
    return model, build_plan(model, facts)


def probe() -> int:
    run = HOME / "probe" / time.strftime("run-%H%M%S")
    facts = parse(write_raw(run / "raw", DEMO.get("hardware", "large")))
    (run / "facts.json").write_text(json.dumps(facts))
    latest = HOME / "probe" / "latest"
    latest.unlink(missing_ok=True)
    latest.symlink_to(run.name)
    print(f"probe stored in {run}", flush=True)
    return 0


def checks(plan: dict, *_args: object, say=lambda r: print(r.line(), flush=True), **_kw: object) -> list[Result]:
    """verify's results: a pass or two per guest, and what the demo asks for."""
    guests = real.strip_notes(plan)["guests"]
    results = [Result("PASS", "the LAN cannot reach 8006 or 22")]
    for name in sorted(guests):
        results += [Result("PASS", f"{name} answers on its ports"), Result("PASS", f"{name}: other zones are refused")]
    results += [Result(*line) for line in DEMO.get("checks", [])]
    for r in results:
        say(r)
        time.sleep(DEMO.get("pace", 0) / 40)
    return results


def paced(name: str, stage: real.Stage) -> real.Stage:
    def run(ctx: real.Context) -> None:
        time.sleep(DEMO.get("pace", 0))
        fail = DEMO.get("fail") or {}
        if fail.get("stage") == name:
            print((HERE / "failures" / fail["output"]).read_text().rstrip("\n"), flush=True)
            raise real.ApplyError(name, fail["message"])
        stage(ctx)

    return run


def apply(args: list[str]) -> int:
    model, plan = planned()
    reach = pickle.loads(HOST.read_bytes()) if HOST.exists() else FakeReach()
    reach.calls = []
    tailnet = FakeTailnet()
    real.Tailnet = lambda client_id, client_secret: tailnet
    real.run_verify = checks
    os.environ.setdefault("TF_VAR_windows_iso", "local:iso/windows.iso")
    (HOME / "web").mkdir(exist_ok=True)
    ctx = real.Context(
        plan,
        model,
        HOME,
        TARGET,
        reach,
        Secrets(FileCodec()),
        render_all(plan, model),
        full="--full" in args,
        lan_host="192.0.2.10",
        remove_guests="--remove-guests" in args,
        close_lan="--close-lan" in args,
        open_lan="--open-lan" in args,
    )
    only = args[args.index("--stage") + 1] if "--stage" in args else None
    stages = [(name, paced(name, stage)) for name, stage in real.STAGES]
    try:
        # run_apply's own list is STAGES: the end of a full run is its too.
        real.STAGES[:] = stages
        real.run_apply(ctx, only="host" if ctx.close_lan or ctx.open_lan else only)
    finally:
        HOST.write_bytes(pickle.dumps(reach))
    return 0


def verify() -> int:
    _, plan = planned()
    results = checks(plan)
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "FAIL", "SKIP", "DEGRADED")}
    print(", ".join(f"{n} {s}" for s, n in counts.items()), flush=True)
    return 1 if counts["FAIL"] else 0


def main() -> int:
    action, args = sys.argv[1], sys.argv[2:]
    try:
        if action == "probe":
            return probe()
        if action == "apply":
            return apply(args)
        if action == "verify":
            return verify()
    except NullBillError as exc:
        for message in exc.messages:
            print(f"nullbill: {message}", file=sys.stderr, flush=True)
        return 1
    print(f"ran {action} {' '.join(args)}".rstrip(), flush=True)
    return 0


sys.exit(main())
