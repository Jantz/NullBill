# Run, watch and cut off the runtime agent

The agent lives in the operator container as the unprivileged `agent` user
(`ansible/roles/operator`). PVE lets its token power-manage and snapshot
guests in the `agent-managed` pool and only read everything else. step-ca
gives it SSH certificates for the principal `agent` that last at most 1 h,
and only agent-managed guests accept them. Every tool call and every
command it runs on a guest goes to Loki before it runs.

References: <https://pve.proxmox.com/pve-docs/chapter-pveum.html>,
<https://smallstep.com/docs/step-ca/provisioners/>

## Start a session

1. Open a shell in the operator: in the Proxmox web UI, select the node `pve`, open **Shell** and run
   `pct enter <operator vmid>`.
2. `su - agent`, then `claude` (inside `tmux` for long work). The CLI reads its key, proxy and rules from
   `/etc/claude-code/`, which only root can change.
3. To act on a guest yourself the way the agent does: `nb-ssh <guest ip> <command>`.

## What it can do

- **Inspect and analyze**: read every guest's state, config and task log
  through the PVE API, metrics and firing alerts in Prometheus, and logs in
  Loki. Its rules (`/etc/claude-code/CLAUDE.md`) list each guest with its
  address and whether it may act on it.
- **Check**: `nb-page verify` runs `nullbill verify` through the NullBill
  page and prints its PASS, FAIL, SKIP and DEGRADED lines. `nb-page jobs`
  shows whether your last setup run failed.
- **Repair**: start, stop, restart and snapshot agent-managed guests, and
  run commands on them as the unprivileged `agent` user.
- **Configure**: anything beyond that it proposes to you, as the change to
  make on the page or the commands to run; it cannot apply it.

For long work, start it inside `tmux` (`tmux new -s agent`, detach with
Ctrl-b d, `tmux attach -t agent` later) so it keeps running when your SSH
connection ends.

## Agents on other computers

An agent that runs elsewhere, such as Claude Code on your laptop, gets the
same API as `nb-page` and nothing more: start `nullbill verify` and read
verify's output. It cannot start setup, read other jobs or use the page as
you.

1. On the page under step 7, name it under **Agents on other computers**
   and press **Make a token**. The page shows `NB_PAGE_URL`, `NB_PAGE_SPKI`
   and `NB_PAGE_TOKEN` once; give them to the agent (its environment, or a
   file only it reads). The page keeps the token's SHA-256 only.
2. The computer needs to be on your tailnet. Signed in as you, it already
   reaches the page. A computer that is not yours joins with an auth key
   tagged `tag:nb-agent` (Settings, Keys; Pre-approved, not Reusable). The
   tailnet policy lets that tag reach the page's port and nothing else,
   and its tests check that on every push.
3. The calls, which pin the page's key the way `nb-page` does:

   ```sh
   page() {
     curl -sS -k --pinnedpubkey "sha256//$NB_PAGE_SPKI" \
       -H "Authorization: Bearer $NB_PAGE_TOKEN" -H 'X-NullBill: 1' "$@"
   }
   page -X POST -H 'Content-Type: application/json' -d '{}' "${NB_PAGE_URL}api/agent/verify"  # starts verify: {"id": ...}
   page "${NB_PAGE_URL}api/agent/jobs/<id>?offset=0"  # text, offset, state: running, done or failed
   page "${NB_PAGE_URL}api/agent/jobs"                # verify jobs and their state
   ```

The page accepts a remote agent's token only from the vpn guest's address,
which is where everything from the tailnet arrives, and the operator's
token only from the operator. Each agent request goes to the nullbill
container's journal with the agent's name (`journalctl -u nullbill-web |
grep 'agent remote:'`), and each verify job records who started it (`by`
in `jobs/<id>.json`). **Revoke** on the page refuses the token at once.

## Read what it did

In Grafana (`http://monitoring.nullbill.internal:3000` over the VPN, Explore, Loki) or with
`curl -G https://monitoring.nullbill.internal:3100/loki/api/v1/query_range`:

- `{job="commands"}`: every agent CLI tool call (`tool Bash: ...`) and
  every command over SSH (`ssh: ...`).
- `{job="commands", uid="<agent's uid>"}`: only the agent's. Trust the `uid`
  label; journald sets it, and the agent cannot. The message text is
  whatever the agent wrote.
- `{job="auth"} |= "ID agent"`: each certificate login, with its serial.

## Cut it off (break glass)

Each step works alone; do them in this order when you want it stopped now.

1. Stop the container: `pct stop <operator vmid>` on the host. Nothing the
   agent holds works without it: its token, certificates and API key are
   only there.
2. Revoke the PVE token: `pveum user token remove nb-agent@pve operator`.
   `nullbill apply` creates a new one later.
3. Stop new SSH certificates: on step-ca, replace
   `/etc/step-ca/secrets/agent-password` and remove the `agent`
   provisioner from `/etc/step-ca/config/ca.json`, then
   `systemctl restart step-ca`. Certificates already issued expire within
   1 h; to end them at once, remove `/etc/ssh/nullbill_user_ca.pub` on the
   agent-managed guests and `systemctl reload ssh`.
4. Revoke the model API key in the Anthropic console.

To bring it back, re-run `nullbill apply`: the step_ca role restores the
provisioner and its password, and apply copies the new password, token and
key to the operator (issue #22).

## Without the agent

Everything the agent does, you can do yourself with your own access:
`nullbill verify` from the controller, `pct`/`qm` on the host, the PVE web
UI, and SSH to guests with your key. Roll back a snapshot yourself
(`pct rollback`/`qm rollback`); the agent can take snapshots but cannot
roll back.
