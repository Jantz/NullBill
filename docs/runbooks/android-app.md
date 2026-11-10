# The page as an app on Android

The NullBill page installs as an app on an Android phone: an icon on the
home screen that opens the page full screen, without the browser's address
bar, and long-press shortcuts to **Agent** and **Activity**. It is the
same page from the server, so it shows the server's state as it is now and
follows each **Update NullBill** by itself. Alerts still come through
ntfy, not through the app.

The page shows these steps too: **Access**, **Trust this server**,
**Android**.

You need: a setup run that has finished the **vpn** and **ca** steps, the
Tailscale login you set the server up with, and a screen lock (PIN,
pattern or password) on the phone, which Android asks for before it
installs a certificate. Use Chrome; Firefox works after one hidden
setting (step 3).

## 1. Tailscale on the phone

1. Install **Tailscale** from Google Play and sign in with the same login
   as the server. Android accepts the server's routes by itself.
2. Turn Tailscale on. The key icon in the status bar shows the VPN is up.
3. Optional, so the app reaches the server whenever you open it: Settings,
   **Network and internet**, **VPN**, the gear beside Tailscale, turn on
   **Always-on VPN**. Samsung: Settings, **Connections**, **More connection
   settings**, **VPN**.
4. Optional, for names such as `nullbill.nullbill.internal`: if Settings,
   **Private DNS** names a provider (such as `dns.google`), set it to
   **Automatic** ([vpn.md](vpn.md#names)). Addresses work either way.

## 2. Open the page

In Chrome, open the address you use on your computer:
`https://10.42.1.2:8443`, or `https://nullbill.nullbill.internal:8443` once
names work (another `network.base` changes the address; the vpn step of a
setup run prints it). Until step 3 Chrome warns about the certificate:
press **Advanced**, **Proceed**, and sign in with your page password.

## 3. Trust the server's certificate

Chrome offers to install the page as an app only once it trusts the
certificate, so this step comes first. The root certificate vouches only
for your server's own names and addresses
([trust-this-server.md](trust-this-server.md)).

1. On the page, **Access**, **Trust this server**, press **Download root
   certificate**. Chrome saves `nullbill-root.crt` to Downloads.
2. Open Settings and search for **CA certificate**. On a Pixel it is
   under **Security and privacy**, **More security and privacy**,
   **Encryption and credentials**, **Install a certificate**; on a Samsung,
   **Security and privacy**, **More security settings**, **Install from
   device storage**.
   Tap **CA certificate**, **Install anyway**, confirm with your screen lock
   and pick `nullbill-root.crt`.
3. Close Chrome (swipe it away in recent apps) and open the page again. It
   opens without a warning.
4. Firefox only: Firefox ignores certificates you install unless told
   otherwise. Settings, **About Firefox**, tap the Firefox logo until it
   says the debug menu is on; back in Settings, **Secret Settings**, turn on
   **Use third party CA certificates**; close Firefox and open it again.

The fingerprint under **Download root certificate** on the page is the one
Android shows under Settings, **Trusted credentials**, **User**, **NullBill
browser CA**.

## 4. Install the app

- **Chrome**: press **Install the app** on **Access**, **Trust this
  server** (it shows once Chrome offers it), or tap the menu (three dots),
  **Install app**. Some Chrome versions say **Add to Home screen** and then
  ask **Install** or **Create shortcut**: pick **Install**.
- **Firefox**: the menu (three dots), **Add app to Home screen**.

Open **NullBill** from the home screen. **Access**, **Trust this server**
then says "You are using the installed app."

## When it does not work

| What you see | What to do |
|---|---|
| The menu has no **Install app**, or only **Create shortcut** | Chrome does not trust the certificate yet: do step 3, close Chrome and open the page again. A shortcut is only a bookmark; remove it and install. |
| "This browser does not trust the server's certificate yet" on the page | The same: step 3. In Firefox, the secret setting in step 3. |
| The app opens, then "The page cannot reach the NullBill server" | Tailscale is off or signed out on the phone. Turn it on (or **Always-on VPN**, step 1). Right after **Update NullBill**, wait a few seconds. |
| It works at home but not on mobile data | Tailscale must be on away from home. If it is and the page is slow, see [vpn.md](vpn.md#direct-connections). |
| Names fail, the address works | **Private DNS** (step 1). |
| A certificate warning came back | The server's browser CA was made again (its key was deleted). Remove the old **NullBill browser CA** from Settings, **Trusted credentials**, **User**, and do step 3 with the new one. |

## Remove it

Long-press the app's icon, **Uninstall** (or **Remove**). To remove the
certificate too: Settings, **Trusted credentials** (or **User
certificates**), **User**, **NullBill browser CA**, **Remove**.
