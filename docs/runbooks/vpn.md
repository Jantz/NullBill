# Reach the server from anywhere (Tailscale)

The vpn guest joins your Tailscale network as a subnet router tagged
`tag:nb-vpn`. It advertises each zone that a role reaches; the policy NullBill
renders decides which person reaches which address in them. Tailscale's free
Personal plan is enough (plan.yaml's `tailnet` section counts what is used).
Setup also points Tailscale's DNS for `nullbill.internal` at the router, so
names such as `forgejo.nullbill.internal` work on every device on the
tailnet ([Names](#names)), and asks your home router for the one port that
lets a phone on mobile data connect without a relay
([Direct connections](#direct-connections)).

Reference: <https://tailscale.com/kb/1019/subnets>,
<https://tailscale.com/kb/1085/auth-keys>, <https://tailscale.com/kb/1054/dns>.

## Once

1. Sign in to the admin console, <https://console.tailscale.com/admin>, with
   the login you gave as the owner on the page (**Setup** page, **What to
   run** card). A new account gets a tailnet of its own on the free
   Personal plan.
2. **Settings, Trust credentials**: add an **OAuth client** with the scopes
   **Policy file** and **DNS**, both set to write; the console adds the
   device scopes that Policy file needs. Add **Users**, write, if the page
   should also delete a person's Tailscale account when you remove them
   ([add-person.md](add-person.md)); without it you do that in the console.
   Copy the client ID and the secret (`tskey-client-...`;
   Tailscale shows it once) and paste both in the **Tailscale** card on the
   page's **Access** page. OAuth clients do not expire. Every setup
   run then trades them for an
   hour-long token and, before it changes anything on the host:
   - counts the tagged devices on the tailnet, and stops past the free
     plan's limit;
   - leaves the policy alone if it already equals the rendered one
     (`build/tailnet/policy.json`, shown on the page), and otherwise has
     Tailscale check it and run its tests, pushes it and reads it back.
   - sends lookups under `nullbill.internal` to the router (split DNS),
     unless they go there already. A client without the DNS scope only
     skips this: addresses keep working, and the stage says so.
   A refusal stops setup with the test Tailscale names. The vpn guest does
   not join until the policy is in place, because Tailscale's default
   policy lets every device reach every route. Run setup once now: a key
   can carry only a tag the policy defines.
   Reference: <https://tailscale.com/kb/1215/oauth-clients>.
3. **Settings, Keys** (<https://console.tailscale.com/admin/settings/keys>):
   press **Generate auth key**. Leave **Reusable** and **Ephemeral** off,
   turn on **Pre-approved** and **Tags** and pick `tag:nb-vpn`, then press
   **Generate key**. Copy it; Tailscale shows it once. It expires after 90
   days at most, so use it soon.
4. Paste it in the same **Tailscale** card and press **Run setup**. The
   vpn stage logs in once, forgets the key and prints the page's address
   on the tailnet.
5. On your phone or laptop, install Tailscale and sign in with the same
   login. Linux needs `sudo tailscale set --accept-routes`; Windows, macOS,
   iOS and Android accept the routes by themselves. Open the address the
   vpn stage printed, `https://nullbill.nullbill.internal:8443`.

The routes need no approval in the admin console: the policy's
`autoApprovers` lets the tagged guest advertise addresses inside the
NullBill network only.

## When it does not join

- `vpn: waiting for a Tailscale OAuth client`: do step 2.
- `vpn: waiting for a Tailscale auth key`: no key is saved; do step 3 and 4.
- `tailnet: Tailscale refused the policy`: the message names the failing
  test. A person reaches something the policy's tests forbid; fix
  `people` or `roles` in the config.
- `tailnet: ... lacks a scope`: make a new OAuth client with the scopes in
  step 2 and paste it. Tailscale does not let you add a scope to a client
  you have.
- `tailnet: the Tailscale OAuth client cannot change DNS`: the client has
  Policy file but not DNS. Names do not work until you paste one with both
  and run setup; addresses work meanwhile.
- `vpn.yml failed`: the log names the reason Tailscale gave. A one-time key
  works once and expires (90 days at most); generate a new one and paste it.
- Joined, but nothing answers: check that the device accepts routes and
  that **Machines** in the admin console shows a **Subnets** badge on the
  `nullbill` machine, and that selecting it shows the routes as approved
  in its **Subnets** section.

## Names

Every guest has a name, `<guest>.nullbill.internal`, which the router's
dnsmasq answers inside the server. The tailnet stage sets Tailscale's split
DNS so each device on the tailnet asks the router for those names and
nothing else, at the router's address in the access zone (`10.42.6.1`).
The policy lets every person reach that address on port 53 only, and the
vpn guest advertises it as a route of its own. Other people you add get
the names by themselves; their access stays what you ticked for them.
Addresses keep working everywhere.

On a device, names need Tailscale's DNS on:

- **Android**: if Settings, **Private DNS** names a provider (such as
  `dns.google`), set it to **Automatic**. A named provider takes every
  lookup past Tailscale.
- **Linux**: `sudo tailscale set --accept-dns` (on by default), with
  `--accept-routes` as in step 5.
- **Windows, macOS, iOS**: nothing to do, unless **Use Tailscale DNS** was
  turned off in the app.

**Run checks** reports `tailnet devices look up server names at the
router` as DEGRADED while Tailscale sends the domain elsewhere, asks the
router for a name from the vpn guest, the way your devices' lookups arrive
(`the router answers lookups of server names from the vpn guest`), and
reports `browsers trust ... by name` for the page, Proxmox, Forgejo and
Grafana. `nslookup forgejo.nullbill.internal` on a device should answer
with Forgejo's address.

The Tailscale app sometimes shows **DNS unavailable**. That warning is the
app's own: it appears after any lookup the app passed on got no answer in
time, from your network's own DNS servers as much as from the server, often
right after the phone changed networks, and it goes away with the next
answer. While names open the page it means nothing about the server.

## Direct connections

A device reaches the server either directly or through one of Tailscale's
relays. Both work. A relay is slower, and video from Jellyfin may buffer.
At home, and from most Wi-Fi networks elsewhere, devices connect directly
with nothing from you. A phone on mobile data does so only when your home
router sends one port, UDP 41642, on to the server: the mobile network
gives the phone no address the server could reach, so the server has to be
the one that can be reached. The port leads to Tailscale on the vpn guest
and to nothing else, and Tailscale answers only devices on your tailnet.

Every quarter of an hour the server asks your home router to open that
port, the way game consoles do (UPnP or NAT-PMP). Many home routers say
yes and you have nothing to do.

**You need:** the server on Tailscale ([Once](#once)); for anything on
the home router, its admin page and password.

1. On the page, open **Access**, **Tailscale** tab, and read **Path**.
   The server measures it while the tab is open, every half minute: it
   reads the path Tailscale on the server uses for each of your devices,
   and pings a device that is idle to find out. **Devices** lists each one
   that answered, and the small line says when it was read.
   - **Direct**: a device away from home is connected directly. Done,
     whatever the home router says about the port.
   - **Port open**: the home router sends the port to the server and
     Tailscale offers it; no device away from home answered just now.
   - **Relayed right now**: a device answers only through a relay. The
     lines below say what the home router answered and what to do. The
     cases are listed under step 3.
   - **Not measured**: no device away from home answered, so the server
     has nothing to go on; this is not a fault. A phone that is asleep may
     not answer: open the page on it, on mobile data.
   - No **Path** line: the server is not on Tailscale yet.
2. Check from the phone: turn Wi-Fi off, open the Tailscale app, tap the
   `nullbill` machine and ping it. The app says **Direct connection** or
   **Relayed connection**. The first seconds of any connection are
   relayed, so ping a few times. On a computer, `tailscale ping nullbill`
   ends with `via <address>:41642` when direct and `via DERP(...)` when
   relayed.
3. If it is relayed, do what fits the lines under **Path**; the tab reads
   it again by itself:
   - **The home router did not open the port, no answer**: UPnP and
     NAT-PMP are off on the home router. Either turn one on (routers
     call it UPnP, NAT-PMP, or port sharing by devices); the server then
     has the port within a quarter of an hour. That lets every device at
     home ask for ports, not only the server. Or forward the port
     yourself: UDP 41642 from the internet to the address the page names,
     port 41642. That address is the NullBill router's on your home
     network, the device with the MAC address `02:4e:42:00:00:01` in the
     home router's device list, not the Proxmox host. Tell the home router
     to always give that device the same address, or the forward points
     nowhere after the address changes. The server cannot see a forward
     it did not ask for; **Path** shows whether it works, as soon as a
     phone on mobile data answers. You may switch off **Ask the home router
     for the port** and press **Run setup**, so the server stops asking for
     what you made by hand; the switch changes nothing about the path.
   - **It does not let this device open ports**: the home router has UPnP
     on but wants each device allowed first. On a FRITZ!Box that is
     **Internet, Permit Access, Port Sharing**: add the device with the
     MAC address above and tick **Permit independent port sharing for this
     device**
     (<https://fritz.com/en/apps/knowledge-base/FRITZ-Box-4020/894_Setting-up-automatic-port-sharing>;
     menu names differ a little between models). The NullBill router asks
     for this one port only. If you would rather not let it ask, forward
     the port yourself as above.
   - **It already sends that port to another device**: remove that
     forwarding of UDP 41642 on the home router. The server asks again
     within a quarter of an hour.
   - **The home router is behind another router**: the home router's own
     address is not a public one, so a port it opens cannot be reached
     from the internet. If the other router is a second box at home (a
     modem with its own router, say), forward UDP 41642 on it to the home
     router, and on the home router to the address the page names unless
     the line says the home router opened the port. If it is your
     provider's (common with mobile and cable
     internet, where customers share addresses), nothing on your side
     opens a port. Some providers give a public IPv4 address on request.
     Until then devices on mobile data use relays, and everything keeps
     working.

**Run the checks** reports this as `devices on mobile data reach the vpn
guest directly`: PASS when the home router sends the port here and
Tailscale offers it to your devices, or when a device away from home is
connected directly; DEGRADED only when a device answers through a relay
and none away from home is direct; and SKIP, with what it read, when no
device away from home answered. A closed port alone is no finding: a port
forwarded by hand or on a second router looks the same from the server.

To have no port open, switch off **Ask the home router for the port** and
press **Run setup**. The server gives the port back. The switch only
decides who opens the port, the server or you by hand: switching it off
never makes a connection direct.

## Close Proxmox to the home network

Proxmox's own web UI and SSH stay open on your home network until you close
them. Do it once Tailscale works:

1. On a device on Tailscale, open the page at the address the vpn stage
   printed and, on the **Access** page, **Home network** tab, press
   **Close to the home network**.
2. Within 15 minutes, open `https://pve.nullbill.internal:8006`, or the
   PVE host's address in the plan's `mgmt` zone (`.254`), over Tailscale to check
   Proxmox answers, then press **Keep it closed** on the page you opened
   over Tailscale. The page accepts that press only when it arrives through
   the vpn guest, which is what proves the way in works.
3. If you do not, the host puts the open firewall back by itself after 15
   minutes, and the next setup run notices. If the host restarts before
   you press it, it puts the open firewall back as it starts.

Once closed, the page answers only from inside the NullBill network: over
Tailscale and on the on-site screen. **Reopen to the home network** in the
same card gives the home network its access back. If Tailscale is down,
press it on the on-site screen. Without one, sign in at the host's own
keyboard and run:

```sh
pct exec <nullbill vmid> -- env LC_ALL=C.UTF-8 NULLBILL_HOME=/var/lib/nullbill /opt/nullbill/venv/bin/nullbill apply --open-lan
```

The nullbill container's VMID is in the plan (`guests.nullbill.vmid`) and
in `pct list`. If that fails too, put the open firewall back by hand at the
host's keyboard:

```sh
cp /root/nullbill/cluster-lan-open.fw /etc/pve/firewall/cluster.fw
```

Then, once the nullbill container works, run the command above as well:
until it records the home network as open, every setup run closes it
again.
