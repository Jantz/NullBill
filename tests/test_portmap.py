"""nb-portmap (ansible/roles/router/files): its own NAT-PMP against a
gateway's answers by RFC 6886, and what Debian's upnpc prints. upnpc's
outputs are those of upnpc 2.2.4 talking to miniupnpd 2.3.1, with
documentation addresses put in; tools/check-guests runs nb-portmap against
a stand-in home router and against miniupnpd."""

import importlib.machinery
import importlib.util
import json
import socket
import struct
import sys
from pathlib import Path

import pytest

from nullbill import direct
from nullbill.flows import SSDP_REPLY_PORT, TAILSCALE_PORT

SCRIPT = Path(__file__).parent.parent / "ansible" / "roles" / "router" / "files" / "nb-portmap"
GATEWAY, ADDRESS, PUBLIC = "192.168.1.1", "192.168.1.57", "203.0.113.7"
SETTINGS = {"enabled": True, "port": TAILSCALE_PORT, "ssdp_port": SSDP_REPLY_PORT, "uplink": "nb-uplink"}
PORT = str(TAILSCALE_PORT)


def pmp_mapped(public_port: int = TAILSCALE_PORT, lifetime: int = 3600, public: str = PUBLIC) -> dict:
    """A gateway that says its address and grants this port for so long,
    whatever is asked for."""
    return {"address": public, "port": public_port, "lifetime": lifetime}


def pmp_failed(code: int) -> dict:
    """A gateway that answers every request with this result code."""
    return {"result": code}


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


