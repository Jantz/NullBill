# ADR 0001: Write the nullbill CLI in Python

Status: proposed, awaiting owner review

## Context

`nullbill` runs on a controller, gathers probe output over SSH, sizes a plan
and will drive OpenTofu, Ansible and Packer. The candidates were Python and Go.

Ansible's control node must have Python
(https://docs.ansible.com/ansible/latest/installation_guide/intro_installation.html),
so every controller already carries a Python runtime. Go would give a single
static binary but would add a second toolchain next to the one Ansible needs.

## Decision

Python 3.11 or newer, with two runtime dependencies: PyYAML for config and
plans, and jsonschema to validate `config.yaml` against
`src/nullbill/data/schema.yaml`.

## Consequences

- One runtime on the controller, shared with Ansible.
- Installation is `pip install`, not a binary download; a controller needs a
  virtual environment or pipx.
- Python 3.11 is the floor because `datetime.UTC` is used; Debian 12 ships 3.11.
