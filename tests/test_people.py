"""nullbill.people: what the page's People tab adds, shows and says."""

import pytest
from conftest import fixture_config

from nullbill import people
from nullbill.errors import NullBillError
from nullbill.model import build_model, merge
from nullbill.plan import build_plan

MEDIA = {"intent": {"workloads": ["agents", "linux-ci", "windows-ci", "windows-dev", "inference", "media"]}}


def built(facts, config: dict | None = None) -> tuple[dict, dict]:
    model = build_model(merge(merge(fixture_config("large"), MEDIA), config or {}))
    return model, build_plan(model, facts("large"))


def test_only_services_with_a_shared_port_can_be_ticked(facts):
    model, plan = built(facts)
    listed = {s["name"]: s for s in people.shareable(model, plan)}
    assert list(listed) == ["forgejo", "jellyfin", "minecraft", "terraria", "factorio", "model-gateway"]
    assert listed["forgejo"] == {
        "name": "forgejo",
        "title": "Forgejo",
        "what": "Git repositories and CI",
        "ports": ["tcp/443", "tcp/22"],
        "planned": True,
    }
    # Not planned: still listed, so a tick survives a workload switched off.
    model, plan = built(facts, {"intent": {"workloads": ["agents"]}})
    games = {"minecraft": False, "terraria": False, "factorio": False}
    assert {s["name"]: s["planned"] for s in people.shareable(model, plan)} == {
        "forgejo": True,
        "jellyfin": False,
        **games,
        "model-gateway": False,
    }
    # A service the owner marks in config.yaml is listed by its own name.
    ssh = {"services": {"workspace": {"ports": [{"port": 22, "proto": "tcp", "from": ["role:owner", "shared"]}]}}}
    model, plan = built(facts, ssh)
    (workspace,) = [s for s in people.shareable(model, plan) if s["name"] == "workspace"]
    assert (workspace["title"], workspace["what"]) == ("workspace", "Credential-free workspace for coding agents.")


def test_adding_a_person_makes_a_friend_with_the_ticked_services(facts):
    model, _ = built(facts)
    config = fixture_config("large")
    saved = people.save(config, model, " Alex ", " alex@example.internal ", ["jellyfin", "forgejo"])
    assert saved["people"]["alex"] == {"role": "friend", "login": "alex@example.internal", "services": ["forgejo", "jellyfin"]}
    assert "alex" not in config["people"]
    build_model(saved)
    # Changing keeps the role a hand-written entry has; no services, no key.
    changed = people.save(saved, model, "dev-b", "dev-b@example.internal", [])
    assert changed["people"]["dev-b"] == {"role": "default", "login": "dev-b@example.internal"}
    assert people.remove(saved, "alex")["people"] == config["people"]


@pytest.mark.parametrize(
    ("name", "login", "services", "message"),
    [
        ("Al ex", "alex@example.internal", [], "name the person with lowercase letters"),
        ("alex", "alex", [], "the email address they sign in to Tailscale with"),
        ("owner-a", "other@example.internal", [], "owner-a is you"),
        ("alex", "Owner-A@example.internal", [], "is the login of you already"),
        ("alex", "dev-b@example.internal", [], "is the login of dev-b already"),
        ("alex", "alex@example.internal", ["windows-dev"], "windows-dev cannot be given to a person"),
        ("alex", "alex@example.internal", ["monitoring"], "monitoring cannot be given to a person"),
    ],
)
def test_what_the_page_refuses(facts, name, login, services, message):
    model, _ = built(facts)
    with pytest.raises(NullBillError, match=message):
        people.save(fixture_config("large"), model, name, login, services)


def test_the_owner_cannot_be_removed(facts):
    config = fixture_config("large")
    with pytest.raises(NullBillError, match="the owner stays"):
        people.remove(config, "owner-a")
    with pytest.raises(NullBillError, match="no person named nobody"):
        people.remove(config, "nobody")


def test_instructions_say_how_to_join_and_what_opens(facts):
    model, plan = built(facts, {"people": {"friend-c": {"services": ["forgejo", "jellyfin", "model-gateway"]}}})
    text = people.instructions(model, plan, "friend-c", names=True)
    assert "sign in as friend-c@example.internal" in text and people.DOWNLOAD in text
    assert "sudo tailscale set --accept-routes --accept-dns" in text
    assert (
        "- Forgejo: Git repositories and CI\n  https://forgejo.nullbill.internal/\n  If the name does not open: https://10.42.2.17/\n"
        in text
    )
    assert "Git over SSH is git@forgejo.nullbill.internal." in text
    assert "- Jellyfin: Films, series and music\n  http://jellyfin.nullbill.internal:8096/\n" in text
    # The gateway by address, where its certificate is the one devices trust.
    assert "- Local AI model: " in text and "\n  https://10.42.4.21:4000/v1\n  It asks for an API key" in text
    assert "10.42.1." not in text  # nothing of mgmt
    # Without names on the tailnet: addresses only.
    by_address = people.instructions(model, plan, "friend-c", names=False)
    assert "http://10.42.7.24:8096/" in by_address and "nullbill.internal" not in by_address and "If the name" not in by_address


