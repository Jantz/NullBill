# ADR 0015: Services of the owner's own, and lists that stay usable

Status: accepted (the owner asked: "Future service support can balloon, add
better management, like 100 game servers etc. Decide if every single
service needs to be manually supported by NullBill to set up and manage or
a NullBill agent can add a custom 3rd party service without NullBill
support.", 2026-11-10; the decision is Claude's, as he asked)

## Context

Every service NullBill runs so far is built in: an entry in
`catalog.yaml`, an Ansible role with a check that runs the real program in
a container, checks of its own in `verify`, text on the page. Three game
servers took about as much work as the monitoring stack. The owner will
want programs nobody here has heard of, and there are thousands.

Most of what a built-in service gets is not about its program at all. The
guest, its address and name, both firewall layers, the proxy's allow-list,
updates, node_exporter and its alerts, nightly backups, on-demand power,
sharing with a person and verify's probes of every allowed and denied flow
are generated from one entry under `services`. Since milestone 1 a test
adds a service by YAML alone and plan gives it all of that. What YAML alone
cannot give is the program in the guest, and a row on the page.

The page has an agent that already acts on the server through a gate the
owner sets (ADR 0010): it reads freely, and what changes the server waits
for the owner unless he chose otherwise. It can read a program's install
guide and write a setup script. It cannot be trusted to have got it right.

What was checked before deciding (2026-11-10):

- `ansible/roles/custom_service/tests/check.sh`, in a Debian 12 container:
  a setup script runs once, as root, with no terminal, with the service's
  name, address and ports and the proxy in its environment, and installs a
  Debian package; the same script does not run again and a changed one
  does; a script that fails stops the playbook with its last lines and runs
  again next time; one that hangs is ended after its time; text that is no
  script, or whose SHA-256 is not the one apply named, never runs.
- `tests/test_custom.py`: a service the agent's tool defines builds a model
  and a plan; its guest lets in only the ports it lists, from the vpn
  guest, and monitoring on 9100; it goes out only to the router's DNS, the
  proxy, the CA, the log store and the model gateway; the proxy gets an
  allow-list for its address alone; a friend ticked for it gets exactly its
  shared port in the Tailscale policy. A hundred of them plan on the large
  fixture, render both firewall layers and list on the page.
- `tools/check-firewall`, `tools/check-guests` and `tools/check-tofu` with
  the fixture `large-custom` (a container and a VM of the owner's own):
  stock pve-firewall compiles their files, nft, dnsmasq and squid accept
  the router's and the proxy's, and `tofu validate` accepts the guests.
- The page in Chromium with 36 such services beside the built-in ones.

Not checked, because nothing here can: any of it on Proxmox VE; a VM of
the owner's own from first boot to a running program (its setup goes the
way NullBill's own VMs do, over SSH through the host, and verify asks it
through the guest agent); Docker, in such a VM or in a container; a script the agent wrote
for a real program, run through the real proxy. How long a setup run takes
with a hundred guests was not measured.

## Decision

- **Both.** Built-in services stay as they are: for what many owners want
  and for what needs more than a guest (a GPU, the kept disks, a pool of
  clones, a vendor's sign-in tunnelled through the proxy). Everything else
  is a **service of the owner's own**: NullBill supplies what is around the
  program, and a setup script supplies the program. The owner's words on
  the page are "Your own services"; the code says `custom`.
- **A definition and a script, nothing else.** A service of the owner's
  own is an entry under `services` in `config.yaml`, in the catalog's own
  schema, and one file beside it, `custom/<name>/setup.sh`. There is no
  second format, no plugin interface and no image registry. Someone who
  never uses the agent writes both by hand and gets the same.
- **A guest of its own per service**, a container unless the program needs
  a kernel of its own or runs containers itself (Docker), then a VM. A program NullBill knows
  nothing about shares a kernel namespace, a disk and an address with
  nothing else, so what it breaks or leaks is its own.
- **A zone for them, `apps`**, whose only way out is the egress proxy; a
  game server goes in `games`. From either a guest reaches no other guest
  except the ones every guest reaches (DNS, the proxy, the CA, the log
  store) and the model gateway, so a program of the owner's own can use
  the local model with the gateway's key.
- **What the agent may define is narrower than what config.yaml can say**
  (`custom.define`): a guest of its own in `apps`, `games` or a zone the
  owner defined that does not go out directly; never `mgmt` or `access`;
  ports open only to the owner, to people he ticks (`shared`), to a named
  zone or a named service, never from the host or from everything, never
  22 or 9100; names for the proxy, never addresses; no GPU, no kept disk,
  no pool. The owner can write more by hand; plan checks that as before.
- **Ids from a range of their own.** The catalog keeps ids below 64;
  services of the owner's own take 64 to 127 and 192 to 253, which leaves
  128 to 191 to the pools' clones. That is 126 services, and a later
  NullBill can add built-in ones without taking an address in use.
- **The script runs as root in its guest, once per version**
  (`ansible/roles/custom_service`, apply's `custom` stage). `nb-custom-setup`
  keeps the SHA-256 of the script that last ran to its end; the same
  script is not run again, a changed one is, and so is one that failed.
  It has an hour. It gets the proxy in its environment and nothing from
  NullBill's secrets.
- **A script that fails does not stop setup.** The stage runs last before
  the checks, says which script failed and goes on; `verify` then fails by
  name (`<name>: its setup script ran as it is now`). A program NullBill
  does not know must not keep backups, Tailscale or the checks from
  running.
- **Adding one is the owner's decision, read in full.** The agent's
  `add_service` shows the definition line by line (the guest, each port
  and who reaches it, what it may fetch from, when it runs) and the whole
  script as a diff. A new service, a changed script, and a change of
  ports, zone, kind or allow-list are `risky`: they wait for the owner's
  typed word in Manual and in Auto. A change that brings no code and opens
  nothing (a title, memory, when it runs) is a `change` like saving the
  choices. A script that contains one of the server's secrets is refused.
  Nothing is built by saving: the next setup run does that, and that run
  is its own request.
- **Removing is never the agent's alone.** Turning a service off is a tick
  like any other; its guest is deleted by the setup run that asks the
  owner first ("type delete", as for every guest). Only then can the
  definition be removed, by the agent or under What to run, and the script
  is set aside in `custom/.removed`, not erased.
- **verify covers what NullBill made, and says so.** Every allowed and
  denied flow of the guest is probed like any other's. Of the program it
  checks two things: that the script beside `config.yaml` is the one that
  last ran, and that no systemd unit in the guest has failed (DEGRADED).
  Whether the program works is not NullBill's to know; `GuestDown` and the
  update alerts cover the guest.
- **Nothing stops by itself unseen.** A container's use is counted by TCP
  connections to its ports. One of the owner's own that listens on no TCP
  port would be stopped under its users, so `stop: auto` is refused for it
  by the tool, by plan and on the page.
- **Lists made for many.** Setup, What to run, groups its rows (Workloads,
  Game servers, Your own services, Extras), each group with a count; a
  group of more than eight shows what is on, chosen or on the server and
  keeps the rest behind one button; a filter above narrows every group and
  When it runs by name. The Services page has the same filter. The plan,
  the meter and each row come from the server as before.

## Consequences

- **A hundred servers of one game are a hundred services of the owner's
  own**, one definition and one script each (the scripts can be the same).
  Several instances of a built-in game (`minecraft` twice) are not built:
  the catalog's games are one guest each, with one pin and one world.
- A service of the owner's own has no page of its own: no sign-in the page
  can show, no settings beside its tick, no version the page knows. Its
  secrets are made by its script inside its guest and stay there.
- NullBill does not update the program. Debian's packages in the guest
  update nightly like everywhere; what the script fetched stays until the
  script changes.
- The script is trusted as much as the owner trusted it when he typed the
  word. It runs as root in an unprivileged container (or a VM) that
  reaches the proxy's allow-list and its own names. It cannot reach the
  host, the NullBill container, other zones or the home network.
- `proxy_allow` is where a careless definition leaks most: a name on it is
  somewhere the guest can send data. The request shows the list; the owner
  reads it.
- A setup run starts each on-demand guest to see whether it has anything
  to do there, as it does for the built-in ones, so runs grow with the
  number of guests.
- Access, People lists every shareable service to tick; with very many it
  is a long list without a filter yet.
- The model gateway's port is now open from the `apps` zone as it is from
  `build`; its key is still needed.
