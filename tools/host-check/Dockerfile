# A Debian 12 stand-in for the PVE 8 host: the packages the baseline
# touches, the Proxmox key, and Ansible to run the role locally.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
# No init in the container: package scripts must not try to start services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ansible-core python3-apt openssh-server ca-certificates curl \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg \
      https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg \
 && rm -rf /var/lib/apt/lists/*
COPY check.sh /usr/local/bin/check.sh
ENTRYPOINT ["bash", "/usr/local/bin/check.sh"]
