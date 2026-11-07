# A Debian 13 stand-in for the PVE 9 host: the packages the baseline
# touches, the Proxmox key, and Ansible to run the role locally.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
# No init in the container: package scripts must not try to start services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ansible-core python3-apt openssh-server ca-certificates curl lvm2 e2fsprogs \
      dosfstools exfatprogs \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /usr/share/keyrings/proxmox-archive-keyring.gpg \
      https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
 && echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c - \
 && rm -rf /var/lib/apt/lists/*
COPY legacy-disk.sh /usr/local/lib/legacy-disk.sh
COPY check.sh /usr/local/bin/check.sh
ENTRYPOINT ["bash", "/usr/local/bin/check.sh"]
