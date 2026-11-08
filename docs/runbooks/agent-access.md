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
  make on the page or the commands to run, unless you let it manage the
  server (below).

## Let it manage the server

On the page under step 6, **Let the agent manage the server**, press
**Turn on**. You chose this over the confined default
(docs/adr/0003-agent-access.md); it lasts until you press **Turn off**.
While it is on, the agent also:

- has root on Proxmox: `ssh pve '<command>'`, one command per session,
  only from the operator's address, each command written to the host's
  journal (and Loki) before it runs;
- runs setup and updates through the page: `nb-page apply`,
  `nb-page apply <stage>`, `nb-page upgrade` (NullBill to the newest commit
  on main), and reads every job's output. Each of those jobs shows on the
  page like your own, and your ntfy topic gets a message when it starts
  and when it ends;
- proposes fixes to NullBill as pull requests, if you saved a GitHub token
  in the same step (fine-grained, NullBill's repository only, Contents and
  Pull requests read and write). Its commits use your GitHub noreply
  address. It never pushes to main; you merge.

Its rules tell it to ask you before anything that deletes data or cannot
be undone, and before a host reboot. That is the model's cooperation, not a
control: with root it could do anything. verify shows
`the agent has no root on the PVE host` as DEGRADED while this is on.

**Turn off** takes its key off Proxmox at once. If the page cannot reach
Proxmox then, it says so; stop the operator container (below) to cut the
agent off anyway.

A typical session: `claude`, then ask it to check the server and fix what
it finds, or to update everything: it runs `nb-page upgrade`, then
`nb-page apply`, then `nb-page verify`, and tells you what changed.

For long work, start it inside `tmux` (`tmux new -s agent`, detach with
Ctrl-b d, `tmux attach -t agent` later) so it keeps running when you close the
Shell.

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

Commands it ran as root on Proxmox: `{job="commands", host="pve"} |= "ssh: "`.

In Grafana (`http://<monitoring address>:3000` over Tailscale, normally
`http://10.42.1.14:3000`; Explore, Loki) or with
`curl -G https://<monitoring address>:3100/loki/api/v1/query_range`:

- `{job="commands"}`: every agent CLI tool call (`tool Bash: ...`) and
  every command over SSH (`ssh: ...`).
- `{job="commands", uid="<agent's uid>"}`: only the agent's. Trust the `uid`
  label; journald sets it, and the agent cannot. The message text is
  whatever the agent wrote.
- `{job="auth"} |= "ID agent"`: each certificate login, with its serial.

## Cut it off (break glass)

Each step works alone; do them in this order when you want it stopped now.

1. Stop the container: `pct stop <operator vmid>` on the host. Nothing the
   agent holds works without it: its token, certificates, host key and API
   key are only there. Then turn the switch off on the page, or remove the
   line ending in `nullbill-agent-root` from `/root/.ssh/authorized_keys`
   on the host yourself.
2. Revoke the PVE token: `pveum user token remove nb-agent@pve operator`.
   `nullbill apply` creates a new one later.
3. Stop new SSH certificates: on step-ca, replace
   `/etc/step-ca/secrets/agent-password` and remove the `agent`
   provisioner from `/etc/step-ca/config/ca.json`, then
   `systemctl restart step-ca`. Certificates already issued expire within
   1 h; to end them at once, remove `/etc/ssh/nullbill_user_ca.pub` on the
   agent-managed guests and `systemctl reload ssh`.
4. Revoke the model API key in the Anthropic console, and the GitHub token
   in GitHub's settings.

To bring it back, re-run `nullbill apply`: the step_ca role restores the
provisioner and its password, and apply copies the new password, token and
key to the operator (issue #22).

## Without the agent

Everything the agent does, you can do yourself with your own access:
**Run checks** on the page, `pct`/`qm` in the Proxmox Shell, the PVE web
UI, and `pct enter <vmid>` into a guest. Roll back a snapshot yourself
(`pct rollback`/`qm rollback`); the agent can take snapshots but cannot
roll back.
