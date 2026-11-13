"""The pinned versions (nullbill/pins.py, data/pins.yaml, tools/pins)."""

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest
import yaml

from nullbill import pins
from nullbill.errors import NullBillError
from nullbill.model import build_model

ROOT = Path(__file__).resolve().parent.parent

CATALOG = """\
games:
  minecraft:
    # 26.3 needs Java 25.
    version: "26.3"
    url: https://example.internal/a/server.jar
    servers:
      paper:
        build: 248
      fabric:
        loader: 0.20.1
        libraries:
          - file: asm-9.jar
            url: https://example.internal/asm-9.jar
    mods: []
  terraria:
    version: 1.4.5.8
"""


def tool():
    loader = importlib.machinery.SourceFileLoader("nb_pins_tool", str(ROOT / "tools" / "pins"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_every_pin_is_read_from_the_file_that_holds_it_and_has_a_source():
    table = pins.table()
    now = pins.current(pins.checkout(ROOT))
    assert set(now) == set(table)
    assert all(now.values())
    sources = tool()
    # A pin a person moves has no source: tools/pins leaves it.
    by_hand = {name for name, pin in table.items() if pin.get("by_hand")}
    assert by_hand == {"debian"}
    assert set(sources.SOURCES) | set(sources.FOLLOW_MINECRAFT) == set(table) - by_hand
    services = set(build_model({"intent": {"workloads": []}})["services"]) | {"minecraft", "terraria", "factorio"}
    for name, pin in table.items():
        assert pin["hold"] == pins.ALL or isinstance(pin["hold"], int), name
        assert set(pin.get("services", [])) <= services, name


def test_a_version_is_written_in_place_and_all_else_stays():
    path = "src/nullbill/data/catalog.yaml"
    assert pins.value(CATALOG, path, "games.minecraft.version") == "26.3"
    moved = pins.put(CATALOG, path, "games.minecraft.version", "26.4")
    assert moved == CATALOG.replace('"26.3"', '"26.4"')
    # Text that YAML would read as a number is quoted; a plain one is not.
    moved = pins.put(CATALOG, path, "games.terraria.version", "1.5")
    assert 'version: "1.5"\n' in moved and yaml.safe_load(moved)["games"]["terraria"]["version"] == "1.5"
    assert "version: 1.4.5.9\n" in pins.put(CATALOG, path, "games.terraria.version", "1.4.5.9")
    # A number stays a number: Paper's build is one in the schema.
    moved = pins.put(CATALOG, path, "games.minecraft.servers.paper.build", 300)
    assert yaml.safe_load(moved)["games"]["minecraft"]["servers"]["paper"]["build"] == 300
    libraries = [
        {"file": "asm-10.jar", "url": "https://example.internal/asm-10.jar"},
        {"file": "b.jar", "url": "https://example.internal/b.jar"},
    ]
    moved = pins.put(CATALOG, path, "games.minecraft.servers.fabric.libraries", libraries)
    loaded = yaml.safe_load(moved)
    assert loaded["games"]["minecraft"]["servers"]["fabric"]["libraries"] == libraries
    assert loaded["games"]["minecraft"]["mods"] == [] and "# 26.3 needs Java 25." in moved
    with pytest.raises(NullBillError):
        pins.put(CATALOG, path, "games.factorio.version", "2")
    script = "set -eu\ntofu_version=1.13.2\ntofu_sha256=aa\n"
    assert pins.value(script, "tools/setup-controller", "tofu_version") == "1.13.2"
    assert pins.put(script, "tools/setup-controller", "tofu_version", "1.14.0") == script.replace("1.13.2", "1.14.0")


def test_a_big_step_is_one_in_the_numbers_a_pin_holds():
    assert not pins.held({"hold": 1}, "15.0.11", "15.0.12")
    assert pins.held({"hold": 1}, "15.0.11", "17.0.1")
    assert not pins.held({"hold": 1}, "12.4+deb13", "12.5+deb13")
    assert pins.held({"hold": 2}, "26.3", "26.4") and not pins.held({"hold": 2}, "26.3", "26.3.1")
    assert pins.held({"hold": 2}, "0.30.1-1", "0.31.0-1") and not pins.held({"hold": 2}, "0.30.1-1", "0.30.2-1")
    assert pins.held({"hold": pins.ALL}, "1.4.5.8", "1.4.5.9") and not pins.held({"hold": pins.ALL}, "1.4.5.8", "1.4.5.8")
    assert not pins.held({"hold": 0}, "b12205", "b12367")


TABLE = """\
forgejo: {title: Forgejo, file: forgejo.yml, key: forgejo_version, hold: 1, services: [forgejo]}
minecraft: {title: Minecraft, file: catalog.yaml, key: games.minecraft.version, hold: 2, services: [minecraft]}
tailscale: {title: Tailscale, file: vpn.yml, key: vpn_tailscale_version, hold: 1}
"""


def tree(forgejo, minecraft, tailscale, table=TABLE):
    files = {
        pins.TABLE: table,
        "forgejo.yml": f"forgejo_version: {forgejo}\n",
        "catalog.yaml": f'games:\n  minecraft:\n    version: "{minecraft}"\n',
        "vpn.yml": f"vpn_tailscale_version: {tailscale}\n",
    }
    return files.get


def test_an_update_asks_only_about_big_steps_of_what_the_server_runs():
    old, new = tree("15.0.11", "26.3", "1.104.5"), tree("17.0.1", "26.4", "1.104.7")
    found = pins.steps(old, new)
    assert [(s["name"], s["from"], s["to"], s["held"]) for s in found] == [
        ("forgejo", "15.0.11", "17.0.1", True),
        ("minecraft", "26.3", "26.4", True),
        ("tailscale", "1.104.5", "1.104.7", False),
    ]
    # No Minecraft on this server: its step is nobody's question here.
    assert [s["name"] for s in pins.asks(found, {"guests": {"forgejo": {}, "vpn": {}}})] == ["forgejo"]
    # A further Minecraft server counts as Minecraft.
    plan = {"guests": {"skyblock": {}}, "games": {"skyblock": {"game": "minecraft"}}}
    assert [s["name"] for s in pins.asks(found, plan)] == ["minecraft"]
    # A pin for every server asks every server; nothing built, nothing asked.
    big = pins.steps(old, tree("15.0.11", "26.3", "2.0.0"))
    assert [s["name"] for s in pins.asks(big, {"guests": {}})] == ["tailscale"] and pins.asks(big, None) == []
    # An older version without the table, or without a pin, is no step.
    assert pins.steps(lambda path: None, new) == [] and pins.steps(old, lambda path: None) == []


def test_every_role_that_installs_step_cli_pins_the_same_one():
    # A guest with two of these roles would otherwise be asked to downgrade it.
    def default(role: str, key: str) -> str:
        return yaml.safe_load((ROOT / "ansible" / "roles" / role / "defaults" / "main.yml").read_text())[key]

    found = {
        default("step_ca", "step_cli_version"),
        default("operator", "step_cli_version"),
        default("tls_cert", "tls_cert_step_cli_version"),
    }
    assert len(found) == 1
    assert all("step_cli_version:" not in path.read_text() for path in (ROOT / "ansible" / "roles").glob("*/tasks/*.yml"))
