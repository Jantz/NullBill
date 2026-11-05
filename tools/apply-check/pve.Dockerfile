# A Debian 12 stand-in for the PVE host, reached over SSH: what the host
# baseline touches, /etc/pve/firewall, and a pct that runs `pct exec`
# commands here and logs the VMID they were meant for.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
COPY extra-ca.crt /tmp/extra-ca.crt
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-apt openssh-server ca-certificates curl \
 && cat /tmp/extra-ca.crt >> /etc/ssl/certs/ca-certificates.crt \
 && curl -fsSLo /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg \
      https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /etc/pve/firewall /run/sshd /root/.ssh \
 && echo "deb https://enterprise.proxmox.com/debian/pve bookworm pve-enterprise" >/etc/apt/sources.list.d/pve-enterprise.list
COPY fake-pct /usr/sbin/pct
COPY id.pub /root/.ssh/authorized_keys
RUN chmod 0755 /usr/sbin/pct && chmod 0600 /root/.ssh/authorized_keys && ssh-keygen -A >/dev/null
CMD ["/usr/sbin/sshd", "-D", "-e"]
