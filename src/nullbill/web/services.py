"""What the owner opens on the server, for the page's Services card.

Each entry is shown only when the plan has its guest. Addresses come from
the plan; passwords are the secrets apply keeps, revealed on request only.
`watch` is the instance name monitoring gives the guest (prometheus.yml.j2),
so the page can say whether it answers.
"""

from nullbill import legacy


def shares(plan: dict) -> list[str]:
    """The kept disks' share names, and those of copies of converted ones."""
    return [m["share"] for m in legacy.shares(plan)] + [c["name"] for c in legacy.copies(plan)]


# A password the page may reveal, by the name the page asks for: the
# secret apply keeps it under. Nothing else in the secrets is readable.
PASSWORDS = {
    "grafana": "grafana_admin_password",
    "forgejo": "forgejo_admin_password",
    "files": "files_share_password",
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


def _entries(plan: dict, guests: dict) -> list[dict]:
    def ip(name: str) -> str | None:
        return (guests.get(name) or {}).get("ip")

    def vmid(name: str) -> int | None:
        return (guests.get(name) or {}).get("vmid")

    host = ((plan.get("zones") or {}).get("mgmt") or {}).get("host_ip")
    shell = "In Proxmox, select the node and open Shell, then run"
    out = [
        {
            "name": "proxmox",
            "title": "Proxmox",
            "what": "Each guest's console, start and stop, snapshots, the host's own updates and its Shell.",
            "url": f"https://{host}:8006" if host else None,
            "user": "root, realm Linux PAM",
            "password_note": "Its password is the one you chose when installing Proxmox.",
            "watch": "pve",
        }
    ]
    if ip("monitoring"):
        out.append(
            {
                "name": "grafana",
                "title": "Grafana",
                "what": "Metrics and logs of every guest. Open Explore and pick Prometheus or Loki.",
                "url": f"http://{ip('monitoring')}:3000",
                "user": "admin",
                "password": "grafana",
                "watch": "monitoring",
            }
        )
    if ip("forgejo"):
        out.append(
            {
                "name": "forgejo",
                "title": "Forgejo",
                "what": "Your Git repositories and CI. Make your own user and add your SSH key to it.",
                "url": f"https://{ip('forgejo')}/",
                "addresses": [f"Git over SSH: git@{ip('forgejo')}"],
                "user": "nullbill-admin",
                "password": "forgejo",
                "password_note": "Forgejo asks for a new password at the first sign-in; this one then stops working.",
                "watch": "forgejo",
            }
        )
    if ip("files"):
        out.append(
            {
                "name": "files",
                "title": "Kept disk files",
                "what": "The files on the disk you kept, over SMB. Not backed up.",
                "addresses": [f"\\\\{ip('files')}\\{share}" for share in shares(plan)],
                "user": "nbshare",
                "password": "files",
                "watch": "files",
            }
        )
    if ip("workspace"):
        out.append(
            {
                "name": "workspace",
                "title": "Workspace",
                "what": "Debian for your work and the coding agent, with nothing secret on it.",
                "addresses": [f"ssh root@{ip('workspace')}"],
                "how": (
                    f"It takes SSH keys only and has none of yours yet. {shell} pct enter {vmid('workspace')}"
                    " and add your public key to /root/.ssh/authorized_keys."
                ),
                "watch": "workspace",
            }
        )
    if ip("operator"):
        out.append(
            {
                "name": "operator",
                "title": "Coding agent",
                "what": "The agent CLI, signed in with the credential from step 6.",
                "how": f"{shell} pct enter {vmid('operator')}, then su - agent, tmux new -s agent and claude.",
                "watch": "operator",
            }
        )
    if ip("windows-dev"):
        out.append(
            {
                "name": "windows-dev",
                "title": "Windows desktop",
                "what": "A Windows VM for you, over Remote Desktop.",
                "addresses": [f"Remote Desktop: {ip('windows-dev')}"],
                "how": "Install Windows from the VM's console in Proxmox first and turn on Remote Desktop (windows-desktop.md).",
            }
        )
    if "linux-runner" in guests:
        out.append(
            {
                "name": "linux-runner",
                "title": "Linux CI",
                "what": "Each Forgejo Actions job with runs-on: linux gets a fresh VM, destroyed afterwards.",
                "how": "Turn on Actions in a repository's settings and add .forgejo/workflows/<name>.yml.",
            }
        )
    if "inference" in guests:
        out.append(
            {
                "name": "inference",
                "title": "Local AI",
                "what": "NullBill makes this guest but does not install the model server yet.",
                "watch": "inference",
            }
        )
    return out


def services(plan: dict) -> dict:
    guests = plan.get("guests") or {}
    return {
        "items": _entries(plan, guests) if guests else [],
        "background": [label for name, label in BACKGROUND.items() if name in guests],
    }
