"""The certificate authority the owner's browsers trust (docs/runbooks/trust-this-server.md).

step-ca's root has no name constraints, so a device that trusted it would
take a certificate for any website from whoever holds the CA guest's keys.
This root is made and kept in NULLBILL_HOME (like the log shipper CA) and is
constrained to the plan's domain and network, so trusting it on a phone
vouches for this server and nothing else. It signs only what the owner opens
in a browser: the page, Proxmox, Forgejo and Grafana, by address or by name,
and the model gateway by address. Services keep step-ca's certificates for
each other, except that Forgejo's clients (the CI dispatcher and runners)
trust this root too, since people and runners open Forgejo by one name.
"""

import hashlib
import ipaddress
import secrets
import shlex
import ssl
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from nullbill.probe import Runner
from nullbill.render import strip_notes
from nullbill.results import Result

DIR = "browser-ca"
CA_DAYS = 3650
# Apple refuses a TLS certificate valid for longer than 825 days, from any root.
DAYS = 825
# apply issues a new one when fewer days than this are left; verify warns at WARN_DAYS.
RENEW_DAYS = 90
WARN_DAYS = 30
NAME = "NullBill browser CA"


def _openssl(args: list[str], stdin: str | None = None) -> str:
    done = subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"openssl {args[0]}: {done.stderr.strip()[:200]}")
    return done.stdout


def constraints(domain: str, network: str) -> str:
    net = ipaddress.ip_network(network)
    return f"critical,permitted;DNS:{domain},permitted;IP:{net.network_address}/{net.netmask}"


def root(home: Path, domain: str, network: str) -> Path:
    """The CA's certificate, made once, limited to domain and network."""
    directory = home / DIR
    crt, key = directory / "ca.crt", directory / "ca.key"
    if not crt.exists():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _openssl(
            ["req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", str(CA_DAYS)]
            + ["-subj", f"/CN={NAME}/O=NullBill", "-keyout", str(key), "-out", str(crt)]
            + ["-addext", "basicConstraints=critical,CA:TRUE,pathlen:0", "-addext", "keyUsage=critical,keyCertSign,cRLSign"]
            + ["-addext", f"nameConstraints={constraints(domain, network)}"]
        )
        key.chmod(0o600)
    return crt


def der(crt: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(crt.read_text())


def fingerprint(crt: Path) -> str:
    """SHA-256, as Android and Firefox show it."""
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der(crt)).digest())


def sans(names: list[str], addresses: list[str]) -> list[str]:
    return [f"DNS:{n}" for n in names] + [f"IP:{a}" for a in addresses]


def _san_list(pem: str) -> list[str]:
    out = _openssl(["x509", "-noout", "-ext", "subjectAltName"], pem)
    found = out.splitlines()[1:] if "Subject Alternative Name" in out else []
    entries = [e.strip() for e in ",".join(found).split(",") if e.strip()]
    return sorted(e.replace("IP Address:", "IP:") for e in entries)


def current(ca: Path, crt: Path, wanted: list[str], days: int = RENEW_DAYS) -> bool:
    """crt is ca's, names exactly wanted and is good for another `days`."""
    if not crt.exists():
        return False
    pem = crt.read_text()
    verified = subprocess.run(["openssl", "verify", "-CAfile", str(ca), str(crt)], capture_output=True, check=False)
    if verified.returncode != 0:
        return False
    ending = subprocess.run(["openssl", "x509", "-checkend", str(days * 86400), "-noout"], input=pem.encode(), capture_output=True)
    return ending.returncode == 0 and _san_list(pem) == sorted(wanted)


def issue(home: Path, ca: Path, crt: Path, key: Path, wanted: list[str]) -> bool:
    """A new certificate at crt for wanted, unless the one there is current.
    Keeps an existing key, so pins of the page's public key still hold.
    Returns whether it issued one."""
    if current(ca, crt, wanted):
        return False
    if not key.exists():
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _openssl(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key)])
        key.chmod(0o600)
    common = next(s for s in wanted if s.startswith("DNS:")).removeprefix("DNS:")
    csr = _openssl(["req", "-new", "-key", str(key), "-subj", f"/CN={common}"])
    extensions = "\n".join(
        [
            "basicConstraints=critical,CA:FALSE",
            "keyUsage=critical,digitalSignature",
            "extendedKeyUsage=serverAuth",
            f"subjectAltName={','.join(wanted)}",
            "",
        ]
    )
    with tempfile.NamedTemporaryFile("w", dir=home / DIR, suffix=".ext") as ext:
        ext.write(extensions)
        ext.flush()
        signed = _openssl(
            ["x509", "-req", "-CA", str(ca), "-CAkey", str(ca.with_suffix(".key")), "-days", str(DAYS)]
            + ["-set_serial", f"0x{secrets.token_hex(16)}", "-extfile", ext.name],
            csr,
        )
    fresh = crt.with_suffix(".new")
    fresh.write_text(signed)
    fresh.replace(crt)
    return True


