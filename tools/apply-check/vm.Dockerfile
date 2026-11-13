# A Debian 13 stand-in for a VM: sshd and the cloud-init user `nullbill`
# with the owner's key and passwordless sudo, as OpenTofu configures it.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
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
