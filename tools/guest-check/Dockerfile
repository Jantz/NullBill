# The tools that read the router and egress-proxy files, at the versions
# Debian 13 ships, to check rendered files without the guests. For
# portmap.sh also the UPnP client nb-portmap runs (miniupnpc) and
# miniupnpd, a home router's side of them; for tunnel.sh WireGuard's tools,
# and wireguard-go for a kernel that has no WireGuard of its own.
FROM debian:13-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2
# No init in the container: package scripts must not try to start services.
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nftables dnsmasq-base squid ifupdown \
      iproute2 python3 miniupnpc miniupnpd-nftables wireguard-tools wireguard-go \
 && rm -rf /var/lib/apt/lists/*
COPY check.sh portmap.sh tunnel.sh /usr/local/bin/
COPY home-router /usr/local/bin/home-router
ENTRYPOINT ["bash", "/usr/local/bin/check.sh"]
