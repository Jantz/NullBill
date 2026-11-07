# First install on the server

Every step from a switched-off machine to a working NullBill, in the order
you do them. Each part starts with **You need**: have those ready before you
begin that part.

This is the first run on real hardware. Everything has run against
stand-ins (containers playing the host, guests and services), but this is
the first time it meets Proxmox itself. Expect to stop at a stage, fix it,
and press the button again. Every stage can be run again safely.

Overview:

1. Gather accounts and apps (on your phone and computer)
2. Prepare the disks (on the old Windows PC, if a disk comes from one)
3. Put the server together
4. Set up the BIOS
5. Make the Proxmox USB stick
6. Install Proxmox VE 9
7. Sign in to Proxmox once
8. Install NullBill
9. The setup page, steps 1 to 8: first setup run
10. Join Tailscale and open the page from anywhere
11. Close the home network (setup page step 9)
12. Protect what only you can protect: Proxmox second factor, backup key
13. Open your kept disk's files

## 1. Gather accounts and apps

You can do this part days ahead, away from the server.

**You need:** your phone, a computer with a web browser, a password manager.

1. **ntfy**: install the free ntfy app on your phone (App Store or Google
   Play). No account. You subscribe to your topic in part 9.
2. **Tailscale account** (needed in part 9, step 3, even if you set up
   Tailscale later): sign up at <https://login.tailscale.com/start> with
   Google, Microsoft, GitHub, Apple or another sign-in it offers. The free
   Personal plan is enough. Open <https://console.tailscale.com/admin/users>
   and note your login exactly as it is shown there; for a GitHub sign-in it
   looks like `name@github`. The setup page asks for it.
3. **Coding agent credential** (only if you want the AI agents): either an
   Anthropic API key (`sk-ant-api...`) from <https://console.anthropic.com/>,
   or a subscription token: on a computer where you use Claude Code with a
   Pro, Max, Team or Enterprise plan, run `claude setup-token`. It prints a
   token (`sk-ant-oat...`) that lasts one year and is not saved anywhere, so
   put it in your password manager straight away.
4. **Healthchecks.io** (optional, to hear about power cuts): sign up at
   <https://healthchecks.io/>, add a check, and set its schedule to a period
   of 10 minutes and a grace time of 10 minutes. Copy its ping URL
   (`https://hc-ping.com/...`). It stays "new" until the server pings it.
   Healthchecks.io alerts the address you signed up with; its
   **Integrations** page adds other channels.

## 2. Prepare the disks

Decide what each disk in the server is for. There are three uses:

- **System disk**: Proxmox and every guest live on it. It is erased in
  part 6.
- **Backup disk** (optional): an empty second disk for the nightly backups.
  NullBill uses a disk for backups only when it is blank, or when you tell
  it to erase one.
- **Kept disk** (optional): a disk from a Windows PC whose files you want
  to keep and reach over Tailscale. NullBill never formats it.
  [legacy-disk.md](legacy-disk.md) has the details.

**You need:** the old Windows PC, still starting, if a disk to keep comes
from it. That includes the case where the server itself ran Windows until
now.

1. **Write down every disk**: its model and size, and what it will be.
   On Windows, open PowerShell and run `Get-PhysicalDisk`. It lists each
   disk's name (FriendlyName) and size. Part 6 shows disks by model and
   size, and you pick the system disk from that list.
2. **Kept disk only. Check for BitLocker**: open a Command Prompt as
   administrator and run `manage-bde -status`. If the disk you keep shows
   **Protection On** or a **Conversion Status** other than **Fully
   Decrypted**, NullBill cannot read it. Turn BitLocker (or **Device
   encryption** under **Settings, Privacy & security**) off for that drive
   and wait until `manage-bde -status` shows **Fully Decrypted**.
3. **Kept disk only. Shut Windows down fully**, as the last thing you do on
   that PC. In a Command Prompt or the Run box (Windows key + R):

   ```
   shutdown /s /t 0
   ```

   A normal shutdown with Fast Startup on (the Windows default) leaves the
   disk half asleep. NullBill then keeps it read-only and does not let you
   change files on it. If the PC no longer starts, or you only want to read
   the files, skip this step. Do not start Windows from that disk again.
