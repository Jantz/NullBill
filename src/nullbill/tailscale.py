"""The Tailscale API calls apply makes: push the rendered policy, read it
back, count tagged devices, send the plan's domain to the router (split DNS).

The owner makes an OAuth client in Tailscale's admin console with the
policy_file scope (and the device scopes the console adds to it) and the dns
scope, and pastes its ID and secret on the page. OAuth clients do not expire; each run trades
them for an access token that lasts an hour
(https://tailscale.com/kb/1215/oauth-clients). `-` names the client's own
tailnet (https://tailscale.com/kb/1101/api).
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from nullbill.errors import NullBillError

API = "https://api.tailscale.com/api/v2/"
# The checks point this at a stand-in.
API_ENV = "NULLBILL_TAILSCALE_API"
TIMEOUT_S = 30


class TailscaleError(NullBillError):
    pass


class MissingScope(TailscaleError):
    """The OAuth client was made without a scope the call needs."""


class Tailnet:
    def __init__(self, client_id: str, client_secret: str, api: str | None = None):
        self.api = api or os.environ.get(API_ENV) or API
        self.client_id = client_id
        self.client_secret = client_secret
        self.token: str | None = None

    def _call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(urllib.parse.urljoin(self.api, path), data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()
        except (urllib.error.URLError, TimeoutError) as error:
            raise TailscaleError(f"Tailscale's API did not answer: {getattr(error, 'reason', error)}") from error

    def _authed(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, dict, bytes]:
        if self.token is None:
            form = urllib.parse.urlencode(
                {"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"}
            )
            status, _, data = self._call("POST", "oauth/token", form.encode(), {"Content-Type": "application/x-www-form-urlencoded"})
            if status != 200:
                raise TailscaleError(
                    f"Tailscale refused the OAuth client ({status} {_message(data)}); make a new one and paste it on the page"
                )
            self.token = json.loads(data)["access_token"]
        return self._call(method, path, body, {"Authorization": f"Bearer {self.token}", **(headers or {})})

    def _ok(self, what: str, status: int, data: bytes, scopes: str = "policy_file and devices:core:read") -> bytes:
        if status == 403:
            raise MissingScope(f"{what}: the OAuth client lacks a scope ({_message(data)}); give it {scopes}")
        if not 200 <= status < 300:
            raise TailscaleError(f"{what} failed ({status} {_message(data)})")
        return data

    def policy(self) -> tuple[dict, str]:
        """The live policy as JSON, and its ETag."""
        status, headers, data = self._authed("GET", "tailnet/-/acl", headers={"Accept": "application/json"})
        data = self._ok("reading the tailnet policy", status, data)
        return json.loads(data), _header(headers, "ETag")

    def validate(self, policy: dict) -> None:
        """Tailscale's own check of a policy and its tests, changing nothing.
        Passing is an empty body; failing tests still answer 200."""
        status, _, data = self._authed("POST", "tailnet/-/acl/validate", json.dumps(policy).encode(), {"Content-Type": "application/json"})
        data = self._ok("checking the tailnet policy", status, data)
        if data.strip() and json.loads(data).get("message"):
            raise TailscaleError(f"Tailscale refused the policy: {_message(data)}")

    def set_policy(self, policy: dict, etag: str) -> None:
        headers = {"Content-Type": "application/json", "If-Match": etag}
        status, _, data = self._authed("POST", "tailnet/-/acl", json.dumps(policy).encode(), headers)
        if status == 412:
            raise TailscaleError("the tailnet policy changed while apply pushed it; run apply again")
        self._ok("pushing the tailnet policy", status, data)

    def split_dns(self) -> dict:
        """{domain: [nameserver, ...]} the tailnet's devices send that domain's lookups to."""
        status, _, data = self._authed("GET", "tailnet/-/dns/split-dns")
        return json.loads(self._ok("reading the tailnet's split DNS", status, data, "dns") or b"{}")

    def set_split_dns(self, domain: str, nameservers: list[str]) -> None:
        """Change domain's nameservers and leave every other domain's alone (PATCH)."""
        body = json.dumps({domain: nameservers}).encode()
        status, _, data = self._authed("PATCH", "tailnet/-/dns/split-dns", body, {"Content-Type": "application/json"})
        self._ok("setting the tailnet's split DNS", status, data, "dns")

    def devices(self) -> list[dict]:
        status, _, data = self._authed("GET", "tailnet/-/devices")
        return json.loads(self._ok("listing the tailnet's devices", status, data)).get("devices", [])


def _header(headers: dict, name: str) -> str:
    return next((v for k, v in headers.items() if k.lower() == name.lower()), "")


def _message(data: bytes) -> str:
    """Tailscale's error message, with the failing tests it names."""
    try:
        body = json.loads(data)
    except ValueError:
        return data.decode(errors="replace").strip()[:300]
    parts = [str(body.get("message", ""))]
    for item in body.get("data") or []:
        parts.append(f"{item.get('user', '')}: {'; '.join(map(str, item.get('errors', [])))}")
    return " | ".join(p for p in parts if p)
