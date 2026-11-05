# A Debian 12 stand-in for a fresh PVE host, to run install.sh end to end.
# Fake pct, pveam, pvesm and pveversion stand in for Proxmox; pct drives a
# sibling container (ct.Dockerfile) through the docker socket, so the
# nullbill container is a real, separate machine.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      ca-certificates curl git iproute2 openssh-server docker.io >/dev/null \
 && if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates >/dev/null; fi \
 && mkdir -p /etc/pve /root/.ssh /run/sshd && ssh-keygen -A >/dev/null \
 && git config --global --add safe.directory '*'
COPY fakes/pct fakes/pveam fakes/pvesm fakes/pveversion /usr/local/sbin/
RUN chmod 0755 /usr/local/sbin/*
