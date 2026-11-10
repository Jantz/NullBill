"""nb-portmap (ansible/roles/router/files) against what Debian 12's natpmpc
and upnpc print. The outputs are those of natpmpc 20150609 and upnpc 2.2.4
talking to miniupnpd 2.3.1, with documentation addresses put in;
tools/check-guests runs the real tools against a stand-in home router."""

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from nullbill import direct
from nullbill.flows import SSDP_REPLY_PORT, TAILSCALE_PORT

SCRIPT = Path(__file__).parent.parent / "ansible" / "roles" / "router" / "files" / "nb-portmap"
GATEWAY, ADDRESS, PUBLIC = "192.168.1.1", "192.168.1.57", "203.0.113.7"
SETTINGS = {"enabled": True, "port": TAILSCALE_PORT, "ssdp_port": SSDP_REPLY_PORT, "uplink": "nb-uplink"}
PORT = str(TAILSCALE_PORT)


def pmp_mapped(public_port: int = TAILSCALE_PORT, lifetime: int = 3600) -> str:
    return (
        "initnatpmp() returned 0 (SUCCESS)\n"
        f"using gateway : {GATEWAY}\n"
        "sendpublicaddressrequest returned 2 (SUCCESS)\n"
        "readnatpmpresponseorretry returned 0 (OK)\n"
        f"Public IP address : {PUBLIC}\n"
        "epoch = 2770\n"
        "sendnewportmappingrequest returned 12 (SUCCESS)\n"
        "readnatpmpresponseorretry returned 0 (OK)\n"
        f"Mapped public port {public_port} protocol UDP to local port {PORT} liftime {lifetime}\n"
        "epoch = 2770\n"
        "closenatpmp() returned 0 (SUCCESS)\n"
    )


def pmp_failed(why: str, code: int) -> str:
    return (
        f"readnatpmpresponseorretry() failed : {why}\n"
        "  errno=0 'Success'\n"
        "initnatpmp() returned 0 (SUCCESS)\n"
        f"using gateway : {GATEWAY}\n"
        "sendpublicaddressrequest returned 2 (SUCCESS)\n"
        f"readnatpmpresponseorretry returned {code} (FAILED)\n"
    )


UPNP_HEAD = (
    "upnpc : miniupnpc library test client, version 2.2.4.\n"
    " (c) 2005-2022 Thomas Bernard.\n"
    "Go to http://miniupnp.free.fr/ or https://miniupnp.tuxfamily.org/\n"
    "for more information.\n"
)
UPNP_FOUND = (
    UPNP_HEAD + "List of UPNP devices found on the network :\n"
    f" desc: http://{GATEWAY}:39815/rootDesc.xml\n"
    " st: urn:schemas-upnp-org:device:InternetGatewayDevice:1\n\n"
    f"Found valid IGD : http://{GATEWAY}:39815/ctl/IPConn\n"
    f"Local LAN ip address : {ADDRESS}\n"
)
UPNP_NONE = UPNP_HEAD + "No IGD UPnP Device found on the network !\n"
UPNP_NOT_CONNECTED = (
    "No valid UPNP Internet Gateway Device found.\n" + UPNP_HEAD + "List of UPNP devices found on the network :\n"
    f" desc: http://{GATEWAY}:40225/rootDesc.xml\n"
    " st: urn:schemas-upnp-org:device:InternetGatewayDevice:1\n\n"
    f"Found a (not connected?) IGD : http://{GATEWAY}:40225/ctl/IPConn\n"
)


def upnp_mapped(internal: str = ADDRESS, duration: int = 3600) -> str:
    return (
        UPNP_FOUND + f"ExternalIPAddress = {PUBLIC}\n"
        f"InternalIP:Port = {internal}:{PORT}\n"
        f"external {PUBLIC}:{PORT} UDP is redirected to internal {internal}:{PORT} (duration={duration})\n"
    )


# What each prints when a mapping is given back, also when none was there.
PMP_GONE = pmp_mapped(public_port=0, lifetime=0)
UPNP_GONE = UPNP_FOUND + "UPNP_DeletePortMapping() returned : 0\n"


