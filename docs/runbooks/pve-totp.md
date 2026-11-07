# Enrol TOTP for the PVE web UI

The host baseline makes SSH key-only. The web UI on port 8006 still logs in
with a password, so give every account that can sign in there a second
factor. Only the owner role reaches 8006, through the VPN.

Reference: <https://pve.proxmox.com/pve-docs/chapter-pveum.html#pveum_tfa_auth>

You do this yourself in the web UI; nullbill never sees the secret.

1. Sign in to the host (`pve.internal`) at `https://<its mgmt address>:8006`
   over Tailscale (or its home-network address while that is open) as
   `root@pam`.
2. Open **Datacenter → Permissions → Two Factor** and choose **Add → TOTP**.
3. Pick the user, give the entry a description such as `phone`, scan the QR
   code with your authenticator app, enter the code it shows, and save.
4. In the same panel choose **Add → Recovery Keys** for the same user. Store
   the keys offline, outside `$NULLBILL_HOME`, which only holds config.
5. Sign out and sign in again. The UI must ask for the TOTP code after the
   password. Do not close your first session until this works.
6. Repeat for every other PVE user that has a password.

If you lose the authenticator, sign in with a recovery key and enrol a new
TOTP entry. With neither, use SSH as root (key-only) and remove the second
factor from the command line; see the reference above for the current
command for your PVE version.
