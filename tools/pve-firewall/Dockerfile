# The stock pve-firewall parser from PVE 9, to check rendered files without
# a PVE host.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /usr/share/keyrings/proxmox-archive-keyring.gpg https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
 && echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c - \
 && printf 'Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/proxmox.sources \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pve-firewall=6.0.7 > /tmp/install.log 2>&1 \
 ; test -f /usr/share/perl5/PVE/Firewall.pm || { tail -30 /tmp/install.log; exit 1; }
COPY check.pl /usr/local/bin/check.pl
ENTRYPOINT ["perl", "/usr/local/bin/check.pl"]
