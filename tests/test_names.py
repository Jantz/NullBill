"""verify's check that the tailnet sends the plan's names to the router."""

from conftest import dns_status, fixture_config, getent

from nullbill import names
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes


def plan_of(facts):
    return strip_notes(build_plan(build_model(fixture_config("small")), facts("small")))


def answering(plan, rc, out, looked_up=None):
    vpn = plan["guests"]["vpn"]["vmid"]

    def run(target, command):
        if command == f"pct exec {vpn} -- getent ahostsv4 router.nullbill.internal":
            return looked_up or (0, getent("10.42.1.1", "router.nullbill.internal"), "")
        assert command == f"pct exec {vpn} -- tailscale dns status"
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
    assert [(r.status, r.name) for r in ok] == [("PASS", names.LABEL), ("PASS", names.ANSWERS)]

    other, _ = names.check(plan, "root@pve", answering(plan, 0, dns_status("nullbill.internal", "192.0.2.53")))
    assert other.status == "DEGRADED" and "sends nullbill.internal to 192.0.2.53, not 10.42.6.1" in other.detail
    none, _ = names.check(plan, "root@pve", answering(plan, 0, dns_status("example.com", "192.0.2.53")))
    assert none.status == "DEGRADED" and "to nowhere" in none.detail and "DNS write" in none.detail

    out = "\n=== MagicDNS configuration ===\n\nNo tailnet information available; make sure you're logged in to a tailnet.\n"
    waiting, asked = names.check(plan, "root@pve", answering(plan, 0, out))
    assert waiting.status == "SKIP" and "not on the tailnet yet" in waiting.detail and asked.status == "PASS"
    down, unasked = names.check(plan, "root@pve", answering(plan, 2, ""))
    assert down.status == "FAIL" and "no such container" in down.detail
    assert (unasked.status, unasked.name) == ("SKIP", names.ANSWERS)


def test_the_router_is_asked_for_a_name_the_way_a_devices_lookup_reaches_it(facts):
    """Tailscale's setting says where lookups go; whether an answer comes
    back from there is asked, from the guest every such lookup passes."""
    plan = plan_of(facts)
    status = dns_status("nullbill.internal", "10.42.6.1")

    def asked(looked_up):
        return names.check(plan, "root@pve", answering(plan, 0, status, looked_up))[1]

    assert asked((0, getent("10.42.1.1", "router.nullbill.internal"), "")) == names.Result("PASS", names.ANSWERS)
    # getent exits 2 and prints nothing when no answer comes.
    silent = asked((2, "", ""))
    assert (silent.status, silent.detail) == ("FAIL", "asked 10.42.6.1 for router.nullbill.internal: no answer")
    wrong = asked((0, getent("10.42.9.9", "router.nullbill.internal"), ""))
    assert (wrong.status, wrong.detail) == ("FAIL", "asked 10.42.6.1 for router.nullbill.internal: it answered 10.42.9.9, not 10.42.1.1")
    assert [(r.status, r.name) for r in names.skipped(plan, "no target")] == [("SKIP", names.LABEL), ("SKIP", names.ANSWERS)]


def test_names_are_recorded_for_the_page(tmp_path):
    assert not names.active(tmp_path)
    names.record(tmp_path, "nullbill.internal", "10.42.6.1")
    assert names.active(tmp_path)
    names.forget(tmp_path)
    names.forget(tmp_path)
    assert not names.active(tmp_path)
