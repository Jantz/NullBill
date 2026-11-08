# ADR 0005: How apply reaches the host and guests, and where secrets live

Status: proposed, awaiting owner review

## Context

The roles from M2 to M5 are written and container-checked, but `nullbill
apply` only renders files (#22). The real apply must:

- bring up an empty PVE host in one command and re-run safely;
- pass secrets (API tokens, database passwords, one-time certificate
  tokens) to roles without writing them in plaintext on the controller;
- reach each guest before the VPN exists, including the forgejo guest,
  which runs no OpenSSH because Forgejo's own SSH server takes port 22.

## Decision

**Order.** apply runs these stages and stops at the first failure. Each
stage is idempotent, so re-running apply resumes.

1. Render `build/`, as `--dry-run` does today.
2. Host: run the baseline role, then install the pve-firewall files.
3. OpenTofu (`build/tofu`): bridges, images, guests, pools and tokens.
4. Router: install the nftables, dnsmasq and interface files.
5. step-ca. Then, for each service guest, a one-time token and `tls-cert.yml`.
6. Services, in dependency order: postgres, monitoring, PBS, forgejo,
   egress proxy, log shippers, operator and runner dispatcher.
7. Bake each pool's golden image, then convert it to a template.
8. Run `verify`, and print its summary.

**Reach.**
- The host is reached over SSH as `target.ssh`, as `probe` and `verify` do.
- Containers are reached through the host with `pct exec`, using the
  `community.proxmox.proxmox_pct_remote` connection plugin (collection
  2.1.0, checked against its SHA-256 by `tools/install-apply-deps`, with
  paramiko). This works
  before guest networking or the VPN exists, and for containers without
  sshd. `ansible/connection_plugins/nb_pct.py` is that plugin with
  pipelining turned on: each module goes to the container on the stdin of
  one `pct exec`, where the plugin alone takes four (temporary directory,
  copy, chmod, run). VMs are pipelined too. The apply check counts 7
  `pct exec` calls for one `copy` task, against 11 without.
- VMs (router, PBS, the golden images) are reached over SSH as `nullbill`
  with the owner's key, jumping through the host (`ProxyJump`), which sits
  on the mgmt bridge. Each VM's host key is pinned on first sight in
  `$NULLBILL_HOME/known_hosts`, kept apart from the owner's own.

**Reruns.**
- After a playbook succeeds on a machine, apply leaves an HMAC of its
  inputs there (`/var/lib/nullbill/applied/<playbook>`): the playbook,
  every role it reaches (not their `tests/`), the connection plugin, and
  the variables (`src/nullbill/applied.py`). The next run reads it first
  and leaves that machine out when it is the same. The mark goes before a
  run starts, so a run that stops part way is run whole next time, and a
  guest OpenTofu makes afresh has none.
- What depends on more than its inputs runs anyway: `tls-cert.yml` every
  time (certificates last 24 h), the runner dispatcher after a bake (the
  bake stopped it), the golden images (baked from a fresh VM). The NVIDIA
  host role's inputs include the running kernel and the loaded module.
- `apply --full` runs everything, and so does the run after one whose
  checks failed (`$NULLBILL_HOME/apply-full-next`), in case a guest
  changed under unchanged inputs.

**Secrets.**
- `$NULLBILL_HOME/secrets.sops.yaml` is encrypted with sops, using an age
  key at `$NULLBILL_HOME/age.key` that the owner keeps in their backups.
- apply generates each secret once (database passwords, the shipper CA) or
  reads it back from OpenTofu's encrypted state (API tokens). It decrypts
  only in memory, and passes values to Ansible through a temporary file
  that is mode 0600 and deleted when the stage ends. No secret is ever
  written under `build/`.
- One-time step-ca tokens are made per run and never stored.

## Alternatives

- **SSH into every container.** It needs sshd and the owner's key in each
  container, plus the VPN or a jump host. It also cannot reach Forgejo's
  guest without moving git off port 22.
- **Plain 0600 files in `$NULLBILL_HOME`.** No new tool, but anything that
  copies `$NULLBILL_HOME` (a sync client, a backup) copies the secrets in
  the clear. OpenTofu state is already encrypted for the same reason.
- **Ansible Vault.** It is built in, but whole files are encrypted with one
  password, so diffs and reviews of which secrets exist are opaque. sops
  keeps the keys readable and encrypts only the values.

## Consequences

- The controller needs sops 3.13.3 and age 1.3.2 (built with `go install`,
  which Go's checksum database verifies), ansible-core 2.19 with paramiko
  (`requirements-apply.txt`) and the `community.proxmox` collection.
  `tools/check-apply` builds the same versions for its stand-ins.
- Losing `age.key` loses the secrets. The services keep running, but apply
  has to regenerate every secret. The runbook says to back the key up next
  to the PBS encryption key.
- apply can be tested end to end against stand-ins (a fake `pct`, the
  fake PVE API, containers as guests) before any host exists.
