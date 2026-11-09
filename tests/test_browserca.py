import socket
import ssl
import subprocess
import threading

import pytest
from fixturedata import fixture_config

from nullbill import browserca
from nullbill.model import build_model
from nullbill.plan import build_plan


def ca(tmp_path, network="10.42.0.0/16"):
    return browserca.root(tmp_path, "nullbill.internal", network)


def issue(tmp_path, root, name, sans):
    crt, key = tmp_path / browserca.DIR / f"{name}.crt", tmp_path / browserca.DIR / f"{name}.key"
    return browserca.issue(tmp_path, root, crt, key, sans), crt, key


def text(crt):
    return subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-text"], capture_output=True, text=True, check=True).stdout


def test_the_root_vouches_only_for_the_plans_domain_and_network(tmp_path):
    root = ca(tmp_path)
    out = text(root)
    assert "X509v3 Name Constraints: critical" in out
    assert "DNS:nullbill.internal" in out and "IP:10.42.0.0/255.255.0.0" in out
    assert "CA:TRUE, pathlen:0" in out
    assert oct((tmp_path / browserca.DIR / "ca.key").stat().st_mode & 0o777) == "0o600"
    assert ca(tmp_path).read_text() == root.read_text()  # made once

    # A certificate its key signs for any other name or address is refused.
    for sans in (["DNS:example.com", "IP:10.42.1.2"], ["DNS:pve.nullbill.internal", "IP:192.0.2.10"]):
        made, crt, _ = issue(tmp_path, root, "other", sans)
        assert made and not browserca.current(root, crt, sans)
        verified = subprocess.run(["openssl", "verify", "-CAfile", str(root), str(crt)], capture_output=True, text=True)
        assert "permitted subtree violation" in verified.stdout + verified.stderr


def test_a_certificate_is_kept_while_it_names_what_is_wanted_and_has_time_left(tmp_path):
    root = ca(tmp_path)
    sans = browserca.sans(["pve.nullbill.internal"], ["10.42.1.254"])
    made, crt, key = issue(tmp_path, root, "pve", sans)
    assert made and browserca.current(root, crt, sans)
    first, first_key = crt.read_text(), key.read_text()
    assert issue(tmp_path, root, "pve", sans)[0] is False and crt.read_text() == first

    out = text(crt)
    assert "DNS:pve.nullbill.internal, IP Address:10.42.1.254" in out
    assert "TLS Web Server Authentication" in out and "CA:FALSE" in out
    # Fewer than RENEW_DAYS left: a new one, for the same key.
    assert not browserca.current(root, crt, sans, days=browserca.DAYS + 1)
    # Another address: a new one, for the same key.
    moved = browserca.sans(["pve.nullbill.internal"], ["10.42.1.253"])
    assert issue(tmp_path, root, "pve", moved)[0] and crt.read_text() != first and key.read_text() == first_key


def test_the_pages_self_signed_certificate_is_replaced_for_the_same_key(tmp_path):
    from nullbill.web.server import ensure_certificate

    cert, key = ensure_certificate(tmp_path)
    pin = subprocess.run(["sh", "-c", "openssl x509 -in $0 -pubkey -noout", cert], capture_output=True, text=True).stdout
    root = ca(tmp_path)
    sans = browserca.sans(["nullbill.nullbill.internal"], ["10.42.1.2"])
    assert browserca.issue(tmp_path, root, cert, key, sans)
    assert subprocess.run(["sh", "-c", "openssl x509 -in $0 -pubkey -noout", cert], capture_output=True, text=True).stdout == pin
    assert browserca.current(root, cert, sans)


@pytest.mark.parametrize("address, trusted", [("127.0.0.1", True), ("127.0.0.2", False)])
def test_a_browser_that_trusts_the_root_accepts_the_server_by_address(tmp_path, address, trusted):
    """A real handshake: the certificate must name the address the client
    opened, inside the root's constraints."""
    root = ca(tmp_path, network="127.0.0.0/8")
    _, crt, key = issue(tmp_path, root, "page", browserca.sans(["nullbill.nullbill.internal"], [address]))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.minimum_version = ssl.TLSVersion.TLSv1_2
    server.load_cert_chain(crt, key)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        try:
            server.wrap_socket(conn, server_side=True).close()
        except ssl.SSLError:
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client = ssl.create_default_context(cafile=str(root))
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(("127.0.0.1", port)) as raw:
        if trusted:
            client.wrap_socket(raw, server_hostname="127.0.0.1").close()
        else:
            with pytest.raises(ssl.SSLCertVerificationError):
                client.wrap_socket(raw, server_hostname="127.0.0.1")
    listener.close()


def test_der_and_fingerprint_match_openssl(tmp_path):
    root = ca(tmp_path)
    out = subprocess.run(["openssl", "x509", "-in", str(root), "-noout", "-fingerprint", "-sha256"], capture_output=True, text=True)
    assert browserca.fingerprint(root) == out.stdout.strip().partition("=")[2]
    der = subprocess.run(["openssl", "x509", "-in", str(root), "-outform", "der"], capture_output=True).stdout
    assert browserca.der(root) == der


def plan_of(facts):
    return build_plan(build_model(fixture_config("small")), facts("small"))


def test_verify_checks_the_page_and_once_closed_proxmox_from_the_host(tmp_path, facts):
    plan = plan_of(facts)
    root = ca(tmp_path)
    assert browserca.check(plan, tmp_path / "none.crt", "root@pve", None, lan_closed=True) == browserca.skipped(
        plan, "no setup run has made the browser CA yet"
    )

    seen = []

    def run(target, command):
        seen.append(command)
        lines = [line for line in command.splitlines() if line.startswith("out=$(curl")]
        answers = ["rc=0 ", "rc=60 curl: (60) SSL certificate problem: self-signed certificate"]
        return 0, "".join(f"{a}\n" for a in answers[: len(lines)]), ""

    results = browserca.check(plan, root, "root@pve", run, lan_closed=True)
    assert [(r.status, r.name) for r in results] == [("PASS", "browsers trust the page"), ("DEGRADED", "browsers trust Proxmox")]
    assert "self-signed certificate" in results[1].detail and "https://10.42.1.254:8006/" in results[1].detail
    assert root.read_text().strip() in seen[0] and "--cacert" in seen[0]

    results = browserca.check(plan, root, "root@pve", run, lan_closed=False)
    assert [(r.status, r.name) for r in results] == [("PASS", "browsers trust the page"), ("SKIP", "browsers trust Proxmox")]
    assert "https://10.42.1.254:8006/" not in seen[-1]
