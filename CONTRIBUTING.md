# Contributing

Thanks for helping. NullBill is a small project run by one owner, so issues
and short pull requests work best.

## Before you write code

- Open an issue first for anything larger than a fix, so we can agree on
  the approach. A change of tool needs a design record in `docs/adr/`.
- [`CLAUDE.md`](CLAUDE.md) maps the code: where each stage, role and check
  lives.

## Set up

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -e .
tools/install-hooks   # secret and deny-list checks on commit and push
tools/check           # everything CI runs; the container checks need docker
```

The pre-push hook needs `NULLBILL_HOME` set to a directory outside the
repository with a non-empty `denylist.txt`: words that must never be
pushed, such as your own name and hostnames.

## Rules for every change

- **Privacy.** This repository is public. Never commit secrets, real
  hostnames, IP or MAC addresses, usernames, emails, serial numbers or real
  probe output. Use `example.internal`, RFC 1918 or RFC 5737 addresses,
  placeholder names and synthetic hardware.
- **Checks.** Run `tools/check` before you push and say in the pull request
  what you ran. A check that cannot run reports SKIP, never PASS.
- **Golden files.** If a plan or render changes on purpose, regenerate with
  `UPDATE_GOLDEN=1 python3 -m pytest` and review the diff.
- **Nothing half done.** No stubs, no dead code, no numbers a script in
  this repository cannot reproduce. Unfinished work is an issue.
- **Commits** say what changed and why, in plain words.

## Pull requests

CI runs every check on each pull request. A docs-only change may skip the
container checks with a line in the pull request body that starts with
`CI-Skip: docker`; see `tools/check` for the names.

By contributing you agree that your contribution is licensed under the
Apache License 2.0, like the rest of the project.
