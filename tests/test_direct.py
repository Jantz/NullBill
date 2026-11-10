"""Whether a device on mobile data reaches the vpn guest directly: what
nb-portmap on the router heard from the home router, set against the
addresses Tailscale offers for the vpn guest."""

import json

import pytest
from conftest import HOME_PUBLIC, ROUTER_CLOCK, direct_answers, fixture_config, guest_exec, portmap_state, tailscale_status

from nullbill import direct
from nullbill.flows import TAILSCALE_PORT
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

OURS = (HOME_PUBLIC, TAILSCALE_PORT)
CLOSED = {"method": None, "external": None, "why": {"NAT-PMP": "no answer", "UPnP": "no answer"}}
# A forward the server did not ask for cannot be seen from the server.
BY_HAND = f"or forward UDP 41642 to 192.168.1.57 there yourself and switch off {direct.SWITCH}"


def setup(facts, **config):
    model = build_model({**fixture_config("small"), **config})
    return strip_notes(build_plan(model, facts("small"))), model


def host(answers: dict[str, str], failing: dict[str, tuple[int, str, str]] | None = None):
    asked = []

    def run(target, command):
        asked.append(command)
        if failing and command in failing:
            return failing[command]
        return 0, answers[command], ""

    run.asked = asked
    return run


def test_what_tailscale_offers_is_read_from_its_status():
    status = json.loads(tailscale_status())
    # Not the guest's own address, which nothing outside can reach, and no IPv6: the zones carry none.
    assert direct.offered(status) == [OURS]
    assert direct.offered({"Self": {"Addrs": None}}) == [] and direct.offered({}) == []
    addrs = ["100.64.3.9:41642", "192.168.1.57:41642", "nonsense", "198.51.100.4:7"]
    assert direct.offered({"Self": {"Addrs": addrs}}) == [("198.51.100.4", 7)]


def test_the_commands_reach_both_guests_through_the_host(facts):
    plan, _ = setup(facts)
    assert direct.vpn_command(plan) == f"pct exec {plan['guests']['vpn']['vmid']} -- tailscale status --json"
    read = "sh -c 'cat /var/lib/nullbill-portmap/state.json && date +%s'"
    router = f"qm guest exec {plan['guests']['router']['vmid']} --timeout 15 -- {read}"
    assert direct.router_command(plan) == router


def test_direct_when_the_home_router_opened_the_port_and_tailscale_offers_it(facts):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan)))
    assert found == direct.Finding(direct.DIRECT, "", TAILSCALE_PORT, "192.168.1.57", "UPnP")
    assert direct.result(found) == [direct.Result("PASS", direct.LABEL)]
    assert direct.line(found) == "devices on any network reach the server directly: the home router sends UDP 41642 here (UPnP)"


def test_a_home_router_that_does_not_answer_leaves_relays_and_says_what_to_do(facts):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, portmap_state(**CLOSED))))
    assert (found.kind, found.method, found.address) == (direct.CLOSED, None, "192.168.1.57")
    assert found.detail == (
        "the home router did not open UDP 41642 (NAT-PMP: no answer, UPnP: no answer), so devices on mobile data go through "
        "Tailscale's relays; on the home router, turn on UPnP or NAT-PMP; or forward UDP 41642 to 192.168.1.57 there yourself "
        "and switch off Ask the home router for the port (Access page, Tailscale)"
    )
    # Relays carry everything, only slower: not a failure.
    assert direct.result(found) == [direct.Result("DEGRADED", direct.LABEL, found.detail)]
    assert direct.line(found) == found.detail


@pytest.mark.parametrize(
    ("state", "addrs", "kind", "words"),
    [
        # miniupnpd and upnpc on a router whose own address is a provider's shared one, or a second box's.
        (
            {**CLOSED, "why": dict.fromkeys(("NAT-PMP", "UPnP"), direct.NO_PUBLIC_ADDRESS)},
            None,
            direct.SHARED,
            "forward UDP 41642 on it to the home router, and on the home router to 192.168.1.57",
        ),
        # Routers that map the port anyway: their outside address gives it away.
        ({"external": "100.64.7.2:41642"}, ["198.51.100.4:1025"], direct.SHARED, "if that router is your provider's, devices on"),
        ({"external": "192.168.0.2:41642"}, ["198.51.100.4:41642"], direct.SHARED, "forward UDP 41642 on it to the home router"),
        # A public address that is not where the world sees the server.
        ({}, ["198.51.100.4:41642"], direct.SHARED, "at another address than the home router's own"),
        ({}, [f"{HOME_PUBLIC}:1025"], direct.UNSEEN, "leave it from port 1025"),
        ({}, ["10.42.6.11:41642"], direct.UNSEEN, "has found no public address yet"),
        ({**CLOSED, "why": {"NAT-PMP": "it offered port 41643: 41642 goes to another device"}}, None, direct.CLOSED, "another device"),
    ],
)
def test_an_open_port_counts_only_where_tailscale_offers_it(facts, state, addrs, kind, words):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, portmap_state(**state), addrs)))
    assert found.kind == kind and words in found.detail
    assert direct.result(found)[0].status == "DEGRADED"
    assert "relays" in found.detail or kind == direct.UNSEEN


