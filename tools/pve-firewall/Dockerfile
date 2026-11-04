# The stock pve-firewall parser, to check rendered files without a PVE host.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg \
 && echo "deb http://download.proxmox.com/debian/pve bookworm pve-no-subscription" > /etc/apt/sources.list.d/pve.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pve-firewall=5.1.3 > /tmp/install.log 2>&1 \
 ; test -f /usr/share/perl5/PVE/Firewall.pm || { tail -30 /tmp/install.log; exit 1; }
COPY check.pl /usr/local/bin/check.pl
ENTRYPOINT ["perl", "/usr/local/bin/check.pl"]
