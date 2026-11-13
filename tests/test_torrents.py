"""The download client: the plan's torrents section, what both firewall
layers let its guest reach, the owner's WireGuard file, the role's
variables, apply's stage and verify's checks, on the large class with a
kept disk the owner made editable."""

import copy
import json
import os
import shlex
from pathlib import Path

import pytest
from fixturedata import VPN_SERVER, legacy_config, torrents_config

from nullbill import apply, people, power, torrents
from nullbill.artifacts import render_all
from nullbill.cli import plan_text
from nullbill.errors import NullBillError, Refused
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.render import strip_notes
from nullbill.results import Result

ROOT = Path(__file__).parent.parent
GOLDEN = Path(__file__).parent / "golden"
DISK = "ata-SYNTHETIC_HDD_0002"
SERVER, PORT = VPN_SERVER.split(":")
KEY = "A" * 43 + "="
PEER = "B" * 43 + "="
CONF = f"""# from the provider
[Interface]
PrivateKey = {KEY}
Address = 10.2.0.2/32, fd00::2/128
DNS = 10.2.0.1

[Peer]
PublicKey = {PEER}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {VPN_SERVER}
"""
BASE = "10.42.0.0/16"


def planned(facts, config=None):
    model = build_model(config or torrents_config())
    return strip_notes(build_plan(model, facts("large"))), model


def errors(facts, config) -> str:
    with pytest.raises(NullBillError) as raised:
        build_plan(build_model(config), facts("large"))
    return str(raised.value)


def test_golden_torrents(facts):
    """Set UPDATE_GOLDEN=1 to rewrite; review the diff before committing it."""
    text = plan_text(build_model(torrents_config()), json.dumps(facts("large"), sort_keys=True).encode())
    path = GOLDEN / "large-torrents.plan.yaml"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text()


# The plan.


def test_the_client_is_planned_only_when_asked_for(facts):
    plan, _ = planned(facts, legacy_config())
    assert plan["torrents"] == {} and torrents.QBITTORRENT not in plan["guests"] and torrents.ZONE not in plan["zones"]
    assert not torrents.enabled(plan) and torrents.skipped(plan, "why") == []


def test_behind_a_vpn_the_zone_goes_out_to_the_vpns_server_alone(facts):
    plan, model = planned(facts)
    assert plan["torrents"] == {
        "route": "vpn",
        "endpoint": VPN_SERVER,
        "store": {"share": "data", "folder": "Downloads"},
        "keep_free_gib": 25,
    }
    assert plan["zones"]["torrents"]["egress"] == "tunnel"
    assert torrents.endpoint(plan) == (SERVER, int(PORT))
    guest = plan["guests"]["qbittorrent"]
    assert (guest["vmid"], guest["ip"], guest["zone"]) == (2028, "10.42.10.28", "torrents")
    files = render_all(build_plan(model, facts("large")), model)
    nft = files["router/etc/nftables.conf"]
    out = [line.strip() for line in nft.splitlines() if 'iifname "nb-torrents" oifname "nb-uplink"' in line]
    rule = f'iifname "nb-torrents" oifname "nb-uplink" ip daddr {SERVER} udp dport {PORT} accept'
    assert out == [f"{rule} # WireGuard to the owner's VPN, the zone's only way out"]
    guest_fw = files["firewall/2028.fw"]
    assert f"OUT ACCEPT -dest {SERVER} -p udp -dport {PORT} # WireGuard to the owner's VPN, the only way out" in guest_fw
    # Packages come through the proxy, and nothing else leaves.
    assert "-dport 3128" in guest_fw
    assert sum(line.startswith("OUT ACCEPT") and "-dest +nb-svc-" not in line for line in guest_fw.splitlines()) == 1
    # Names are looked up through the tunnel: the router answers this zone for NullBill's own names only.
    assert "interface=nb-torrents" in files["router/etc/nullbill/dnsmasq-internal.conf"]
    assert "interface=nb-torrents" not in files["router/etc/nullbill/dnsmasq-external.conf"]


