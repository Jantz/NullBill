"""What the owner opens on the server, for the page's Services card.

Each entry is shown only when the plan has its guest. Addresses come from
the plan; once the last setup run left the tailnet resolving the plan's
names (names.py), each entry leads with the name and keeps the address as a
fallback. Passwords are the secrets apply keeps, revealed on request only.
`watch` is the instance name monitoring gives the guest (prometheus.yml.j2),
so the page can say whether it answers.
"""

from nullbill import games, legacy, media, torrents


def shares(plan: dict) -> list[str]:
    """The kept disks' share names, and those of copies of converted ones."""
    return [m["share"] for m in legacy.shares(plan)] + [c["name"] for c in legacy.copies(plan)]


# A password the page may reveal, by the name the page asks for: the
# secret apply keeps it under. Nothing else in the secrets is readable.
PASSWORDS = {
    "grafana": "grafana_admin_password",
    "forgejo": "forgejo_admin_password",
    "forgejo-owner": "forgejo_owner_password",
    "files": "files_share_password",
    "model-gateway": "model_gateway_key",
    "jellyfin": "jellyfin_admin_password",
    torrents.QBITTORRENT: torrents.PASSWORD_SECRET,
}

# Guests that run on their own and have nothing for the owner to open.
BACKGROUND = {
    "router": "router",
    "egress-proxy": "internet proxy",
    "step-ca": "certificate authority",
    "postgres": "PostgreSQL",
    "runner-dispatcher": "CI dispatcher",
    "vpn": "Tailscale entry",
    "pbs": "backup server",
}


def step(text: str, done: bool | None = None, href: str | None = None, link: str | None = None, code: str | None = None) -> dict:
    """One thing to do to set a service up: done is known for some, href
    and link point at the page that does it, code is shown to copy."""
    return {k: v for k, v in {"text": text, "done": done, "href": href, "link": link, "code": code}.items() if v is not None}


SSH_KEYS = {"href": "#access/ssh", "link": "Access, SSH keys"}

CI_EXAMPLE = """on: [push]
jobs:
  test:
    runs-on: linux
    steps:
      - uses: actions/checkout@v4
      - run: uname -a && git log -1 --oneline
"""


