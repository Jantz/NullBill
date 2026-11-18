#!/usr/bin/env bash
# Container check for the model_gateway role, run by tools/check-roles:
# converge twice, run LiteLLM and nginx as their units do in front of a
# stand-in for llama-server, and prove over HTTPS that only the gateway's
# key gets an answer, that the model is offered under its own name and as
# "local", that the gateway passes the inference key on, and that the
# clients' key is not LiteLLM's: it reaches the OpenAI paths only, and
# LiteLLM's own admin key stays in the guest.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

name="model-gateway.nullbill.internal"
test_pki /tmp/pki "$name"
guest=$(hostname -i | cut -d' ' -f1)
browser_pki /tmp/browser "$name" "$guest"
apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx-light >/dev/null 2>&1
install -d -m 0755 /etc/nullbill/tls
install -m 0644 /tmp/pki/ca.crt /etc/nullbill/tls/root_ca.crt
install -m 0644 /tmp/pki/loki.crt "/etc/nullbill/tls/$name.crt"
install -m 0640 -g www-data /tmp/pki/loki.key "/etc/nullbill/tls/$name.key"
echo "127.0.0.1 $name inference.nullbill.internal" >>/etc/hosts

key=sk-check-gateway-key-0123456789
backend_key=check-inference-key-0123456789
python3 - "$key" "$backend_key" >/tmp/vars.json <<'PY'
import json, sys
print(json.dumps({
    "model_gateway_manage_services": False, "model_gateway_key": sys.argv[1],
    "model_gateway_backend_key": sys.argv[2], "model_gateway_model": "qwen3.5-9b",
    "model_gateway_browser_cert": open("/tmp/browser/browser.crt").read(),
    "model_gateway_browser_key": open("/tmp/browser/browser.key").read(),
}))
PY
converge model-gateway.yml -e @/tmp/vars.json

systemd-analyze verify /etc/systemd/system/litellm.service >/tmp/unit.log 2>&1 || { cat /tmp/unit.log; fail "litellm unit"; }
[ "$(stat -c '%U:%G %a' /etc/litellm/env)" = "root:litellm 640" ] || fail "keys root:litellm 0640"
[ "$(stat -c '%U:%G %a' /etc/litellm/master.env)" = "root:litellm 640" ] || fail "master key root:litellm 0640"
for f in keys.conf upstream.conf; do
  [ "$(stat -c '%U:%G %a' "/etc/nginx/nullbill/$f")" = "root:root 600" ] || fail "$f is root's alone"
done
master=$(sed -n 's/^NB_MASTER_KEY=//p' /etc/litellm/master.env)
[[ $master =~ ^sk-[A-Za-z0-9_-]{40,}$ ]] || fail "a master key was made"
[ "$master" != "$key" ] || fail "the master key is not the clients' key"
! grep -rq -- "$master" /etc/litellm/env /etc/litellm/config.yaml /etc/nginx/conf.d || fail "the master key is only where it belongs"
grep -q -- "Bearer $master" /etc/nginx/nullbill/upstream.conf || fail "nginx passes the master key"
pass "the litellm unit passes systemd-analyze; its keys are root's, readable by litellm; its own admin key was made here"

# llama-server's OpenAI API, as far as the gateway uses it: one answer, and
# only for the inference key. It records the model asked for.
cat >/tmp/backend.py <<'PY'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = sys.argv[1]

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            self.send_response(401)
            self.end_headers()
            return
        with open("/tmp/backend-asked", "a") as f:
            f.write(body["model"] + "\n")
        answer = {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                  "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hello from llama"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4}}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
PY
python3 /tmp/backend.py "$backend_key" >/tmp/backend.log 2>&1 &

# LiteLLM as its unit runs it, then nginx.
exec_start=$(sed -n 's/^ExecStart=//p' /etc/systemd/system/litellm.service)
set -a
while read -r env_file; do
  # shellcheck disable=SC1090
  . "$env_file"
