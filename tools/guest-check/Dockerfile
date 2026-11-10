# The tools that read the router and egress-proxy files, at the versions
# Debian 12 ships, to check rendered files without the guests. For
# portmap.sh also the clients nb-portmap runs (miniupnpc, natpmpc) and
# miniupnpd, a home router's side of them.
FROM debian:12-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
# No init in the container: package scripts must not try to start services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nftables dnsmasq-base squid ifupdown \
      iproute2 python3 miniupnpc natpmpc miniupnpd-nftables \
 && rm -rf /var/lib/apt/lists/*
COPY check.sh portmap.sh /usr/local/bin/
COPY home-router /usr/local/bin/home-router
ENTRYPOINT ["bash", "/usr/local/bin/check.sh"]
