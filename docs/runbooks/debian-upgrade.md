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
   security updates install every night as before.
3. You get one message on your alert topic when something was installed,
   and one when something waits for you.

Small steps install by themselves. A big one waits: a new major version
of a service you run (Forgejo 17 to 18, say), a new Minecraft version your
players must follow, or a new Debian. **Updates** lists it as "Waits for
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

Windows, backups (PBS) and services of your own are not touched. A service
of your own stays on Debian 12, which gets security updates until June
2028.

The snapshots are named `nb-before-debian-13`. A setup run a week or more
later deletes them.

## 3. When an upgrade fails

The run stops at "Bring guests from before to Debian 13" and names the
guest. That guest was put back from its snapshot and runs as before; the
guests before it are on Debian 13 and stay there; the ones after it were
not touched. The end of the upgrade's log is in the run's details, and the
whole log is in the guest at `/var/log/nb-release-upgrade.log` (as it was
before the guest was put back, so read it from the run's details).

Run setup again to try once more. If it stops at the same guest, copy the
details and report them.

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
**Restore**.

After putting a guest back, delete its line from
`/var/lib/nullbill/releases.json` in the NullBill container if it has one,
so the next setup run looks at the guest again.