def test_from_home_the_zone_goes_out_directly_and_the_plan_says_what_that_shows(facts):
    config = torrents_config("home")
    plan, model = planned(facts, config)
    assert plan["torrents"]["route"] == "home" and "endpoint" not in plan["torrents"]
    assert plan["zones"]["torrents"]["egress"] == "direct"
    assert torrents.endpoint(plan) is None
    text = plan_text(model, json.dumps(facts("large"), sort_keys=True).encode())
    assert "Everyone else in a download sees your home's public address" in text
    nft = render_all(build_plan(model, facts("large")), model)["router/etc/nftables.conf"]
    assert "WireGuard to the owner's VPN" not in nft


def test_from_home_one_port_reaches_the_client_and_the_router_asks_the_home_router_for_it(facts):
    plan, model = planned(facts, torrents_config("home"))
    assert (plan["torrents"]["port"], plan["torrents"]["port_mapping"]) == (41643, True)
    assert torrents.peer_port(plan) == 41643
    files = render_all(build_plan(model, facts("large")), model)
    nft = files["router/etc/nftables.conf"]
    assert 'iifname "nb-uplink" ip daddr 10.42.10.28 meta l4proto { tcp, udp } th dport 41643 accept # BitTorrent, from other peers' in nft
    assert 'iifname "nb-uplink" tcp dport 41643 dnat to 10.42.10.28:41643' in nft
    assert 'iifname "nb-uplink" udp dport 41643 dnat to 10.42.10.28:41643' in nft
    # Tailscale's port keeps its own way in, in the same chain.
    assert nft.count("chain prerouting") == 1 and "udp dport 41642 dnat to" in nft
    guest_fw = files["firewall/2028.fw"]
    assert "IN ACCEPT -p tcp -dport 41643 # BitTorrent, from other peers" in guest_fw
    assert "IN ACCEPT -p udp -dport 41643 # BitTorrent, from other peers" in guest_fw
    asked = json.loads(files["router/etc/nullbill/portmap.json"])
    assert asked["more"] == {"torrents": {"enabled": True, "port": 41643, "protos": ["tcp", "udp"]}}
    assert torrents.variables(plan, model, "pw", None)["qbittorrent_listen_port"] == 41643
    # The owner forwards it by hand: the way in stays, nobody asks.
    config = torrents_config("home")
    config["torrents"]["port_mapping"] = False
    plan, model = planned(facts, config)
    files = render_all(build_plan(model, facts("large")), model)
    assert json.loads(files["router/etc/nullbill/portmap.json"])["more"]["torrents"]["enabled"] is False
    assert "tcp dport 41643 dnat to" in files["router/etc/nftables.conf"]


def test_behind_a_vpn_no_port_is_open_at_home(facts):
    plan, model = planned(facts)
    assert "port" not in plan["torrents"] and torrents.peer_port(plan) is None and torrents.port_request(plan) is None
    files = render_all(build_plan(model, facts("large")), model)
    assert "41643" not in files["router/etc/nftables.conf"] and "41643" not in files["firewall/2028.fw"]
    assert "more" not in json.loads(files["router/etc/nullbill/portmap.json"])


def router_says(more=None, exitcode=0):
    """`qm guest exec` on the router: nb-portmap's state and the clock."""
    state = {"at": 1000, "enabled": True, "port": 41642, "address": "192.168.1.57", "gateway": "192.168.1.1", "held": []}
    state |= {"more": more} if more is not None else {}
    return 0, json.dumps({"exited": 1, "exitcode": exitcode, "out-data": json.dumps(state) + "\n1010\n"}), ""


