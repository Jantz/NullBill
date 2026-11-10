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

## The whole platform, bigger jobs, model and pictures (2026-11-10)

The agent could run commands but not use what the page itself offers, and
a job of many steps asked for a card at every step in Manual. Now:

- **Every feature of the page has a tool**, each through the same gate:
  `services` (the Services page and every guest), `metrics` (one Prometheus
  query), `on_demand` (start, stop, keep on), `people`, `set_person`,
  `remove_person`, `port_mapping`, `check_updates`, `test_alert`,
  `stop_job`, `check_service`, and `set_choices` now takes `games` and
  `power`. `docs/runbooks/agent-services.md` tells it how to work inside
  each built-in service without guessing. Kept from the agent on purpose:
  disks, SSH keys, passwords, Tailscale's keys, and its own access mode and
  root switch. Giving a person more than they had is risky (word `share`)
  and the card says what sharing means: that service's own ports over
  Tailscale, no shell or root in its guest, nothing else on the server.
- **A task** is a job the owner approves once (`start_task`, a change-tier
  card that lists the plan and the guests). Until it ends (`finish_task`,
  **End task** above the chat, a new conversation, or six hours) a
  change-tier action inside the guests it names runs without a card, and so
  do setup runs when the task says so. Each still shows in the chat as
  "Ran for the task" and in the audit log with the task's id. Risky stays
  risky and blocked stays blocked. A task cannot name the Proxmox host, the
  NullBill container or a guest of the `core` workload.
- **A service of the owner's own in one go.** `add_service` with
  `build: true` puts on its one risky card what approving it does: saves
  the service, starts the setup run and starts a task for the new guest.
  The agent is told when the run ends, reads `check_service`, and may
  correct its own script inside that task (the chat shows the diff). A
  change to what the service opens, its zone, kind or allow-list is risky
  as before. The reasoning: the owner already approved a script that runs
  as root in that guest, and the task gives no more than that.
- **Plan.** `show_plan` keeps one checklist card in the chat, updated in
  place, so a long job reads as progress and not as a column of tool lines.
- **Model and thinking level** are the owner's choice (under the message
  box and on the Agent page), passed to the CLI as `--model` and
  `--effort`; "Default" passes neither. Each answer names the model that
  gave it. A turn may now take up to 200 steps and an hour.
- **Pictures.** The owner attaches up to four (button, paste or drop); the
  browser scales them to 1568 px, the server checks the type by the file's
  first bytes, keeps them in `$NULLBILL_HOME/agent/images` (the newest 200)
  and hands them to the CLI as image blocks over `--input-format
  stream-json`. A picture goes to the model as it is: the page cannot take
  a secret out of an image as it does out of text, and says so beside the
  button.
- **Not built: the agent taking screenshots itself.** The NullBill
  container has no browser and reaches only the host and Prometheus; a
  headless browser there, with a route to every service's web page, is a
  tool change and a wider network for the container that holds the secrets.
  It waits for the owner's decision.
- **What the owner reads.** A card's result is a sentence in the owner's
  words (`chat.Told`); the advice on what to do next goes to the agent
  only. Tables the agent writes render as tables, and its rules ask for
  short answers with one fact a line.
