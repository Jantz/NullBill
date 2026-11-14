"""Whether a device on mobile data reaches the vpn guest directly: what
nb-portmap on the router heard from the home router, set against the
addresses Tailscale offers for the vpn guest, and the paths tailscaled on
the vpn guest has to the devices on the tailnet: the ones in use, and for an
idle device the one its answer to a ping came by."""

import json

import pytest
from conftest import HOME_PUBLIC, ROUTER_CLOCK, direct_answers, fixture_config, guest_exec, portmap_state, tailscale_peer, tailscale_status

from nullbill import direct
from nullbill.flows import TAILSCALE_PORT
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes

OURS = (HOME_PUBLIC, TAILSCALE_PORT)
# A phone on mobile data in use, seen at its carrier's address; a laptop on the home network; a tablet with no path in use.
AWAY = tailscale_peer("phone", "203.0.113.80:41641")
AT_HOME = tailscale_peer("laptop", "192.168.1.30:41641")
IDLE = tailscale_peer("tablet")
RELAY = {"tablet": "DERP(fra)"}
NOT_MEASURED = "no device away from home answered, so the path from mobile data was not measured"
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


def test_a_home_router_that_does_not_answer_says_what_it_read_and_what_to_do(facts):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, portmap_state(**CLOSED))))
    assert (found.kind, found.method, found.address) == (direct.CLOSED, None, "192.168.1.57")
    assert found.detail == (
        "the home router did not open UDP 41642 (NAT-PMP: no answer, UPnP: no answer); "
        "on the home router, turn on UPnP or NAT-PMP; or forward UDP 41642 to 192.168.1.57 there yourself "
        "and switch off Ask the home router for the port (Access page, Tailscale)"
    )
    # A closed port does not put every phone on a relay, and no device is
    # connected to show which it is: nothing here confirms a problem.
    assert direct.result(found) == [direct.Result("SKIP", direct.LABEL, f"{NOT_MEASURED}; {found.detail}")]
    assert direct.line(found) == f"{NOT_MEASURED}; {found.detail}"


def test_the_paths_in_use_are_read_from_tailscales_status():
    status = json.loads(tailscale_status(peers=[IDLE, AWAY, AT_HOME]))
    # A device without a path in use is not in it: nothing says how it would connect.
    assert direct.devices(status) == (
        direct.Device("laptop", direct.DIRECT, direct.HOME),
        direct.Device("phone", direct.DIRECT, direct.AWAY),
    )
    # A provider's shared address is no home network's, but it is not the internet either.
    shared = json.loads(tailscale_status(peers=[tailscale_peer("phone", "100.64.9.9:41641")]))
    assert direct.devices(shared) == (direct.Device("phone", direct.DIRECT, direct.HOME),)
    # An IPv6 address says nothing about where the device is.
    six = json.loads(tailscale_status(peers=[tailscale_peer("phone", "[2001:db8::80]:41641")]))
    assert direct.devices(six) == (direct.Device("phone", direct.DIRECT),)
    assert direct.devices({}) == () and direct.devices({"Peer": None}) == ()
    nameless = {"Peer": {"k": {"Active": True, "CurAddr": "192.168.1.9:7", "DNSName": "pixel.example-tailnet.ts.net."}, "j": "nonsense"}}
    assert direct.devices(nameless) == (direct.Device("pixel", direct.DIRECT, direct.HOME),)


def test_the_devices_to_ping_are_those_on_the_tailnet_without_a_direct_path(facts):
    plan, _ = setup(facts)
    via_peer = tailscale_peer("camera", peer_relay="198.51.100.9:7:42")
    off = tailscale_peer("desktop", online=False)
    odd = tailscale_peer("watch") | {"TailscaleIPs": ["$(reboot)", "fd7a:115c:a1e0::9"]}
    status = json.loads(tailscale_status(peers=[IDLE, AWAY, AT_HOME, via_peer, off, odd]))
    # By its IPv4 address on the tailnet; what is no address never reaches the shell.
    asked = direct.idle(status)
    assert {address: peer["HostName"] for address, peer in asked.items()} == {"100.64.0.5": "camera", "100.64.0.2": "tablet"}
    one = 'tailscale ping --c 3 --timeout 2s "$a" 2>&1 | sed "s/^/$a /"'
    script = f"for a in 100.64.0.5 100.64.0.2; do ({one}) & done; wait"
    assert direct.ping_command(plan, list(asked)) == f"pct exec {plan['guests']['vpn']['vmid']} -- sh -c '{script}'"
    many = json.loads(tailscale_status(peers=[tailscale_peer(f"phone-{n:02}") for n in range(12)]))
    assert len(direct.idle(many)) == direct.MOST
    assert direct.idle({}) == {}