def upnp_failed(code: int, name: str) -> str:
    return UPNP_FOUND + f"ExternalIPAddress = {PUBLIC}\nAddPortMapping({PORT}, {PORT}, {ADDRESS}) failed with code {code} ({name})\n"


@pytest.fixture
def portmap(monkeypatch):
    # No __pycache__ next to the script the role copies.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    loader = importlib.machinery.SourceFileLoader("nb_portmap", str(SCRIPT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class Tools:
    """Stands in for ip, natpmpc and upnpc: records each command line and
    answers with what the real tool prints."""

    def __init__(self, natpmpc: str = "", upnpc: str = UPNP_NONE, uplink: bool = True):
        self.answers = {"natpmpc": natpmpc, "upnpc": upnpc}
        self.uplink = uplink
        self.calls = []

    def __call__(self, argv, wait):
        if argv[0] == "ip":
            if not self.uplink:
                return "[]"
            route = [{"dst": "default", "gateway": GATEWAY, "dev": "nb-uplink", "protocol": "dhcp", "prefsrc": ADDRESS, "flags": []}]
            addr = [{"ifindex": 2, "ifname": "nb-uplink", "addr_info": [{"family": "inet", "local": ADDRESS, "prefixlen": 24}]}]
            return json.dumps(route if "route" in argv else addr)
        self.calls.append(argv)
        answer = self.answers[argv[0]]
        return answer(argv) if callable(answer) else answer


def ask(portmap, monkeypatch, tools, settings=None, before=None):
    monkeypatch.setattr(portmap, "run", tools)
    return portmap.once(settings or SETTINGS, before or {})


def test_nat_pmp_is_asked_first_and_its_mapping_is_enough(portmap, monkeypatch):
    tools = Tools(natpmpc=pmp_mapped())
    state = ask(portmap, monkeypatch, tools)
    assert tools.calls == [["natpmpc", "-g", GATEWAY, "-a", PORT, PORT, "udp", "3600"]]
    assert (state["method"], state["external"], state["why"]) == ("NAT-PMP", f"{PUBLIC}:{PORT}", {})
    assert (state["address"], state["gateway"], state["port"], state["enabled"]) == (ADDRESS, GATEWAY, TAILSCALE_PORT, True)


def test_upnp_is_asked_when_nat_pmp_gives_nothing(portmap, monkeypatch):
    tools = Tools(natpmpc=pmp_failed("the gateway does not support nat-pmp", -7), upnpc=upnp_mapped())
    state = ask(portmap, monkeypatch, tools)
    # From the uplink, and with the search answered on the port the router's ruleset lets in.
    assert tools.calls[1] == ["upnpc", "-m", "nb-uplink", "-z", "1901", "-e", "nullbill", "-a", ADDRESS, PORT, PORT, "UDP", "3600"]
    assert (state["method"], state["external"]) == ("UPnP", f"{PUBLIC}:{PORT}")
    assert state["why"] == {"NAT-PMP": "not offered"}


def test_a_router_that_only_keeps_mappings_for_good_is_asked_again_without_a_lease(portmap, monkeypatch):
    def upnpc(argv):
        return upnp_failed(725, "OnlyPermanentLeasesSupported") if argv[-1] == "3600" else upnp_mapped(duration=0)

    tools = Tools(upnpc=upnpc)
    state = ask(portmap, monkeypatch, tools)
    assert [c[-1] for c in tools.calls if c[0] == "upnpc"] == ["3600", "UDP"]
    assert state["method"] == "UPnP"


@pytest.mark.parametrize(
    ("natpmpc", "upnpc", "why"),
    [
        ("", UPNP_NONE, {"NAT-PMP": "no answer", "UPnP": "no answer"}),
        # A home router whose own address is not public: no port to open.
        (pmp_failed("network failure", -52), UPNP_NOT_CONNECTED, dict.fromkeys(("NAT-PMP", "UPnP"), direct.NO_PUBLIC_ADDRESS)),
        (
            pmp_failed("not authorized", -51),
            upnp_failed(606, "Action not authorized"),
            dict.fromkeys(("NAT-PMP", "UPnP"), "it does not let this device open ports"),
        ),
        ("", upnp_failed(718, "ConflictInMappingEntry"), {"NAT-PMP": "no answer", "UPnP": "it already sends that port to another device"}),
        ("", upnp_failed(501, "Action Failed"), {"NAT-PMP": "no answer", "UPnP": "it answered error 501 (Action Failed)"}),
        # The mapping read back names another device: not ours.
        ("", upnp_mapped(internal="192.168.1.50"), {"NAT-PMP": "no answer", "UPnP": "it already sends that port to another device"}),
    ],
)
def test_why_the_home_router_did_not_open_the_port(portmap, monkeypatch, natpmpc, upnpc, why):
    state = ask(portmap, monkeypatch, Tools(natpmpc=natpmpc, upnpc=upnpc))
    assert (state["method"], state["external"], state["why"]) == (None, None, why)


