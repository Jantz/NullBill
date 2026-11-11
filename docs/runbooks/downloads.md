# Downloads (qBittorrent)

The workload **Downloads (qBittorrent)** is a BitTorrent client on the
server. What it finishes lands in one folder of a disk you kept, where
Jellyfin and the file share already look. Why it is built this way:
[ADR 0020](../adr/0020-download-client.md).

| What | Where |
|---|---|
| Its page | `http://10.42.10.28:8080` (guest 2028), from your devices on the tailnet |
| Sign-in | user `nullbill-admin`, password from **Show password** on the page's **Services** page |
| Finished downloads | the folder `Downloads` on the kept disk; in Jellyfin `/media/<disk>/Downloads` |
| Unfinished ones | `Downloads/incomplete`, which Jellyfin skips |

While something downloads, the parts you already have are uploaded to
others. What you download and share is your own responsibility.

## 1. Decide which way downloads go

Everyone else in a download sees the address the client sends from, and so
does anyone who watches that download. NullBill does not choose for you:

- **Through a VPN of my own.** Everything the client sends leaves through
  your VPN, and others see the VPN's address. If the VPN drops, the client
  sends nothing until it is back. NullBill supplies no VPN: you need an
  account with a provider that offers WireGuard.
- **From my home connection, without a VPN.** Others see your home's
  public address. The checks then report **downloads leave through a VPN**
  as degraded, for as long as it stays that way. The server asks your home
  router to open one port (TCP and UDP 41643) so that others can connect
  to you, which seeding and private trackers need; part 5 has the rest.

## 2. Turn it on

**You need:** a kept disk you may change files on. On the **Setup** page,
under **Disks**, the disk is set to **Keep its files and share them
to me** with **Let me change files on it** ticked
([legacy-disk.md](legacy-disk.md)). For the VPN: your provider's WireGuard
file.

1. For a VPN, sign in on your provider's site and make a **WireGuard
   configuration** (often under "manual setup" or "router"). Pick one
   server; the file names it by address in most cases. It is a few lines
   that start with `[Interface]`.
2. On the **Setup** page, in **What to run**, tick **Downloads
   (qBittorrent)**.
3. Choose the way downloads go. For the VPN, paste the whole file into
   **Your VPN's WireGuard file**. The line above it then names the server
   the firewall will open to.
4. Check **Kept disk**, **Folder on it** and **Room to keep free**, then
   press **Save and plan**. A file that cannot be used is refused here
   with every reason listed; nothing is saved until it reads.
5. Open **Review and run** and start setup. The stage **Set up the
   download client** installs it.
6. **Run the checks.** Behind a VPN they show that the tunnel is up, that
   the client sends through it and its container reaches nothing outside
   by itself, and that a dropped tunnel stops the client's traffic.

The file holds a key. NullBill keeps it with its secrets and never shows
it again; **Forget the saved file** drops it.

## 3. Use it

**You need:** a device on your tailnet.

1. On the **Services** page open **Downloads** and sign in as
   `nullbill-admin` with the password shown there.
2. Add a torrent file or a magnet link. Leave the save path as it is, or
   choose a folder under `/downloads`. The client can write nowhere else:
   a download told to go to another path fetches into `incomplete` and
   stays there, and qBittorrent says so only in its log ("Failed to move
   torrent"). The checks (**every download is saved inside the downloads
   folder**) and an alert name such a download; **Set location** to a
   folder under `/downloads` finishes it.
3. A finished download is in Jellyfin after its next library scan
   (**Dashboard**, **Libraries**, **Scan All Libraries** does it at once),
   and in the file share under the same folder.

To let someone else add downloads, tick **Downloads** for them under
**Access**, **People**, and send them the sign-in. There is one password
and no separate accounts: with it they download in your name and can
change every setting.

## 4. Feeds and private trackers

**RSS.** Feeds and download rules are on from the start. In qBittorrent
open the **RSS** tab, add your tracker's feed with **New subscription**,
then make a rule under **RSS Downloader** that names what to fetch. The
client fetches feeds the same way it sends everything else: through the
VPN when you chose one. If you switch RSS off in its options, it stays
off.

**Private trackers.** Add their torrents as any other. qBittorrent turns
DHT, peer exchange and local discovery off for a private torrent by
itself. Three things to know:

- The tracker sees the address the client sends from: your VPN's, or your
  home's. Check that your tracker allows VPN addresses, and pick one
  server and stay on it if the tracker ties your account to an address.
- From home, the open port (part 5) makes you reachable, which is what
  trackers mean by "connectable". Behind a VPN no port is open: you
  download at full speed and seed to everyone who is reachable themselves,
  but your ratio grows more slowly. A VPN provider's port forwarding is
  not built.
- A feed address or a passkey in a torrent is a secret of yours. It is
  kept in qBittorrent's own files in its container, which is in the
  nightly backups.