done < <(sed -n 's/^EnvironmentFile=//p' /etc/systemd/system/litellm.service)
set +a
# shellcheck disable=SC2086 # the unit's command line, split as systemd does
(cd /var/lib/litellm && runuser -u litellm -- env HOME=/var/lib/litellm $exec_start >/tmp/litellm.log 2>&1 &)
nginx
url="https://$name:4000"
for _ in $(seq 90); do curl -fsS --cacert /tmp/pki/ca.crt "$url/health/liveliness" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS --cacert /tmp/pki/ca.crt "$url/health/liveliness" >/dev/null || { tail -30 /tmp/litellm.log; fail "the gateway answers over HTTPS"; }
pass "LiteLLM answers over HTTPS with the step-ca certificate"
curl -fsS --cacert /tmp/browser/browser-ca.crt "https://$guest:4000/health/liveliness" >/dev/null \
  || fail "a browser that opens the address trusts the gateway with the browser CA alone"
pass "a browser that opens the address gets the browser CA's certificate"

ask() { # key model
  curl -sS --cacert /tmp/pki/ca.crt -o /tmp/answer.json -w '%{http_code}' "$url/v1/chat/completions" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d "{\"model\": \"$2\", \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}"
}
# nb-busy's question (the updates role installs nb-busy on a guest). Nobody
# has asked the gateway anything yet: /health/liveliness above was this
# check's own, so start from an empty log as a guest starts.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends iproute2 procps >/dev/null
install -m 0755 /ansible/roles/updates/files/nb-busy /usr/local/sbin/nb-busy
busy=/etc/nullbill/busy.d/model-gateway
: >/var/log/nginx/access.log
[ -z "$($busy)" ] || { $busy; fail "an unused gateway reports nothing"; }
# Monitoring's certificate probe: a TLS handshake and nothing more.
openssl s_client -connect "$guest:4000" -servername "$name" </dev/null >/dev/null 2>&1 || fail "TLS handshake"
sleep 1
[ -z "$($busy)" ] || { $busy; cat /var/log/nginx/access.log; fail "a certificate probe is not use"; }

code=$(ask sk-wrong-key qwen3.5-9b)
[ "${code:0:1}" != 2 ] || fail "a wrong key got an answer"
code=$(curl -sS --cacert /tmp/pki/ca.crt -o /dev/null -w '%{http_code}' "$url/v1/models")
[ "${code:0:1}" != 2 ] || fail "no key got the model list"
test ! -e /tmp/backend-asked || fail "a refused request reached the model"
pass "without the gateway's key nothing gets through"

for model in qwen3.5-9b local; do
  [ "$(ask "$key" "$model")" = 200 ] || { cat /tmp/answer.json; tail -20 /tmp/litellm.log; fail "the key gets an answer from $model"; }
  grep -q 'hello from llama' /tmp/answer.json || { cat /tmp/answer.json; fail "the answer is the model's"; }
done
[ "$(sort -u /tmp/backend-asked)" = qwen3.5-9b ] || { cat /tmp/backend-asked; fail "the backend is asked for its own model name"; }
curl -fsS --cacert /tmp/pki/ca.crt -H "Authorization: Bearer $key" "$url/v1/models" >/tmp/models.json
python3 -c 'import json; ids={m["id"] for m in json.load(open("/tmp/models.json"))["data"]}; assert ids == {"qwen3.5-9b", "local"}, ids' \
  || fail "models listed as qwen3.5-9b and local"
pass "with the key, qwen3.5-9b and local both reach the model with the inference key"

