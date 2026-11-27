# The KDE desktop

The workload **KDE desktop (Fedora)** makes one Fedora VM, `desktop`
(VMID 2029, in NullBill's `desktop` zone), with KDE Plasma. You use it two
ways: on a monitor, keyboard and mouse plugged into the server, and by
Remote Desktop from your own devices over Tailscale. ADR 0023 says why it
is built this way.

It runs on demand: it is off until you start it on the **Services** page.
While it runs it has the server's graphics card to itself, so the **Local
AI models**, which use the same card, stop until the desktop is off again;
the page asks before it stops them.

## 1. Turn it on

**You need:** the setup page. For the server's own screen: a graphics card
in the server that is not the processor's own, a monitor plugged into that
card, and a keyboard and mouse plugged into the server.

1. If you want to use it at the server, plug the keyboard and mouse into
   the server first, then on the **Setup** page press **Read the
   hardware**: the desktop takes the USB ports where a keyboard or mouse
   was seen. To name ports yourself, set `desktop.usb` in `config.yaml`
   (`["1-2", "1-3"]`; the desktop's row under What to run names the ports
   it took).
2. On the **Setup** page, in **What to run**, tick **KDE desktop
   (Fedora)**, press **Save**, and check the **Plan** tab shows no errors.
   It needs IOMMU (VT-d on Intel, AMD-Vi on AMD) on in the server's
   firmware settings to borrow the card; the plan says so if it is off,
   and if the card shares its IOMMU group with other devices.
3. Press **Run setup**, read the review and start it. The first run
   installs Plasma and restarts the VM once into Fedora's newest kernel;
   it takes a while. If the local AI models are in use at that moment, the
   run leaves the desktop for the next one and says so.

## 2. Use it from your devices

**You need:** Tailscale on the device, the desktop's password (Services
page, **KDE desktop**, the eye beside the password).

1. Install the server's root certificate on the device (**Access**,
   **Trust this server**), so Remote Desktop knows the desktop is yours.
2. On the **Services** page press **Start** in the KDE desktop's row. If
   the local AI models run, the page asks before it stops them.
3. Connect to the address the row shows, as the user it shows, with the
   password:
   - Windows: **Remote Desktop Connection**.
   - Mac, iPhone, iPad, Android: Microsoft's **Windows App**, **Add PC**.
   - Linux: KRDC or Remmina.
4. The desktop opens at your window's size. The monitor at the server goes
   dark while you are connected and shows the lock screen again after.

## 3. Use it at the server

1. Start it on the **Services** page (from a phone is fine).
2. The monitor on the graphics card shows Plasma's lock screen; sign in
   with the same password. The keyboard and mouse in the desktop's USB
   ports are the desktop's while it runs.

## 4. Stop it

Press **Stop** in its row, or shut down from Plasma's menu. The graphics
card goes back to the server within a minute and the local AI models start
again by themselves. It does not stop by itself (it asks: the alert
**OnDemandLeftOn** reminds you after 4 hours without use).

## What it does by itself

- Installs every Fedora update daily (`dnf5-automatic`), never restarting
  on its own; the alert **RebootRequired** says when a restart waits.
- Fedora's next release (a new number, twice a year) is yours to take: Plasma's
  Discover offers it. NullBill makes a new desktop from the newest one.
- Backed up nightly with the other guests when backups are on.

## When something is wrong

- **The page says the desktop cannot start because the card is in use:**
  the local AI models answered a request in the last minutes. Wait, or
  start anyway: the page asks.
- **What to run says the card shares its IOMMU group:** if it also says
  the hardware was read before NullBill knew the desktop, press **Read the
  hardware** on Setup, Hardware and save again: the older reading counted
  the PCIe port the card sits behind, which stays with the server. A
  device named after a new reading really would go along with the card.
- **The monitor stays black:** check the cable is in the card the Services
  page names, not in the motherboard's outputs. The card's own driver in
  Fedora is `nouveau` for NVIDIA (`lspci -k` in the desktop's terminal).
- **Remote Desktop shows only black:** restart the desktop on the Services
  page and connect again. If it stays black, the card may have no screen
  to draw on: an HDMI dummy plug in the card gives it one. Tell the agent.
- **After you sign out, Remote Desktop no longer answers:** the desktop
  waits at its greeter, and Remote Desktop runs inside your session. Sign
  in at the server, or restart the desktop on the Services page.
- **Remote Desktop warns about the certificate:** the device does not
  trust the server's root yet (step 2.1), or you typed another name than
  the row shows.
- **Not tested on real hardware yet:** handing the card to the VM and back
  to the host's NVIDIA driver, NVK on the RTX 4070, USB by port, the local
  screen. Tell the agent what you see.
