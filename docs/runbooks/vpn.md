# Reach the server from anywhere (Tailscale)

The vpn guest joins your Tailscale network as a subnet router tagged
`tag:nb-vpn`. It advertises each zone that a role reaches; the policy NullBill
renders decides which person reaches which address in them. Tailscale's free
Personal plan is enough (plan.yaml's `tailnet` section counts what is used).

Reference: <https://tailscale.com/kb/1019/subnets>,
<https://tailscale.com/kb/1085/auth-keys>.

## Once

1. Sign in to <https://login.tailscale.com/admin> with the login you gave as
   the owner on the page.
2. **Access controls**: replace the policy with the one the page shows under
   step 7 (it is also `build/tailnet/policy.json` from `nullbill apply
   --dry-run`) and save. Tailscale runs the policy's tests before it accepts
   it; a refusal names the test.
3. **Settings, Keys, Generate auth key**: not reusable, not ephemeral,
   pre-approved, tag `tag:nb-vpn`. Copy it; Tailscale shows it once.
4. Paste it on the page under step 7 and press **Run setup**. The vpn stage
   logs in once, forgets the key and prints the page's address on the
   tailnet.
5. On your phone or laptop, install Tailscale and sign in. On Linux, run
   `tailscale set --accept-routes`; other systems accept the routes as they
   are. Open the address the vpn stage printed.

The routes need no approval in the admin console: the policy's
`autoApprovers` lets the tagged guest advertise addresses inside the
NullBill network only.

## When it does not join

- `vpn: waiting for a Tailscale auth key`: no key is saved; do step 3 and 4.
- `vpn.yml failed`: the log names the reason Tailscale gave. A one-time key
  works once and expires (90 days at most); generate a new one and paste it.
- Joined, but nothing answers: check that the device accepts routes and
  that the admin console lists the routes on the `nullbill` machine.

## After it works

Proxmox's own web UI and SSH stay open on your home network until you close
them (#9). Do that only after you have reached the page and the Proxmox UI
over Tailscale from outside your home network.
