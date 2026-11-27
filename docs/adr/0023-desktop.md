# ADR 0023: A KDE desktop on the server

Status: accepted (the owner asked for "full Fedora KDE Plasma that I can
remote into, allow GPU acceleration, if needed remove GPU from other
services if they can't use it at the same time", then "also make it so
that you can use the Fedora desktop locally", 2026-11-27; the design was
left to Claude)

## Context

The owner wants a full desktop on the server: Fedora's KDE Plasma, reached
from their own devices over Tailscale, and used at the server itself with a
monitor, keyboard and mouse plugged in. It should draw with the graphics
card, and where something else uses that card (the local AI models, ADR
0008) the two take turns rather than one silently breaking the other.

What was checked before deciding (2026-11-27):

- Fedora 45's KDE packages in a Fedora 45 container and in a Fedora 45
  cloud image under QEMU: `@kde-desktop-environment` brings Plasma 6.8,
  Plasma Login Manager (`plasma-login-manager`, the display manager Fedora's
  KDE edition now uses, configured in `/etc/plasmalogin.conf.d/`) and
  `plasma-setup`, the first-run wizard, which runs only while
  `/etc/plasma-setup-done` is missing (its unit's `ConditionPathExists`).
- KRDP 6.8.3, KDE's own RDP server (`krdp`,
  https://invent.kde.org/plasma/krdp): its settings are
  `krdpserversettings.kcfg` in `krdpserverrc` (`ListenPort`,
  `SystemUserEnabled`, `Certificate`, `CertificateKey`,
  `AutogenerateCertificates`, `Autostart`, `ExclusiveMode`); its user unit
  `app-org.kde.krdpserver.service` is `WantedBy=plasma-workspace.target`
  and Fedora's preset leaves it off. Run for real with FreeRDP 3.33: with
  `SystemUserEnabled` it signs the session's own user in through PAM after
  TLS, so a wrong password ends in `pam_authenticate failure` and another
  user is refused; FreeRDP with `/cert:deny` takes a certificate from a CA
  in the system store for the name it connects to.
- KScreenLocker 6.8's `kscreenlockerrc` `[Daemon] LockOnStart`.
- Ansible 2.19 (apply's) against Fedora 45: its module code fails under
  Fedora's Python 3.15 (`module 'dataclasses' has no attribute
  '_is_type'`), and runs under Fedora's `python3.13`; Fedora's libdnf5 is
  built for 3.15 alone, so the role calls `dnf` itself.
- Proxmox VE's PCI passthrough (https://pve.proxmox.com/wiki/PCI_Passthrough):
  a VM takes a card's whole IOMMU group, PVE binds the card to `vfio-pci`
  when the VM starts, and `x-vga` with `vga: none` makes the card the VM's
  only screen. USB passthrough by physical port (`qm set --usbN
  host=<bus>-<port>`, https://pve.proxmox.com/pve-docs/qm.1.html) gives the
  VM whatever is plugged into that port.

Not checked, because nothing here can: any of it on Proxmox VE, the card
going to the VM and back to the host's NVIDIA driver, NVK on the owner's
RTX 4070, USB passthrough, a monitor at the server, KRDP over Tailscale
from a real client. A container has no GPU and no compositor, so KRDP's
picture is checked only on the QEMU VM's virtual screen.

## Decision

- **A VM, not a container.** Only a VM can own a graphics card's outputs
  and the server's USB ports, and Plasma needs a real seat for the local
  screen. VMID 2029, zone `desktop` (direct egress, as a desktop's browser
  needs), made from Fedora's Generic cloud image (`images.fedora-cloud`,
  moved daily by `tools/pins`; a desktop that exists keeps its own and takes
  Fedora's next release when its owner says yes in Plasma). UEFI without
  pre-enrolled keys, CPU type host.
- **The card is borrowed, not given.** The plan marks the discrete card
  `lent_to: desktop` and keeps it the host's: the local models keep their
  budget. While the desktop runs the card (every function, its HDMI audio
  too) is the VM's; what uses it on the host stops first and starts again
  when the desktop stops. The host's `nb-power` does the handing over
  (`gpu` in its rules): `start` refuses with the list of what would stop
  (exit 3, `error: gpu`) until the caller names them, so the page asks the
  owner; `stop`, the idle tick and the end of a setup run give the card back
  (driver_override cleared, unbound from `vfio-pci`, `drivers_probe`, NVIDIA's
  device nodes again) and start what was stopped. With nothing on the host
  that uses the card it stays `vfio-pci`'s. `desktop.gpu: false` keeps every
  card with the host; the desktop then has a virtual screen.
- **The desktop runs on demand** (`stop: ask`, ADR 0013), since starting it
  takes the card. In use: someone on Remote Desktop, the screen at the
  server unlocked, updates installing (`nb-desktop busy`).
- **Setup never takes the card from someone using it.** The desktop stage
  has `nb-power ensure` hand the card over only when what has it is unused
  (exit 4 otherwise, and the stage leaves the desktop for the next run),
  and stops the desktop again right after its playbook, so the card is back
  for the rest of the run. A guest that is off because the desktop has its
  card is left as it is by every stage (`apply.card_away`), and verify
  skips its checks saying why.
- **KRDP for remote use.** KDE's own server, in the user's own session,
  signing in with the desktop user's system password through PAM; port 3389
  from the owner's devices only (`role:owner`). `ExclusiveMode`: a client
  gets a screen of its own size while the monitor at the server goes dark.
  Its certificate is the browser CA's for the desktop's name and address
  (docs/runbooks/trust-this-server.md), so a device that trusts the server
  trusts the desktop.
  Settings are written to `/etc/xdg/krdpserverrc`, immutable, because the
  port is the firewall's. Sunshine/Moonlight streams games better but needs
  its own pairing and clients; xrdp gives a separate X session without the
  card; VNC sends no encryption of its own.
- **A session from boot.** Plasma Login Manager signs the desktop user in
  at boot and Plasma locks at once, so the monitor shows a lock screen and
  Remote Desktop has a session to show. Not again after a sign-out
  (`Relogin=false`, 2026-11-27): the first real desktop showed only black
  over Remote Desktop, and the owner chose to try this first; after a
  sign-out the greeter waits, and Remote Desktop is back after a restart.
- **Fedora's own drivers.** Mesa from Fedora (NVK on NVIDIA, radeonsi and
  RADV on AMD, iris and ANV on Intel) on the kernel's modules, so a daily
  update never leaves the screen without a driver; NVIDIA's own driver
  would have to be built against each new kernel. `kernel-modules` and
  `linux-firmware` are added to the cloud image, which leaves them out, and
  the VM restarts once into the kernel that has them.
- **Daily updates, all of them** (`dnf5-automatic`, never a restart by
  itself), with the update metrics the monitoring role's alerts read.
- **The host** loads VFIO's modules and keeps nouveau off an NVIDIA card it
  lends (the same file `nvidia_host` writes).

## Consequences

- While the desktop runs the local AI models are off; the page says so
  before Start and the Services page names it.
- A server whose only graphics card the desktop borrows has no screen of
  its own while the desktop is off unless the card goes back to a host
  driver; `host.console` refuses that combination.
- The Debian-only stages (updates, release, log shipping) leave the desktop
  out; it has its own role for each of those parts.
- Not built: sound over Remote Desktop beyond what KRDP sends, a second
  user, a card that stays the desktop's for good.
