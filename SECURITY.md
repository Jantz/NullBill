# Security policy

NullBill builds a server's firewall, VPN access, certificates and backups,
so a flaw in it can expose the machines it manages. Please report one
privately.

## Report a vulnerability

Use GitHub's private reporting: open the repository's **Security** tab and
choose **Report a vulnerability**. Only the maintainer sees the report.
If that button is missing, open an issue titled "Security contact" with no
details, and the maintainer will open a private channel. Please do not put
the details in a public issue, pull request or discussion.

Include what you found, how to reproduce it (a config or plan from the
synthetic fixtures in `tests/fixtures/` is ideal), and what an attacker
gains. Leave out anything that identifies your own systems.

You can expect an answer within a week. Fixes land on `main`, and the
report is published as an advisory once a fix is out.

## Supported versions

Only the latest commit on `main` is supported; there are no releases yet.

## Out of scope

Vulnerabilities in the software NullBill installs (Proxmox VE, Tailscale,
Forgejo, step-ca and so on) belong with those projects. Tell us too if
NullBill pins an affected version, so we can move the pin.
