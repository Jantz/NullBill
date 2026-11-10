# ADR 0017: A download client beside Jellyfin

Status: accepted (the owner asked what should come next and left the choice
and the design to Claude, 2026-11-10; the brief was a download client whose
finished downloads land where Jellyfin's library sees them)

## Context

Jellyfin (ADR 0009) plays what is on the disks the owner kept. Getting new
files onto such a disk meant copying them over the file share from another
computer. A download client on the server closes that gap, and it is the
one piece people who run a media server at home most often add next.

It is also the first workload whose traffic cannot follow NullBill's rule
for everything else. BitTorrent talks to whoever else holds a file, at any
address and on any port, so the egress proxy, which passes named HTTPS
sites, cannot carry it. And everyone in a download sees the address every
other participant sends from. For a server at home that address is the
home's.

What was checked before deciding (2026-11-10):

- qBittorrent 4.5.2 as Debian 12 ships it (`qbittorrent-nox`, libtorrent
  2.0.8), run for real in the role's check: where its settings live, that
  it starts only once its legal notice is accepted, the format of its
  password hash, that a password written before the first start means the
  well-known default never works, its API for adding, pausing and resuming,
  and that a request from another address without a sign-in gets 403.
- RSS in that version, in the same check: a feed is read and a download
  rule adds the torrent it offers.
- Linux sends one user's traffic by a routing table of its own
  (`ip rule ... uidrange`), in a container with only `NET_ADMIN`; a route
  of type `unreachable` behind the tunnel's makes that user's packets fail
  at once when the tunnel's interface is gone.
- The whole path in network namespaces with the rendered router ruleset, a
  real WireGuard tunnel and the role's own `nb-torrents`
  (`tools/guest-check/tunnel.sh`): the client's user arrives at a server on
  the "internet" from the VPN's address and never from the home's; root in
  the same guest reaches nothing outside; with the guest's own routes
  deleted the router still lets nothing but the tunnel pass; with the
  tunnel down, its interface down or the VPN's server gone, nothing gets
  out, and it carries again once the server is back.
- The port from home, in network namespaces with the rendered ruleset, the
  role's `nb-portmap` and a stand-in home router
  (`tools/guest-check/portmap.sh`): after `nb-portmap` asked, a peer on the
  "internet" reaches the client's guest on TCP and UDP 41643 from its own
  address; the port next door does not; switched off, the port is given
  back and Tailscale's stays.
- The page in Chromium at desktop and phone width: ticking it, choosing a
  way, pasting a file, a refused file, saving, forgetting the file.

Not checked, because nothing here can: any of it on Proxmox VE (WireGuard
inside an unprivileged container with the host's module, the unit's
`BindReadOnlyPaths` there, writing to an ntfs-3g mount through a bind mount
point), and a real VPN provider's file and server.

## Decision

- **Built in, not a service of the owner's own.** ADR 0015's script would
  have to get exactly the part wrong answers hurt most: which way the
  traffic leaves. That belongs in both firewall layers and in `verify`,
  which only a built-in workload has.
- **qBittorrent from Debian.** Maintained, free, a web page of its own, and
  in Debian 12, so its security fixes arrive with every other package's
  (the updates role). No pinned download to chase. Its page is plain HTTP on
  8080, reached over Tailscale only, as Jellyfin's is.
- **Its own zone, `torrents`, with one guest.** Nothing else lives there,
  and no zone may reach it; the owner (and people it is ticked for) reach
  its page through the vpn guest. It holds no secret but its own.
- **The way out has no default.** `torrents.route` is `vpn` or `home`, and
  plan refuses the workload until the owner says which. Choosing it on the
  page, under qBittorrent's own notice about uploading, is also the
  owner's answer to that notice. The page agent may tick the workload and
  change where downloads land; choosing the route is `risky` with a typed
  word in every mode, as agreeing to a game's EULA is, and it is told never
  to choose for the owner.
- **`vpn`: the owner's own WireGuard VPN, enforced three times.**
  - The owner pastes their provider's WireGuard file on the page. It holds
    a private key, so it is kept in the secrets (`torrents_vpn_conf`,
    reduced to the lines NullBill uses) and never shown again; only the
    server's address and port go to `config.yaml`
    (`torrents.vpn.endpoint`), because the firewall is made from them.
  - The plan turns the zone's egress into `tunnel`: the router's nftables
    and the guest's pve-firewall file let the guest send UDP to that one
    address and port and nothing else outside. Packages come through the
    egress proxy, and the router answers the zone for NullBill's own names
    only.
  - In the guest, `nb-torrents tunnel up` (unit `nb-tunnel`) makes `wg0`
    and sends everything the client's user (`qbt`, uid 1000) sends, except
    to NullBill's own network, by a table that holds the tunnel and behind
    it an `unreachable` route. qBittorrent is bound to `wg0` and its unit
    requires the tunnel's; it reads names from the VPN's DNS servers. No
    `wg-quick`: nothing is run from the pasted file.
  A VPN is never required and NullBill supplies none.
- **`home`: said plainly.** The zone goes out directly, like `services`.
  The page, the plan and the Services page say that everyone else in a
  download sees the home's public address, and `verify` reports
  `downloads leave through a VPN` as DEGRADED for as long as it is so.
- **From home, one port in, asked for like Tailscale's** (the owner,
  2026-11-10: "No VPN is needed. I need an open port forwarded from the
  home router to the torrent client", like the direct connection setup).
  qBittorrent listens for other peers on TCP and UDP 41643
  (`flows.TORRENTS_PORT`; not one of BitTorrent's well-known ports, which
  some trackers refuse). With `route: home` the router's nftables sends
  that port from the uplink on to the client's guest with the sender's
  address kept, and the guest's pve-firewall file accepts it. The router's
  `nb-portmap` (ADR 0014) asks the home router for it every 15 minutes,
  NAT-PMP then UPnP, in both protocols, as a further port beside
  Tailscale's (`more` in its settings and state); `torrents.port_mapping:
  false` (the row's tick) makes it give the port back, for an owner who
  forwards it by hand or wants it shut. `verify` says what the home router
  answered: PASS when it opened both, DEGRADED with its reasons when not
  (downloads still work), SKIP when the owner forwards by hand, which
  cannot be seen from the server. qBittorrent's own UPnP stays off: its
  container cannot reach the home router, and one program asks for
  NullBill's ports. Behind a VPN no port is open at home.
- **Downloads land on a kept disk.** `torrents.folder` (default
  `Downloads`) of a kept disk the owner made editable (`disks.<id>.mode:
  rw`; `torrents.share` names it when there are several) is the guest's
  one bind mount point, at `/downloads`. The client sees that folder and
  nothing else of the disk. Jellyfin already reads the whole disk, so a
  finished download is at `/media/<share>/<folder>` without a copy, and
  the file share shows it too. Unfinished downloads wait in
  `incomplete/` with a `.ignore` file, which Jellyfin skips. Plan refuses
  the workload without such a disk and says how to make one editable.
  Like the rest of a kept disk, downloads are not backed up.
- **RSS is on from the first start** (the owner asked for feeds and
  private trackers, 2026-11-10), written into qBittorrent's settings
  before it first runs and not enforced afterwards. Feeds are fetched by
  the client's user, so they take the same way out as everything else.
- **A nearly full disk pauses downloads, it does not fail them.**
  `torrents.keep_free_gib` (25): every minute `nb-torrents tick` pauses
  what is downloading while the disk has less free, and resumes exactly
  those once it has that plus a tenth more. The owner's kept disk is
  over 90% full today; without this the first large download would fill
  it for Jellyfin and the file share too. Alert `DownloadsPausedDiskFull`
  (warn) tells them; `DiskAlmostFull` leaves `/downloads` out, as it
  leaves the share out.
- **On demand like the rest.** `power.torrents`: always on by default,
  stops by itself after 30 idle minutes when the owner puts it on demand.
  In use means: something downloads or seeds, its page is open, or it
  cannot be asked (`/etc/nullbill/busy.d/torrents`).
- **Sharing is one password.** Its port is `shared`, so a person can be
  given it under Access, People. qBittorrent has one sign-in and no
  accounts: the tick list says that whoever has the password downloads in
  the owner's name and can change every setting, which includes running a
  program in the container after a download.
- **verify proves the claim.** Behind a VPN: the tunnel's last handshake is
  fresh; the client's user reaches an address on the internet through the
  tunnel while root in the same guest reaches nothing; and with the
  tunnel taken down for a moment the client's user reaches nothing either
  (`nb-torrents tunnel drop`, which puts it back). Also: the page asks for
  a sign-in, the folder is writable, downloads are not paused for room.

## Consequences

- Without a kept disk that may be changed there is nothing to tick yet. A
  store of its own (a volume on a storage disk) is not built; it would
  need a second way for Jellyfin to see it.
- The VPN's server must be an IPv4 address the firewall can name. A file
  that gives a name is looked up once, when it is saved; the NullBill
  container resolves no outside names today, so such a file is refused
  with that reason and the owner puts the address in. A provider that
  moves its server needs the file pasted again; `DownloadTunnelDown`
  (warn) says when the tunnel stopped answering.
- The download client's port is the second NullBill opens to the
  internet, after Tailscale's, and the first with a program behind it
  that answers strangers: libtorrent, in a container that sees one folder
  and holds nothing else. Debian's security updates are its patches.
- Behind a VPN the provider's port forwarding is not used, so others
  cannot open connections to the client there. Downloads work; seeding to
  people behind routers of their own is slower. Open question in
  `OPEN_QUESTIONS.md`.
- A home router without a public address of its own (a provider's shared
  address, a second router) cannot open the port; `verify` says so with
  the home router's words.
- WireGuard's module is loaded on the Proxmox host
  (`/etc/modules-load.d/nullbill-wireguard.conf`), only with `route: vpn`.
- While the tunnel is down the client sends nothing and its page still
  answers. Nothing leaks, so this warns and does not page.
- The search plugins and the "run a program" setting stay as qBittorrent
  ships them; they act inside a container that sees one folder and, behind
  a VPN, reaches only the VPN.
- IPv6 is not carried: the tunnel gets the file's IPv4 address only, and
  the zone has no IPv6 route, so nothing leaves beside it.
