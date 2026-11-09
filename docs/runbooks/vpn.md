# Reach the server from anywhere (Tailscale)

The vpn guest joins your Tailscale network as a subnet router tagged
`tag:nb-vpn`. It advertises each zone that a role reaches; the policy NullBill
renders decides which person reaches which address in them. Tailscale's free
Personal plan is enough (plan.yaml's `tailnet` section counts what is used).
Setup also points Tailscale's DNS for `nullbill.internal` at the router, so
names such as `forgejo.nullbill.internal` work on every device on the
tailnet ([Names](#names)).

Reference: <https://tailscale.com/kb/1019/subnets>,
<https://tailscale.com/kb/1085/auth-keys>, <https://tailscale.com/kb/1054/dns>.

## Once

1. Sign in to the admin console, <https://console.tailscale.com/admin>, with
   the login you gave as the owner on the page (**Setup** page, **What to
   run** card). A new account gets a tailnet of its own on the free
   Personal plan.
2. **Settings, Trust credentials**: add an **OAuth client** with the scopes
   **Policy file** and **DNS**, both set to write; the console adds the
   device scopes that Policy file needs. Copy the client ID and the secret (`tskey-client-...`;
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
  test. A person's role reaches something the policy's tests forbid; fix
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
the names by themselves; their access stays what their role allows.
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
router` as DEGRADED while Tailscale sends the domain elsewhere, and
`browsers trust ... by name` for the page, Proxmox, Forgejo and Grafana.
`nslookup forgejo.nullbill.internal` on a device should answer with
Forgejo's address.

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