def test_a_pinged_device_is_judged_by_the_way_its_last_answer_came():
    status = json.loads(tailscale_status(peers=[tailscale_peer(n) for n in ("phone", "tablet", "laptop", "camera", "watch", "tv")]))
    asked = direct.idle(status)
    answers = "\n".join(
        [
            # Tailscale's own lines (cmd/tailscale/cli/ping.go): the first answer comes through a relay.
            "100.64.0.2 pong from phone (100.64.0.2) via DERP(fra) in 38ms",
            "100.64.0.2 pong from phone (100.64.0.2) via 203.0.113.80:41641 in 12ms",
            "100.64.0.3 pong from tablet (100.64.0.3) via DERP(fra) in 38ms",
            "100.64.0.3 pong from tablet (100.64.0.3, 52001) via DERP(fra) in 41ms",
            "100.64.0.3 direct connection not established",
            "100.64.0.4 pong from laptop (100.64.0.4) via 192.168.1.30:41641 in 1ms",
            "100.64.0.5 pong from camera (100.64.0.5) via peer-relay(198.51.100.9:7:vni:42) in 9ms",
            # Asleep: on the tailnet as far as Tailscale's servers know, and silent.
            '100.64.0.6 ping "100.64.0.6" timed out',
            "100.64.0.6 no reply",
            # A line this does not know says nothing.
            "100.64.0.7 pong from tv (100.64.0.7) via somewhere in 3ms",
            "100.64.0.9 pong from stranger (100.64.0.9) via 203.0.113.99:1 in 3ms",
        ]
    )
    assert direct.pongs(answers)["100.64.0.3"] == "DERP(fra)"
    assert direct.devices(status, asked, answers) == (
        direct.Device("camera", direct.RELAYED),
        direct.Device("laptop", direct.DIRECT, direct.HOME),
        direct.Device("phone", direct.DIRECT, direct.AWAY),
        direct.Device("tablet", direct.RELAYED),
    )
    # tailscale's ping failing as a whole measures nothing, and claims nothing.
    assert direct.devices(status, asked, "sh: 1: tailscale: not found\n") == ()


@pytest.mark.parametrize(
    "state",
    [
        portmap_state(**CLOSED),
        # The owner's own case: a second router before the home router, the port forwarded on both by hand.
        portmap_state(**{**CLOSED, "why": dict.fromkeys(("NAT-PMP", "UPnP"), direct.NO_PUBLIC_ADDRESS)}),
        portmap_state(external="192.168.0.2:41642"),
        portmap_state(enabled=False, method=None, external=None, why={}),
    ],
)
def test_a_device_away_from_home_on_a_direct_path_settles_it_whatever_the_home_router_said(facts, state):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, state, peers=[AWAY, AT_HOME, IDLE], pongs=RELAY)))
    assert found.kind != direct.DIRECT and direct.verdict(found) == direct.MEASURED
    said = "phone is connected directly from outside the home network"
    assert direct.result(found) == [direct.Result("PASS", direct.LABEL, said)]
    assert direct.line(found).startswith(f"{said}; ")


def test_a_device_that_answers_only_through_a_relay_is_what_degrades_the_check(facts):
    plan, model = setup(facts)
    peers = [IDLE, AT_HOME, tailscale_peer("phone")]
    answers = direct_answers(plan, portmap_state(**CLOSED), peers=peers, pongs=RELAY | {"phone": "DERP(nue)"})
    run = host(answers)
    found = direct.find(plan, model, "root@pve.internal", run)
    # The idle devices were pinged, in one go, and the laptop left alone.
    assert [c for c in run.asked if "tailscale ping" in c] == [direct.ping_command(plan, ["100.64.0.4", "100.64.0.2"])]
    assert direct.verdict(found) == direct.ON_RELAY
    [result] = direct.result(found)
    assert result.status == "DEGRADED"
    assert result.detail.startswith(
        "phone and tablet answer only through a Tailscale relay right now; the home router did not open UDP 41642"
    )
    # A device at home on a direct path says nothing about mobile data.
    home_only = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, portmap_state(**CLOSED), peers=[AT_HOME])))
    assert direct.verdict(home_only) == direct.UNMEASURED
    assert direct.result(home_only)[0].status == "SKIP"