def test_a_mapping_without_a_public_address_is_given_back(portmap, monkeypatch):
    # Nothing to set against the addresses Tailscale offers, so it could not be reported as open.
    nameless = upnp_mapped().replace(f"ExternalIPAddress = {PUBLIC}\n", "GetExternalIPAddress failed.\n")
    nameless = nameless.replace(f"external {PUBLIC}:", "external :")
    tools = Tools(natpmpc=pmp_mapped().replace(f"Public IP address : {PUBLIC}\n", ""), upnpc=nameless)
    state = ask(portmap, monkeypatch, tools)
    assert [c[-4:] if c[0] == "natpmpc" else c[-3:] for c in tools.calls] == [
        [PORT, PORT, "udp", "3600"],
        [PORT, PORT, "udp", "0"],
        [PORT, "UDP", "3600"],
        ["-d", PORT, "UDP"],
    ]
    assert state["why"] == dict.fromkeys(("NAT-PMP", "UPnP"), "it mapped the port without saying its public address")
    assert (state["method"], state["held"]) == (None, [])


def test_another_public_port_is_given_back(portmap, monkeypatch):
    # Tailscale tells devices the port its servers see, never this one.
    tools = Tools(natpmpc=pmp_mapped(public_port=41643))
    state = ask(portmap, monkeypatch, tools)
    assert tools.calls[1] == ["natpmpc", "-g", GATEWAY, "-a", "0", PORT, "udp", "0"]
    assert state["why"]["NAT-PMP"] == f"it offered port 41643: {PORT} goes to another device"
    assert direct.OTHER_DEVICE in state["why"]["NAT-PMP"]
    assert state["method"] is None


def test_switched_off_it_asks_for_nothing_and_gives_back_what_it_held(portmap, monkeypatch):
    off = SETTINGS | {"enabled": False}
    tools = Tools(natpmpc=PMP_GONE, upnpc=UPNP_GONE)
    state = ask(portmap, monkeypatch, tools, off)
    assert tools.calls == [] and (state["enabled"], state["method"]) == (False, None)
    # Where a forward made by hand goes is still known.
    assert state["address"] == ADDRESS

    state = ask(portmap, monkeypatch, tools, off, {"held": ["NAT-PMP"]})
    assert tools.calls == [["natpmpc", "-g", GATEWAY, "-a", PORT, PORT, "udp", "0"]] and state["held"] == []
    tools.calls.clear()
    state = ask(portmap, monkeypatch, tools, off, {"held": ["UPnP"]})
    assert tools.calls == [["upnpc", "-m", "nb-uplink", "-z", "1901", "-d", PORT, "UDP"]] and state["held"] == []