def test_verify_says_whether_the_home_router_opened_the_port(facts):
    plan, _ = planned(facts, torrents_config("home"))
    both = {"tcp": "203.0.113.7:41643", "udp": "203.0.113.7:41643"}
    entry = {"enabled": True, "port": 41643, "protos": ["tcp", "udp"], "open": both, "method": {}, "why": {}, "held": []}
    assert torrents.port_result(plan, *router_says({"torrents": entry})) == Result("PASS", torrents.PORT_LABEL)
    shut = entry | {"open": {"tcp": None, "udp": None}, "why": {"tcp": {"NAT-PMP": "not offered", "UPnP": "no answer"}}}
    found = torrents.port_result(plan, *router_says({"torrents": shut}))
    assert found.status == "DEGRADED"
    assert "it did not open TCP and UDP 41643 when asked (NAT-PMP: not offered; UPnP: no answer)" in found.detail
    assert "forward TCP and UDP 41643 to 192.168.1.57 yourself" in found.detail
    # The home router's answer is evidence, never proof: the owner's own home router said it had no public
    # address while a forward made by hand let the internet in. "may", and how to test from outside.
    assert "so the port may be shut. That answer is no proof" in found.detail and "port checking website" in found.detail
    # Measured beats said: once a peer got in from outside, it is open whatever the home router answered.
    reached = Result("PASS", torrents.PORT_LABEL, "others have connected to it from outside")
    assert torrents.port_result(plan, *router_says({"torrents": shut}), "connected") == reached
    assert torrents.port_result(plan, 255, "", "VM 2001 is not running", "connected") == reached
    assert torrents.port_result(plan, *router_says({"torrents": shut}), "firewalled").status == "DEGRADED"
    # Not asked yet, or nothing to read: no finding either way.
    assert torrents.port_result(plan, *router_says()).status == "SKIP"
    assert torrents.port_result(plan, 255, "", "VM 2001 is not running").status == "SKIP"
    # Forwarded by hand cannot be seen from the server, so it is never called open.
    config = torrents_config("home")
    config["torrents"]["port_mapping"] = False
    plan, _ = planned(facts, config)
    found = torrents.port_result(plan, *router_says({"torrents": entry}))
    assert found.status == "SKIP" and "made by hand cannot be seen from here" in found.detail
    assert "run the checks again, which pass once anyone has connected from outside" in found.detail
    assert torrents.port_result(plan, *router_says({"torrents": entry}), "connected") == reached


def test_a_download_saved_outside_the_folder_is_named(facts):
    """The owner's server: a save path outside /downloads failed with
    "mkdir (): Permission denied" in qBittorrent's log only, and the
    download sat in incomplete."""
    assert torrents.paths_result(0, "inside\n", "") == Result("PASS", torrents.PATHS_LABEL)
    said = [f'"Film {n}" is set to save to /srv/films, outside /downloads' for n in range(1, 5)]
    said.append('at 12:22 on 11 Nov qBittorrent could not move "Film 1" to /srv/films: Permission denied')
    found = torrents.paths_result(0, "\n".join(said), "")
    # The page shows a check's detail one line per "; ": each thing wrong now,
    # the rest counted, why it matters, what to do. No line is a wall of log.
    lines = found.detail.split("; ")
    assert found.status == "DEGRADED" and lines[:3] == said[:3] and lines[3].startswith("and 2 more")
    assert lines[4] == "qBittorrent can write under /downloads only, so such a download stays unfinished"
    assert lines[5].startswith("to fix it, right-click the download in qBittorrent, Set location") and len(lines) == 6
    assert max(map(len, lines)) < 120
    assert torrents.paths_result(1, "", "nb-torrents: Connection refused").status == "FAIL"
    plan, _ = planned(facts, torrents_config("home"))
    results = torrents.check(plan, "t", answers(paths="A is saved to /tmp, outside /downloads"), "198.51.100.80")
    assert [(r.status, r.name) for r in results][3] == ("DEGRADED", torrents.PATHS_LABEL)


def test_downloads_may_go_to_a_kept_disk_that_is_not_shared(facts):
    config = torrents_config()
    config["disks"][DISK] = {"use": "legacy", "mode": "rw", "share": False}
    bare, model = planned(facts, config)
    assert bare["torrents"]["store"] == {"share": "data", "folder": "Downloads"}
    text = plan_text(model, json.dumps(facts("large"), sort_keys=True).encode())
    assert "neither the file share nor Jellyfin" in " ".join(text.split()).replace(" # ", " ")
    found = torrents.store(bare)
    assert found["path"] == "/mnt/nb-legacy/data/Downloads" and found["shared"] is False
    assert "files" not in bare["guests"] or not [m for m in bare["disks"][DISK]["partitions"] if "share" in m]