@pytest.mark.parametrize(
    ("why", "advice"),
    [
        ({"NAT-PMP": "not offered", "UPnP": "no answer"}, f"turn on UPnP or NAT-PMP; {BY_HAND}"),
        # A FRITZ!Box until the device is allowed to share ports itself.
        ({"NAT-PMP": "no answer", "UPnP": direct.REFUSED}, f"allow the device at 192.168.1.57 to open ports by itself; {BY_HAND}"),
        (
            {"NAT-PMP": "no answer", "UPnP": "it already sends that port to another device"},
            "remove the forwarding of UDP 41642 to that device: the server asks again within a quarter of an hour",
        ),
        (
            {"NAT-PMP": "it offered port 41643: 41642 goes to another device", "UPnP": "no answer"},
            "remove the forwarding of UDP 41642 to that device: the server asks again within a quarter of an hour",
        ),
    ],
)
def test_what_to_do_on_the_home_router_follows_from_what_it_answered(facts, why, advice):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, portmap_state(**CLOSED | {"why": why}))))
    assert found.kind == direct.CLOSED and found.detail.endswith(f"; on the home router, {advice}")


def test_switched_off_nothing_can_be_checked_and_the_owner_is_told_where_to_forward(facts):
    plan, model = setup(facts, tailnet={"port_mapping": False})
    state = portmap_state(enabled=False, method=None, external=None, why={})
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, state)))
    assert found.detail == (
        "the home router is not asked for UDP 41642, so the server cannot tell whether the port is open; "
        "forward UDP 41642 to 192.168.1.57 on the home router yourself, or devices on mobile data go through Tailscale's relays"
    )
    assert found.kind == direct.OFF
    # A port forwarded by hand looks like a closed one from in here.
    assert direct.result(found) == [direct.Result("SKIP", direct.LABEL, found.detail)]
    # The router has not heard of the switch yet: what config.yaml says counts.
    assert direct.find(plan, model, "root@pve.internal", host(direct_answers(plan))).kind == direct.OFF


def test_off_the_tailnet_the_router_is_not_asked(facts):
    plan, model = setup(facts)
    run = host(direct_answers(plan, backend="NeedsLogin"))
    found = direct.find(plan, model, "root@pve.internal", run)
    assert found.kind == direct.WAITING and run.asked == [direct.vpn_command(plan)]
    assert direct.result(found) == [direct.Result("SKIP", direct.LABEL, "the vpn guest is not on the tailnet yet")]


def test_an_answer_that_cannot_be_read_or_is_old_fails(facts):
    plan, model = setup(facts)
    answers = direct_answers(plan)

    def kind_and_detail(**changes):
        found = direct.find(plan, model, "root@pve.internal", host(answers | changes.get("answers", {}), changes.get("failing")))
        assert direct.result(found)[0].status == "FAIL"
        return found.kind, found.detail

    # No guest agent in the router, as before its first updates stage.
    failing = {direct.router_command(plan): (255, "", "QEMU guest agent is not running\n")}
    assert kind_and_detail(failing=failing) == (direct.UNREAD, "cannot ask the router: QEMU guest agent is not running")
    failing = {direct.vpn_command(plan): (1, "", "Configuration file 'nodes/pve/lxc/2011.conf' does not exist\n")}
    assert kind_and_detail(failing=failing)[1].startswith("tailscale status on the vpn guest: Configuration file")
    # Before nb-portmap's first run there is no file; qm still exits 0.
    missing = guest_exec("", 1, "cat: /var/lib/nullbill-portmap/state.json: No such file or directory\n")
    detail = kind_and_detail(answers={direct.router_command(plan): missing})[1]
    assert detail == "nothing to read on the router: cat: /var/lib/nullbill-portmap/state.json: No such file or directory"
    late = json.dumps({"pid": 4242, "exited": 0})
    assert kind_and_detail(answers={direct.router_command(plan): late})[1] == "the router did not answer in time"
    other = guest_exec(f"not json\n{ROUTER_CLOCK}\n")
    assert kind_and_detail(answers={direct.router_command(plan): other})[1] == "the router's answer is not nb-portmap's"
    # The timer stopped: the mapping it reported has run out since.
    old = guest_exec(f"{json.dumps(portmap_state(at=ROUTER_CLOCK - 3 * 3600))}\n{ROUTER_CLOCK}\n")
    detail = kind_and_detail(answers={direct.router_command(plan): old})[1]
    assert detail == "nb-portmap on the router last ran 180 minutes ago; nullbill-portmap.timer runs it every 15"


def test_the_record_for_the_page_never_holds_the_homes_public_address(facts, tmp_path):
    plan, model = setup(facts)
    assert direct.recorded(tmp_path) is None
    cases = [(None, None), (portmap_state(**CLOSED), None), (None, [f"{HOME_PUBLIC}:1025"]), (None, ["198.51.100.4:41642"])]
    for state, addrs in cases:
        results = direct.check(plan, model, "root@pve.internal", host(direct_answers(plan, state, addrs)), tmp_path)
        text = (tmp_path / direct.STATE).read_text()
        assert HOME_PUBLIC not in text and "198.51.100.4" not in text
        assert not any(HOME_PUBLIC in r.detail or "198.51.100.4" in r.detail for r in results)
    found = direct.recorded(tmp_path)
    assert found["kind"] == direct.SHARED and found["address"] == "192.168.1.57" and found["port"] == TAILSCALE_PORT
    # Without a home, verify only reports.
    (tmp_path / direct.STATE).unlink()
    direct.check(plan, model, "root@pve.internal", host(direct_answers(plan)))
    assert direct.recorded(tmp_path) is None
    (tmp_path / direct.STATE).write_text("[]")
    assert direct.recorded(tmp_path) is None


def test_no_vpn_guest_no_check(facts):
    plan, model = setup(facts)
    assert direct.skipped(plan, "no target.ssh in config.yaml") == [direct.Result("SKIP", direct.LABEL, "no target.ssh in config.yaml")]
    del plan["guests"]["vpn"]
    assert direct.find(plan, model, "root@pve.internal", host({})) is None
    assert direct.result(None) == [] and direct.skipped(plan, "no target") == []