def test_every_mapping_it_may_still_hold_stays_on_the_record_until_given_back(portmap, monkeypatch):
    # Some home routers keep a UPnP mapping until told otherwise, so the record must not lose one.
    mapped = ask(portmap, monkeypatch, Tools(upnpc=upnp_mapped()))
    assert mapped["held"] == ["UPnP"]
    # The home router stops answering, or answers over the other protocol: the earlier mapping may still be there.
    silent = ask(portmap, monkeypatch, Tools(), before=mapped)
    assert (silent["method"], silent["held"]) == (None, ["UPnP"])
    both = ask(portmap, monkeypatch, Tools(natpmpc=pmp_mapped()), before=silent)
    assert (both["method"], both["held"]) == ("NAT-PMP", ["NAT-PMP", "UPnP"])
    # Switched off while the uplink is down: nobody to tell yet.
    off = SETTINGS | {"enabled": False}
    down = Tools(uplink=False)
    waiting = ask(portmap, monkeypatch, down, off, both)
    assert down.calls == [] and waiting["held"] == ["NAT-PMP", "UPnP"]
    # A home router that does not answer has not given it back: asked again at the next run.
    tools = Tools(natpmpc=PMP_GONE)
    silent = ask(portmap, monkeypatch, tools, off, waiting)
    assert [c[0] for c in tools.calls] == ["natpmpc", "upnpc"] and silent["held"] == ["UPnP"]
    # One without NAT-PMP holds no such mapping.
    gone = Tools(natpmpc=pmp_failed("the gateway does not support nat-pmp", -7), upnpc=UPNP_GONE)
    assert ask(portmap, monkeypatch, gone, off, waiting)["held"] == []
    tools = Tools(upnpc=UPNP_GONE)
    done = ask(portmap, monkeypatch, tools, off, silent)
    assert [c[0] for c in tools.calls] == ["upnpc"] and done["held"] == []
    # A record that is not this script's is no reason to ask for anything.
    for odd in ({"held": "UPnP"}, {"held": ["PCP", 7]}, {"held": None}, {"method": "UPnP"}):
        tools = Tools()
        assert ask(portmap, monkeypatch, tools, off, odd)["held"] == [] and tools.calls == []


def test_without_an_uplink_nothing_is_asked(portmap, monkeypatch):
    tools = Tools(uplink=False)
    state = ask(portmap, monkeypatch, tools)
    assert tools.calls == [] and state["method"] is None
    assert state["why"]["UPnP"] == "nb-uplink has no address or no default route"


def test_a_missing_or_hanging_tool_is_no_answer(portmap):
    assert portmap.run(["nb-no-such-tool"], 1) == ""
    assert portmap.run([sys.executable, "-c", "import time; print('late', flush=True); time.sleep(30)"], 1) == ""
    assert portmap.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"], 10) == "out\nerr\n"


def test_main_writes_the_state_for_verify_and_exits_0_whatever_the_answer(portmap, monkeypatch, tmp_path, capsys):
    settings = tmp_path / "portmap.json"
    settings.write_text(json.dumps(SETTINGS))
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(sys, "argv", ["nb-portmap", str(settings)])

    monkeypatch.setattr(portmap, "run", Tools())
    assert portmap.main() == 0
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert state["method"] is None and state["at"] > 0
    assert capsys.readouterr().out == f"the home router did not open UDP {PORT}: NAT-PMP: no answer; UPnP: no answer\n"

    monkeypatch.setattr(portmap, "run", Tools(natpmpc=pmp_mapped()))
    assert portmap.main() == 0
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert (state["method"], state["held"]) == ("NAT-PMP", ["NAT-PMP"])
    assert capsys.readouterr().out == f"the home router sends UDP {PORT} here (NAT-PMP, {PUBLIC}:{PORT})\n"
    assert not (tmp_path / "run" / "state.part").exists()

    # Switched off after it held a mapping: the state file says which to give back.
    settings.write_text(json.dumps(SETTINGS | {"enabled": False}))
    tools = Tools()
    monkeypatch.setattr(portmap, "run", tools)
    assert portmap.main() == 0
    assert tools.calls == [["natpmpc", "-g", GATEWAY, "-a", PORT, PORT, "udp", "0"]]
    off = f"not asking the home router for UDP {PORT} (tailnet.port_mapping is off)"
    assert capsys.readouterr().out == f"{off}; it has not answered about the mapping made over NAT-PMP yet\n"
    assert json.loads((tmp_path / "run" / "state.json").read_text())["held"] == ["NAT-PMP"]
    monkeypatch.setattr(portmap, "run", Tools(natpmpc=PMP_GONE))
    assert portmap.main() == 0
    assert capsys.readouterr().out == f"{off}\n"
    assert json.loads((tmp_path / "run" / "state.json").read_text())["held"] == []


def test_its_words_are_the_ones_direct_py_reads(portmap):
    assert portmap.NO_PUBLIC_ADDRESS == direct.NO_PUBLIC_ADDRESS
    assert portmap.REFUSED == direct.REFUSED
    assert direct.OTHER_DEVICE in portmap.TAKEN
