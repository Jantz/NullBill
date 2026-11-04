"""Router and egress-proxy files. tools/check-guests parses them with the real tools."""

import re

import pytest
from conftest import fixture_config

from nullbill.errors import NullBillError
from nullbill.model import build_model
from nullbill.plan import build_plan
from nullbill.proxy import render_proxy
from nullbill.render import strip_notes
from nullbill.router import render_router


def planned(facts, cls: str, **config) -> tuple[dict, dict]:
    model = build_model({**fixture_config(cls), **config})
    return build_plan(model, facts(cls)), model


def test_router_forwards_only_planned_flows(facts):
    plan, model = planned(facts, "mid")
    nft = render_router(plan, model)["etc/nftables.conf"]
    assert nft.count("policy drop;") == 2
    assert "ip saddr { 10.42.2.17 } ip daddr { 10.42.2.13 } tcp dport 5432 accept" in nft
    egress = [line for line in nft.splitlines() if 'oifname "nb-uplink"' in line and "accept" in line]
    # Only direct zones reach the uplink, and never a private range.
    assert [re.search(r'iifname "(\S+)"', line)[1] for line in egress] == ["nb-services", "nb-access"]
    assert all("ip daddr != @private" in line for line in egress)


def test_proxy_zones_resolve_internal_names_only(facts):
    plan, model = planned(facts, "mid")
    files = render_router(plan, model)
    internal = files["etc/nullbill/dnsmasq-internal.conf"]
    external = files["etc/nullbill/dnsmasq-external.conf"]
    assert "no-resolv" in internal and "no-resolv" not in external
    assert re.findall(r"^interface=(\S+)", internal, re.M) == ["nb-mgmt", "nb-build", "nb-inference", "nb-windows"]
    assert re.findall(r"^interface=(\S+)", external, re.M) == ["nb-services", "nb-access"]
    # Pool clones take their address from the internal instance.
    assert "dhcp-range=10.42.3.129,10.42.3.190" in internal


def test_router_links_are_unique_and_short(facts):
    plan, model = planned(facts, "large")
    links = {p: t for p, t in render_router(plan, model).items() if p.endswith(".link")}
    macs = [re.search(r"MACAddress=(\S+)", t)[1] for t in links.values()]
    names = [re.search(r"Name=(\S+)", t)[1] for t in links.values()]
    assert len(set(macs)) == len(macs) == len(strip_notes(plan)["zones"]) + 1
    assert all(m.startswith("02:4e:42:") for m in macs)
    assert all(len(n) <= 15 for n in names)


def test_squid_admits_proxy_zones_only(facts):
    plan, model = planned(facts, "mid")
    conf = render_proxy(plan, model)["etc/squid/squid.conf"]
    clients = re.search(r"^acl clients src (.+)$", conf, re.M)[1].split()
    assert clients == ["10.42.1.0/24", "10.42.3.0/24", "10.42.4.0/24", "10.42.5.0/24"]
    access = re.findall(r"^http_access .+$", conf, re.M)
    assert access[-2:] == ["http_access allow allowed", "http_access deny all"]
    assert access.index("http_access deny private") < access.index("http_access allow allowed")


def test_config_allow_list_replaces_catalog(facts):
    plan, model = planned(facts, "small", proxy={"allow": [".example.org", "deb.debian.org"]})
    conf = render_proxy(plan, model)["etc/squid/squid.conf"]
    assert "acl allowed dstdomain -n .example.org deb.debian.org\n" in conf


def test_verify_name_must_be_on_the_allow_list(facts):
    with pytest.raises(NullBillError) as exc:
        planned(facts, "small", proxy={"allow": [".example.org"]})
    assert exc.value.messages == ["verify.public_name: deb.debian.org is not on proxy.allow, so verify could not prove the proxy works"]