## 5. The open port, from home

**You need:** nothing, if your home router has UPnP or NAT-PMP on, as most
do.

With **From my home connection** chosen and **Ask my home router to open
port 41643 for it** ticked (it is, unless you untick it), the server asks
your home router for TCP and UDP 41643 every 15 minutes and passes that
port on to the download client. It is the same request the server makes
for Tailscale's port. Only the download client answers on it.

After setup, **Run the checks** shows **the home router sends the download
client's port to it**:

| It says | Meaning | What to do |
|---|---|---|
| passed: others have connected to it from outside | Measured: qBittorrent has taken a connection from the internet. This counts whatever the home router answered. | Nothing. |
| passed | Your home router said it opened the port. | Nothing. |
| degraded, with what the home router answered | It did not open the port when asked, and nobody has connected from outside yet. The port may be shut. The answer is no proof: a forward you made by hand may work all the same. | Test from outside (below). If it is shut: on the home router turn on UPnP or NAT-PMP, or forward TCP and UDP 41643 to the address the check names and untick the box. |
| degraded: it says it has no public address of its own | The home router says it is not on the internet itself: a second router stands in front, or your provider shares one address between customers. The server cannot open a port on that other router, or see one. | With a second router of your own, forward TCP and UDP 41643 on it to the home router, and on the home router to the address the check names; then test from outside. With a provider's shared address, ask for a public one, or use the VPN way. |
| skipped | You unticked the box. A port forwarded by hand cannot be seen from the server. | Test from outside. |

**To test from outside:** start a download that has many peers and run the
checks again; they pass once anyone has connected to qBittorrent from the
internet (the connection sign in the bar at the bottom of its page then
reads Connected). Or, from a device at home, ask a port checking website
for TCP 41643. The row under **Setup**, **What to run**, **Downloads**
names the address a forward made by hand goes to.

Do not change the listening port in qBittorrent's options: setup sets it
back to 41643, the only port the firewall passes on.

## 6. When the disk fills up

Downloads pause by themselves while the disk has less free than **Room to
keep free** (25 GiB unless you changed it), and go on once it has that and
a tenth more. Finished files keep seeding. The alert **downloads are
paused** tells you; delete or move files, or lower the number under
**Setup**, **What to run**.

## 7. When something is wrong

| What you see | What it means | What to do |
|---|---|---|
| Alert: the download client's VPN does not answer | The tunnel has had no answer from the VPN's server for 15 minutes. The client sends nothing meanwhile. | Check your VPN account. If the provider changed the server, make a new file and paste it under **Setup**, **What to run**, then run setup. |
| Plan: no VPN yet | **Through a VPN of my own** is chosen and no file is saved. | Paste the file, or choose the other way. |
| The file is refused: its Endpoint could not be looked up | The file names the server instead of giving its address. | Put the server's IPv4 address in the `Endpoint` line, or ask the provider for a file with one. |
| Plan: downloads go to a kept disk that can be written to | No kept disk has **Let me change files on it**. | Tick it under **Setup**, **What to run**, **Disks**; [legacy-disk.md](legacy-disk.md) says what Windows must have done first. |
| Nothing downloads, the page works | Paused for room, or the tunnel is down. | In the Proxmox shell: `pct exec 2028 -- nb-torrents space` and `pct exec 2028 -- nb-torrents tunnel status`. |
| A download stays in `incomplete` at 100 %; alert: a download is set to be saved outside /downloads, or qBittorrent could not move a finished download | Its save path lies outside `/downloads`, where the client cannot write. | In qBittorrent, right-click it, **Set location**, and choose a folder under `/downloads`. `pct exec 2028 -- nb-torrents paths` names each such download and what qBittorrent could not move. The check and the alerts end once the download has a location under `/downloads` or is removed. |

## 8. Turning it off

Unticking it deletes the client with its list of downloads and its
settings at the next setup run, which asks first. The downloaded files
stay on the kept disk. A saved VPN file stays until you press **Forget
the saved file**.

## What is not built

- A VPN provider's port forwarding: behind a VPN others cannot open
  connections to the client, so seeding is slower there. From home the
  port is open (part 5).
- A place for downloads other than a kept disk. A kept disk that is not
  shared (`disks.<id>.share: false`) can take them; then neither the file
  share nor Jellyfin shows them.
- HTTPS for the client's page; it is reached over Tailscale only, as
  Jellyfin is.

Not yet tried on real hardware: all of it. The path through a WireGuard
tunnel and both firewall layers is tested in containers and network
namespaces, and so is the open port, against a stand-in home router;
Proxmox itself, your home router, a real provider and a real kept disk
are not.

First run on real hardware, 2026-11-11: the client ran and downloaded. It
showed that a save path outside `/downloads` failed without a word, and
that the home router's answer about the port was taken as proof when a
forward made by hand worked; both are changed as described above.