# (label, guest that runs curl or None for the host, URL, address the URL's
# name resolves to there or None).
Endpoint = tuple[str, str | None, str, str | None]


def _endpoints(plan: dict) -> list[Endpoint]:
    """What a browser opens, by address and by name, as verify reaches it:
    the page and Proxmox from the host, Forgejo and Grafana inside their
    guests (the host has no way to them)."""
    bare = strip_notes(plan)
    domain, guests = bare["domain"], bare["guests"]
    page, host = guests["nullbill"]["ip"], bare["zones"]["mgmt"]["host_ip"]
    out: list[Endpoint] = [
        ("browsers trust the page", None, f"https://{page}:8443/", None),
        ("browsers trust the page by name", None, f"https://nullbill.{domain}:8443/", page),
        ("browsers trust Proxmox", None, f"https://{host}:8006/", None),
        ("browsers trust Proxmox by name", None, f"https://pve.{domain}:8006/", host),
    ]
    if "forgejo" in guests:
        out.append(("browsers trust Forgejo by name", "forgejo", f"https://forgejo.{domain}/", "127.0.0.1"))
    if "monitoring" in guests:
        out.append(("browsers trust Grafana by name", "monitoring", f"https://monitoring.{domain}:3000/", "127.0.0.1"))
    return out


def _proxmox(label: str) -> bool:
    return label.startswith("browsers trust Proxmox")


def skipped(plan: dict, reason: str) -> list[Result]:
    return [Result("SKIP", label, reason) for label, *_ in _endpoints(plan)]


def _script(root_pem: str, endpoints: list[Endpoint]) -> str:
    script = "f=$(mktemp); trap 'rm -f \"$f\"' EXIT; cat > \"$f\" <<'PEM'\n" + root_pem.strip() + "\nPEM\n"
    for _, _, url, address in endpoints:
        resolve = ""
        if address:
            parsed = urlsplit(url)
            resolve = f"--resolve {shlex.quote(f'{parsed.hostname}:{parsed.port or 443}:{address}')} "
        script += f'out=$(curl -sS -o /dev/null -m 10 --noproxy \'*\' --cacert "$f" {resolve}{shlex.quote(url)} 2>&1); echo "rc=$? $out"\n'
    return script


def check(plan: dict, root_crt: Path, target: str, run: Runner, lan_closed: bool) -> list[Result]:
    """Each endpoint's certificate as a browser that trusts only this root
    sees it. Proxmox serves the browser CA's only once the home network is
    closed (apply.stage_host)."""
    if not root_crt.is_file():
        return skipped(plan, "no setup run has made the browser CA yet")
    bare = strip_notes(plan)
    endpoints = [e for e in _endpoints(plan) if lan_closed or not _proxmox(e[0])]
    lines: dict[str, str] = {}
    for guest in dict.fromkeys(e[1] for e in endpoints):
        group = [e for e in endpoints if e[1] == guest]
        script = _script(root_crt.read_text(), group)
        command = script if guest is None else f"pct exec {bare['guests'][guest]['vmid']} -- bash -c {shlex.quote(script)}"
        rc, out, err = run(target, command)
        got = out.splitlines()
        for i, (label, *_) in enumerate(group):
            lines[label] = got[i] if rc == 0 and i < len(got) else f"rc=? {(err or out).strip()[:120]}"
    results = []
    for label, _, url, _ in _endpoints(plan):
        if label not in lines:
            results.append(Result("SKIP", label, "Proxmox gets it once the home network is closed"))
        elif lines[label].startswith("rc=0 "):
            results.append(Result("PASS", label))
        else:
            line = lines[label]
            results.append(Result("DEGRADED", label, f"{url}: {line.partition(' ')[2] or line} (run setup)"))
    return results
