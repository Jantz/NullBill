# A Debian 13 stand-in for the PVE 9 host, reached over SSH: what the host
# baseline touches, /etc/pve/firewall, and a pct that runs `pct exec`
# commands here and logs the VMID they were meant for.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
COPY extra-ca.crt /tmp/extra-ca.crt
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-apt openssh-server ca-certificates curl \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /usr/share/keyrings/proxmox-archive-keyring.gpg \
      https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg \
 && echo "136673be77aba35dcce385b28737689ad64fd785a797e57897589aed08db6e45  /usr/share/keyrings/proxmox-archive-keyring.gpg" | sha256sum -c - \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /etc/pve/firewall /run/sshd /root/.ssh \
 && printf 'Types: deb\nURIs: https://enterprise.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-enterprise\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n' \
      >/etc/apt/sources.list.d/pve-enterprise.sources
COPY fake-pct /usr/sbin/pct
COPY id.pub /root/.ssh/authorized_keys
RUN chmod 0755 /usr/sbin/pct && chmod 0600 /root/.ssh/authorized_keys && ssh-keygen -A >/dev/null
CMD ["/usr/sbin/sshd", "-D", "-e"]
