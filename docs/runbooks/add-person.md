# Add or remove a person

Friends and family reach the server through your Tailscale network, with
their own Tailscale account, and open only the services you tick for them.
Tailscale's policy holds each of them to that; nothing on the server tells
people apart. The design is in
[ADR 0011](../adr/0011-people.md).

You need:

- the first setup run finished and the server on Tailscale
  ([vpn.md](vpn.md));
- the Tailscale OAuth client saved on the page (**Access**, **Tailscale**);
- the email address your friend signs in to Tailscale with, or will. A
  Google, Microsoft, GitHub or Apple account works; they need no paid plan.

Tailscale's free plan holds six people, you included.

## What a person gets

A tick opens that service's own port to the person's devices, the way a
website is open to a visitor or a game server to a player. Nothing more:

- They get no login to the server or to the service's container: no
  shell, no root. Proxmox, the page, the services you did not tick and
  your own devices do not answer them.
- Inside a service they are what its own account makes them: a Jellyfin
  user you made watches and cannot change the server; a Minecraft or
  Factorio player has no admin commands unless you name them as an admin
  under **Setup**, **What to run**; everyone who joins a Terraria world can
  build and break; a Forgejo user you made can run CI jobs, which run
  their code in a throwaway runner VM. The page says this under each
  service once you tick it.
- What stays is the risk of a security hole in the shared program
  itself. Each one runs in a container or VM of its own that has no
  rights on the server and reaches only what the firewall allows.

## Add

1. On the page open **Access**, **People**, and press **Add a person**.
2. Type their Tailscale login. The short name for yourself (`alex`) is
   filled in from it and can be changed. Tick what they may open: under
   each ticked service the page says which port opens and what they can
   do there, and under the list what they will open in all. Only services
   meant for other people are listed: Forgejo, Jellyfin, the local AI
   model, each game server. Proxmox, this page, Grafana and the rest stay
   yours alone.
3. Press **Add**. The page sends the new access policy to Tailscale, which
   runs the policy's own tests first (your friend reaches what you ticked
   and is refused everywhere else). When it says **In force now**, it is.
   If Tailscale refuses, nothing was saved and the page shows why.
4. The page now shows **How alex joins**:
   - Open **Tailscale's Users page** from the link, press **Invite
     external users** and send the invitation to their address. Only you
     can do this: Tailscale lets no program send invitations.
   - Press **Copy** and send them the steps, by whatever you use to talk
     to them. They say how to install Tailscale, which account to use and
     what opens, by name and by address.
   - If one of their services uses HTTPS (Forgejo, the local AI model),
     the page also offers **nullbill-root.crt**. Send it along; without it
     their browser warns about the certificate. It can vouch only for your
     server's own names and addresses.
5. Once they have joined, **People** shows their devices and whether each
   is connected. Press **Refresh** to ask Tailscale again.

For Forgejo and Jellyfin they also need an account in the service itself:
make one there (Forgejo: **Site administration, User accounts**; Jellyfin:
**Dashboard, Users**) and send them its sign-in.

Access belongs to the login, not to the invitation. If someone opens the
invitation with another account, they join your Tailscale network and can
open nothing; **People** then lists that login with **Add them**, or you
remove it on Tailscale's Users page.

## Change what someone may open

**Edit** in their row, tick or untick, **Save**. Tailscale has the change
within seconds, also for devices that are connected. No setup run.

## Remove

**Remove** in their row. From that moment they open nothing. If your OAuth
client has the **Users** scope (write), the page also deletes their
Tailscale account from your network, with its devices, which frees their
place among the six. Otherwise it tells you to do that on Tailscale's Users
page.

## When something is off

- **Waits for setup**: before the first setup run, or without an OAuth
  client, people are saved and get their access with the next setup run.
- **Changed in Tailscale**: the policy in Tailscale is not the one the page
  made, because someone edited it in the admin console or a change did not
  arrive. **Send to Tailscale** puts the page's policy back. `nullbill
  verify` reports the same as `Tailscale holds each person to the services
  picked for them`.
- **Tailscale did not take it**: the page lists each failing test or the
  limit under that line. Nothing was saved.
- They joined and nothing opens: on Linux they must run
  `sudo tailscale set --accept-routes --accept-dns`; on other devices
  Tailscale's app does it. If only names fail, addresses still work
  ([vpn.md](vpn.md#names)).

## By hand

The page writes `people` in `$NULLBILL_HOME/config.yaml`; you can too:

```yaml
people:
  owner: {role: owner, login: owner-a@example.internal}
  alex: {role: friend, login: alex@example.internal, services: [forgejo, jellyfin]}
```

Then `nullbill plan` (it fails past the free plan's limits and names
them) and `nullbill apply --stage tailnet`, which pushes the policy.
Roles other than `friend` reach what `roles.<name>.reach` lists in
`catalog.yaml`, for everyone who holds the role; `services` adds to that.
A service can be listed only if one of its ports has `shared` in its
`from`.

Reference: <https://tailscale.com/kb/1337/policy-syntax>,
<https://tailscale.com/kb/1271/invite-any-user>
