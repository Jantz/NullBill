# Trust this server in your browsers

Browsers warn on the page, Proxmox, Forgejo, Grafana and the model gateway until the device
trusts NullBill's browser CA. The connection is encrypted either way; the
warning says only that the browser does not know who signed the
certificate. Install the root certificate once on each device you use.

You need: a setup run that has finished the **ca** step (that run makes the
CA), and the device, signed in to Tailscale if you are away from home.

## What the root vouches for

apply makes the browser CA in `$NULLBILL_HOME/browser-ca/` on its first
**ca** step (`src/nullbill/browserca.py`). Its root carries critical name
constraints: the plan's domain (`nullbill.internal`) and network
(`10.42.0.0/16`). Browsers refuse any certificate it signs for another name
or address, so trusting it on a phone vouches for your server and for
nothing else. step-ca's root has no such limit, which is why the page offers
this one and not step-ca's: services keep step-ca's certificates among
themselves. Forgejo is the exception: people and the CI runners open it by
the same name, so its name has this CA's certificate and the CI dispatcher
and runner images trust this root as well as step-ca's.

It signs, for their addresses and `<name>.nullbill.internal`:

| What | Address | When |
|---|---|---|
| The page | `https://10.42.1.2:8443` | each **ca** step; the page picks it up within a minute, with the same key, so the on-site screen's and the agents' pins hold |
| Proxmox | `https://10.42.1.254:8006` | the **host** step of the first setup run after you close the server to the home network; until then PVE's own CA signs it, since apply still reaches Proxmox at its home network address, which the root cannot name |
| Forgejo | `https://10.42.2.17` | the **forgejo** step; by its name too |
| Grafana | `https://10.42.1.14:3000` | the **monitoring** step; by its name (`monitoring.nullbill.internal`) too |
| Model gateway | `https://10.42.4.21:4000` | the **inference** step; for browsers and extensions such as Page Assist; clients that ask for `model-gateway.nullbill.internal` still get step-ca's |

Each lasts 825 days, the longest Apple devices accept; a setup run makes a
new one when fewer than 90 days are left. **Run checks** reports
`browsers trust the page`, `browsers trust Proxmox` and each `... by name`
(the page, Proxmox, Forgejo and Grafana) as DEGRADED when a browser with
only this root would warn.

Jellyfin (`http://10.42.7.24:8096`) is plain HTTP inside Tailscale's
encryption: browsers mark it "Not secure" in the address bar but show no
warning page.

## Install it

On the page, **Access**, **Trust this server**, press **Download root
certificate** (`/nullbill-root.crt`). The page shows the steps for each
system; they are:

- **Android**: Settings, search for **CA certificate** (Samsung: Security
  and privacy, More security settings, Install from device storage), **CA
  certificate**, **Install anyway**, pick `nullbill-root.crt`. Chrome trusts
  it at once. If Firefox still warns: Settings, About Firefox, tap the logo
  until the debug menu is on; Settings, Secret Settings, turn on **Use third
  party CA certificates**; restart Firefox.
- **Windows**: open the file, **Install Certificate**, **Current User**,
  **Place all certificates in the following store**, **Trusted Root
  Certification Authorities**, **Finish**, **Yes**. Restart the browser;
  Edge, Chrome and Firefox use the Windows store.
- **iPhone, iPad**: download in Safari, Settings, **Profile Downloaded**,
  **Install**; then Settings, General, About, **Certificate Trust
  Settings**, turn on **NullBill browser CA**.
- **macOS**: open the file, Keychain Access adds it; open it there, **Trust**,
  **When using this certificate: Always Trust**.
- **Linux, Firefox**: Settings, Privacy & Security, **View Certificates**,
  **Authorities**, **Import**, tick **Trust this CA to identify websites**.

## Remove it

Android: Settings, **Trusted credentials** (or **User certificates**), User,
NullBill browser CA, **Remove**. Windows: `certmgr.msc`, Trusted Root
Certification Authorities, Certificates, delete **NullBill browser CA**.

## The CA's key

`$NULLBILL_HOME/browser-ca/ca.key` signs each renewal, so it stays there,
in the nightly encrypted backup of the `nullbill` container. Whoever holds it
can impersonate only your server's own addresses and names, which root on
the `nullbill` container could do anyway. If you think it leaked: remove the
root from your devices, delete `$NULLBILL_HOME/browser-ca/`, run setup (it
makes a new CA and signs everything again) and install the new root.
