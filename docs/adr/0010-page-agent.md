# ADR 0010: The agent in NullBill's page

Status: accepted (the owner asked for the page to be rebuilt around an agent
that can debug and update everything, asks before critical choices, with
Manual, Auto and YOLO access modes, 2026-11-09; the design decided by
Claude under his standing "pick, be critical and decide")

## Context

Until now the agent that manages the server was the operator agent
(ADR 0003): a Claude Code session in the operator container, reached from
the Proxmox shell, with root on the host only while the owner's switch is
on. That agent exists only after setup has built the operator container,
which is too late: most help is needed while the first setup run fails.
The owner wants to sign in an agent on NullBill's page right after
installing, talk to it from his phone, and approve what it changes there.

## Decision

The page runs its own agent, in the NullBill container, from the first
minute:

- Claude Code headless (`claude -p --output-format stream-json`), one
  process per turn, resumed by session id, pinned by version and SHA-512
  like the operator's (`tools/setup-controller` installs it). It runs as
  the unprivileged user `nbagent` whose home is 0700 and who cannot read
  `$NULLBILL_HOME`; its credential (API key or subscription token, kept in
  sops) reaches it only through the environment of its own process.
- Its built-in tools are read-only (Read, Grep, Glob over NullBill's
  source, WebSearch, WebFetch). Everything it does to the server goes
  through one MCP server, `nullbill agent-tools`, which calls the page over
  a unix socket (0660, group `nbagent`) with a token that is valid only
  while that turn runs. The agent holds no SSH key and no API token.
- The page decides. `web/gate.py` sorts each request into read, change,
  risky (needs a typed word) or blocked, and the owner's access mode says
  which tiers run without asking: Manual runs read only, Auto runs read and
  change, YOLO runs read, change and risky. Blocked never runs in any mode:
  reading secrets, erasing disks, deleting backups, silencing the command
  log, writing authorized_keys and touching the NullBill container itself.
  Unknown programs count as change.
- What waits for the owner is a request card in the chat with Approve and
  Decline (risky ones need the word typed); requests expire after 30
  minutes. The page carries out an approved request itself and tells the
  agent the result in its next turn. `ask_owner` puts a question with
  tappable answers in the chat.
- Every action is written to `$NULLBILL_HOME/agent/audit.jsonl` before it
  runs; commands on Proxmox are first logged to its journal (`nb-cmd`),
  which ships to the log store. An action that cannot be recorded does not
  run. Output is cut to 20 000 characters and every known secret value is
  taken out before the model sees it.
- When a run fails while the agent is signed in, it looks into it by itself
  once (the owner can turn that off), and runs it starts are announced on
  the owner's phone like the operator's.

The page itself was rebuilt around this: the agent is docked beside every
page on a wide screen and is its own tab on a phone; signing it in is the
first setup step; a failed run offers "Ask the agent to fix it". The page
is installable as an app (manifest, icons from `tools/make-icons`, a
service worker that keeps only the page's own files and never `/api/`);
browsers allow that once they trust NullBill's root certificate.

## Consequences

- The operator agent stays for long work and remote agents; the page agent
  does not replace the owner's root switch, which still governs the
  operator's key on the host.
- The page agent can do on the host what its tier allows, through the
  page's own SSH access; in YOLO that includes reboots and deletes outside
  the blocked list. The checks do not warn about the mode; the Agent page
  shows it, and the log records every action with the mode it ran in.
- The gate reads a command lexically: it is the owner's view of what the
  agent asks for, not a sandbox. Anything it cannot read ahead (a variable,
  an unknown program, a name built at run time) is at least change, and code
  it cannot see into (a script, an interpreter, `eval`, `sh` reading its
  input) is risky. A rule that sorts a command too low is a hole; the rules
  are a maintained list with regression tests (`tests/test_gate.py`).
- Enforcement by privilege is follow-up work, not done here: running the
  agent's host commands as an unprivileged user with narrow sudo rules, a
  PVE token with only the privileges each tier needs, and a log setup the
  agent's user cannot change. Until then the blocked list holds only as far
  as the lexical reading does, and the audit log and the `nb-cmd` journal
  lines are the record of anything that slips past it.
- Besides single commands the agent has: `run_steps` (up to 12 commands,
  one card, the worst tier decides, stops at the first failure),
  `write_file` (the card shows the diff; the old file is kept under
  `/var/lib/nullbill-agent-backup`), background runs (`systemd-run`, output
  in `/var/log/nb-agent/<id>.log`, read with `read_run`), `update_packages`
  (simulated first; removing packages is risky), `watch_job`, `check_reach`
  and `remember` (notes the owner reads and edits on the Agent page).
