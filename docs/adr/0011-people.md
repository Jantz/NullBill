# ADR 0011: Friends on the tailnet, each with the services picked for them

Status: accepted (the owner asked for "a secure and simple to manage system
for adding friends to the Tailscale network such that the admin can manage
which services the account can access", as automated as possible and
understandable for all parties, 2026-11-09; the design decided by Claude
under his "I trust you to make good design choices")

## Context

People reached NullBill through a role in `config.yaml`: the role's `reach`
named zones and services, the tailnet policy gave the role's group those
ports, and adding someone meant editing the file, running setup and
inviting them by hand (docs/runbooks/add-person.md as it was). Two roles
exist besides the owner's, and the free plan holds three groups, so "this
friend gets Jellyfin, that one Forgejo and Jellyfin" had no place.

The vpn guest is a subnet router that masquerades everyone, so nothing
behind it can tell one person from another. The tailnet policy is the only
per-person gate (`tailnet.py`).

What was checked before deciding (2026-11-09, Tailscale's API description
at `api.tailscale.com/api/v2?outputOpenapiSchema=true`, its pricing page
and its docs on sharing, inviting and policy tests):

- The Personal plan holds 6 users, 3 groups in the policy and 50 tagged
  devices. `catalog.yaml` had those numbers already.
- An invitation to join a tailnet can be made only with a key that belongs
  to a user: "Only permitted for user-owned keys, because invites require
  an inviting user". An OAuth client has no scope for it. The same holds
  for invitations to share one device.
- A shared device does not bring its subnet routes: "Shared machines do
  not advertise subnets", while people invited into the tailnet do reach
  subnet routers.
- An invitation link is good once, for 30 days, for whoever opens it.
- With the `users` scope an OAuth client lists users and deletes one,
  which also removes that user's devices. `devices:core:read`, which the
  client holds already, lists each device with its user's login, its name
  and when it was last seen.
- A policy test's `src` may be a login; Tailscale evaluates it as a device
  of that user would be treated, per protocol.
- A grant's `src` may be a login directly, without a group.

Not checked, because it needs a real tailnet: whether Tailscale takes a
policy whose grants and tests name a login that has not joined yet. The
policy named role people the same way before this change. If Tailscale
refuses, the page says so and saves nothing.

## Decision

- **A person is invited into the tailnet** with their own Tailscale
  account. Sharing the vpn guest with them would be simpler to undo but
  gives no routes, so nothing behind the vpn guest would open.
- **The owner sends the invitation** from Tailscale's Users page, since
  nothing else may. Everything else is the page's work: it writes the
  policy, has Tailscale test it, tells the owner what to send and to whom,
  shows who has joined, and removes the account again.
- **Access belongs to the login, never to the invitation.** The policy
  names each person's login. Whoever else opens an invitation joins the
  tailnet and reaches nothing, and the page lists that login as one it
  does not know.
- **A port is given to a person only if the catalog says it may be.** A
  port's `from` lists `shared` for that (Forgejo's 443 and 22, Jellyfin's
  8096, the model gateway's 4000). Admin ports and everything in mgmt
  stay with the owner; plan refuses `shared` on a mgmt service.
  `people.<name>.services` lists what the owner ticked, and the new role
  `friend` reaches nothing by itself.
- **`shared` opens the firewall like a role does**, from the vpn guest to
  the port, whoever holds it or nobody. So ticking a service for someone
  changes the tailnet policy and nothing on the server: one push to
  Tailscale, in force within seconds, no setup run.
  `tests/test_tailnet.py` holds that a person more or less changes no
  rendered file but `tailnet/policy.json`.
- **Grants by login.** A person's ticked services are a grant from their
  login. A group is made only for a role that somebody holds and some port
  names, so friends use up none of the three.
- **Tailscale tests every push.** For each person the policy carries a
  test: every port they hold is accepted, and Proxmox, SSH to the host and
  to the router, and every person-facing port they do not hold are denied.
  A mistake in rendering fails at Tailscale before it is in force.
- **The page pushes first and saves second.** A change that Tailscale
  refuses, or that passes a free-plan limit, leaves `config.yaml` as it
  was. Before the first setup run, or without an OAuth client, it is saved
  and the tailnet stage brings it to Tailscale.
- **Removing someone** pushes the policy without them, then deletes their
  Tailscale user if the client holds `users`; without that scope the page
  says to do it on Tailscale's Users page.
- **verify compares** the policy in Tailscale with the rendered one (`the
  policy in Tailscale is not the plan's` is a FAIL), so a change made by
  hand in the admin console shows, and the People tab offers to send the
  plan's policy again.
- **What the person is told** is written by the page in the owner's voice:
  install Tailscale, accept the invitation with that account, what then
  opens and where, by name and by address. Each shareable service carries
  that text in its `share` block in the catalog. For a service on HTTPS
  the owner sends the browser CA's root along; its name constraints let it
  vouch for this server only.

## Considered and dropped

- **A Tailscale group per person or per service.** Three groups in all.
- **Tagged devices joined with an auth key the page makes.** No account
  for the friend, but a key in a chat message is access for whoever reads
  it, the phone apps cannot take one, and every device would be the
  owner's to name and expire.
- **Funnel or a public reverse proxy** for friends without Tailscale. The
  project keeps nothing public.
- **A gate on the vpn guest by source address.** Userspace networking
  masquerades; there is no source to match.
- **Service accounts made for the friend** (Forgejo, Jellyfin). They are
  the services' own business and differ per service; the instructions say
  the owner makes the account. A later change may do it.

## Consequences

- At most five people besides the owner on the free plan. plan and the
  page say so at the sixth.
- A person reaches every shared port of a ticked service, not one of
  them: Forgejo means HTTPS and Git over SSH.
- A friend is a member of the owner's tailnet. The policy grants nothing
  toward a person's devices, so members do not reach each other, and a
  member cannot change the policy, make keys or advertise routes that
  count.
- An OAuth client made before this change has no `users` scope. Nothing
  breaks: removing a person then ends with a line on what to do in
  Tailscale.
- A new service that friends should get needs `shared` on its port and,
  for good instructions, a `share` block. Game servers are the first to
  come.