# upnpc 2.2.8 (Debian 13) and miniupnpd 2.3.9 on an address a provider shares.
UPNP_RESERVED = (
    "upnpc: miniupnpc library test client, version 2.2.8.\n"
    " (c) 2005-2024 Thomas Bernard.\n"
    "More information at https://miniupnp.tuxfamily.org/ or http://miniupnp.free.fr/\n\n"
    "List of UPNP devices found on the network :\n"
    f" desc: http://{GATEWAY}:42469/rootDesc.xml\n"
    " st: urn:schemas-upnp-org:device:InternetGatewayDevice:1\n\n"
    f"Found an IGD with a reserved IP address (100.64.7.2) : http://{GATEWAY}:42469/ctl/IPConn\n"
    f"Local LAN ip address : {ADDRESS}\n"
    "ExternalIPAddress = 100.64.7.2\n"
    f"AddPortMapping({TAILSCALE_PORT}, {TAILSCALE_PORT}, {ADDRESS}) failed with code 501 (Action Failed)\n"
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
    """Stands in for ip, the gateway's NAT-PMP and upnpc: records each
    mapping request (as `natpmpc -a public private protocol lifetime` would
    be typed) and each upnpc command line, and answers as the real ones do.
    `natpmpc` is "" for a gateway that says nothing, None for one where
    nothing listens."""

    def __init__(self, natpmpc: dict | str | None = "", upnpc: str = UPNP_NONE, uplink: bool = True):
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

    def exchange(self, gateway, packet):
        kind = packet[1]
        argv = None
        if kind:
            inner, outer, lease = struct.unpack("!HHI", packet[4:])
            argv = ["natpmpc", "-g", gateway, "-a", str(outer), str(inner), {1: "udp", 2: "tcp"}[kind], str(lease)]
            self.calls.append(argv)
        answer = self.answers["natpmpc"]
        answer = answer(argv) if callable(answer) else answer
        if not isinstance(answer, dict):
            return b"" if answer is None else None
        head = struct.pack("!BBHI", 0, 128 + kind, answer.get("result", 0), 2770)
        if "result" in answer:
            return head
        if not kind:
            return head + socket.inet_aton(answer["address"])
        return head + struct.pack("!HHI", inner, answer["port"], answer["lifetime"])


def use(portmap, monkeypatch, tools):
    monkeypatch.setattr(portmap, "run", tools)
    monkeypatch.setattr(portmap, "exchange", tools.exchange)
    return tools


def ask(portmap, monkeypatch, tools, settings=None, before=None):
    use(portmap, monkeypatch, tools)
    return portmap.once(settings or SETTINGS, before or {})


def test_nat_pmp_is_asked_first_and_its_mapping_is_enough(portmap, monkeypatch):
    tools = Tools(natpmpc=pmp_mapped())
    state = ask(portmap, monkeypatch, tools)
    assert tools.calls == [["natpmpc", "-g", GATEWAY, "-a", PORT, PORT, "udp", "3600"]]
    assert (state["method"], state["external"], state["why"]) == ("NAT-PMP", f"{PUBLIC}:{PORT}", {})
    assert (state["address"], state["gateway"], state["port"], state["enabled"]) == (ADDRESS, GATEWAY, TAILSCALE_PORT, True)


def test_upnp_is_asked_when_nat_pmp_gives_nothing(portmap, monkeypatch):
    tools = Tools(natpmpc=None, upnpc=upnp_mapped())
    state = ask(portmap, monkeypatch, tools)
    # From the uplink, and with the search answered on the port the router's ruleset lets in.
    assert tools.calls[0] == ["upnpc", "-m", "nb-uplink", "-z", "1901", "-e", "nullbill", "-a", ADDRESS, PORT, PORT, "UDP", "3600"]
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
        (pmp_failed(3), UPNP_NOT_CONNECTED, dict.fromkeys(("NAT-PMP", "UPnP"), direct.NO_PUBLIC_ADDRESS)),
        (pmp_failed(3), UPNP_RESERVED, dict.fromkeys(("NAT-PMP", "UPnP"), direct.NO_PUBLIC_ADDRESS)),
        (
            pmp_failed(2),
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
    tools = Tools(natpmpc=pmp_mapped(public="0.0.0.0"), upnpc=nameless)
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
    gone = Tools(natpmpc=None, upnpc=UPNP_GONE)
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

    use(portmap, monkeypatch, Tools())
    assert portmap.main() == 0
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert state["method"] is None and state["at"] > 0
    assert capsys.readouterr().out == f"the home router did not open UDP {PORT}: NAT-PMP: no answer; UPnP: no answer\n"

    use(portmap, monkeypatch, Tools(natpmpc=pmp_mapped()))
    assert portmap.main() == 0
    state = json.loads((tmp_path / "run" / "state.json").read_text())
    assert (state["method"], state["held"]) == ("NAT-PMP", ["NAT-PMP"])
    assert capsys.readouterr().out == f"the home router sends UDP {PORT} here (NAT-PMP, {PUBLIC}:{PORT})\n"
    assert not (tmp_path / "run" / "state.part").exists()

    # Switched off after it held a mapping: the state file says which to give back.
    settings.write_text(json.dumps(SETTINGS | {"enabled": False}))
    tools = Tools()
    use(portmap, monkeypatch, tools)
    assert portmap.main() == 0
    assert tools.calls == [["natpmpc", "-g", GATEWAY, "-a", PORT, PORT, "udp", "0"]]
    off = f"not asking the home router for UDP {PORT} (tailnet.port_mapping is off)"
    assert capsys.readouterr().out == f"{off}; it has not answered about the mapping made over NAT-PMP yet\n"
    assert json.loads((tmp_path / "run" / "state.json").read_text())["held"] == ["NAT-PMP"]
    use(portmap, monkeypatch, Tools(natpmpc=PMP_GONE))
    assert portmap.main() == 0
    assert capsys.readouterr().out == f"{off}\n"
    assert json.loads((tmp_path / "run" / "state.json").read_text())["held"] == []


def test_its_words_are_the_ones_direct_py_reads(portmap):
    assert portmap.NO_PUBLIC_ADDRESS == direct.NO_PUBLIC_ADDRESS
    assert portmap.REFUSED == direct.REFUSED
    assert direct.OTHER_DEVICE in portmap.TAKEN


# A further port (the download client's, nullbill/torrents.py): TCP and UDP.
MORE_PORT = 41643
MORE = SETTINGS | {"more": {"torrents": {"enabled": True, "port": MORE_PORT, "protos": ["tcp", "udp"]}}}


def pmp_by_port(argv):
    """A gateway that maps whatever is asked for."""
    return pmp_mapped(int(argv[4]), int(argv[7])) if argv else pmp_mapped()


def test_a_further_port_is_asked_for_in_each_of_its_protocols(portmap, monkeypatch):
    tools = Tools(natpmpc=pmp_by_port)
    state = ask(portmap, monkeypatch, tools, MORE)
    port = str(MORE_PORT)
    assert tools.calls == [
        ["natpmpc", "-g", GATEWAY, "-a", PORT, PORT, "udp", "3600"],
        ["natpmpc", "-g", GATEWAY, "-a", port, port, "tcp", "3600"],
        ["natpmpc", "-g", GATEWAY, "-a", port, port, "udp", "3600"],
    ]
    # Tailscale's port is told as before, for direct.py.
    assert (state["method"], state["external"]) == ("NAT-PMP", f"{PUBLIC}:{PORT}")
    assert state["more"] == {
        "torrents": {
            "enabled": True,
            "port": MORE_PORT,
            "protos": ["tcp", "udp"],
            "open": {"tcp": f"{PUBLIC}:{port}", "udp": f"{PUBLIC}:{port}"},
            "method": {"tcp": "NAT-PMP", "udp": "NAT-PMP"},
            "why": {"tcp": {}, "udp": {}},
            "held": ["NAT-PMP tcp", "NAT-PMP udp"],
        }
    }
    # Without one, the state is what it always was.
    assert "more" not in ask(portmap, monkeypatch, Tools(natpmpc=pmp_mapped()))


def test_a_further_port_the_home_router_does_not_open_says_why_per_protocol(portmap, monkeypatch):
    tools = Tools(natpmpc=None)
    entry = ask(portmap, monkeypatch, tools, MORE)["more"]["torrents"]
    assert entry["open"] == {"tcp": None, "udp": None} and entry["held"] == []
    assert entry["why"]["tcp"] == {"NAT-PMP": "not offered", "UPnP": "no answer"}
    assert ["upnpc", "-m", "nb-uplink", "-z", "1901", "-e", "nullbill", "-a", ADDRESS, "41643", "41643", "TCP", "3600"] in tools.calls


def test_a_further_port_switched_off_or_no_longer_wanted_is_given_back_and_forgotten(portmap, monkeypatch):
    held = ask(portmap, monkeypatch, Tools(natpmpc=pmp_by_port), MORE)
    off = MORE | {"more": {"torrents": MORE["more"]["torrents"] | {"enabled": False}}}
    tools = Tools(natpmpc=pmp_by_port)
    state = ask(portmap, monkeypatch, tools, off, held)
    port = str(MORE_PORT)
    assert ["natpmpc", "-g", GATEWAY, "-a", port, port, "tcp", "0"] in tools.calls
    assert ["natpmpc", "-g", GATEWAY, "-a", port, port, "udp", "0"] in tools.calls
    assert state["more"]["torrents"]["held"] == [] and state["more"]["torrents"]["open"] == {}
    # Gone from the settings (the client now sends through a VPN): given back, then forgotten.
    tools = Tools(natpmpc=pmp_by_port)
    state = ask(portmap, monkeypatch, tools, SETTINGS, held)
    assert ["natpmpc", "-g", GATEWAY, "-a", port, port, "tcp", "0"] in tools.calls and "more" not in state
    # A home router that does not answer keeps it on the record.
    state = ask(portmap, monkeypatch, Tools(natpmpc=lambda argv: pmp_mapped() if not argv or argv[4] == PORT else ""), SETTINGS, held)
    assert state["more"]["torrents"]["held"] == ["NAT-PMP tcp", "NAT-PMP udp"] and state["more"]["torrents"]["enabled"] is False
