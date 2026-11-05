"""nullbill.tailscale against a stand-in for Tailscale's API over real HTTP."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from nullbill.tailscale import Tailnet, TailscaleError

CLIENT = ("kExampleCNTRL", "tskey-client-kExampleCNTRL-secret")


class FakeAPI:
    def __init__(self):
        self.policy = {"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}
        self.version = 0
        self.failing_tests = None
        self.scopes = {"policy_file", "devices:core:read"}
        self.requests = []
        self.devices = [{"id": "1", "hostname": "nullbill", "tags": ["tag:nb-vpn"]}]


@pytest.fixture
def api():
    state = FakeAPI()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=None, headers=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authed(self, scope):
            if self.headers.get("Authorization") != "Bearer access-1":
                self.reply(401, {"message": "unauthorized"})
                return False
            if scope not in state.scopes:
                self.reply(403, {"message": f"calling actor does not have enough permissions: {scope}"})
                return False
            return True

        def body(self):
            return self.rfile.read(int(self.headers.get("Content-Length", 0)))

        def do_GET(self):
            state.requests.append(("GET", self.path, dict(self.headers)))
            if self.path == "/api/v2/tailnet/-/acl" and self.authed("policy_file"):
                assert self.headers["Accept"] == "application/json"
                self.reply(200, state.policy, {"ETag": f'"v{state.version}"'})
            elif self.path == "/api/v2/tailnet/-/devices" and self.authed("devices:core:read"):
                self.reply(200, {"devices": state.devices})

        def do_POST(self):
            body = self.body()
            state.requests.append(("POST", self.path, dict(self.headers)))
            if self.path == "/api/v2/oauth/token":
                form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                if (form.get("client_id"), form.get("client_secret")) != CLIENT:
                    return self.reply(401, {"message": "invalid client"})
                return self.reply(200, {"access_token": "access-1", "token_type": "Bearer", "expires_in": 3600})
            if self.path == "/api/v2/tailnet/-/acl/validate" and self.authed("policy_file"):
                if state.failing_tests:
                    return self.reply(
                        200, {"message": "test(s) failed", "data": [{"user": "member@example.internal", "errors": [state.failing_tests]}]}
                    )
                return self.reply(200)
            if self.path == "/api/v2/tailnet/-/acl" and self.authed("policy_file"):
                if self.headers.get("If-Match") != f'"v{state.version}"':
                    return self.reply(412, {"message": "precondition failed, invalid old hash"})
                state.policy = json.loads(body)
                state.version += 1
                return self.reply(200, state.policy)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = f"http://127.0.0.1:{server.server_port}/api/v2/"
    yield state
    server.shutdown()


def test_push_reads_back_and_lists_devices(api):
    tailnet = Tailnet(*CLIENT, api=api.url)
    live, etag = tailnet.policy()
    want = {"groups": {"group:nb-owner": ["owner-a@example.internal"]}, "grants": [], "tests": []}
    assert live != want and etag == '"v0"'
    tailnet.validate(want)
    tailnet.set_policy(want, etag)
    assert tailnet.policy() == (want, '"v1"')
    assert tailnet.devices()[0]["tags"] == ["tag:nb-vpn"]
    # One token for the whole run.
    assert [r[1] for r in api.requests].count("/api/v2/oauth/token") == 1


def test_failing_tests_and_a_stale_etag_refuse_the_push(api):
    tailnet = Tailnet(*CLIENT, api=api.url)
    api.failing_tests = "address 10.42.1.254:22: want: Drop, got: Accept"
    with pytest.raises(TailscaleError, match=r"refused the policy: test\(s\) failed \| member@example.internal: address 10.42.1.254:22"):
        tailnet.validate({"tests": []})
    with pytest.raises(TailscaleError, match="changed while apply pushed it"):
        tailnet.set_policy({}, '"v7"')
    assert api.version == 0


def test_a_wrong_client_or_a_missing_scope_says_what_to_fix(api):
    with pytest.raises(TailscaleError, match="refused the OAuth client .401 invalid client.; make a new one"):
        Tailnet("kOther", "tskey-client-other", api=api.url).policy()
    api.scopes = {"policy_file"}
    with pytest.raises(TailscaleError, match="lacks a scope .*devices:core:read"):
        Tailnet(*CLIENT, api=api.url).devices()


def test_no_answer_is_an_error_not_a_crash():
    with pytest.raises(TailscaleError, match="did not answer"):
        Tailnet(*CLIENT, api="http://127.0.0.1:9/api/v2/").policy()