def test_an_idle_phone_on_mobile_data_is_measured_by_pinging_it(facts):
    # The owner's own case: the port forwarded by hand and not asked for, a
    # PC at home in use, and the phone on mobile data in a pocket.
    plan, model = setup(facts, tailnet={"port_mapping": False})
    state = portmap_state(enabled=False, method=None, external=None, why={})
    peers = [AT_HOME, tailscale_peer("phone")]
    found = direct.find(
        plan, model, "root@pve.internal", host(direct_answers(plan, state, peers=peers, pongs={"phone": "203.0.113.80:41641"}))
    )
    assert found.kind == direct.OFF and direct.verdict(found) == direct.MEASURED
    assert found.devices == (direct.Device("laptop", direct.DIRECT, direct.HOME), direct.Device("phone", direct.DIRECT, direct.AWAY))
    assert direct.result(found) == [direct.Result("PASS", direct.LABEL, "phone is connected directly from outside the home network")]
    # Asleep, it does not answer: nothing is claimed about it either way.
    silent = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, state, peers=peers)))
    assert silent.devices == (direct.Device("laptop", direct.DIRECT, direct.HOME),) and direct.verdict(silent) == direct.UNMEASURED
    assert direct.result(silent)[0].status == "SKIP"
    # A ping that cannot be run leaves what tailscaled has in use.
    answers = direct_answers(plan, state, peers=peers)
    failing = {c: (255, "", "Connection timed out\n") for c in answers if "tailscale ping" in c}
    assert direct.find(plan, model, "root@pve.internal", host(answers, failing)).devices == silent.devices


def test_an_open_port_passes_without_a_device_and_a_relayed_device_does_not_undo_it(facts):
    plan, model = setup(facts)
    found = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, peers=[IDLE], pongs=RELAY)))
    assert found.kind == direct.DIRECT and direct.verdict(found) == direct.OPEN
    assert direct.result(found) == [direct.Result("PASS", direct.LABEL)]


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
        ({"external": "100.64.7.2:41642"}, ["198.51.100.4:1025"], direct.SHARED, "if it is your provider's, only the provider can"),
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
    # What the server read about the port, and no word on paths it did not see.
    assert direct.result(found) == [direct.Result("SKIP", direct.LABEL, f"{NOT_MEASURED}; {found.detail}")]
    assert "relay" not in found.detail


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
        "to have it open, forward UDP 41642 to 192.168.1.57 on the home router yourself"
    )
    assert found.kind == direct.OFF
    # A port forwarded by hand looks like a closed one from in here.
    assert direct.result(found) == [direct.Result("SKIP", direct.LABEL, f"{NOT_MEASURED}; {found.detail}")]
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
    assert kind_and_detail(failing=failing)[1].startswith("cannot read Tailscale on the vpn guest: Configuration file")
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


def test_the_record_for_the_page_never_holds_a_public_address(facts, tmp_path):
    plan, model = setup(facts)
    assert direct.recorded(tmp_path) is None
    cases = [(None, None), (portmap_state(**CLOSED), None), (None, [f"{HOME_PUBLIC}:1025"]), (None, ["198.51.100.4:41642"])]
    for state, addrs in cases:
        run = host(direct_answers(plan, state, addrs, peers=[AWAY, AT_HOME]))
        results = direct.check(plan, model, "root@pve.internal", run, tmp_path)
        text = (tmp_path / direct.STATE).read_text()
        assert HOME_PUBLIC not in text and "198.51.100.4" not in text and "203.0.113.80" not in text and "192.168.1.30" not in text
        assert not any(a in r.detail for r in results for a in (HOME_PUBLIC, "198.51.100.4", "203.0.113.80"))
    found = direct.recorded(tmp_path)
    assert found["kind"] == direct.SHARED and found["address"] == "192.168.1.57" and found["port"] == TAILSCALE_PORT
    assert found["devices"] == [{"name": "laptop", "path": "direct", "where": "home"}, {"name": "phone", "path": "direct", "where": "away"}]


def test_the_record_says_when_it_was_read_and_when_a_device_away_was_last_direct(facts, tmp_path):
    plan, model = setup(facts)
    closed = portmap_state(**CLOSED)
    seen = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, closed, peers=[AWAY])))
    direct.record(tmp_path, seen, now=lambda: 1000.4)
    kept = direct.recorded(tmp_path)
    assert (kept["verdict"], kept["at"], kept["seen"]) == (direct.MEASURED, 1000, {"names": ["phone"], "at": 1000})
    # Later nobody is connected: what was measured then stays, with its time.
    idle = direct.find(plan, model, "root@pve.internal", host(direct_answers(plan, closed)))
    direct.record(tmp_path, idle, now=lambda: 5000)
    kept = direct.recorded(tmp_path)
    assert (kept["verdict"], kept["at"], kept["seen"]) == (direct.UNMEASURED, 5000, {"names": ["phone"], "at": 1000})
    assert kept["devices"] == []
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
