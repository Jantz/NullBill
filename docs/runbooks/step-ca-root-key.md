# Take the step-ca root key offline

`step ca init` leaves the root key, encrypted with the CA's key password,
in `/etc/step-ca/secrets/root_ca_key`. The CA signs with the intermediate
key and never needs the root key, so keep it offline instead.

Reference: <https://smallstep.com/docs/step-ca/certificate-authority-server-production/>

You do this yourself, after the first apply of step-ca:

1. From the controller, copy the key and its password to offline media,
   never into `$NULLBILL_HOME` or the repo:

   ```sh
   ssh root@pve.<your domain> pct pull <step-ca vmid> /etc/step-ca/secrets/root_ca_key ./root_ca_key
   ssh root@pve.<your domain> pct pull <step-ca vmid> /etc/step-ca/secrets/password ./root_ca_password
   ```

   `pct pull` writes to the host; move both files from the host to the
   offline media and delete them on the host.
2. Check the copy opens: `step crypto key inspect ./root_ca_key --password-file ./root_ca_password`.
3. Delete the key from the CA: `ssh root@pve.<your domain> pct exec <step-ca vmid> -- shred -u /etc/step-ca/secrets/root_ca_key`.
4. Keep `/etc/step-ca/certs/root_ca.crt`; it is public and every guest trusts it.

The root key is needed again only to sign a new intermediate, when the
current one nears expiry or is compromised.
