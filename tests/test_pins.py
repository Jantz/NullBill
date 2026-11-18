"""The pinned versions (nullbill/pins.py, data/pins.yaml, tools/pins)."""

import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
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
    # A pin for every server asks every server.
    big = pins.steps(old, tree("15.0.11", "26.3", "2.0.0"))
    assert [s["name"] for s in pins.asks(big, {"guests": {}})] == ["tailscale"]
    # No plan to say what the server runs: every big step is asked, none let through.
    assert [s["name"] for s in pins.asks(found, None)] == ["forgejo", "minecraft"]
    # An older version without the table, or without a pin, is no step.
    assert pins.steps(lambda path: None, new) == [] and pins.steps(old, lambda path: None) == []


def test_a_new_minecraft_waits_for_its_paper_and_fabric_and_then_for_the_owner(monkeypatch):
    sources, now = tool(), pins.current(pins.checkout(ROOT))
    table = pins.table()
    assert table["minecraft"]["hold"] == pins.ALL  # every plugin and mod names one Minecraft
    newer = "99.1"
    moved = [(table["minecraft"]["file"], table["minecraft"]["key"], newer)]
    monkeypatch.setitem(sources.SOURCES, "minecraft", lambda: moved)
    asked = []

    def follows(name, ready):
        def source(version):
            asked.append((name, version))
            if not ready:
                raise SystemExit(f"{name}: nothing for Minecraft {version} yet")
            # Paper pins a build number, Fabric a loader version.
            return [(table[name]["file"], table[name]["key"], 9999 if name == "paper" else "9999")]

        return source

    monkeypatch.setitem(sources.FOLLOW_MINECRAFT, "paper", follows("paper", False))
    monkeypatch.setitem(sources.FOLLOW_MINECRAFT, "fabric", follows("fabric", True))
    unread = {}
    assert sources.offered(["minecraft", "paper", "fabric"], False, unread) == {}
    assert asked == [("paper", newer), ("fabric", newer)] and unread["minecraft"] == "waits for paper"
    monkeypatch.setitem(sources.FOLLOW_MINECRAFT, "paper", follows("paper", True))
    unread = {}
    assert set(sources.offered(["minecraft", "paper", "fabric"], False, unread)) == {"minecraft", "paper", "fabric"} and unread == {}
    assert pins.held(table["minecraft"], now["minecraft"], newer)


def test_every_job_of_a_workflow_has_steps_or_calls_one():
    # GitHub refuses the whole file for one job without them, and says so nowhere a check sees.
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            assert ("uses" in job) != ("steps" in job), f"{path.name}: {name}"
            assert not {"run", "shell"} & set(job), f"{path.name}: {name}"
            for step in job.get("steps", []):
                assert ("uses" in step) != ("run" in step), f"{path.name}: {name}"


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


# Vendors' answers as an attacker would craft them: nothing of them may
# reach a file Ansible templates.
INJECTED = "0.30.9-1{{ lookup('pipe', 'id') }}"


@pytest.mark.parametrize("bad", [INJECTED, "1.0 #x", "1.0\nkey: x", "1.0: x", '1"0', "1'0", "50%", "{1}", True])
def test_a_pin_that_is_no_version_hash_or_address_is_never_written(bad):
    with pytest.raises(NullBillError):
        pins.put("step_ca_version: 0.30.2-1\n", "ansible/roles/step_ca/defaults/main.yml", "step_ca_version", bad)
    with pytest.raises(NullBillError):
        pins.put(CATALOG, "src/nullbill/data/catalog.yaml", "games.minecraft.servers.fabric.libraries", [{"file": bad, "url": "u"}])
    with pytest.raises(NullBillError):
        pins.put("tofu_version=1\n", "tools/setup-controller", "tofu_version", bad)


LIBRARY = {"file": "asm-9.jar", "url": "https://maven.fabricmc.net/org/ow2/asm/asm/9/asm-9.jar", "sha256": "a" * 64}


