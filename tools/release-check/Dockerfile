# A Debian 12 container standing in for a guest built before guests ran
# Debian 13: PostgreSQL 15 with data in it, nginx with a changed settings
# file, and the timers that would get in the way. tools/check-release moves
# it to Debian 13 with the script apply's release stage runs in each guest,
# and then does the same to a second one set up as the NullBill container.
FROM debian:12-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
# extra-ca.crt is empty unless the build runs behind a TLS-intercepting proxy.
COPY extra-ca.crt /tmp/extra-ca.crt
# No init in the container: package scripts must not try to start services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      postgresql-15 nginx-light unattended-upgrades python3 ca-certificates curl openssh-server git \
 && if [ -s /tmp/extra-ca.crt ]; then cp /tmp/extra-ca.crt /usr/local/share/ca-certificates/extra-ca.crt && update-ca-certificates >/dev/null; fi \
 && git_safe='[safe]\n\tdirectory = *\n' && printf "$git_safe" > /etc/gitconfig \
 && rm -rf /var/lib/apt/lists/*
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt
COPY check.sh controller.sh /usr/local/lib/
