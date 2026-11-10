import os
from pathlib import Path

import pytest
from conftest import CLASSES, fixture_config

from nullbill.artifacts import render_all, write_build
from nullbill.model import build_model
from nullbill.plan import build_plan

GOLDEN = Path(__file__).parent / "golden"


def rendered(facts, cls: str) -> dict[str, str]:
    model = build_model(fixture_config(cls))
    return render_all(build_plan(model, facts(cls)), model)


def rules(text: str) -> list[str]:
    return [line.split(" #")[0] for line in text.split("[RULES]\n", 1)[1].splitlines()]


@pytest.mark.parametrize("cls", CLASSES)
def test_golden(cls, facts):
    """Set UPDATE_GOLDEN=1 to rewrite; review the diff before committing it."""
    text = "".join(f"===== {path}\n{body}" for path, body in rendered(facts, cls).items())
    path = GOLDEN / f"{cls}.render.txt"
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text()


def test_every_guest_defaults_to_deny(facts):
    files = rendered(facts, "mid")
    guest_files = [f for f in files if f.startswith("firewall/") and f.split("/")[-1][0].isdigit()]
    assert len(guest_files) == 17
    for path in guest_files:
        if path == "firewall/2001.fw":  # router
            continue
        assert "policy_in: DROP\npolicy_out: DROP\n" in files[path], path
        assert "ipfilter: 1" in files[path], path
    # install.sh's LAN leg is net0; the mgmt leg apply adds is filtered.
    assert "[IPSET ipfilter-net1]\n10.42.1.2\n" in files["firewall/2002.fw"]


def test_forgejo_rules(facts):
    forgejo = rules(rendered(facts, "mid")["firewall/2017.fw"])
    assert "IN ACCEPT -source +nb-zone-build -p tcp -dport 443" in forgejo
    assert "OUT ACCEPT -dest +nb-svc-postgres -p tcp -dport 5432" in forgejo
    # services egresses directly, but never into private ranges.
    assert forgejo[-2:] == ["OUT DROP -dest +nb-private", "OUT ACCEPT"]


def test_vpn_reaches_devices_at_home_only_from_tailscales_port(facts):
    vpn = rules(rendered(facts, "mid")["firewall/2011.fw"])
    assert vpn[-4:] == [
        "OUT DROP -dest +nb-zones",
        "OUT ACCEPT -dest +nb-private -p udp -sport 41642",
        "OUT DROP -dest +nb-private",
        "OUT ACCEPT",
    ]


def test_vpn_takes_tailscales_port_from_anywhere_and_nothing_else_unasked(facts):
    # What the home router forwards and the router passes on (direct.py); a
    # phone on mobile data reaches the vpn guest directly only this way.
    files = rendered(facts, "mid")
    vpn = rules(files["firewall/2011.fw"])
    anywhere = [r for r in vpn if r.startswith("IN ") and "-source" not in r]
    assert anywhere == ["IN ACCEPT -p udp -dport 41642"]
    assert [path for path, text in files.items() if path.startswith("firewall/") and "41642" in text] == ["firewall/2011.fw"]


def test_proxy_zone_has_no_direct_egress(facts):
    workspace = rules(rendered(facts, "mid")["firewall/2019.fw"])
    assert "OUT ACCEPT -dest +nb-svc-egress-proxy -p tcp -dport 3128" in workspace
    assert "OUT ACCEPT" not in workspace
    assert not any("postgres" in r for r in workspace)


def test_the_lan_leaves_the_management_ipset_only_when_closed(facts):
    files = rendered(facts, "small")
    assert "[IPSET management]\n10.42.6.11\n" in files["firewall/cluster.fw"]
    assert "[IPSET management]\n10.42.6.11\n" in files["firewall/cluster-lan-open.fw"]
    # PVE adds local_network to management whatever it holds: closed, it is the vpn guest, not the LAN.
    assert "[ALIASES]\nlocal_network 10.42.6.11 #" in files["firewall/cluster.fw"]
    assert "[ALIASES]" not in files["firewall/cluster-lan-open.fw"]
    vpn = rules(files["firewall/2011.fw"])
    assert "OUT ACCEPT -dest 10.42.1.254 -p tcp -dport 8006" in vpn


def test_pool_template_uses_group(facts):
    files = rendered(facts, "mid")
    assert rules(files["firewall/2030.fw"]) == ["GROUP nbp-2030"]
    assert "[group nbp-2030] # linux-runner clones\n" in files["firewall/cluster.fw"]


def test_write_build_replaces_stale_files(tmp_path):
    (tmp_path / "build" / "old").mkdir(parents=True)
    (tmp_path / "build" / "old" / "stale.fw").write_text("x")
    write_build({"firewall/a.fw": "a\n"}, tmp_path / "build")
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == ["build/firewall/a.fw"]


def test_host_source_admits_only_the_host(facts):
    files = rendered(facts, "small")
    pbs = rules(files["firewall/2015.fw"])
    assert "IN ACCEPT -source +nb-host -p tcp -dport 8007" in pbs
    assert not any("nb-zone-mgmt" in r for r in pbs if "8007" in r)
    assert "[IPSET nb-host]\n10.42.1.254\n" in files["firewall/cluster.fw"]