# LiteLLM's admin routes, with the clients' key: nginx refuses them all.
admin() { # key method path
  curl -sS --path-as-is --cacert /tmp/pki/ca.crt -o /tmp/admin.json -w '%{http_code}' -X "$2" "$url$3" \
    -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d '{}'
}
for route in "POST /key/generate" "GET /key/list" "GET /model/info" "GET /v1/model/info" "POST /model/new" \
  "GET /config/yaml" "POST /config/update" "GET /global/spend" "POST /user/new" "GET /health" "GET /" \
  "GET /v1/../key/list" "GET /v1/models/../../key/list" "GET /v1/models/..%2F..%2Fkey%2Flist"; do
  code=$(admin "$key" "${route% *}" "${route#* }")
  [ "$code" = 404 ] || [ "$code" = 400 ] || { cat /tmp/admin.json; fail "the clients' key is refused $route (got $code)"; }
done
# Spelled so that nginx reads an OpenAI path and LiteLLM, given the
# client's spelling, a passthrough or admin route: LiteLLM gets nginx's.
for path in "/openai/x%2F..%2F..%2Fv1/models" "/key/list/../../v1/models" "/model/info/%2E%2E/%2E%2E/v1/models"; do
  code=$(admin "$key" GET "$path")
  case $code in
    404 | 400) ;;
    200) grep -q '"qwen3.5-9b"' /tmp/admin.json || { cat /tmp/admin.json; fail "$path reaches the model list alone"; } ;;
    *) cat /tmp/admin.json; fail "$path is refused or the model list (got $code)" ;;
  esac
done
# The same route with LiteLLM's own key, on localhost: it is an admin route.
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $master" http://127.0.0.1:4001/model/info)
[ "$code" = 200 ] || fail "LiteLLM answers /model/info for its own key (got $code)"
code=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $key" http://127.0.0.1:4001/model/info)
[ "${code:0:1}" != 2 ] || fail "LiteLLM does not take the clients' key"
# Only the clients' key passes nginx: not LiteLLM's, not another case, not in another header.
[ "$(ask "$master" local)" = 401 ] || fail "nginx refuses LiteLLM's own key"
[ "$(ask "${key^^}" local)" = 401 ] || fail "nginx compares the key with its case"
code=$(curl -sS --cacert /tmp/pki/ca.crt -o /dev/null -w '%{http_code}' -H "X-Api-Key: $key" -H "X-LiteLLM-Api-Key: $master" "$url/v1/models")
[ "$code" = 401 ] || fail "a key in another header is not the key (got $code)"
# A browser's CORS preflight carries no key and is not nginx's to refuse.
code=$(curl -sS --cacert /tmp/pki/ca.crt -o /dev/null -w '%{http_code}' -X OPTIONS "$url/v1/chat/completions" \
  -H 'Origin: https://example.internal' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: authorization')
if ! { [ "$code" != 401 ] && [ "$code" != 404 ]; }; then fail "a CORS preflight reaches LiteLLM (got $code)"; fi
pass "the clients' key reaches the OpenAI paths alone; LiteLLM's admin routes take only its own key, which stays in the guest"

# After the requests above the gateway is in use for two minutes, and for
# as long as a client stays connected.
$busy | grep -Eqx "answered a request [0-9]+ s ago" || { $busy; fail "a request answered just now counts as use"; }
touch -d "3 minutes ago" /var/log/nginx/access.log
[ -z "$($busy)" ] || { $busy; fail "idle again two minutes after the last request"; }
python3 - "$guest" <<'PY' &
import socket, ssl, sys, time
context = ssl.create_default_context(cafile="/tmp/browser/browser-ca.crt")
with socket.create_connection((sys.argv[1], 4000)) as raw, context.wrap_socket(raw, server_hostname=sys.argv[1]):
    open("/tmp/held", "w").close()
    time.sleep(20)
PY
for _ in $(seq 50); do [ -e /tmp/held ] && break; sleep 0.1; done
[ "$($busy)" = "1 client connected" ] || { $busy; fail "a client that stays connected counts as use"; }
[ "$(nb-busy)" = "1 client connected" ] || { nb-busy; fail "nb-busy passes the gateway's answer on"; }
pass "nb-busy: unused and probed by monitoring it says nothing; a request or a connected client is use"