def test_the_way_downloads_go_is_never_chosen_for_the_owner(facts):
    config = torrents_config()
    del config["torrents"]
    said = errors(facts, config)
    assert "torrents.route: say which way downloads go: vpn" in said and "home" in said
    config["torrents"] = {"route": "vpn"}
    assert "torrents.vpn: no VPN yet: paste your provider's WireGuard file on the page" in errors(facts, config)


@pytest.mark.parametrize("server", ["10.0.0.5:51820", "192.168.1.9:51820", "10.42.3.4:51820", "127.0.0.1:51820"])
def test_a_vpn_server_is_an_address_on_the_internet(facts, server):
    config = torrents_config()
    config["torrents"]["vpn"]["endpoint"] = server
    assert "is not an address on the internet" in errors(facts, config)


def test_downloads_need_a_kept_disk_that_can_be_written_to(facts):
    config = torrents_config()
    config["disks"][DISK] = {"use": "legacy"}
    assert "the kept disk data is read-only; keep one and make it editable" in errors(facts, config)
    config["disks"] = {}
    assert "torrents: downloads go to a kept disk that can be written to, and no disk is kept" in errors(facts, config)
    config = torrents_config()
    config["torrents"]["share"] = "films"
    assert "torrents.share: no kept disk is called 'films'; kept are: data" in errors(facts, config)


def test_jellyfin_sees_the_downloads_through_the_disk_it_already_reads(facts):
    plan, model = planned(facts)
    found = torrents.store(plan)
    assert found["path"] == "/mnt/nb-legacy/data/Downloads" and found["mode"] == "rw"
    # The client's guest gets that folder and nothing else of the disk, never backed up.
    assert torrents.mount_point(found) == "/mnt/nb-legacy/data/Downloads,mp=/downloads,backup=0,ro=0"
    text = plan_text(model, json.dumps(facts("large"), sort_keys=True).encode())
    assert "Jellyfin shows them under /media/data/Downloads." in text


def test_the_client_runs_always_and_stops_itself_only_when_put_on_demand(facts):
    _, model = planned(facts)
    assert model["power"]["torrents"] == {"title": "Downloads", "run": "always", "stop": "auto", "idle_minutes": 30}
    assert (ROOT / "ansible/roles/qbittorrent/tasks/main.yml").read_text().count("/etc/nullbill/busy.d/torrents") >= 1


def test_sharing_the_client_says_what_its_one_password_gives(facts):
    plan, model = planned(facts)
    entry = next(s for s in people.shareable(model, plan) if s["name"] == "qbittorrent")
    assert "its one password" in entry["access"] and "running programs in its container" in entry["access"]


# The owner's WireGuard file.


def test_a_providers_file_is_read_and_reduced_to_what_nullbill_uses():
    conf = torrents.read_conf(CONF, BASE)
    assert conf == {"private_key": KEY, "address": "10.2.0.2/32", "dns": ["10.2.0.1"], "public_key": PEER, "endpoint": VPN_SERVER}
    kept = torrents.saved_conf(conf)
    assert torrents.read_conf(kept, BASE) == conf and "fd00" not in kept
    wg = torrents.wg_conf(conf)
    assert "Address" not in wg and "DNS" not in wg and f"PersistentKeepalive = {torrents.KEEPALIVE_S}" in wg


def test_a_server_given_by_name_is_looked_up_when_the_file_is_saved():
    named = CONF.replace(VPN_SERVER, f"vpn.example.internal:{PORT}")
    assert torrents.read_conf(named, BASE, lambda host: SERVER)["endpoint"] == VPN_SERVER
    with pytest.raises(NullBillError, match="names the server"):
        torrents.read_conf(named, BASE)

    def unknown(host):
        raise OSError(-2, "Name or service not known")

    with pytest.raises(NullBillError, match="could not be looked up from here"):
        torrents.read_conf(named, BASE, unknown)


