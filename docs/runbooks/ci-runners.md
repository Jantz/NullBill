# CI runners

Each Forgejo Actions job runs on a fresh clone of the pool's golden image,
destroyed afterwards ([ADR 0004](../adr/0004-ci-runners.md)). VMIDs and
labels come from `$NULLBILL_HOME/plan.yaml`: `template_vmid` and
`clone_vmids` under each pool service.

## Use it

In a repository, enable Actions (Settings, Units), then add
`.forgejo/workflows/<name>.yml` with `runs-on: linux`. Jobs run directly
on the clone as the user `runner`, with git and Node.js installed. Network
access beyond Forgejo goes through the egress proxy's allow-list.

## See what it is doing

On the host:

```sh
pct exec <dispatcher vmid> -- journalctl -u nb-dispatcher -f
qm list | awk '$1 >= <first clone vmid> && $1 <= <last clone vmid>'
```

A clone's own log is on its serial console: `qm terminal <vmid>`.

## A job never starts

1. `journalctl -u nb-dispatcher` on the dispatcher shows `cloning`. If it
   does not, check the job's `runs-on` against the pool's labels, and check
   that a VMID in the range is free (`qm list`).
2. It shows `stopping <vmid>: never fetched its job`. The clone could not
   reach the dispatcher on 8443, or could not verify its certificate. Run
   `nullbill verify`; the `build` zone's probe to `runner-dispatcher
   tcp/8443` and the dispatcher's TLS check point at which.
3. The job waits for a clone, the clone retries, and the dispatcher logs
   `clone ... failed`. Its PVE token is missing a grant; `nullbill verify`
   lists each `dispatcher token ...` check.

## Re-bake the golden image

When no clones exist (a linked clone keeps its template's disk in use),
destroy the template on the host, `qm destroy <template vmid> --purge`,
then run apply. OpenTofu recreates the VM from the cloud image, and apply
bakes it and converts it again. apply cannot do this yet (#22).

## Clean up by hand

The dispatcher destroys stopped clones in its range. If it is down,
`qm stop <vmid> && qm destroy <vmid> --purge` on the host. Then list
leftover registrations in Forgejo (Site administration, Actions, Runners)
and delete any named `nb-*` whose clone is gone.

## Rotate its tokens

- PVE: `pveum user token remove nb-dispatcher@pve dispatcher`. Then run
  apply, which recreates the token and reinstalls it in the dispatcher.
- Forgejo: delete the `dispatcher` token under the admin user's
  Applications. Then run apply.
