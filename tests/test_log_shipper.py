"""log_shipper's nb-log-shipper: what it sends to Loki."""

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "ansible/roles/log_shipper/files/nb-log-shipper"
_loader = SourceFileLoader("nb_log_shipper", str(SCRIPT))
shipper = importlib.util.module_from_spec(importlib.util.spec_from_loader("nb_log_shipper", _loader))
_loader.exec_module(shipper)

CONF = {"host": "pve", "sources": [{"job": "auth", "match": ["SYSLOG_IDENTIFIER=sshd"]}]}
NOW = 1_800_000_000.0


def entry(age_s: float, message: str) -> dict:
    return {"SYSLOG_IDENTIFIER": "sshd", "_UID": "0", "MESSAGE": message, "__REALTIME_TIMESTAMP": str(int((NOW - age_s) * 1_000_000))}


def test_entries_older_than_loki_takes_are_left_out_not_retried_forever():
    """Loki refuses a push holding any entry over 7 days old (its default),
    and the shipper retries a refused batch; so a first start on a host with
    an older journal would never get past it."""
    body = json.loads(shipper.payload(CONF, [entry(8 * 86400, "old"), entry(60, "new")], now=NOW))
    assert [v[1] for s in body["streams"] for v in s["values"]] == ["new"]
    assert shipper.payload(CONF, [entry(8 * 86400, "old")], now=NOW) is None


def test_long_commands_reach_loki_whole_in_pushes_the_gate_takes():
    """journalctl's JSON has null for a field over 4 KiB unless --all; and a
    batch of long commands must not exceed the gate's 4m body limit, which
    would refuse it on every retry."""
    assert "--all" in shipper.journal_args({"sources": [{"match": ["SYSLOG_IDENTIFIER=nb-cmd"]}]}, None)
    long = "x" * 30_000
    entries = [entry(60, f"ssh: {i} {long}") for i in range(200)]
    runs = shipper.parts(entries)
    assert [e for run in runs for e in run] == entries and len(runs) > 1
    for run in runs:
        body = shipper.payload(CONF, run, now=NOW)
        assert len(body) < 2 << 20
    sent = [v[1] for run in runs for s in json.loads(shipper.payload(CONF, run, now=NOW))["streams"] for v in s["values"]]
    assert sent == [e["MESSAGE"] for e in entries]
