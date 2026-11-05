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
  2.1.0, pinned in `ansible/requirements.yml`, with paramiko). This works
  before guest networking or the VPN exists, and for containers without
  sshd.
- VMs (router, PBS, the golden images) are reached over SSH as `nullbill`
  with the owner's key, jumping through the host (`ProxyJump`), which sits
  on the mgmt bridge.

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

- The controller needs `sops` and `age` (pinned single binaries), the
  `community.proxmox` collection and paramiko. `tools/check` installs the
  same versions for the checks.
- Losing `age.key` loses the secrets. The services keep running, but apply
  has to regenerate every secret. The runbook says to back the key up next
  to the PBS encryption key.
- apply can be tested end to end against stand-ins (a fake `pct`, the
  fake PVE API, containers as guests) before any host exists.