def test_the_root_certificate_is_sent_along_for_what_opens_over_https(facts):
    model, plan = built(facts, {"people": {"friend-c": {"services": ["jellyfin"]}}})
    assert not people.wants_root(model, plan, "friend-c")  # Jellyfin is plain HTTP inside Tailscale
    assert people.ROOT_FILE not in people.instructions(model, plan, "friend-c", names=True, root=True)
    model, plan = built(facts, {"people": {"friend-c": {"services": ["forgejo", "jellyfin"]}}})
    assert people.wants_root(model, plan, "friend-c")
    text = people.instructions(model, plan, "friend-c", names=True, root=True)
    assert f"I send you the file {people.ROOT_FILE}" in text and "\n- Android: " in text
    # Before the first setup run there is no root to send.
    assert people.ROOT_FILE not in people.instructions(model, plan, "friend-c", names=True)


def test_instructions_cover_a_roles_own_reach_and_services_without_a_share_text(facts):
    voice = {"id": 40, "kind": "lxc", "zone": "media", "workload": "core", "floor": {"cores": 1, "memory_mib": 512, "disk_gib": 4}}
    voice["ports"] = [
        {"port": 34197, "proto": "udp", "from": ["role:owner", "shared"]},
        {"port": 27015, "proto": "tcp", "from": ["role:owner"]},
    ]
    model, plan = built(facts, {"services": {"voice": voice}, "people": {"dev-b": {"services": ["voice"]}}})
    text = people.instructions(model, plan, "dev-b", names=True)
    # What the default role reaches by itself, then what was ticked; the
    # admin port, which does not list shared, is not offered.
    assert text.index("- Forgejo") < text.index("- workspace\n  workspace.nullbill.internal, port 22\n") < text.index("- voice")
    assert "- voice\n  voice.nullbill.internal, port 34197 (UDP)\n  If the name does not open: 10.42.7.40, port 34197 (UDP)\n" in text
    assert "27015" not in text
    assert people.reach(model, plan, "dev-b", shared=False) == {"forgejo": ["tcp/443", "tcp/22"], "workspace": ["tcp/22"]}
    assert people.reach(model, plan, "dev-b", shared=True) == {"voice": ["udp/34197"]}


def test_nothing_ticked_or_nothing_planned_says_so(facts):
    model, plan = built(facts, {"people": {"friend-c": {"services": []}}})
    assert people.instructions(model, plan, "friend-c", names=True).endswith("Nothing is open for you yet. I will tell you when it is.\n")
    assert people.instructions(model, None, "friend-c", names=True).endswith("I will tell you when it is.\n")


def test_rows_show_each_persons_devices_and_the_owner_first(facts):
    model, plan = built(facts)
    listed = [
        {
            "user": "Friend-C@example.internal",
            "hostname": "phone",
            "os": "android",
            "lastSeen": "2026-11-09T20:00:00Z",
            "connectedToControl": False,
        },
        {
            "user": "friend-c@example.internal",
            "name": "laptop.tail1234.ts.net",
            "os": "linux",
            "lastSeen": "2026-11-01T08:00:00Z",
            "connectedToControl": True,
        },
        {"user": "owner-a@example.internal", "hostname": "nullbill", "tags": ["tag:nb-vpn"], "lastSeen": "2026-11-09T20:00:00Z"},
        {"user": "guest@example.internal", "hostname": "shared-in", "isExternal": True},
        {"user": "stranger@example.internal", "hostname": "tablet", "lastSeen": "never"},
    ]
    on_tailnet = people.devices(listed)
    rows = people.rows(model, plan, on_tailnet)
    assert [r["name"] for r in rows] == ["owner-a", "dev-b", "friend-c"]
    owner, dev, friend = rows
    # The vpn guest is tagged: nobody's device.
    assert (owner["owner"], owner["devices"], dev["devices"]) == (True, [], [])
    assert (dev["services"], dev["by_role"]) == ([], ["forgejo", "workspace"])
    assert (friend["services"], friend["by_role"]) == (["forgejo", "model-gateway"], [])
    assert [(d["name"], d["connected"]) for d in friend["devices"]] == [("laptop", True), ("phone", False)]
    assert friend["devices"][1]["seen"] == 1794254400.0
    assert people.strangers(model, on_tailnet) == ["stranger@example.internal"]
    # While Tailscale cannot be asked, devices are unknown, not none.
    assert people.rows(model, plan, None)[2]["devices"] is None


def test_the_tailnets_owner_is_never_the_user_to_remove():
    users = [
        {"id": "u1", "loginName": "owner-a@example.internal", "role": "owner"},
        {"id": "u2", "loginName": "Friend-C@example.internal", "role": "member"},
    ]
    assert people.find_user(users, "friend-c@example.internal")["id"] == "u2"
    assert people.find_user(users, "owner-a@example.internal") is None
    assert people.find_user(users, "nobody@example.internal") is None