def test_each_pin_takes_only_what_its_vendor_publishes():
    catalog = pins.CATALOG
    pins.check(catalog, "games.minecraft.url", "https://piston-data.mojang.com/v1/objects/ab/server.jar")
    for path, key, bad in [
        (catalog, "games.minecraft.url", "https://example.internal/server.jar"),
        (catalog, "games.minecraft.url", "https://piston-data.mojang.com/../x.jar"),
        (catalog, "games.minecraft.sha256", "ab" * 31),
        (catalog, "games.minecraft.java", "25"),
        (catalog, "images.debian-lxc.url", "http://download.proxmox.com/images/aplinfo.dat?x"),
        ("ansible/roles/step_ca/defaults/main.yml", "step_ca_version", INJECTED),
        ("ansible/roles/step_ca/defaults/main.yml", "step_ca_repo_key_url", "https://example.internal/key"),
        ("ansible/roles/qbittorrent/defaults/main.yml", "qbittorrent_commit", "main"),
    ]:
        with pytest.raises(NullBillError):
            pins.check(path, key, bad)
    pins.check(catalog, "games.minecraft.servers.fabric.libraries", [LIBRARY])
    elsewhere = LIBRARY | {"url": "https://example.internal/asm-9.jar"}
    with pytest.raises(NullBillError):
        pins.check(catalog, "games.minecraft.servers.fabric.libraries", [elsewhere])
    # The schema holds a Fabric library to the same two Mavens, in config.yaml too.
    with pytest.raises(NullBillError, match="libraries"):
        build_model({"intent": {"workloads": []}, "games": {"minecraft": {"servers": {"fabric": {"libraries": [elsewhere]}}}}})


def test_every_pin_tools_pins_moves_is_pin_data_and_holds_a_value_it_may():
    for name, pin in pins.table().items():
        if not pin.get("by_hand"):
            assert pin["key"] in pins.WRITES[pin["file"]], name
    for path, keys in pins.WRITES.items():
        doc = yaml.safe_load((ROOT / path).read_text())
        for key in keys:
            pins.check(path, key, pins._at(doc, key))
    assert tool().CATALOG == pins.CATALOG


def test_a_source_that_answers_what_no_pin_may_hold_moves_nothing(monkeypatch):
    sources, table = tool(), pins.table()
    pin = table["step-ca"]
    monkeypatch.setitem(sources.SOURCES, "step-ca", lambda: [(pin["file"], pin["key"], INJECTED)])
    monkeypatch.setitem(sources.SOURCES, "tailscale", lambda: [(pins.CATALOG, "images.release.version", "14")])
    unread = {}
    assert sources.offered(["step-ca", "tailscale"], False, unread) == {}
    assert set(unread) == {"step-ca", "tailscale"}
    with pytest.raises(NullBillError):
        sources.write([(pin["file"], pin["key"], INJECTED)])


@pytest.fixture(scope="module")
def signer(tmp_path_factory):
    """A key of our own, (sign, public key): stands in for a vendor's."""
    home = tmp_path_factory.mktemp("gpg")
    home.chmod(0o700)
    gpg = ["gpg", "--homedir", str(home), "--batch", "--quiet", "--pinentry-mode", "loopback", "--passphrase", ""]
    subprocess.run([*gpg, "--quick-gen-key", "Vendor <vendor@example.internal>", "ed25519", "sign", "never"], check=True)
    public = subprocess.run([*gpg, "--export"], capture_output=True, check=True).stdout

    def sign(data: bytes, detached: bool = False) -> bytes:
        how = ["--detach-sign", "--armor"] if detached else ["--clearsign"]
        return subprocess.run([*gpg, *how], input=data, capture_output=True, check=True).stdout

    return sign, public


def vendor(monkeypatch, sources, files):
    """tools/pins asks the network; these files answer instead."""

    def get(url):
        if url not in files:
            raise OSError(f"no {url}")
        return files[url]

    monkeypatch.setattr(sources, "get", get)


def apt_repository(sources, signer, tmp_path, monkeypatch, versions, tamper=False, key=None):
    sign, public = signer
    key = public if key is None else key
    dists, key_url, role, sha_key = sources.SMALLSTEP
    defaults = tmp_path / "ansible" / "roles" / role / "defaults" / "main.yml"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(f"{sha_key}: {hashlib.sha256(public).hexdigest()}\n")
    monkeypatch.setattr(sources, "ROOT", tmp_path)
    index = "".join(f"Package: {name}\nVersion: {version}\n\n" for name, version in versions).encode()
    release = f"Suite: debs\nSHA256:\n {hashlib.sha256(index).hexdigest()} {len(index)} main/binary-amd64/Packages\n".encode()
    vendor(
        monkeypatch,
        sources,
        {key_url: key, dists + "InRelease": sign(release), dists + "main/binary-amd64/Packages": index + (b"x" if tamper else b"")},
    )


