"""verify's check that the tailnet sends the plan's names to the router."""

from conftest import dns_status, fixture_config

from nullbill import names
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes


def plan_of(facts):
    return strip_notes(build_plan(build_model(fixture_config("small")), facts("small")))


def answering(plan, rc, out):
    def run(target, command):
        assert command == f"pct exec {plan['guests']['vpn']['vmid']} -- tailscale dns status"
        return rc, out, "pct: no such container" if rc else ""

    return run


def test_routes_are_read_from_tailscale_dns_status():
    out = dns_status("nullbill.internal", "10.42.6.1") + "Fallback Resolvers:\n  - 192.0.2.1\n"
    assert names.routes(out) == {"nullbill.internal": ["10.42.6.1"]}
    assert names.routes(out.replace("10.42.6.1", "10.42.6.1:53")) == {"nullbill.internal": ["10.42.6.1"]}
    assert names.routes("Split DNS Routes:\n  (no routes configured: split DNS disabled)\n") == {}


def test_the_tailnet_must_send_the_domain_to_the_router(facts):
    plan = plan_of(facts)
    ok = names.check(plan, "root@pve", answering(plan, 0, dns_status("nullbill.internal", "10.42.6.1")))
    assert [(r.status, r.name) for r in ok] == [("PASS", names.LABEL)]

    (other,) = names.check(plan, "root@pve", answering(plan, 0, dns_status("nullbill.internal", "192.0.2.53")))
    assert other.status == "DEGRADED" and "sends nullbill.internal to 192.0.2.53, not 10.42.6.1" in other.detail
    (none,) = names.check(plan, "root@pve", answering(plan, 0, dns_status("example.com", "192.0.2.53")))
    assert none.status == "DEGRADED" and "to nowhere" in none.detail and "DNS write" in none.detail

    out = "\n=== MagicDNS configuration ===\n\nNo tailnet information available; make sure you're logged in to a tailnet.\n"
    (waiting,) = names.check(plan, "root@pve", answering(plan, 0, out))
    assert waiting.status == "SKIP" and "not on the tailnet yet" in waiting.detail
    (down,) = names.check(plan, "root@pve", answering(plan, 2, ""))
    assert down.status == "FAIL" and "no such container" in down.detail


def test_names_are_recorded_for_the_page(tmp_path):
    assert not names.active(tmp_path)
    names.record(tmp_path, "nullbill.internal", "10.42.6.1")
    assert names.active(tmp_path)
    names.forget(tmp_path)
    names.forget(tmp_path)
    assert not names.active(tmp_path)
