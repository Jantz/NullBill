# A Windows desktop on the server

The workload **A Windows desktop VM** makes one Windows 11 VM, `windows-dev`
(VMID 2022, address `10.42.5.22` in NullBill's windows zone), with 2 cores,
4 GiB of memory and a 96 GiB disk. You reach it with Remote Desktop over
Tailscale. NullBill makes the VM and its firewall; you install Windows in
it yourself, once.

On a 16 GiB server it fits next to the AI agents, without Linux CI runners
and without backups (the **Plan** card on the setup page's **Setup** page
says so if it does not).

What NullBill does not do for it yet: Windows Update is not set up through
NullBill's proxy, and the VM is not backed up unless backups are on.

## 1. Upload the Windows installer

**You need:** about 6 GB free on the computer you download with, and the
Proxmox web UI.

1. Download the Windows 11 ISO (**Download Windows 11 Disk Image (ISO)
   for x64 devices**) from <https://www.microsoft.com/software-download/windows11>.
   Choose your language; the file is called something like
   `Win11_25H2_English_x64.iso`.
2. In the Proxmox web UI, open **Datacenter**, **pve**, **local (pve)**,
   **ISO Images**, press **Upload**, pick the file and press **Upload**.
   Keep the `Win` in its name: setup finds the ISO by it. Keep only one
   Windows ISO there.

## 2. Make the VM

**You need:** the ISO uploaded, the setup page.

1. On the **Setup** page, in **What to run**, tick **A Windows desktop
   VM**, untick what does not fit (Linux CI runners, Nightly backups on
   16 GiB), press **Save and plan**, and check the **Plan** card shows no
   errors.
2. Press **Run setup** at the top of the page. The tofu stage prints
   `tofu: Windows guests install from local:iso/...` and makes the VM.

## 3. Install Windows

**You need:** the Proxmox web UI, and a Windows product key if you have one
(Windows also installs and runs without one, unactivated).

1. In the Proxmox web UI, select **2022 (windows-dev)** and open
   **Console**. If it shows a UEFI menu or a shell instead of the Windows
   installer, press **Reset** at the top and, as soon as **Press any key
   to boot from CD or DVD** appears, press a key in the console.
2. Install as usual. Pick **Windows 11 Pro** (Remote Desktop needs Pro;
   Home cannot be reached that way). Without a key, choose **I don't have a
   product key**. Install on the one disk it shows.
3. The VM reaches the internet only through NullBill's proxy, so Windows'
   first-start setup finds no network. If it asks to connect and offers
   nothing to skip it, press **Shift+F10** and run
   `start ms-cxh:localonly` to make a local account. If that does not
   work on your Windows version, stop there and ask in the chat.
4. When the desktop shows: **Settings, System, Remote Desktop**, turn it on.

## 4. Use it

**You need:** a device on Tailscale (first-install part 10).

- Windows: the **Remote Desktop Connection** app, computer `10.42.5.22`.
- macOS, iPhone, iPad, Android: Microsoft's **Windows App**, add a PC
  `10.42.5.22`.

Sign in with the account you made in step 3.
