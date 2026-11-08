# Jellyfin, the media server

The workload **Media server (Jellyfin)** plays the films, series and music
on the disks you keep from a previous OS, in the browser and in Jellyfin's
apps for phones, tablets and TVs, over Tailscale. The disks stay
read-only to Jellyfin. Why it is built this way:
[ADR 0009](../adr/0009-media-server.md).

| What | Where |
|---|---|
| Jellyfin | `http://10.42.7.24:8096` (guest 2024), from your devices on the tailnet |
| Sign-in | user `nullbill-admin`, password from **Show password** on the page's **Services** page |
| Your kept disks, inside Jellyfin | `/media/<name>`, one folder per kept disk |

## 1. Turn it on

**You need:** the setup page, and a disk with your media kept under
**What to run** on its **Setup** page ([legacy-disk.md](legacy-disk.md)).
Without one Jellyfin has nothing to play.

1. On the **Setup** page, in **What to run**, tick **Media server
   (Jellyfin)**, press **Save and plan**, and check that the **Plan** card
   shows no problems. The plan's `media` section says
   whether Jellyfin transcodes on the processor's graphics or on the
   processor itself.
2. Press **Run setup** at the top of the page. The stage **Set up
   Jellyfin** installs it, finishes its first-run setup and makes its
   admin.
3. **Run checks** shows Jellyfin's checks: it answers with its setup done,
   shows nothing without sign-in, reads each kept disk and cannot write to
   it, and, with the processor's graphics, transcodes with Quick Sync.

To transcode on the processor's graphics, they must be on: on the
i9-9900K, the BIOS setting that keeps integrated graphics on beside a
graphics card (often **IGPU Multi-Monitor**). Press **Read the hardware**
on the **Setup** page again after changing it. A 9900KF has none.

## 2. Add your libraries

**You need:** Jellyfin open and signed in.

1. Open **Dashboard**, then **Libraries**, then **Add Media Library**.
2. Pick the content type (Movies, Shows, Music and so on), press the plus
   next to **Folders** and choose a folder under `/media`, such as
   `/media/data/Films`.
3. Press **OK**. Jellyfin reads the folder and fetches titles and posters
   through the server's internet proxy.

Jellyfin keeps its own data (posters, watch history) in its guest, never on
your disks.

## 3. Watch on other devices

**You need:** Tailscale on the device, signed in with your account.

- In a browser: `http://10.42.7.24:8096`.
- Jellyfin's apps (Android, iOS, Android TV, Fire TV and others): add the
  server `http://10.42.7.24:8096` and sign in.

TVs without Tailscale on your home network cannot reach it yet.

## If setup stops at Set up Jellyfin

- "no render node for the integrated graphics": the processor's graphics
  are switched off now. Press **Read the hardware** on the **Setup** page
  again and run setup again; Jellyfin then transcodes on the processor.
- "Jellyfin refuses nullbill-admin": you changed the admin password in
  Jellyfin, and setup needs it to make its key again. In Proxmox, select
  the node, open **Shell** and run `pct enter 2024`, then put the new
  password in `/etc/nullbill/jellyfin-admin-password`, and run setup
  again. **Show password** then shows the new one.