4. **Copy what you cannot lose.** NullBill does not back a kept disk up.
   Anything on a disk you mark as the system disk is erased in part 6.

## 3. Put the server together

**You need:** the disks from part 2, an Ethernet cable to your home router,
a monitor and a USB keyboard for the install, and a screwdriver.

1. Fit the system disk and, if you have one, the backup disk.
2. Kept disk: leave it out for now. Fitting it after Proxmox is installed
   means it cannot be picked by mistake in part 6. If it is already in the
   machine and hard to take out (for example an NVMe disk under the graphics
   card), leave it in and take extra care in part 6.
3. Plug the Ethernet cable into the motherboard's network port. Proxmox
   needs a wired connection; it cannot run its network over Wi-Fi.
4. Plug in the monitor and keyboard. If you want the on-site screen (a
   monitor that stays on the server and shows the setup page), plug the
   monitor into the motherboard's video output, not the graphics card. The
   i9-9900K's own graphics then drive it, and the RTX 4070 stays free for
   guests. A 9900KF has no integrated graphics and cannot do this.

## 4. Set up the BIOS

The menus below are ASRock's (its Z390 boards), with ASUS's in
brackets. Other makers use similar names in other menus; the board's manual
lists them.

**You need:** the server from part 3, switched off.

1. Switch on and press **Delete** (or **F2**) until the BIOS opens. On
   ASRock, **F6** switches from Easy Mode to the full menus; on ASUS, **F7**
   opens **Advanced Mode**.
2. Set these, then save and exit (**F10**):

   | Setting | ASRock menu (ASUS menu) | Value | Why |
   | --- | --- | --- | --- |
   | Intel Virtualization Technology | Advanced, CPU Configuration (same) | Enabled | Required: the router, the backup server and the CI runners are virtual machines. |
   | VT-d | Advanced, Chipset Configuration (Advanced, System Agent (SA) Configuration) | Enabled | Needed to hand the GPU to a guest later; harmless now. |
   | SATA Mode Selection | Advanced, Storage Configuration (Advanced, PCH Storage Configuration) | AHCI | With RAID or Intel RST, Proxmox may not see the disks. |
   | Restore on AC/Power Loss | Advanced, Chipset Configuration (Advanced, APM Configuration) | Power On | The server starts by itself after a power cut. |
   | Fast Boot | Boot (same) | Disabled | So the USB stick and the BIOS key are always seen at start. |
   | CSM | Boot, CSM (Boot, CSM, Launch CSM) | Disabled | Proxmox installs in UEFI mode. The options under it disappear; that is expected. |
   | IGPU Multi-Monitor | Advanced, Chipset Configuration (Advanced, System Agent (SA) Configuration, Graphics Configuration) | Enabled | Only for the on-site screen: keeps the integrated graphics on next to the RTX 4070. |

   Secure Boot can stay as it is: the Proxmox VE 9 installer starts with it
   on. If the stick does not start in part 6, turn Secure Boot off and try
   again.

## 5. Make the Proxmox USB stick

**You need:** a USB stick of 2 GB or more (it is erased) and a computer.

1. Download the **Proxmox VE 9** ISO installer from
   <https://www.proxmox.com/en/downloads>. NullBill needs version 9;
   version 8 reached its end of life in 2026-08.
2. Write it to the stick with balenaEtcher, Rufus (choose **DD mode** when
   it asks), or `dd` on Linux.

## 6. Install Proxmox VE 9

Reference: <https://pve.proxmox.com/pve-docs/chapter-pve-installation.html>.

**You need:**

- the stick from part 5;
- your notes from part 2 (the system disk's model and size);
- a root password (at least 8 characters, in your password manager) and
  your email address;
- your home network's details, from the router's admin page or its label:
  the router's address (the gateway, often `192.168.1.1` or
  `192.168.0.1`) and the range of addresses its DHCP hands out. Pick a free
  address outside that range for the server, or reserve one for it in the
  router. NullBill's own networks use `10.42.x.x`; if your home network does
  too, stop and say so before you go on.