def _entries(plan: dict, guests: dict, by_name: bool, forgejo_user: str | None, ssh_keys: int) -> list[dict]:
    domain = plan.get("domain", "nullbill.internal")

    def ip(name: str) -> str | None:
        return (guests.get(name) or {}).get("ip")

    def at(name: str) -> str | None:
        """How the owner's devices reach a guest: its name, or its address."""
        return f"{name}.{domain}" if by_name and ip(name) else ip(name)

    def fallback(name: str, url: str) -> list[str]:
        """The address form of a URL that leads with the name."""
        return [f"By address: {url.replace(f'{name}.{domain}', ip(name), 1)}"] if by_name else []

    def vmid(name: str) -> int | None:
        return (guests.get(name) or {}).get("vmid")

    keys_saved = ssh_keys > 0
    add_key = step("Paste your SSH public key, then press Run setup.", keys_saved, **SSH_KEYS)
    host = ((plan.get("zones") or {}).get("mgmt") or {}).get("host_ip")
    shell = "In Proxmox, select the node and open Shell, then run"
    out = [
        {
            "name": "proxmox",
            "title": "Proxmox",
            "what": "Each guest's console, start and stop, snapshots, the host's own updates and its Shell.",
            "url": (f"https://pve.{domain}:8006" if by_name else f"https://{host}:8006") if host else None,
            "addresses": [f"By address: https://{host}:8006"] if host and by_name else [],
            "user": "root, realm Linux PAM",
            "password_note": "Its password is the one you chose when installing Proxmox.",
            "steps": [
                step("Sign in as root, realm Linux PAM, with the password you chose when installing Proxmox."),
                step(
                    "Turn on two-factor sign-in: Datacenter, Permissions, Two Factor, Add, TOTP. Scan the code with an "
                    "authenticator app and type the number it shows."
                ),
                step(
                    "In the same place add Recovery Keys and keep them on paper. Without your phone they are the way back "
                    "in, besides a screen and keyboard on the server."
                ),
            ],
            "watch": "pve",
        }
    ]
    if ip("monitoring"):
        out.append(
            {
                "name": "grafana",
                "title": "Grafana",
                "what": "Metrics and logs of every guest. Open Explore and pick Prometheus or Loki.",
                "url": f"https://{at('monitoring')}:3000",
                "addresses": fallback("monitoring", f"https://{at('monitoring')}:3000"),
                "user": "admin",
                "password": "grafana",
                "steps": [
                    step("Sign in as admin with the password above."),
                    step("Open Explore, pick Prometheus and run this: each guest that answers shows 1.", code="up"),
                    step("Pick Loki instead for logs. This shows every command an agent ran:", code='{job="commands"}'),
                ],
                "watch": "monitoring",
            }
        )
    if ip("forgejo"):
        repo = f"git@{at('forgejo')}:{forgejo_user or '<you>'}/<repository>.git"
        if forgejo_user:
            first = [
                add_key,
                step(f"Sign in as {forgejo_user}, your own user, with the password above. Change it under Settings, Account if you like."),
            ]
        else:
            first = [
                add_key,
                step("Press Run setup: it makes your own Forgejo user, named from your Tailscale login, with that key."),
            ]
        out.append(
            {
                "name": "forgejo",
                "title": "Forgejo",
                "what": "Your Git repositories and CI.",
                "url": f"https://{at('forgejo')}/",
                "addresses": [f"Git over SSH: git@{at('forgejo')}", *fallback("forgejo", f"https://{at('forgejo')}/")],
                "user": forgejo_user or "nullbill-admin",
                "password": "forgejo-owner" if forgejo_user else "forgejo",
                "password_note": (
                    "nullbill-admin is a spare administrator; you do not need it."
                    if forgejo_user
                    else "Forgejo asks for a new password at the first sign-in; this one then stops working."
                ),
                "steps": [
                    *first,
                    step("Make a repository with +, New repository, then clone it over SSH:", code=f"git clone {repo}"),
                    step(
                        "Or bring one from GitHub: +, New migration, GitHub. "
                        "Tick This repository will be a mirror to keep it following GitHub."
                    ),
                ],
                "watch": "forgejo",
            }
        )
    if ip("files"):
        names = shares(plan)
        out.append(
            {
                "name": "files",
                "title": "Kept disk files",
                "what": "The files on the disk you kept, over SMB. Not backed up.",
                "addresses": [f"\\\\{at('files')}\\{share}" for share in names] + fallback("files", f"\\\\{at('files')}"),
                "user": "nbshare",
                "password": "files",
                "steps": [
                    step(
                        "Windows: paste the address into File Explorer's address bar. macOS: in Finder, Go, Connect to Server, "
                        f"smb://{at('files')}/{names[0] if names else '<share>'}. Linux: your file manager's Other Locations, the same way."
                    ),
                    step("Sign in as nbshare with the password above, and let the device remember it."),
                    step(
                        "The files are read-only unless you chose Let me change files on it for the disk.",
                        href="#setup/choices",
                        link="Setup, What to run",
                    ),
                ],
                "watch": "files",
            }
        )
    if ip(media.JELLYFIN):
        library = media.library(plan)
        url = f"http://{at(media.JELLYFIN)}:{media.PORT}/"
        if library:
            folders = ", ".join(f"{media.MEDIA_ROOT}/{n}" for n in library)
            shelf = step(
                f"Setup adds a library for each disk you kept ({folders}), once; Jellyfin then reads it, which takes a while "
                "for a big disk. To keep films and series apart, add a library of that kind for their folder under "
                "Dashboard, Libraries, and remove the first one."
            )
        else:
            shelf = step(
                "Keep a disk with your media, then press Run setup; Jellyfin then shows it.",
                href="#setup/choices",
                link="Setup, What to run",
            )
        out.append(
            {
                "name": media.JELLYFIN,
                "title": "Jellyfin",
                "what": "Films, series and music from the disks you kept, in the browser or Jellyfin's apps for phones and TVs.",
                "url": url,
                "addresses": fallback(media.JELLYFIN, url),
                "user": media.ADMIN_USER,
                "password": media.JELLYFIN,
                "steps": [
                    step(f"Sign in as {media.ADMIN_USER} with the password above."),
                    shelf,
                    step(f"On a phone with Tailscale on, install Jellyfin's app and add the server {url.rstrip('/')}."),
                ],
                "watch": media.JELLYFIN,
            }
        )
    if ip(torrents.QBITTORRENT):
        found = plan.get("torrents") or {}
        url = f"http://{at(torrents.QBITTORRENT)}:{torrents.PORT}/"
        where = found.get("store")
        behind = found.get("route") == torrents.VPN
        out.append(
            {
                "name": torrents.QBITTORRENT,
                "title": "Downloads",
                "what": (
                    f"qBittorrent. {torrents.WHAT_VPN}" if behind else f"qBittorrent, from your home connection. {torrents.WHAT_HOME}"
                ),
                "url": url,
                "addresses": fallback(torrents.QBITTORRENT, url),
                "user": torrents.ADMIN_USER,
                "password": torrents.QBITTORRENT,
                "steps": [
                    step(f"Sign in as {torrents.ADMIN_USER} with the password above."),
                    step(
                        f"Add a torrent file or a magnet link. It downloads into {where['folder']} on the kept disk {where['share']}"
                        + (
                            f", which Jellyfin shows under {media.MEDIA_ROOT}/{where['share']}/{where['folder']}."
                            if ip(media.JELLYFIN) and where["share"] in media.library(plan)
                            else "."
                        )
                        if where
                        else "Add a torrent file or a magnet link."
                    ),
                    step(
                        f"Downloads pause while the disk has under {found.get('keep_free_gib')} GiB free "
                        "and go on by themselves once it has more. "
                        "Change that, the folder or the way downloads go here:",
                        href="#setup/choices",
                        link="Setup, What to run",
                    ),
                ],
                "watch": torrents.QBITTORRENT,
            }
        )
    if ip("workspace"):
        proxy = ip("egress-proxy")
        out.append(
            {
                "name": "workspace",
                "title": "Workspace",
                "what": "Debian for your work and the coding agent, with nothing secret on it.",
                "addresses": [f"ssh root@{at('workspace')}", *fallback("workspace", f"ssh root@{at('workspace')}")],
                "steps": [
                    add_key,
                    step("Connect from your device:", code=f"ssh root@{at('workspace')}"),
                    step(f"Without a key: {shell}", code=f"pct enter {vmid('workspace')}"),
                    *(
                        [
                            step(
                                "apt already goes through the server's internet proxy. For other tools, set it first:",
                                code=f"export http_proxy=http://{proxy}:3128 https_proxy=http://{proxy}:3128",
                            )
                        ]
                        if proxy
                        else []
                    ),
                ],
                "watch": "workspace",
            }
        )
    if ip("operator"):
        out.append(
            {
                "name": "operator",
                "title": "Coding agent",
                "what": "The agent CLI, signed in with the credential saved on the Agent page.",
                "steps": [
                    step("The quickest way: ask the agent beside every page, or on a phone the Agent tab."),
                    step(
                        f"For long work, the operator's own agent keeps running while you are away. {shell}",
                        code=f"pct enter {vmid('operator')}\nsu - agent\ntmux new -s agent\nclaude",
                    ),
                    step(
                        "To let it run setup and updates itself, turn on Root on Proxmox.", href="#agent/settings", link="Agent, Settings"
                    ),
                ],
                "watch": "operator",
            }
        )
    if ip("windows-dev"):
        out.append(
            {
                "name": "windows-dev",
                "title": "Windows desktop",
                "what": "A Windows VM for you, over Remote Desktop.",
                "addresses": [f"Remote Desktop: {at('windows-dev')}", *fallback("windows-dev", f"Remote Desktop: {at('windows-dev')}")],
                "steps": [
                    step("Install Windows from the VM's console in Proxmox (windows-desktop.md)."),
                    step("In Windows, turn on Settings, System, Remote Desktop, then connect to the address above."),
                ],
            }
        )
    if "linux-runner" in guests:
        out.append(
            {
                "name": "linux-runner",
                "title": "Linux CI",
                "what": "Each Forgejo Actions job with runs-on: linux gets a fresh VM, destroyed afterwards.",
                "steps": [
                    step(
                        "Make a repository in Forgejo; Actions is on in new ones. Add this as .forgejo/workflows/ci.yml and push it:",
                        code=CI_EXAMPLE,
                    ),
                    step("The run shows under the repository's Actions tab. A job waits a minute or two while its VM starts."),
                ],
                "watch": "runner-dispatcher" if "runner-dispatcher" in guests else None,
            }
        )
    model = (plan.get("inference") or {}).get("model")
    if ip("model-gateway") and model:
        name = f"model-gateway.{plan.get('domain', 'nullbill.internal')}"
        url = f"https://{ip('model-gateway')}:4000/v1"
        out.append(
            {
                "name": "model-gateway",
                "title": "Local AI model",
                "what": f"{model} on your graphics card, behind an OpenAI-compatible API. Ask for it as local.",
                "addresses": [f"From your devices: {url}", f"From the workspace: https://{name}:4000/v1"],
                "password": "model-gateway",
                "password_note": "This is the API key.",
                "steps": [
                    step(
                        "On each device, install the server's root certificate first.",
                        href="#access/trust",
                        link="Access, Trust this server",
                    ),
                    step(
                        "In Page Assist (a browser extension): Settings, OpenAI Compatible API, Add Provider. Base URL as below, "
                        f"the API key above, then pick {model} as the model.",
                        code=url,
                    ),
                    step(
                        "Leave temperature and the other sampling settings unset and do not set a low answer length: "
                        "the model thinks first, and the server already uses the settings its makers advise."
                    ),
                    step("Any other OpenAI-compatible program works the same way, with local as the model."),
                ],
                "watch": "model-gateway",
            }
        )
    for name, game in (plan.get("games") or {}).items():
        if not ip(name):
            continue
        number = game["port"].partition("/")[2]
        where = f"{at(name)}:{number}"
        # A further server of a game says which (`game`); the game's own does not.
        of = game.get("game", name)
        client = f"{games.CLIENT[of]} {game['version']}"
        steps = [
            step(f"In the game choose {games.JOIN[of]} and type the address. Your game must be {client}, the server's version."),
            step(
                f"Tick {game['title']} for each friend who may join, and send them what the page writes.",
                href="#access/people",
                link="Access, People",
            ),
        ]
        if of in games.ADMIN_COMMAND:
            steps.append(
                step(
                    f"To be an admin in the game, add your player name beside {game['title']}, then press Run setup.",
                    bool(game.get("admins")),
                    href="#setup/choices",
                    link="Setup, What to run",
                )
            )
        if "space_age" in game:
            steps.append(
                step(
                    "Every player needs the Space Age expansion."
                    if game["space_age"]
                    else f"It runs the base game, so nobody needs the Space Age expansion (games.{name}.space_age in config.yaml)."
                )
            )
        out.append(
            {
                "name": name,
                # The page asks for this server's worlds, players, log and
                # console when the row is opened (/api/game).
                "game": of,
                # Which server runs it and its plugins or mods (/api/game/mods).
                "mods": of == "minecraft",
                "title": game["title"],
                "what": f"A {games.TITLE[of]} server for you and the people you let in. Players need {client}.",
                "addresses": [f"In the game: {where}", *([f"By address: {ip(name)}:{number}"] if by_name else [])],
                "steps": steps,
                "watch": name,
            }
        )
    for name, own in (plan.get("custom") or {}).items():
        if not ip(name):
            continue
        ports = [port.partition("/")[2] for port in own["ports"]]
        first = ports[0] if ports else ""
        if own.get("open"):
            fill = {"{host}": at(name), "{ip}": ip(name), "{port}": first}
            where = own["open"]
            for key, value in fill.items():
                where = where.replace(key, value)
            url = where if "://" in where else None
            addresses = [*([] if url else [where]), *fallback(name, where)]
        else:
            url = None
            addresses = [f"{at(name)}:{port}" for port in ports] + ([f"By address: {ip(name)}"] if by_name and ports else [])
        steps = []
        if own.get("shared"):
            steps.append(
                step(
                    f"Tick {own['title']} for each person who may use it, and send them what the page writes.",
                    href="#access/people",
                    link="Access, People",
                )
            )
        steps.append(
            step(
                "NullBill made this guest, its firewall, updates and backups. What runs inside came from its setup script, "
                "which you can read there; to change it, ask the agent or edit the script, then press Run setup.",
                href="#setup/choices",
                link="Setup, What to run",
            )
        )
        if guests[name]["kind"] == "lxc":
            steps.append(step(f"To look inside. {shell}", code=f"pct enter {vmid(name)}"))
        out.append(
            {
                "name": name,
                "title": own["title"],
                "what": own.get("what") or "A service of your own.",
                **({"url": url} if url else {}),
                "addresses": addresses if addresses or url else ["It opens no port to your devices."],
                "steps": steps,
                "own": True,
                "watch": name,
            }
        )
    return out


def services(plan: dict, by_name: bool = False, forgejo_user: str | None = None, ssh_keys: int = 0) -> dict:
    guests = plan.get("guests") or {}
    return {
        "items": _entries(plan, guests, by_name, forgejo_user, ssh_keys) if guests else [],
        "background": [label for name, label in BACKGROUND.items() if name in guests],
    }
