# Set up the controller for apply

The controller is the machine `nullbill` runs on (OPEN_QUESTIONS 3). It is
never the PVE host. `probe`, `plan` and `verify` need only Python and SSH;
`apply` also needs Ansible, sops and age.

## Once

1. Python 3.11 or newer, in a virtual environment:

   ```sh
   python3 -m venv ~/.venvs/nullbill
   . ~/.venvs/nullbill/bin/activate
   pip install -e .
   tools/install-apply-deps        # ansible-core, paramiko, community.proxmox
   ```

   `tools/install-apply-deps` installs `requirements-apply.txt` and checks
   the community.proxmox tarball against its SHA-256 before installing it
   into `.ansible/collections`, where apply looks for it.

2. sops and age, built from source. Go's checksum database verifies them:

   ```sh
   go install github.com/getsops/sops/v3/cmd/sops@v3.13.3
   go install filippo.io/age/cmd/age@v1.3.2 filippo.io/age/cmd/age-keygen@v1.3.2
   ```

   Put `$(go env GOPATH)/bin` on PATH.

3. SSH to the host as `target.ssh`, written as `user@address`. The
   container connection uses paramiko, which does not read `~/.ssh/config`
   aliases. Log in once with `ssh root@<address> true`, so the host key is
   in `~/.ssh/known_hosts`, and keep the key in your agent.

## Secrets

The first `nullbill apply` creates `$NULLBILL_HOME/age.key`. Back it up
next to the PBS encryption key (`pbs-encryption-key.md`). Without it, apply
cannot read `secrets.sops.yaml` and has to make every secret again.

apply makes the secrets it can (database passwords, tokens). Add the ones
only you have, such as the agent's model API key, with:

```sh
cd "$NULLBILL_HOME"
SOPS_AGE_KEY_FILE=age.key sops edit secrets.sops.yaml
```

VM host keys are pinned on first sight in `$NULLBILL_HOME/known_hosts`. If
you rebuild a VM, delete its line there.

## Run

```sh
nullbill apply --dry-run          # render only, touch nothing
nullbill apply                    # every stage, stopping at the first failure
nullbill apply --stage host       # one stage
nullbill apply --close-lan        # also close LAN access to 8006 and 22 (#9)
```

Every stage is safe to re-run. Until VPN reach is confirmed, apply keeps
PVE's default LAN access to the web UI and SSH. Pass `--close-lan` only
after you have logged in to the host over the VPN.
