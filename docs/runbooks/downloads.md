# Downloads (qBittorrent)

The workload **Downloads (qBittorrent)** is a BitTorrent client on the
server. What it finishes lands in one folder of a disk you kept, where
Jellyfin and the file share already look. Why it is built this way:
[ADR 0017](../adr/0017-download-client.md).

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
  as degraded, for as long as it stays that way.

## 2. Turn it on

**You need:** a kept disk you may change files on. On the **Setup** page,
under **Other disks**, the disk is set to **Keep its files and share them
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
2. Add a torrent file or a magnet link. Leave the save path as it is.
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
- No incoming port is open, with or without a VPN. You download at full
  speed and seed to everyone who is reachable themselves, but two people
  who are both unreachable cannot connect, so your ratio grows more slowly.
  Opening a port depends on the VPN provider and is not built yet.
- A feed address or a passkey in a torrent is a secret of yours. It is
  kept in qBittorrent's own files in its container, which is in the
  nightly backups.

## 5. When the disk fills up

Downloads pause by themselves while the disk has less free than **Room to
keep free** (25 GiB unless you changed it), and go on once it has that and
a tenth more. Finished files keep seeding. The alert **downloads are
paused** tells you; delete or move files, or lower the number under
**Setup**, **What to run**.

## 6. When something is wrong

| What you see | What it means | What to do |
|---|---|---|
| Alert: the download client's VPN does not answer | The tunnel has had no answer from the VPN's server for 15 minutes. The client sends nothing meanwhile. | Check your VPN account. If the provider changed the server, make a new file and paste it under **Setup**, **What to run**, then run setup. |
| Plan: no VPN yet | **Through a VPN of my own** is chosen and no file is saved. | Paste the file, or choose the other way. |
| The file is refused: its Endpoint could not be looked up | The file names the server instead of giving its address. | Put the server's IPv4 address in the `Endpoint` line, or ask the provider for a file with one. |
| Plan: downloads go to a kept disk that can be written to | No kept disk has **Let me change files on it**. | Tick it under **Other disks**; [legacy-disk.md](legacy-disk.md) says what Windows must have done first. |
| Nothing downloads, the page works | Paused for room, or the tunnel is down. | In the Proxmox shell: `pct exec 2028 -- nb-torrents space` and `pct exec 2028 -- nb-torrents tunnel status`. |

## 7. Turning it off

Unticking it deletes the client with its list of downloads and its
settings at the next setup run, which asks first. The downloaded files
stay on the kept disk. A saved VPN file stays until you press **Forget
the saved file**.

## What is not built

- A VPN provider's port forwarding: others cannot open connections to the
  client, so seeding to people behind routers of their own is slower.
- A place for downloads other than a kept disk.
- HTTPS for the client's page; it is reached over Tailscale only, as
  Jellyfin is.

Not yet tried on real hardware: all of it. The path through a WireGuard
tunnel and both firewall layers is tested in containers and network
namespaces; Proxmox itself, a real provider and a real kept disk are not.