def test_apt_versions_come_only_from_an_index_the_pinned_key_signs(signer, tmp_path, monkeypatch):
    sources = tool()
    good = [("step-ca", "0.30.2-1"), ("step-ca", "0.30.3-1"), ("step-cli", "0.31.0-1")]
    apt_repository(sources, signer, tmp_path, monkeypatch, good)
    assert {key: value for _, key, value in sources.step_ca()}["step_ca_version"] == "0.30.3-1"
    # A version that would template something on the controller is no version.
    apt_repository(sources, signer, tmp_path, monkeypatch, [("step-ca", "0.30.2-1"), ("step-ca", INJECTED), ("step-cli", "0.31.0-1")])
    assert {key: value for _, key, value in sources.step_ca()}["step_ca_version"] == "0.30.2-1"
    apt_repository(sources, signer, tmp_path, monkeypatch, [("step-ca", INJECTED), ("step-cli", "0.31.0-1")])
    with pytest.raises(SystemExit, match="no step-ca"):
        sources.step_ca()
    # An index changed after signing, a key the role does not pin, and an
    # InRelease no key of the pinned file signed are all refused.
    apt_repository(sources, signer, tmp_path, monkeypatch, good, tamper=True)
    with pytest.raises(SystemExit, match="not the index"):
        sources.step_ca()
    apt_repository(sources, signer, tmp_path, monkeypatch, good, key=b"another key")
    with pytest.raises(SystemExit, match="not the key"):
        sources.step_ca()
    apt_repository(sources, signer, tmp_path, monkeypatch, good)
    dists = sources.SMALLSTEP[0]
    files = {sources.SMALLSTEP[1]: signer[1], dists + "InRelease": b"Suite: debs\nSHA256:\n"}
    vendor(monkeypatch, sources, files)
    with pytest.raises(SystemExit, match="no valid signature"):
        sources.step_ca()


def test_the_container_template_comes_only_from_the_index_proxmox_signs(signer, tmp_path, monkeypatch):
    sources = tool()
    sign, public = signer
    url, role, sha_key = sources.PROXMOX_KEYRING
    defaults = tmp_path / "ansible" / "roles" / role / "defaults" / "main.yml"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(f"{sha_key}: {hashlib.sha256(public).hexdigest()}\n")
    monkeypatch.setattr(sources, "ROOT", tmp_path)
    index = sources.PROXMOX + "aplinfo-pve-9.dat"

    def listing(location):
        text = f"Package: debian-13-standard\nVersion: 13.7-1\nArchitecture: amd64\nLocation: {location}\nsha512sum: {'b' * 128}\n\n"
        return text.encode()

    good = listing("system/debian-13-standard_13.7-1_amd64.tar.zst")
    vendor(monkeypatch, sources, {url: public, index: good, index + ".asc": sign(good, detached=True)})
    assert sources.debian_template()[0][2] == sources.PROXMOX + "system/debian-13-standard_13.7-1_amd64.tar.zst"
    # Unsigned, or signed but pointing out of Proxmox's folder: refused.
    vendor(monkeypatch, sources, {url: public, index: good, index + ".asc": sign(b"other", detached=True)})
    with pytest.raises(SystemExit, match="no valid signature"):
        sources.debian_template()
    bad = listing("../../example.internal/x.tar.zst")
    vendor(monkeypatch, sources, {url: public, index: bad, index + ".asc": sign(bad, detached=True)})
    with pytest.raises(SystemExit, match="debian-template"):
        sources.debian_template()


def test_fabric_libraries_come_only_from_fabrics_maven_or_maven_central(monkeypatch):
    sources = tool()
    jar = b"jar"
    loaders = json.dumps([{"loader": {"version": "0.20.1", "stable": True}}]).encode()

    def server(maven):
        return json.dumps({"libraries": [{"name": "org.ow2.asm:asm:9.10.1", "url": maven}]}).encode()

    central = "https://repo1.maven.org/maven2/org/ow2/asm/asm/9.10.1/asm-9.10.1.jar"
    files = {
        sources.FABRIC_LOADERS.format("26.3"): loaders,
        sources.FABRIC_SERVER.format("26.3", "0.20.1"): server("https://repo1.maven.org/maven2/"),
        central: jar,
        central + ".sha256": hashlib.sha256(jar).hexdigest().encode(),
    }
    vendor(monkeypatch, sources, files)
    libraries = sources.fabric("26.3")[2][2]
    assert libraries == [{"file": "asm-9.10.1.jar", "url": central, "sha256": hashlib.sha256(jar).hexdigest()}]
    files[sources.FABRIC_SERVER.format("26.3", "0.20.1")] = server("https://maven.example.internal/")
    with pytest.raises(SystemExit, match="not Fabric's Maven"):
        sources.fabric("26.3")