def test_everything_wrong_with_a_file_is_said_at_once_and_never_the_key():
    bad = f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.42.9.9/32\n\n[Peer]\nAllowedIPs = 10.0.0.0/8\nEndpoint = 10.1.2.3:51820\n"
    with pytest.raises(Refused) as raised:
        torrents.read_conf(bad, BASE)
    said = raised.value.messages
    assert raised.value.headline == "This WireGuard file cannot be used"
    assert any("lies inside NullBill's own network" in m for m in said)
    assert any("names no DNS server" in m for m in said)
    assert any("no PublicKey" in m for m in said)
    assert any("lacks 0.0.0.0/0" in m for m in said)
    assert any("is not an address on the internet" in m for m in said)
    assert KEY not in "\n".join(said)
    with pytest.raises(NullBillError, match="a WireGuard file is a few lines"):
        torrents.read_conf("x" * 5000, BASE)


# The role's variables and apply's stage.


def test_the_role_gets_the_tunnel_from_the_saved_file(facts):
    plan, model = planned(facts)
    got = torrents.variables(plan, model, "pw", torrents.saved_conf(torrents.read_conf(CONF, BASE)))
    assert got["qbittorrent_route"] == "vpn" and got["qbittorrent_base"] == BASE and got["qbittorrent_keep_free_gib"] == 25
    assert got["qbittorrent_tunnel"] == {
        "interface": "wg0",
        "address": "10.2.0.2/32",
        "dns": ["10.2.0.1"],
        "mtu": 1420,
        "endpoint": VPN_SERVER,
        "handshake_s": torrents.HANDSHAKE_S,
    }
    assert f"Endpoint = {VPN_SERVER}" in got["qbittorrent_wg_conf"]
    with pytest.raises(NullBillError, match="no WireGuard file is saved"):
        torrents.variables(plan, model, "pw", None)
    other = CONF.replace(VPN_SERVER, "198.51.100.9:51820")
    with pytest.raises(NullBillError, match="the plan opens the firewall to 203.0.113.44:51820"):
        torrents.variables(plan, model, "pw", other)


def test_from_home_the_role_gets_no_tunnel(facts):
    plan, model = planned(facts, torrents_config("home"))
    got = torrents.variables(plan, model, "pw", None)
    assert got["qbittorrent_route"] == "home" and got["qbittorrent_tunnel"] == {} and got["qbittorrent_wg_conf"] == ""


class Reach:
    """The host and the client's guest, as apply's torrents stage meets them."""

    def __init__(self, modprobe=0):
        self.calls = []
        self.modprobe = modprobe

    def host(self, command, stdin=None, timeout=900):
        self.calls.append(("host", command, stdin))
        if command.startswith("modprobe "):
            return self.modprobe, "", "modprobe: FATAL: Module wireguard not found\n" if self.modprobe else ""
        if command.startswith("pct exec 2028 -- ") and "nb-torrents settings" in command:
            return 0, "changed\n", ""
        if command.startswith("cat "):
            return 0, "\n", ""
        return 0, "", ""

    def playbook(self, playbook, hosts, variables):
        self.calls.append(("playbook", playbook, hosts, variables))
        return 0


@pytest.fixture
def ctx(facts, tmp_path):
    def make(config, reach, secrets=None):
        from test_apply import TARGET, MemoryCodec

        from nullbill.apply import Context
        from nullbill.secretfile import Secrets

        model = build_model(config)
        plan = build_plan(model, facts("large"))
        return Context(plan, model, tmp_path, TARGET, reach, Secrets(MemoryCodec(secrets or {})), render_all(plan, model))

    return make


