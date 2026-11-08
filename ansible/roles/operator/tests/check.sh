#!/usr/bin/env bash
# Container check for the operator role, run by tools/check-roles. The CA,
# an agent-managed guest and the operator share this container: the agent
# user gets a certificate from the real agent provisioner and logs in to
# the guest with nb-ssh. The PVE token, PVE CA and model API key are
# throwaway values; nothing here talks to PVE or the model API.
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

ansible-playbook -i localhost, -c local -e step_ca_manage_services=false /ansible/step-ca.yml >/tmp/ca.log 2>&1 ||
  { cat /tmp/ca.log; fail "step-ca role"; }
echo "127.0.0.1 step-ca.nullbill.internal" >>/etc/hosts
STEPPATH=/etc/step-ca setpriv --reuid=step --regid=step --init-groups \
  step-ca /etc/step-ca/config/ca.json --password-file /etc/step-ca/secrets/password >/tmp/step-ca.log 2>&1 &
for _ in $(seq 20); do
  step ca health --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt >/dev/null 2>&1 && break
  sleep 0.5
done
step ca health --ca-url https://step-ca.nullbill.internal --root /etc/step-ca/certs/root_ca.crt >/dev/null 2>&1 ||
  { cat /tmp/step-ca.log; fail "CA health"; }
ansible-playbook -i localhost, -c local -e agent_ssh_manage_services=false \
  -e "agent_ssh_user_ca_key='$(cat /etc/step-ca/certs/ssh_user_ca_key.pub)'" /ansible/agent-ssh.yml >/tmp/guest.log 2>&1 ||
  { cat /tmp/guest.log; fail "agent_ssh role"; }
start_journald
/usr/sbin/sshd -E /tmp/sshd.log

# A stand-in for the NullBill page: it runs one verify job for the agent's
# token and nothing for anyone else.
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 2 -subj /CN=nullbill \
  -keyout /tmp/page.key -out /tmp/page.crt 2>/dev/null
spki=$(openssl x509 -in /tmp/page.crt -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64)
python3 - >/tmp/page.log 2>&1 <<'PY' &
import http.server, json, ssl
class Page(http.server.BaseHTTPRequestHandler):
    def reply(self, data):
        if self.headers.get("Authorization") != "Bearer page-token" or self.headers.get("X-NullBill") != "1":
            self.send_response(401); self.end_headers(); return
        body = json.dumps(data).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/api/agent/apply":
            open("/tmp/apply-body", "wb").write(body)
        if self.path in ("/api/agent/verify", "/api/agent/apply"):
            self.reply({"id": "20261105T100000000-verify", "state": "running", "text": "", "offset": 0})
        else:
            self.reply({})
    def do_GET(self):
        if self.path.startswith("/api/agent/jobs/20261105T100000000-verify"):
            self.reply({"state": "failed", "rc": 1, "text": "PASS one\nFAIL two\n", "offset": 18})
        elif self.path == "/api/agent/jobs":
            self.reply([{"id": "20261105T100000000-verify", "action": "verify", "state": "failed", "rc": 1}])
server = http.server.HTTPServer(("127.0.0.1", 8444), Page)
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain("/tmp/page.crt", "/tmp/page.key")
server.socket = tls.wrap_socket(server.socket, server_side=True)
server.serve_forever()
PY

