# Add or remove a person

People reach NullBill only through Tailscale. Each person has a role in
`config.yaml`; the role's `reach` decides which zones and services the
tailnet policy lets them reach. The `default` role gets Forgejo and the
workspace; `owner` gets everything, including mgmt.

Reference: <https://tailscale.com/kb/1337/policy-syntax>

## Add

1. In `$NULLBILL_HOME/config.yaml`:

   ```yaml
   people:
     dev-b: {role: default, login: dev-b@example.internal}
   ```

   The login is the address they sign in to Tailscale with.
2. Run `nullbill plan`. It fails if the free plan's user or group limits
   would be passed; it names the limit.
3. Run `nullbill apply --dry-run` and open `build/tailnet/policy.json`.
   Check that the new login appears in the group for their role and
   nowhere else.
4. Until apply pushes the policy (issue #5): paste `policy.json` into the
   Tailscale admin console under **Access controls**. Tailscale runs the
   policy's own tests before it saves; a failing test means the file and
   the plan disagree, so stop and look.
5. Invite the person to the tailnet from the admin console. They reach only
   what their role allows the moment they join.

## Remove

Delete their entry from `people`, repeat steps 2 to 4, then remove the
user in the Tailscale admin console so their devices drop off.