def test_the_stage_loads_wireguard_on_the_host_then_runs_the_role_with_the_owners_file(ctx):
    c = ctx(torrents_config(), Reach(), {torrents.VPN_SECRET: CONF})
    apply.stage_torrents(c)
    commands = [call[1] for call in c.reach.calls]
    assert f"modprobe {torrents.MODULE}" in commands
    (play,) = [call for call in c.reach.calls if call[0] == "playbook"]
    assert play[1] == "qbittorrent.yml" and list(play[2]) == ["qbittorrent"]
    assert play[3]["qbittorrent_password"] == c.secrets.data[torrents.PASSWORD_SECRET]
    assert play[3]["qbittorrent_tunnel"]["endpoint"] == VPN_SERVER
    assert commands.index(f"modprobe {torrents.MODULE}") < c.reach.calls.index(play)
    waited = [i for i, cmd in enumerate(commands) if isinstance(cmd, str) and cmd.endswith("nb-torrents wait")]
    assert waited and waited[0] > c.reach.calls.index(play)
    assert "torrents: downloads go to Downloads on the kept disk data, through your VPN only" in c.log
    assert CONF not in "\n".join(c.log) and KEY not in "\n".join(c.log)


def test_the_stage_stops_before_the_role_without_wireguard_or_without_the_file(ctx):
    c = ctx(torrents_config(), Reach(modprobe=1), {torrents.VPN_SECRET: CONF})
    with pytest.raises(apply.ApplyError, match="the host cannot load WireGuard"):
        apply.stage_torrents(c)
    c = ctx(torrents_config(), Reach())
    with pytest.raises(apply.ApplyError, match="no WireGuard file is saved"):
        apply.stage_torrents(c)
    assert not [call for call in c.reach.calls if call[0] == "playbook"]


def test_from_home_the_stage_leaves_the_hosts_modules_alone(ctx):
    c = ctx(torrents_config("home"), Reach())
    apply.stage_torrents(c)
    assert not any(isinstance(call[1], str) and "modprobe" in call[1] for call in c.reach.calls)
    assert any("from the home connection, whose address others in a download see" in line for line in c.log)


def test_the_stage_does_nothing_without_the_workload(ctx):
    c = ctx(legacy_config(), Reach())
    apply.stage_torrents(c)
    assert c.reach.calls == []


# verify.


def answers(**over):
    """A runner for the client's guest: each nb-torrents question and the
    two shell checks, answered as a healthy guest does unless `over` says."""
    said = {
        "sign-in": "sign-in",
        "writable": "writable",
        "space": "room",
        "paths": "inside",
        "reached": "firewalled",
        "status": "up",
        "through": "through the tunnel only",
        "drop": "stopped",
    }
    said |= over
    asked = []

    def run(target, command):
        inner = shlex.split(command)[-1]
        asked.append(inner)
        for key, mark in (
            ("sign-in", "app/version"),
            ("writable", "findmnt"),
            ("space", "nb-torrents space"),
            ("paths", "nb-torrents paths"),
            ("reached", "nb-torrents reached"),
            ("status", "tunnel status"),
            ("through", "tunnel through"),
            ("drop", "tunnel drop"),
        ):
            if mark in inner:
                if isinstance(said[key], Exception):
                    raise said[key]
                return 0, said[key] + "\n", ""
        return 127, "", "unexpected"

    run.asked = asked
    return run


def test_verify_proves_the_tunnel_and_that_dropping_it_stops_the_client(facts):
    plan, _ = planned(facts)
    run = answers()
    results = torrents.check(plan, "root@192.0.2.10", run, "198.51.100.80")
    assert [r.status for r in results] == ["PASS"] * 7
    assert [r.name for r in results][-3:] == [torrents.TUNNEL_LABEL, torrents.THROUGH_LABEL, torrents.DROP_LABEL]
    assert any(a.endswith("tunnel drop 198.51.100.80") for a in run.asked)
    assert results[1].name == "the download client writes to Downloads on the kept disk data"