vars=$(mktemp)
python3 - "$vars" "$spki" <<'PY'
import json, sys
host_key = " ".join(open("/etc/ssh/ssh_host_ed25519_key.pub").read().split()[:2])
json.dump({
    # This container stands in for the PVE host, too.
    "operator_host_ip": "127.0.0.1",
    "operator_host_key": host_key,
    "operator_github_token": "github_pat_throwaway",
    "operator_git_name": "owner-a",
    "operator_git_email": "1000+owner-a@users.noreply.github.com",
    "operator_denylist": ["serial-AB12CD34", "192.0.2.10"],
    "operator_pve_token": "nb-agent@pve!operator=00000000-0000-0000-0000-000000000000",
    "operator_pve_ca": "throwaway, not a certificate",
    "operator_ca_root": open("/etc/step-ca/certs/root_ca.crt").read(),
    "operator_step_password": open("/etc/step-ca/secrets/agent-password").read(),
    "operator_anthropic_api_key": "throwaway-key",
    "operator_page_url": "https://127.0.0.1:8444/",
    "operator_page_token": "page-token",
    "operator_page_spki": sys.argv[2],
    "operator_guests": [
        {"name": "router", "fqdn": "router.nullbill.internal", "ip": "10.42.1.1", "zone": "mgmt", "kind": "vm", "vmid": 2001, "agent_managed": False},
        {"name": "workspace", "fqdn": "workspace.nullbill.internal", "ip": "10.42.3.20", "zone": "build", "kind": "lxc", "vmid": 2020, "agent_managed": True},
    ],
}, open(sys.argv[1], "w"))
PY
converge operator.yml -e "@$vars"

test "$(stat -c '%a %U %G' /etc/nullbill/agent)" = "750 root agent" || fail "secrets directory mode"
for f in pve-token pve-root-ca.pem ca-root.pem step-password anthropic-api-key; do
  test "$(stat -c '%a %U %G' "/etc/nullbill/agent/$f")" = "640 root agent" || fail "$f mode"
done
if su nobody -s /bin/bash -c 'cat /etc/nullbill/agent/pve-token' >/dev/null 2>&1; then fail "other users read the token"; fi
id -nG agent | grep -qwE 'sudo|root|adm' && fail "agent is in a privileged group"
pass "secrets are root's, readable by the agent user only; the agent has no sudo"

want=$(sed -n 's/^operator_claude_code_sha512: //p' /ansible/roles/operator/defaults/main.yml)
sha512sum /opt/claude-code/*/package.tgz | awk '{print $1}' | grep -qx "$want" || fail "agent CLI checksum"
head -c 4 /opt/claude-code/*/package/claude | grep -q ELF || fail "agent CLI is not an executable"
grep -q "^exec /opt/claude-code/[0-9.]*/package/claude \"\$@\"$" /usr/local/bin/claude || fail "agent CLI wrapper"
python3 -c 'import json; s = json.load(open("/etc/claude-code/managed-settings.json")); assert s["env"]["DISABLE_AUTOUPDATER"] == "1", s' ||
  fail "managed settings"
test "$(su agent -c "$(python3 -c 'import json; print(json.load(open("/etc/claude-code/managed-settings.json"))["apiKeyHelper"])')")" = throwaway-key ||
  fail "the agent CLI's key helper"
if su agent -c 'test -w /etc/claude-code/managed-settings.json || test -w /etc/claude-code/CLAUDE.md'; then
  fail "agent can change its rules"
fi
pass "agent CLI pinned by checksum; its managed settings and rules are root's"

hook=$(python3 -c 'import json; print(json.load(open("/etc/claude-code/managed-settings.json"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"])')
echo '{"tool_name": "Bash", "tool_input": {"command": "echo hook-marker"}}' | su agent -c "$hook" || fail "hook refused a call"
journalctl -t nb-cmd -o json --no-pager | python3 -c '
import json, sys
hits = [e for e in map(json.loads, sys.stdin) if e["MESSAGE"] == "tool Bash: echo hook-marker" and e["_UID"] == sys.argv[1]]
sys.exit(0 if hits else 1)' "$(id -u agent)" || fail "tool call not in the journal under the agent's uid"
stop_journald
code=0
echo '{"tool_name": "Bash", "tool_input": {"command": "true"}}' | su agent -c "$hook" 2>/dev/null || code=$?
start_journald
test "$code" = 2 || fail "hook exit $code with the journal down, expected 2 (block)"
code=0
echo 'not json' | su agent -c "$hook" 2>/dev/null || code=$?
test "$code" = 2 || fail "hook exit $code on input it cannot read, expected 2 (block)"
python3 -c 'import json, sys; h = json.load(open("/etc/claude-code/managed-settings.json"))["hooks"]["PreToolUse"][0]["hooks"][0]
sys.exit(h.get("onFailure") != "block")' || fail "a hook that cannot start or times out does not block"
pass "the agent CLI records each tool call under the agent's uid first, and blocks it when it cannot"

