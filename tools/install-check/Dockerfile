# A Debian 13 stand-in (PVE 9's base) for a fresh PVE host, to run
# install.sh end to end.
# Fake pct, pveam, pvesm and pveversion stand in for Proxmox; pct drives a
# sibling container (ct.Dockerfile) through the docker socket, so the
# nullbill container is a real, separate machine.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
COPY extra-ca.crt /tmp/extra-ca.crt
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      ca-certificates curl git iproute2 openssh-server docker-cli >/dev/null \
 && if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates >/dev/null; fi \
 && mkdir -p /etc/pve /root/.ssh /run/sshd && ssh-keygen -A >/dev/null \
 && git config --global --add safe.directory '*'
COPY fakes/pct fakes/pveam fakes/pvesm fakes/pveversion /usr/local/sbin/
RUN chmod 0755 /usr/local/sbin/*
