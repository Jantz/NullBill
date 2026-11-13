# Updates by themselves, and the move to Debian 13

NullBill keeps every program at its vendor's newest stable version and
installs what is new each night. Why it is built this way:
[ADR 0022](../adr/0022-latest-stable.md).

| What | Where |
|---|---|
| What waits, what the last night did, the switch for nightly updates | the page's **Updates** |
| What the next setup run upgrades | the page's **Setup**, **Review and run** |
| What is pinned now | `tools/pins list` in `/opt/nullbill` |

## 1. What happens each night

You need: nothing. It is on unless you turn it off on **Updates**.

1. Once a day the repository moves every version to the vendor's newest
   stable one, runs every check with them, and only then publishes it.
2. Between 03:00 and 06:00 your server fetches that, installs it and runs
   setup when the update changes something on the server. Debian's own
   security updates install every night as before. The hours are by the
   clock of the NullBill container, which may be UTC and not your time;
   `date` in the container says.
3. You get one message on your alert topic when something was installed
   and set up, and one when something waits for you.

The night sets up only what the night installed. After an update you
installed yourself, or a nightly setup run that failed, setup waits for
you under **Setup**, **Review and run**.

Small steps install by themselves. A big one waits: a new major version
of a service you run (Forgejo 17 to 18, say), any new Minecraft version
(your players and every plugin or mod must follow), or a new Debian. **Updates** lists it as "Waits for
your yes". Press **Update NullBill** when you want it.

## 2. A server built before Debian 13

You need: about an hour in which nobody uses the server, and the Proxmox
host's password or an SSH key for it in case something has to be put back.

Guests made before this version run Debian 12. The first setup run after
the update moves each to Debian 13 in place:

1. On **Updates**, "Debian in every guest, 12 to 13" waits. Press **Update
   NullBill**.
2. Open **Setup**, **Review and run**. It says how many guests are
   upgraded. Press **Start setup**.
3. The run upgrades one guest after another, the router first. For each it
   takes a snapshot in Proxmox, upgrades, restarts the guest and checks it.
   Each guest is away for some minutes; while the router restarts, nothing
   on the server is reachable for about a minute, this page included.
4. PostgreSQL moves from 15 to 17 in the same step. Its old data stays on
   the guest's disk, stopped.
5. When the run has finished, the NullBill container upgrades itself. This
   page is away for about ten minutes and comes back by itself.

Only Debian's own packages move in this step. Programs from their vendors'
repositories (Jellyfin, Grafana, Tailscale, step-ca) stay at their version
until the setup stages after it move them to the pinned one.

Windows, backups (PBS) and services of your own are not touched. A service
of your own stays on Debian 12, which gets security updates until June
2028.

The snapshots are named `nb-before-debian-13`. A setup run a week or more
later deletes them, the NullBill container's too. Until then each grows
with what its guest writes, so look at the disk's free room on
**Overview** during that week.

## 3. When an upgrade fails

The run stops at "Bring guests from before to Debian 13" and names the
guest. The guests before it are on Debian 13 and stay there; the ones
after it were not touched. What happened to the guest itself is the last
sentence of the error:

- "It is back as it was before, from its snapshot": it runs Debian 12 as
  before. This is also what happens when the guest does not answer after
  its restart. Run setup again to try once more. If it stops at the same
  guest, copy the details and report them.
- "Its backup from before is on ...": the file shares, Jellyfin and the
  download client cannot be snapshotted, so nothing put the guest back.
  It is half upgraded. Restore it as section 4 says, then run setup again.
- "Putting back its snapshot failed too": put it back by hand, section 4.

Setup refuses to go on with a guest that was left half upgraded ("an
upgrade began in it and was neither finished nor put back") until you
have put it back, and it leaves the guest's copy alone until then.

The end of the upgrade's log is in the run's details. The whole log is in
the guest at `/var/log/nb-release-upgrade.log` only until the guest is
put back.

When the NullBill container's own upgrade fails, the host puts it back
and the page returns on Debian 12. The log of that try is on the Proxmox
host, `/root/nullbill/controller-upgrade.log`. NullBill does not try
again by itself. To try again, delete
`/var/lib/nullbill/controller-release.json` in the NullBill container and
run setup.

## 4. Putting a guest back by hand

You need: a shell on the Proxmox host.

A container (find its number in Proxmox, here 2013):

```
pct stop 2013
pct rollback 2013 nb-before-debian-13
pct start 2013
```

The router is a VM; use `qm` in place of `pct`. The NullBill container is
2002 unless you chose another number at install.

A guest that Proxmox could not snapshot (the file shares, Jellyfin and the
download client hold folders of your kept disks) got a backup instead.
The run's log names where: `nb-pbs` or `local`. In Proxmox, open that
storage, pick the backup dated just before the upgrade and press
**Restore**. The backup holds the guest's own disk, not the folders of
your kept disks, which the upgrade does not touch.

After putting a guest back, delete its line from
`/var/lib/nullbill/releases.json` in the NullBill container if it has one,
so the next setup run looks at the guest again.