out=$(su - agent -c 'nb-ssh 127.0.0.1 id -un' 2>/tmp/nb-ssh.log) || { cat /tmp/nb-ssh.log /tmp/sshd.log; fail "nb-ssh"; }
test "$out" = agent || fail "nb-ssh logged in as '$out'"
before=$(stat -c %Y /home/agent/.ssh/nb-agent-cert.pub)
sleep 1
su - agent -c 'nb-ssh 127.0.0.1 true'
test "$(stat -c %Y /home/agent/.ssh/nb-agent-cert.pub)" = "$before" || fail "nb-ssh fetched a certificate it did not need"
# ssh's -l agent wins over a user in the destination; agent_ssh's check
# shows the certificate itself never logs in as root.
out=$(su - agent -c 'nb-ssh root@127.0.0.1 id -un' 2>/dev/null || true)
test "$out" != root || fail "nb-ssh logged in as root"
if su - agent -c 'ssh -i ~/.ssh/nb-agent -o IdentitiesOnly=yes -o BatchMode=yes root@127.0.0.1 true' 2>/dev/null; then
  fail "the agent's certificate logs in as root"
fi
pass "nb-ssh logs in to an agent-managed guest as agent with a reused certificate, never as root"

# Root on "the host" once the page's switch puts the agent's key there,
# with the line hostroot.line makes: from the operator, every command
# recorded first.
test "$(stat -c '%a %U' /home/agent/.ssh/nb-host)" = "600 agent" || fail "the agent's host key is not the agent's alone"
if su - agent -c 'ssh -o BatchMode=yes pve true' 2>/dev/null; then fail "ssh pve worked before the switch"; fi
install -d -m 0700 /root/.ssh
echo "from=\"127.0.0.1\",command=\"/usr/local/sbin/nb-agent-command\",no-agent-forwarding,no-X11-forwarding $(cut -d' ' -f1,2 /home/agent/.ssh/nb-host.pub) nullbill-agent-root" >>/root/.ssh/authorized_keys
out=$(su - agent -c "ssh -o BatchMode=yes pve 'id -un; echo root-marker'" 2>/tmp/pve.log) || { cat /tmp/pve.log /tmp/sshd.log; fail "ssh pve"; }
test "$out" = "root
root-marker" || fail "ssh pve ran as '$out'"
journalctl -t nb-cmd -o json --no-pager | python3 -c '
import json, sys
sys.exit(not [e for e in map(json.loads, sys.stdin) if e["MESSAGE"] == "ssh: id -un; echo root-marker" and e["_UID"] == "0"])' ||
  fail "the host command is not in the journal before it ran"
if su - agent -c 'ssh -o BatchMode=yes pve' </dev/null >/dev/null 2>&1; then fail "ssh pve opened a shell without a command"; fi
sed -i '/ nullbill-agent-root$/d' /root/.ssh/authorized_keys
if su - agent -c 'ssh -o BatchMode=yes pve true' 2>/dev/null; then fail "ssh pve worked after the switch went off"; fi
sed -i 's/^127.0.0.1 ssh-ed25519 .*/127.0.0.1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherHostOtherHostOtherHostOtherHostOther/' /etc/ssh/ssh_known_hosts
echo "from=\"127.0.0.1\",command=\"/usr/local/sbin/nb-agent-command\" $(cut -d' ' -f1,2 /home/agent/.ssh/nb-host.pub) nullbill-agent-root" >>/root/.ssh/authorized_keys
if su - agent -c 'ssh -o BatchMode=yes pve true' 2>/dev/null; then fail "ssh pve trusted a host key that is not the pinned one"; fi
sed -i '/ nullbill-agent-root$/d' /root/.ssh/authorized_keys
converge operator.yml -e "@$vars"
pass "with the switch's line the agent runs one recorded command at a time as root on the pinned host, and nothing without it"

