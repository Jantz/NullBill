# ADR 0014: Direct connections from mobile data

Status: accepted (the owner asked to "solve the problem of clients not
being able to directly connect to the NullBill server", 2026-11-09; the
design decided by Claude under his standing "pick, be critical and decide")

## Context

The owner's phone on mobile data reached the server through one of
Tailscale's relays (DERP): the Tailscale app said "Relayed connection".
Everything works that way, only slower, and Jellyfin buffers. Devices on the
home network have connected directly since the vpn guest may send from
tailscaled's port to private addresses.

Tailscale connects two devices directly when one of them can be reached at
an address and port the other learns from Tailscale's servers. A phone on
mobile data cannot be: its carrier sends it out from another port for every
destination. So the server has to be the one, and it sits behind two
routers: NullBill's own router VM, then the home router.

- tailscaled asks its gateway for a port by itself (NAT-PMP, PCP, UPnP).
  Its gateway is the NullBill router, which answers none of them; the home
  router that could is one hop further.
- A port forwarded by hand on the home router ended at the NullBill router
  too: its forward chain took nothing new in from the uplink.

Reference: <https://tailscale.com/kb/1257/connection-types> ("opening
incoming UDP/41641 (by default) on a device's public IP address will
guarantee a direct connection from any peer where it is possible"),
<https://tailscale.com/blog/how-nat-traversal-works>.

What was checked before deciding (2026-11-09, in network namespaces in a
container, with the rendered router files, the roles' own Tailscale 1.104.5,
a self-hosted coordination server with its own relay, miniupnpd 2.3.1 as
the home router, and a carrier NAT that picks a new port per destination):

- Without the change the phone stayed on the relay, while a laptop behind
  an ordinary NAT and a PC on the home network connected directly.
- With the port mapped at the home router and passed on by the NullBill
  router, the phone connected directly at the home's public address and
  that port; with the mapping given back it was relayed again. NAT-PMP and
  UPnP each did it alone.
- A PC at home running Tailscale holds Tailscale's default port, 41641, on
  the home router by the same means. With the server on 41642 both keep a
  mapping.
- Debian 12's `upnpc` (miniupnpc 2.2.4) takes a router only when it says it
  is connected and its outside address is not a reserved one (RFC 1918,
  RFC 6598, RFC 5737), and miniupnpd maps nothing on such an address
  (NAT-PMP: "network failure"). A home router behind its provider's shared
  address therefore answers and opens nothing, which is the truth: a port
  there cannot be reached from the internet.
- A UPnP search is answered by a new packet from the home router to the
  port the search left from, which a default-drop input chain discards.
  `upnpc -z` fixes that port, so one rule lets the answer in.
- The rendered unit ran under systemd 252 as a `DynamicUser`: it got the
  mapping, root (the guest agent) could read what it wrote, and with
  `IPAddressDeny=` on the network base it could reach the home router and
  no NullBill zone.

Not checked, because none is reachable from here: a real home router. The
owner's is the first.

## Decision

- tailscaled listens on UDP 41642 (`flows.TAILSCALE_PORT`), not Tailscale's
  default, so no other Tailscale device at home competes for it on the
  home router.
- The NullBill router sends UDP 41642 arriving on its uplink to the vpn
  guest and keeps the sender's address, and the vpn guest's pve-firewall
  file accepts it. That holds whether the home router's forward was asked
  for or made by hand. Nothing else comes in from the uplink.
- `nb-portmap` on the router VM asks the home router for that port every
  quarter of an hour with a lease of an hour: NAT-PMP first, then UPnP,
  with Debian's `natpmpc` and `miniupnpc` (two new packages on the router;
  this record is their approval; since ADR 0022 `nb-portmap` speaks
  NAT-PMP itself, because Debian 13's `natpmpc` refuses a home router
  whose address is not a public one before it asks). It takes the port only as asked: a router
  that offers another one is told to drop it, since Tailscale would not
  tell devices that one. It runs as a throwaway user that cannot reach a
  NullBill zone, since it reads what a device outside NullBill sends.
- It is on by default (`tailnet.port_mapping`), with a switch on the page
  (**Access**, **Tailscale**) for an owner who forwards the port by hand or
  wants none open. Switched off, it gives back every mapping it may still
  hold. Its record of those is kept across restarts of the router, because
  some home routers keep a UPnP mapping until told otherwise, and the rule
  that lets UPnP's answer in stays, because giving a mapping back starts
  with the same search.
- apply's vpn stage and verify read what the home router answered and set
  it against the addresses Tailscale offers for the vpn guest. The page
  shows the outcome as **Path**, with what to do when the port is closed.
  verify reports it as DEGRADED, not FAIL: relays carry everything.
- Changed on 2026-11-10: what the home router answered is evidence about
  the port and no more. The owner's home router sits behind a second one,
  the port forwarded on both by hand, and his phone on mobile data was
  direct while the page said "Relayed on mobile data" and verify DEGRADED.
  So the path is measured: tailscaled on the vpn guest says for each device
  whether its packets go straight (`CurAddr` in `tailscale status --json`),
  and the vpn guest pings each device that is on the tailnet without a
  direct path (`tailscale ping`, three tries, at most eight devices at
  once), which also has both ends look for a direct path, and takes the way
  the last answer came. A device that does not answer is left out. A direct
  path from an address that is no home network's is a device away from
  home: that settles it (PASS), whatever the home router said. A device
  that answers only through a relay, with none away direct and the port
  not known open, is DEGRADED. With neither, verify SKIPs and says what it
  read. The page reads this itself while Access, Tailscale is open
  (`/api/path`, every half minute) and shows when; only the kind of a
  device's address is kept, never the address.
- The home's public address stays on the router (nb-portmap's state file
  and the journal): it goes to neither the page's record nor a run's log.

Not built:

- IPv6. The zones carry none. It would be the only way past a provider's
  shared IPv4 address.
- PCP, NAT-PMP's successor. Routers that offer it and neither of the other
  two are rare.
- Tailscale's peer relays. One needs a device with an open port of its own,
  which is the same problem.
- A check from outside that a port forwarded by hand works. From in here
  it looks like a closed one; a device away from home that answers
  directly is what shows it, and until one does verify skips the check.
- UPnP's success path in CI: `upnpc` and miniupnpd refuse the documentation
  addresses a check has to use. `tools/guest-check/portmap.sh` covers
  NAT-PMP end to end and UPnP's refusal; `tests/test_portmap.py` reads
  what `upnpc` prints once a mapping is made.

## Consequences

- One UDP port at the home's public address leads to tailscaled on the vpn
  guest, an unprivileged process in an unprivileged container. It answers
  only devices whose keys the tailnet gave it, and an open port for it is
  what Tailscale itself names for this. It is the first port NullBill
  opens to the internet, so the page says so and has the switch.
- The first setup run after this change restarts tailscaled on the new
  port: connections over Tailscale pause for a few seconds.
- A home router with UPnP and NAT-PMP off, one that wants each device
  allowed first (a FRITZ!Box: "Permit independent port sharing for this
  device", which also takes that device out of the FRITZ!Box's own
  firewall), and one behind a second box all need the owner once
  (docs/runbooks/vpn.md). Behind a provider's shared address nothing on the
  owner's side helps, and devices on mobile data stay on relays.
- UPnP switched on at a home router lets every device at home open ports,
  not only this one. An owner who does not want that forwards the one port
  by hand and switches the asking off.
- After the home router restarts, the port is back within a quarter of an
  hour.