1. Put the stick in and switch on. Open the boot menu (**F11** on ASRock,
   **F8** on ASUS, often **F12** elsewhere; the BIOS start screen names it)
   and pick the stick's entry that starts with **UEFI:**. Without that, the
   machine starts whatever is on its disks, and an old Windows may then sit
   at its logo for a long time after the BIOS changes. Windows mounts every
   disk it sees, so if one holds files you keep, let Windows finish and shut
   it down with `shutdown /s /t 0`, or choose **Shut down** in Automatic
   Repair; do not let it repair. If it stays stuck, hold the power button:
   the files stay intact, and NullBill then shares that disk read-only.
2. Choose **Install Proxmox VE (Graphical)** and accept the licence.
3. **Target harddisk**: pick the system disk by its model and size. The
   installer erases it. Never pick the backup disk or a kept disk. Leave
   the file system at the default (ext4).
4. Country, time zone and keyboard layout.
5. The root password and your email address.
6. **Management network**: the network card with the cable (it shows a
   link), **Hostname (FQDN)** `pve.internal`, **IP address** the free
   address you picked with `/24` (for example `192.168.1.20/24`),
   **Gateway** and **DNS server** both the router's address. Note the
   address.

   The hostname's first part, `pve`, becomes the node's name in Proxmox and
   is hard to change later; the guides call the host by it. `.internal` is
   reserved for private networks, like NullBill's own `nullbill.internal`
   names. Your router does not know the name, so you open Proxmox by its
   address.
7. Check the summary and press **Install**. When it restarts, take the
   stick out. The screen then shows `https://<address>:8006`.
8. Kept disk you left out in part 3: shut the server down
   (`shutdown -h now` at its console, signed in as `root`), fit the disk and
   switch on again. Proxmox leaves it alone.

## 7. Sign in to Proxmox once

**You need:** a computer on the same home network, the address and root
password from part 6.

1. Open `https://<address>:8006`. Accept the certificate warning.
2. Sign in as `root` with realm **Linux PAM standard authentication**.
3. Close the "No valid subscription" notice. NullBill switches Proxmox to
   the free update source for you.

If the address does not open and `ping <address>` gets no answer, even
from the router's address: a VPN app on the computer (ZeroTier, WireGuard,
a company VPN) may send your home network's addresses somewhere else. On
Windows, PowerShell shows which connection is used:

```
Find-NetRoute -RemoteIPAddress <address> | Select-Object InterfaceAlias, DestinationPrefix, NextHop
```

It must name your Wi-Fi or Ethernet. If it names a VPN, switch that VPN off
or stop it from taking routes (in ZeroTier: the tray icon, the network,
untick **Allow Managed**), then try again.

## 8. Install NullBill

**You need:** the Proxmox web UI from part 7. The server needs internet
access (it downloads from GitHub, Debian and Proxmox), and the home router
must hand out addresses by DHCP: the NullBill container takes one.

1. In the tree on the left, under **Datacenter**, select the node (the
   hostname you gave), open **Shell** and run:

   ```sh
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Jantz/NullBill/main/install.sh)"
   ```

   It takes a few minutes and ends with three things: the page's address
   (`https://<address>:8443`), its certificate's SHA-256 fingerprint and a
   setup code. Keep that Shell open, or copy all three.

## 9. The setup page, steps 1 to 8: first setup run

The numbers below are the page's own step numbers.

**You need:** the address, fingerprint and setup code from part 8, your
Tailscale login from part 1, and a new password of at least 12 characters
for the page. For steps 5 to 7: your phone with ntfy, the Healthchecks.io
URL and the agent credential if you made them.

1. Open the page's address. Before you accept the certificate warning, open
   the certificate's details from the warning (or the address bar) and check
   that its SHA-256 fingerprint equals the one the Shell printed. If it
   differs, stop.