test "$(su - agent -c 'git config user.email')" = "1000+owner-a@users.noreply.github.com" || fail "the agent's git identity"
printf 'protocol=https\nhost=github.com\n\n' | su - agent -c 'git credential fill' | grep -qx 'password=github_pat_throwaway' ||
  fail "git does not get the GitHub token"
test "$(stat -c '%a %U %G' /etc/nullbill/agent/github-token)" = "640 root agent" || fail "GitHub token mode"
test "$(stat -c '%a %U %G' /etc/nullbill/agent/denylist.txt)" = "640 root agent" || fail "deny-list mode"
grep -qx 'serial-AB12CD34' /etc/nullbill/agent/denylist.txt || fail "deny-list entries"
python3 -c 'import json; s = json.load(open("/etc/claude-code/managed-settings.json")); assert s["env"]["NULLBILL_HOME"] == "/etc/nullbill/agent"' ||
  fail "the repository hooks would not find the deny-list"
gitleaks version | grep -q "$(sed -n 's/^operator_gitleaks_version: //p' /ansible/roles/operator/defaults/main.yml)" || fail "gitleaks"
pass "git pushes with the owner's GitHub token and noreply identity, and the hooks have gitleaks and this server's deny-list"

grep -qxF '| workspace | workspace.nullbill.internal (10.42.3.20) | build | lxc | 2020 | yes |' /etc/claude-code/CLAUDE.md ||
  fail "the agent's rules list the guests it may act on"
grep -qxF '| router | router.nullbill.internal (10.42.1.1) | mgmt | vm | 2001 | no, through the host |' /etc/claude-code/CLAUDE.md ||
  fail "the agent's rules list the router as reached through the host"
code=0
out=$(su - agent -c 'nb-page verify') || code=$?
if [ "$code" != 1 ] || ! grep -qx "FAIL two" <<<"$out"; then
  cat /tmp/page.log
  fail "nb-page verify: exit $code, output '$out'"
fi
su - agent -c 'nb-page jobs' | grep -q "verify.failed.1" || fail "nb-page jobs"
code=0
su - agent -c 'nb-page apply ci' >/dev/null || code=$?
if [ "$code" != 1 ] || [ "$(cat /tmp/apply-body)" != '{"stage":"ci"}' ]; then
  fail "nb-page apply ci: exit $code, sent $(cat /tmp/apply-body)"
fi
sed -i 's#sha256//[^'"'"']*#sha256//AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=#' /usr/local/bin/nb-page
if su - agent -c 'nb-page jobs' >/dev/null 2>&1; then fail "nb-page talked to a page whose key is not the pinned one"; fi
converge operator.yml -e "@$vars"
pass "the agent runs verify and setup through the page, whose key it pins, and its rules list the guests"

# A Claude subscription token instead of an API key.
python3 - "$vars" <<'PY'
import json, sys
v = json.load(open(sys.argv[1]))
v["operator_anthropic_api_key"], v["operator_claude_oauth_token"] = "", "throwaway-token"
json.dump(v, open(sys.argv[1], "w"))
PY
ansible-playbook -i localhost, -c local -e "@$vars" /ansible/operator.yml >/tmp/oauth.log 2>&1 || { cat /tmp/oauth.log; fail "operator with a subscription token"; }
test ! -e /etc/nullbill/agent/anthropic-api-key || fail "the API key was left behind"
test "$(stat -c '%a %U %G' /etc/nullbill/agent/claude-oauth-token)" = "640 root agent" || fail "token mode"
python3 -c 'import json, sys; sys.exit("apiKeyHelper" in json.load(open("/etc/claude-code/managed-settings.json")))' ||
  fail "the key helper stayed with a token"
sed 's#^exec .*#exec env#' /usr/local/bin/claude >/tmp/claude-env
su agent -c 'sh /tmp/claude-env' | grep -qx CLAUDE_CODE_OAUTH_TOKEN=throwaway-token || fail "the wrapper does not pass the token"
su agent -c 'sh /tmp/claude-env' | grep -qx GH_TOKEN=github_pat_throwaway || fail "the wrapper does not pass the GitHub token to gh"
pass "with a subscription token instead, the CLI gets it in its environment and no API key is left"
