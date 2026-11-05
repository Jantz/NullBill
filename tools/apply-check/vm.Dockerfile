# A Debian 12 stand-in for a VM: sshd and the cloud-init user `nullbill`
# with the owner's key and passwordless sudo, as OpenTofu configures it.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 openssh-server sudo \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -s /bin/bash nullbill \
 && echo 'nullbill ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-cloud-init-users \
 && mkdir -p /run/sshd /home/nullbill/.ssh \
 && ssh-keygen -A >/dev/null
COPY id.pub /home/nullbill/.ssh/authorized_keys
RUN chown -R nullbill:nullbill /home/nullbill/.ssh && chmod 0600 /home/nullbill/.ssh/authorized_keys
CMD ["/usr/sbin/sshd", "-D", "-e"]