2. **Sign in** with the setup code.
3. **1. Choose a password**: at least 12 characters, kept in your password
   manager. From now on you sign in with it.
4. **2. Look at the hardware**: press **Probe the server**. It only reads.
5. **3. Choose what to run**:
   - Start with **AI agents** and **Linux CI runners**. Leave local AI
     models and both Windows choices off at 16 GB of memory.
   - Tick **Nightly backups** only with a backup disk from part 2.
   - Tick **On-site screen** only if you plugged the monitor into the
     motherboard (part 3) and turned on iGPU Multi-Monitor (part 4).
   - **Other disks**: each disk besides the system disk, with its
     partitions, labels, sizes and health. A blank one is used for backups.
     For a kept disk choose **Keep its files and share them to me**; tick
     **Let me change files on it** only for an NTFS disk Windows shut down
     fully (part 2), and only if you need to.
   - **Your Tailscale login**: exactly as noted in part 1.
   - Press **Save and plan**.
6. **4. Check the plan**: any errors are listed there. Each one says what does
   not fit; change your choices in step 3 and save again until none are
   left. Open **Plan** and check: the system disk shows as `use: system`, a
   backup disk as `use: pbs`, a kept disk as `use: legacy` with each
   partition it keeps.
7. **5. Get alerts on your phone**: in the ntfy app, add a subscription to
   the topic the page shows (server `ntfy.sh`, no account). Keep the topic
   to yourself: anyone who knows it can read your alerts. Paste the
   Healthchecks.io ping URL if you made one, press **Save**, then **Send a
   test alert** and check that your phone shows it.
8. **6. Sign in the coding agent** (only with AI agents): paste the API key
   or subscription token and press **Save**. A subscription token lasts a
   year; make a new one and paste it here before then.