def test_a_client_that_still_sends_with_the_tunnel_down_fails(facts):
    plan, _ = planned(facts)
    results = torrents.check(plan, "t", answers(drop="reached 198.51.100.80 without the tunnel"), "198.51.100.80")
    last = results[-1]
    assert (last.status, last.name) == ("FAIL", torrents.DROP_LABEL) and "without the tunnel" in last.detail


def test_a_tunnel_that_is_down_is_a_failure_and_the_rest_cannot_be_judged(facts):
    plan, _ = planned(facts)
    run = answers(status="down: no handshake for 400 s")
    results = torrents.check(plan, "t", run, "198.51.100.80")
    assert [r.status for r in results][-3:] == ["FAIL", "SKIP", "SKIP"]
    assert not any("tunnel drop" in a for a in run.asked)


def test_downloads_from_home_are_degraded_and_say_what_others_see(facts):
    plan, _ = planned(facts, torrents_config("home"))
    results = torrents.check(plan, "t", answers(), "198.51.100.80")
    assert [r.status for r in results] == ["PASS", "PASS", "PASS", "PASS", "DEGRADED", "SKIP"]
    assert "sees your home's public address" in results[4].detail and results[5].name == torrents.PORT_LABEL
    results = torrents.check(plan, "t", answers(reached="connected"), "198.51.100.80")
    assert results[5] == Result("PASS", torrents.PORT_LABEL, "others have connected to it from outside")


def test_paused_for_room_is_degraded_and_a_guest_that_is_off_is_skipped(facts):
    plan, _ = planned(facts)
    results = torrents.check(plan, "t", answers(space="paused: 3 GiB free, 25 GiB kept free"), "198.51.100.80")
    assert results[2] == Result("DEGRADED", torrents.SPACE_LABEL, "paused: 3 GiB free, 25 GiB kept free")
    results = torrents.check(plan, "t", answers(**{"sign-in": power.Off("qbittorrent", "Downloads")}), "198.51.100.80")
    assert [r.status for r in results] == ["SKIP"] * 7 and len({r.name for r in results}) == 7


def test_the_fixtures_copy_of_the_plan_is_what_the_checks_render(facts):
    """tools/render-fixtures' large-torrents is this config: the firewall,
    guests and tofu checks, and the tunnel walk, read its render."""
    config = copy.deepcopy(torrents_config())
    assert config["torrents"] == {"route": "vpn", "vpn": {"endpoint": VPN_SERVER}}
    assert "large-torrents" in (ROOT / "tools/render-fixtures").read_text()
    assert "tunnel.sh" in (ROOT / "tools/check-guests").read_text()


def test_a_setup_run_gives_the_client_its_folder_while_stopped_and_sets_it_up_after(monkeypatch, tmp_path, facts):
    """The whole run on the stand-in host: the folder of the kept disk is
    made and mounted while the guest is stopped (the stand-in refuses
    otherwise), once, and the stage runs after."""
    import test_apply

    monkeypatch.setenv("TF_VAR_windows_iso", "local:iso/windows.iso")
    monkeypatch.setattr(apply, "run_verify", test_apply.passing_verify)
    monkeypatch.setattr(apply, "sleep", lambda s: None)
    model = build_model(torrents_config("home"))
    plan = build_plan(model, facts("large"))
    reach = test_apply.FakeReach()
    c = apply.Context(
        plan, model, tmp_path, test_apply.TARGET, reach, test_apply.Secrets(test_apply.MemoryCodec()), render_all(plan, model)
    )
    apply.run_apply(c)
    ct = reach.torrents_ct
    assert ct["mps"] == {"mp0": "/mnt/nb-legacy/data/Downloads,mp=/downloads,backup=0,ro=0"} and ct["status"] == "running"
    assert reach.torrents_asked == [["wait"], ["settings"], ["paths"]]
    # qBittorrent is built in the guest: memory lent for the role, then given back.
    assert ct["memory"] == [torrents.BUILD_MEMORY_MIB, plan["guests"]["qbittorrent"]["memory_mib"]]
    restarts = ct["restarts"]
    apply.run_apply(c)
    assert ct["restarts"] == restarts, "a second run leaves the guest running"
