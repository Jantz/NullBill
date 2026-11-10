# ADR 0017: Saying what a setup run does to each machine before it starts

Status: accepted (the owner asked "What happens to my Minecraft server when
I run this update?", 2026-11-10, and the page could not say; he then left
what to build next to Claude. The design is Claude's)

## Context

**Setup**, **Review and run** listed the guests a run creates, deletes and
changes in Proxmox, the disks, and every changed line of the plan. For the
guests that stay, which is nearly all of them on nearly every run, it said
nothing: not whether their setup runs again, not whether a program in them
restarts, not for how long. The **Updates** page marked a change as
"needs a setup run" and stopped there. The answer existed only in the
code: apply leaves a playbook out on a machine that holds the digest of
the same sources and inputs from its last good run (ADR 0005, Reruns).

The owner should not have to ask, and nobody should have to answer from
memory.

One condition was set with the request: finding this out changes nothing
on the server.

## Options

- **Run the stages with a Reach that only reads.** The stage code would
  compute its real inputs. But stages read and write as they go (they make
  secrets, certificates and tokens, and decide by what a command printed),
  so a read-only run either reaches the server a great deal, with every
  reading a way to fail on a server that is half asleep, or needs a fake
  answer for each command, which is a second apply to keep true. Rejected.
- **`ansible-playbook --check`** over every machine. Exact for what it
  covers, but it logs in to every guest, starts on-demand guests to do so,
  takes minutes, and reports `command` and `shell` tasks as skipped, which
  is where restarts hide. Rejected.
- **`tofu plan`** says what happens to guests in Proxmox. The review has
  that already from the plan and `guests.json`, without asking Proxmox.
- **Compare with what the last runs wrote down.** Chosen.

## Decision

The controller writes down what each playbook run was given, and the page
compares that with what the next run would give. The server is asked
nothing: no SSH, no API call, no job.

**The record.** `run_playbooks` writes one entry per playbook and machine
to `$NULLBILL_HOME/applied.json` (`applied.RECORD`): the digest of the
playbook's sources, one fingerprint per input (and per entry of an input
that is a table, so a changed file can be named), the stage, the commit,
when it last ran, how long that took, and when a run last saw it unchanged.
Fingerprints are HMACs under the key the machines' marks already use, so
the file holds no secret and no value. An entry loses its digest while its
playbook runs and gets it back only when the run ends well, exactly as the
mark on the machine does. The games stage adds how long the server took to
answer again after its setup. A CI pool's golden image keeps no mark; its
entry (`applied.IMAGE`) says when it was baked and with which settings.

**What a run would give.** Each stage declares the playbook runs it makes
in `apply.FORESEEN`: a function from the context to `Step`s (playbook,
machine, inputs), or `None` for a stage that runs none on a machine that
keeps a mark. The stage and its `foresee_` function take the inputs from
the same builder (`forgejo_inputs`, `monitoring_inputs` and so on), so the
preview cannot drift from apply. Inputs only a run can read, such as a
certificate or a token made on a guest, are named in `Step.read` and taken
as unchanged. `preview.py` calls these functions with a `Reach` that
raises on any use and secrets that cannot be saved; a stage that would
need the server is reported as not worked out, never guessed.

**What it restarts.** A playbook that runs again mostly changes nothing.
`preview.effects` reads the roles' YAML and follows what changed (an
input, a file of this version of NullBill) through tasks, registered
results, `set_fact`, includes and handlers to the units they restart or
reload and to reboots. It is conservative: a change it cannot place
counts for the whole role, and the row says "could not be followed". It
never says "restarts", only "may restart": Ansible restarts a unit only
when the file on the machine in fact differs.

**The page.** Review and run shows a row per machine the run touches:
create, delete, change, or set up again, with why (which settings, which
files), what may restart, and how long its setup took the last time.
Everything else is listed by name under "Left alone". The Updates page
shows the same for a waiting update, from the files the two commits hold
(`git archive`; nothing of the new version runs). The agent's `status`
carries both, so it answers from the same source as the page.

## What it does not know

Said on the page under the table, and here in full:

- **A server built before this version** has no record. Until one setup
  run has written it, machines are listed as "Not known", never as left
  alone.
- **What a run reads while it runs.** A certificate renewed or a token
  made during the run changes an input the preview took as unchanged.
  Certificates that lapsed while the server was off make the run renew
  them, which restarts their services.
- **Steps outside playbooks.** A stage that changes a container's devices
  (the GPU, the render node) restarts it; a changed kept disk stops the
  files and Jellyfin guests for the mount. The review's Disks rows and the
  guests' Change rows say when these apply; the restart itself is not
  listed per machine.
- **A waiting update that changes NullBill's Python.** The preview reads
  the new roles but does not run the new code, so it cannot say what the
  new version would compute as inputs. The Updates page says so, and the
  review after updating is exact.
- **A CI image's two roots.** An image is baked again when the plan's
  settings changed or it is a week old, both foreseen; a new root, read
  from the CA guest when it bakes, is not.
- **Changes made by hand** on a machine. apply does not see them either
  until a full run (`--full`, or after failed checks).
- **How long a service is down.** The page gives how long the machine's
  setup took the last time it ran, and for a game how long it took to
  answer again. That is a measured upper bound for the same work, not a
  forecast of the restart alone.

## Consequences

- A new stage must have an entry in `apply.FORESEEN`, and a new playbook
  run a `Step` built from the inputs builder the stage uses
  (`tests/test_preview.py` runs a full setup on six plans and fails when a
  recorded run is not foreseen exactly, or a foreseen one not recorded).
- `applied.sources` now counts a role whose file another role places
  (`host_baseline` installs `agent_ssh`'s `nb-agent-command`), which the
  digest had missed. The host's baseline therefore runs once more after
  this update.
- The first setup run with this version writes the record; every machine
  is "Not known" until then.