9. **7. Reach it from anywhere**, first half: in the Tailscale admin console
   (<https://console.tailscale.com/admin>), signed in with the login from
   part 1, open **Settings, Trust credentials** and add an **OAuth client**
   with the scope **Policy file** set to write; the console adds the device
   scopes it needs. Copy the client ID and the secret (`tskey-client-...`;
   Tailscale shows the secret once). Paste both under **OAuth client ID**
   and **OAuth client secret** and press **Save**. You can skip Tailscale
   for now: the vpn stage then waits and everything else still runs, but
   the page stays reachable only from home.
10. **8. Build it**: press **Run setup**. The log shows each stage: render,
    tailnet, host, tofu, router, proxy, updates, ca, postgres, monitoring,
    forgejo, files, ci, agents, logs, backups, vpn, verify. The first run
    downloads images and packages and takes a while; tofu, ci and the first
    backup are the longest. Without an auth key yet, the vpn stage reports
    `vpn: waiting for a Tailscale auth key`. That is expected.

    If a stage fails, see [If a stage fails](#if-a-stage-fails).
11. **Backups only: copy the backup encryption key** now, while the Proxmox
    web UI still opens from home. Every backup is encrypted with it, and
    without it nobody can read them, you included. In the Proxmox web UI's
    **Shell**, run:

    ```sh
    proxmox-backup-client key paperkey /etc/pve/priv/storage/nb-pbs.enc --output-format text
    ```

    Write the key down or print it, and keep it offline, never as a file on
    a synced disk. Then run
    `proxmox-backup-client key show /etc/pve/priv/storage/nb-pbs.enc` and
    check that its fingerprint equals the one on your copy.
    [pbs-encryption-key.md](pbs-encryption-key.md) explains how a restore
    uses it.

## 10. Join Tailscale and open the page from anywhere

**You need:** the first setup run from part 9 done with the OAuth client
saved (the tailnet stage then put the policy in place), the Tailscale admin
console, and the Tailscale app on the phone or laptop you will use.
[vpn.md](vpn.md) has more on each step.

1. In the admin console, open **Settings, Keys**
   (<https://console.tailscale.com/admin/settings/keys>) and press **Generate
   auth key**. Leave **Reusable** and **Ephemeral** off, turn on
   **Pre-approved** and **Tags** and pick `tag:nb-vpn`, then press
   **Generate key**. Copy it; Tailscale shows it once and it expires within
   90 days.
2. On the setup page, step 7, paste it under **Tailscale auth key** and
   press **Save**.
3. Step 8: press **Run setup** again. This run is shorter. The vpn stage
   logs in once, forgets the key and prints the page's address on the
   tailnet. Note that address: once the home network is closed, it is the
   way in.
4. On your phone or laptop, sign in to the Tailscale app with the same
   login. Windows, macOS, iOS and Android accept the server's routes by
   themselves; on Linux run `sudo tailscale set --accept-routes`.
5. With Tailscale on, open the address the vpn stage printed. Sign in with
   your page password.

## 11. Close the home network (setup page step 9)

Until now Proxmox's web UI and SSH answer anyone on your home network. This
closes them, so only Tailscale gets in.

**You need:** the page open over Tailscale (part 10, step 5), and the
host's `mgmt` address: in step 4's **Plan**, under `zones`, `mgmt`,
`host_ip` (normally `10.42.1.254`).

1. On the page opened over Tailscale, press **Close the home network**.
2. Within 15 minutes, open `https://<mgmt address>:8006` over Tailscale and
   check that Proxmox answers.
3. Back on the page opened over Tailscale, press **Keep it closed**. The
   page accepts it only when it arrives through Tailscale, which proves the
   way in works.

If you do not press it in time, the server opens the home network again by
itself after 15 minutes, or when it restarts first, so a mistake cannot
lock you out. Until it is closed, **Run checks** reports the open home
network as DEGRADED. Once closed, the page answers only over Tailscale and
on the on-site screen; **Open it again** gives the home network its access
back. [vpn.md](vpn.md) says what to do if Tailscale is down.

## 12. Protect what only you can protect

**You need:** Proxmox open at `https://<mgmt address>:8006` over Tailscale,
an authenticator app on your phone (any TOTP app).

1. **Second factor for Proxmox**: the web UI still signs in with a
   password. Add TOTP and recovery keys as
   [pve-totp.md](pve-totp.md) describes, and do not close your first
   session until signing in with the code works.
2. Press **Run checks** on the page. Each line is PASS, FAIL, SKIP or
   DEGRADED. SKIP means the check could not run yet, never that it passed.
   Copy any FAIL into the chat.

## 13. Open your kept disk's files

**You need:** a kept disk chosen in part 9, setup run, and a device on
Tailscale.

1. On the setup page, step 3, **Your kept disks** shows each share's address
   and the user `nbshare`. Press **Show the password** and keep it in your
   password manager.
2. Open the share: on Windows, type `\\<address>\<name>` in File Explorer's
   address bar; on macOS, Finder, **Go**, **Connect to Server**,
   `smb://<address>/<name>`. [legacy-disk.md](legacy-disk.md) lists the
   other systems.

## If a stage fails

The log names the stage and the playbook or command. Copy the last 50 lines
of the log into the chat. Leave out anything you consider private; the log
carries addresses on your network and guest names, not secrets. Fix the
cause and press **Run setup** again; stages that already finished change
nothing the second time.

## After the server was off for a day or more

The services' certificates last a day and renew themselves while the server
runs. After a longer power cut, Forgejo, monitoring and the CI dispatcher
stop working until you press **Run setup**, which replaces the expired
certificates.

## What this run does not cover yet

- Windows guests and local AI models: options to add after a memory
  upgrade.
- Signing in to Forgejo and Grafana yourself. Setup makes their admin
  passwords and keeps them encrypted on the server, but the page does not
  show them yet. Your devices do not resolve the services' names
  (`forgejo.nullbill.internal`) over Tailscale yet, and they do not trust
  NullBill's own certificate authority yet. This run checks the services
  from inside the server.
- Taking the certificate authority's root key offline
  ([step-ca-root-key.md](step-ca-root-key.md)). It is written for a separate
  controller computer; it is not needed for the test.
